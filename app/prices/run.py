"""
Поиск цен по позициям конкурса — задача «prices» обработчика (worker).

Порядок (задаёт код):
  1. Позиции — из результата анализа (AIExtract/analysis.json). Одинаковые артикулы ищутся один раз.
  2. Своя база: тот же артикул в прошлых спецификациях NocoDB (цены отдела закупок, рынков, агента).
  3. Россия: обязательный набор запросов по каждому артикулу (Шаг 5.1g) → Яндекс (отложенный режим или «Срочно»).
     Параллельно — зарубеж: веб-поиск Claude по каждому артикулу (Германия, Китай, США).
  4. Программа открывает найденные страницы товара (по 2 на сайт одновременно), достаёт цену из разметки товара,
     проверяет артикул; если цену рисует JavaScript — открывает страницу встроенным браузером.
  5. Модель (Haiku) по выжимкам решает, тот ли это товар, и даёт цену за 1 единицу позиции.
  6. По каждому рынку — 3 самые низкие подтверждённые цены от разных сайтов со ссылками; зарубеж — в рублях
     по курсу ЦБ (без наценки). Если меньше трёх — пометка «РФ: найдено N из 3 [запросы: «…» | …]» (Шаг 5.1g).
  7. Запись: analysis.json (позиции), Позиции_и_цены.xlsx, AIExtract/prices.json (подробный отчёт),
     проверка tools/check_prices.py, NocoDB — только колонки рынков и «Примечание» в строках спецификации,
     которую создал сайт (цены отдела закупок и данные агента не трогаются).
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .. import db
from ..analysis.llm import Claude, LLMError, Usage
from ..analysis.pricing import money
from ..analysis.run import MSK, OUT_DIR, load_result, save_result
from ..analysis.spec_xlsx import write_spec_xlsx
from . import abroad as abroad_search, fetch, history, yandex
from .articles import mentions, norm, queries_rf
from .extract import analyze_page
from .fx import rates, to_rub
from .judge import judge

from tools import check_prices
from tools import nocodb_client as nc

PRICES_MODEL = os.environ.get("PRICES_MODEL", "claude-haiku-5-5")
ASYNC_TIMEOUT_MIN = float(os.environ.get("PRICES_ASYNC_TIMEOUT_MIN", "30"))
MAX_PAGES = int(os.environ.get("PRICES_MAX_PAGES", "12"))
FETCH_PARALLEL = int(os.environ.get("FETCH_PARALLEL", "8"))
BROWSER_LIMIT = int(os.environ.get("PRICES_BROWSER_PAGES", "30"))
ABROAD_ON = os.environ.get("PRICES_ABROAD", "1").strip() not in ("0", "false", "no", "")
REPORT_FILE = OUT_DIR / "prices.json"

MARKETS = {"rf": ("РФ", "Стоимость товара в РФ"), "abroad": ("Зарубеж", "Стоимость товара зарубежом")}
AVITO_PREFIX = "Стоимость товара на Avito"
MARKET_COLUMNS = [f"{prefix} - {kind} {i}" for _, prefix in MARKETS.values() for i in (1, 2, 3)
                  for kind in ("предложение", "ссылка")]
FOREIGN_MARKETPLACES = ("aliexpress", "alibaba", "ebay.", "amazon.", "made-in-china", "temu.")
SKIP_DOMAINS = ("avito.ru", "youtube.", "vk.com", "wikipedia.", "dzen.ru", "pinterest.", "t.me", "drive2.ru")
# Пометки поиска цен в «Примечании» — при новом поиске заменяются, при повторном анализе переносятся.
MARK_RE = re.compile(r"(?:РФ|Зарубеж|Avito)\s*:\s*(?:найдено\s*\d\s*из\s*3|не\s+найдено)\s*\[[^\]]*\]"
                     r"|Avito:\s*поиск не выполнялся \(на паузе\)"
                     r"|Зарубеж\s+[^\s:;]+:\s*[\d.,]+\s*[A-Z]{3}\s*×\s*курс ЦБ[^;]*"
                     r"|Прошлые цены:[^;]*|Не открылись:[^;]*", re.IGNORECASE)


@dataclass
class Group:
    key: str
    rows: list[dict]
    name: str
    brand_model: str
    arts: list[str]
    rf_queries: list[str]
    candidates: list[dict] = field(default_factory=list)
    abroad_queries: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    chosen: dict = field(default_factory=dict)
    unopened: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def settings() -> dict:
    return {"model": PRICES_MODEL, "abroad": ABROAD_ON, "async_timeout_min": ASYNC_TIMEOUT_MIN,
            "yandex_key": Path(yandex.KEY_FILE).is_file() and bool(Path(yandex.KEY_FILE).read_text().strip()),
            "abroad_searches": abroad_search.SEARCHES}


def estimate(result: dict) -> dict:
    """Примерная стоимость поиска: Яндекс — по числу запросов, Claude — по опыту (поиск ≈ $0.01 + токены)."""
    groups = _groups(list(result.get("positions") or []))
    queries = len({q for g in groups for q in g.rf_queries})
    return {"groups": len(groups), "queries": queries,
            "yandex_async_rub": round(queries * yandex.RUB_ASYNC, 2), "yandex_sync_rub": round(queries * yandex.RUB_SYNC, 2),
            "abroad_usd": round(len(groups) * (abroad_search.SEARCHES * abroad_search.SEARCH_USD + 0.01), 2),
            "judge_usd": round(len(groups) * 0.002, 2)}


def report_path(folder: Path) -> Path:
    return folder / REPORT_FILE


def load_report(folder: Path) -> dict | None:
    p = report_path(folder)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        return None


def _groups(rows: list[dict]) -> list[Group]:
    groups: dict[str, Group] = {}
    for r in rows:
        name, bm = str(r.get("Наименование позиции") or ""), str(r.get("Марка/модель") or "")
        arts, qs = queries_rf(name, bm)
        if bm and bm not in qs and len(bm) <= 60 and arts:
            qs.insert(1, bm)                       # «Марка/модель» целиком (Siemens 6ES7 315-2AH14-0AB0)
        key = norm(arts[0]) if arts else norm(name)[:60]
        if not key:
            continue
        if key in groups:
            groups[key].rows.append(r)
        else:
            groups[key] = Group(key=key, rows=[r], name=name, brand_model=bm, arts=arts, rf_queries=qs[:8])
    return list(groups.values())


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _rank(c: dict, arts: list[str]) -> tuple:
    return (0 if mentions(c["url"], arts) else 1, 0 if mentions(c.get("title", ""), arts) else 1, c.get("position", 99))


def _rf_candidates(g: Group, hits_by_q: dict[str, list]) -> list[dict]:
    cands, seen = [], set()
    for q in g.rf_queries:
        for h in hits_by_q.get(q, []):
            dom = _domain(h.url)
            if h.url in seen or any(s in dom for s in SKIP_DOMAINS):
                continue
            if g.arts and not mentions(f"{h.url} {h.title} {h.snippet}", g.arts):
                continue
            seen.add(h.url)
            market = "abroad" if any(m in dom for m in FOREIGN_MARKETPLACES) else "rf"
            cands.append({"market": market, "source": "yandex", "url": h.url, "domain": dom, "title": h.title,
                          "snippet": h.snippet, "query": q, "position": h.position})
    cands.sort(key=lambda c: _rank(c, g.arts))
    out, per_domain = [], {}
    for c in cands:
        if per_domain.get(c["domain"], 0) >= 2:
            continue
        per_domain[c["domain"]] = per_domain.get(c["domain"], 0) + 1
        out.append(c)
    return out[:MAX_PAGES]


def _abroad_candidates(res: dict) -> list[dict]:
    out, seen = [], set()
    for o in res.get("offers") or []:
        url = o.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        out.append({"market": "abroad", "source": "claude", "url": url, "domain": _domain(url), "title": o.get("title", ""),
                    "reported_price": o.get("price", ""), "reported_currency": o.get("currency", ""),
                    "country": o.get("country", ""), "in_search_results": o.get("in_search_results", False)})
    return out[:8]


def _open(c: dict, arts: list[str]) -> dict:
    page = fetch.fetch(c["url"], foreign=c["market"] == "abroad")
    c["fetch"] = {"status": page.status, "via": page.via, "error": page.error}
    if page.html and not page.error:
        info = analyze_page(page.html, arts)
        c.update(title=info.title or c.get("title", ""), has_article=info.has_article, context=info.context,
                 offers=[o.__dict__ for o in info.offers])
        c["opened"] = True
    else:
        c["opened"] = False
    return c


def _reopen_in_browser(c: dict, arts: list[str]) -> dict:
    page = fetch.browser_html(c["url"], foreign=c["market"] == "abroad")
    c["browser"] = {"error": page.error}
    if page.html and not page.error:
        info = analyze_page(page.html, arts)
        if info.offers:
            c.update(title=info.title or c.get("title", ""), has_article=info.has_article or c.get("has_article"),
                     context=info.context, offers=[o.__dict__ for o in info.offers], opened=True)
    return c


def _fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def _mark(label: str, n: int, queries: list[str]) -> str:
    qs = " | ".join(f"«{q}»" for q in dict.fromkeys(queries))
    status = f"найдено {n} из 3" if n else "не найдено"
    return f"{label}: {status} [запросы: {qs}]"


def _clean_note(note: str) -> str:
    out = MARK_RE.sub("", str(note or ""))
    return re.sub(r"(\s*;\s*){2,}", "; ", out).strip(" ;")


def _marks(note: str) -> list[str]:
    return [m.group(0).strip() for m in MARK_RE.finditer(str(note or ""))]


def carry_over(old_rows: list[dict], new_rows: list[dict]) -> int:
    """Повторный анализ: цены рынков и пометки поиска из прошлых позиций переносятся в новые с тем же «№»
    и той же маркой/моделью (иначе позиция изменилась — цены надо искать заново). Возвращает число строк."""
    old = {(str(r.get("№") or "").strip(), norm(r.get("Марка/модель") or r.get("Наименование позиции") or "")): r
           for r in old_rows or []}
    n = 0
    for r in new_rows:
        o = old.get((str(r.get("№") or "").strip(), norm(r.get("Марка/модель") or r.get("Наименование позиции") or "")))
        if not o or not any(o.get(c) not in (None, "") for c in nc.MARKET_OFFER_FIELDS):
            continue
        for c in nc.MARKET_OFFER_FIELDS:
            if o.get(c) not in (None, "") and r.get(c) in (None, ""):
                r[c] = o[c]
        marks = _marks(o.get("Примечание"))
        if marks:
            r["Примечание"] = "; ".join(x for x in [_clean_note(r.get("Примечание")), *marks] if x)
        n += 1
    return n


def find_prices(client, folder_name: str, folder: Path, progress, urgent: bool = False,
                abroad: bool = True) -> tuple[str, dict]:
    """Поиск цен по позициям анализа. urgent — Яндекс в мгновенном режиме; abroad — искать и за рубежом (Claude)."""
    started = time.time()
    result = load_result(folder)
    if not result or not result.get("positions"):
        raise RuntimeError("сначала нужен анализ документов — цены ищутся по его позициям")
    analyzed_at = result.get("analyzed_at")
    abroad = abroad and ABROAD_ON
    rows = result["positions"]
    groups = _groups(rows)
    usage = Usage()
    claude = Claude(PRICES_MODEL, usage, "low")
    errors: list[str] = []
    ystats = yandex.Stats()

    def step(text: str, frac: float) -> None:
        progress(text, max(0.0, min(frac, 0.99)))

    # 1. Своя база цен.
    step("Своя база цен (прошлые спецификации NocoDB)", 0.02)
    try:
        index = history.load_index(client, lambda s, a, b: step(s, 0.02 + 0.06 * a / max(b, 1)))
        for g in groups:
            g.history = history.lookup(index, g.arts, exclude_tender=result.get("internal_number", ""))
    except Exception as exc:
        errors.append(f"своя база цен: {exc}")

    pool = ThreadPoolExecutor(max_workers=FETCH_PARALLEL)
    try:
        # 2. Зарубеж (Claude) — параллельно с ожиданием Яндекса.
        abroad_futs = {}
        if abroad:
            for g in groups:
                abroad_futs[g.key] = pool.submit(abroad_search.search_abroad, PRICES_MODEL, g.name, g.brand_model, g.arts, usage)

        # 3. Россия (Яндекс).
        all_q = list(dict.fromkeys(q for g in groups for q in g.rf_queries))
        step(f"Яндекс: {len(all_q)} запросов ({'срочно' if urgent else 'отложенный режим'})", 0.1)
        try:
            hits = yandex.search_many(all_q, urgent=urgent, timeout_min=ASYNC_TIMEOUT_MIN, stats=ystats,
                                      progress=lambda s, a, b: step(s, 0.1 + 0.3 * a / max(b, 1)))
        except LLMError as exc:
            errors.append(f"Яндекс: {exc}")
            hits = {}
        errors += ystats.errors[:20]
        for g in groups:
            g.candidates = _rf_candidates(g, hits)

        step("Зарубеж: поиск Claude", 0.42)
        for g in groups:
            fut = abroad_futs.get(g.key)
            if not fut:
                continue
            try:
                res = fut.result()
                g.abroad_queries = res.get("queries") or []
                g.candidates += _abroad_candidates(res)
                if res.get("notes"):
                    g.notes.append(f"зарубеж: {res['notes'][:200]}")
            except Exception as exc:        # ошибка по одному артикулу не останавливает поиск по остальным
                errors.append(f"зарубеж ({g.arts[0] if g.arts else g.name[:30]}): {exc}")

        # 4. Страницы товара.
        todo = [(g, c) for g in groups for c in g.candidates]
        step(f"Открываю страницы: 0 из {len(todo)}", 0.45)
        futs = [pool.submit(_open, c, g.arts) for g, c in todo]
        for i, f in enumerate(futs):
            try:
                f.result()
            except Exception as exc:
                errors.append(f"страница: {exc}")
            if i % 5 == 0:
                step(f"Открываю страницы: {i + 1} из {len(todo)}", 0.45 + 0.3 * (i + 1) / max(len(todo), 1))
        if fetch.CHROMIUM:
            need = [(g, c) for g, c in todo if c.get("opened") is not None and not c.get("offers")
                    and (c.get("has_article") or c["source"] == "claude" or not c.get("opened"))][:BROWSER_LIMIT]
            for i, (g, c) in enumerate(need):
                step(f"Встроенный браузер: {i + 1} из {len(need)}", 0.75 + 0.05 * (i + 1) / max(len(need), 1))
                try:
                    _reopen_in_browser(c, g.arts)
                except Exception as exc:
                    errors.append(f"встроенный браузер: {exc}")

        # 5. Проверка моделью — по позициям.
        step("Проверка предложений моделью", 0.8)
        jfuts = {}
        for g in groups:
            usable = [c for c in g.candidates if c.get("opened")]
            for i, c in enumerate(usable):
                c["i"] = i + 1
                best = (c.get("offers") or [{}])[0]
                c["price"], c["currency"], c["method"] = best.get("price"), best.get("currency"), best.get("method")
                c["availability"], c["condition"] = best.get("availability", ""), best.get("condition", "")
            g.unopened = [c["domain"] for c in g.candidates if not c.get("opened")]
            if usable:
                jfuts[g.key] = (pool.submit(judge, claude, g.rows[0], usable, f"Проверка: {g.arts[0] if g.arts else g.name[:30]}"),
                                usable)
        for g in groups:
            if g.key not in jfuts:
                continue
            fut, usable = jfuts[g.key]
            try:
                verdicts = fut.result()
            except Exception as exc:
                errors.append(f"проверка ({g.arts[0] if g.arts else g.name[:30]}): {exc}")
                verdicts = {}
            for c in usable:
                v = verdicts.get(c["i"]) or {}
                c["verdict"] = v
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # 6. Выбор цен.
    step("Выбор цен и запись", 0.9)
    fx = rates()
    if not fx:
        errors.append("курс ЦБ получить не удалось — зарубежные цены в валюте не пересчитаны")
    for g in groups:
        for mkey in MARKETS:
            accepted = []
            for c in g.candidates:
                v = c.get("verdict") or {}
                if c["market"] != mkey or v.get("match") != "yes":
                    continue
                price = _num(v.get("price")) or c.get("price")
                cur = (v.get("currency") or c.get("currency") or ("RUB" if mkey == "rf" else "")).upper()
                if not price:
                    continue
                rub = to_rub(float(price), cur, fx) if mkey == "abroad" or cur not in ("", "RUB") else float(price)
                if rub is None:
                    continue
                accepted.append({"price_rub": round(rub, 2), "price": float(price), "currency": cur or "RUB",
                                 "url": c.get("url"), "domain": c["domain"], "note": v.get("note", "")})
            accepted.sort(key=lambda x: x["price_rub"])
            top, doms = [], set()
            for a in accepted:
                if a["domain"] in doms:
                    continue
                doms.add(a["domain"])
                top.append(a)
                if len(top) == 3:
                    break
            g.chosen[mkey] = top

    # 7. Запись в позиции.
    for g in groups:
        rf, ab = g.chosen.get("rf", []), g.chosen.get("abroad", [])
        marks = []
        if len(rf) < 3:
            marks.append(_mark("РФ", len(rf), g.rf_queries))
        if len(ab) < 3:
            marks.append(_mark("Зарубеж", len(ab), g.abroad_queries) if g.abroad_queries
                         else f"Зарубеж: {'найдено ' + str(len(ab)) + ' из 3' if ab else 'не найдено'} "
                              f"[запросы: поиск за рубежом {'не выполнялся (выключен)' if not abroad else 'не удался'}]")
        marks.append("Avito: поиск не выполнялся (на паузе)")
        for a in ab:
            if a["currency"] not in ("RUB", ""):
                marks.append(f"Зарубеж {a['domain']}: {a['price']:g} {a['currency']} × курс ЦБ {fx.get('_date', '')}"
                             f" = {_fmt(a['price_rub'])} руб.")
        if g.history:
            h = " | ".join(f"{it['tender']} № {it['n']}: " + ", ".join(
                f"{p['kind']} {p['price']}" for p in it["prices"][:3]) for it in g.history[:3])
            marks.append(f"Прошлые цены: {h}"[:400])
        if g.unopened:
            marks.append("Не открылись: " + ", ".join(dict.fromkeys(g.unopened))[:200])
        for r in g.rows:
            for mkey, (_label, prefix) in MARKETS.items():
                chosen = g.chosen.get(mkey, [])
                for i in (1, 2, 3):
                    a = chosen[i - 1] if i <= len(chosen) else None
                    r[f"{prefix} - предложение {i}"] = a["price_rub"] if a else None
                    r[f"{prefix} - ссылка {i}"] = a["url"] if a else None
            base = _clean_note(r.get("Примечание"))
            r["Примечание"] = "; ".join(x for x in [base, *marks] if x)

    current = load_result(folder) or {}
    if current.get("analyzed_at") != analyzed_at:
        raise RuntimeError("пока искались цены, анализ документов обновился — запустите поиск цен ещё раз")
    result["positions"] = rows
    result["prices_at"] = time.time()
    save_result(folder, result)
    spec_name = (result.get("outputs") or {}).get("spec_xlsx") or "Позиции_и_цены.xlsx"
    spec_path = folder / OUT_DIR / spec_name
    write_spec_xlsx(spec_path, rows)

    checks = []
    try:
        res, _recheck = check_prices.check(str(spec_path), ["РФ", "Зарубеж"], 5)
        if res:
            total, checked, problems = res
            checks.append(f"Проверка цен: {checked} поз. проверено, замечаний — {len(problems)}"
                          + ("" if not problems else ": " + "; ".join(f"№ {n}: {p[0]}" for n, _, p in problems[:8])))
    except Exception as exc:
        checks.append(f"Проверка цен не выполнена: {exc}")

    # 8. NocoDB — только колонки рынков и «Примечание», только в спецификации, созданной сайтом.
    noco = _write_noco(client, result, rows)

    found_rf = sum(1 for g in groups if g.chosen.get("rf"))
    found_ab = sum(1 for g in groups if g.chosen.get("abroad"))
    report = {
        "at": time.time(), "at_text": datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
        "seconds": round(time.time() - started), "urgent": urgent, "model": PRICES_MODEL,
        "fx": {k: fx.get(k) for k in ("USD", "EUR", "CNY", "_date")} if fx else {},
        "abroad": abroad,
        "cost": {"claude_usd": usage.total_usd(), "claude": usage.totals(), "yandex_rub": ystats.rub,
                 "yandex_sync": ystats.sync, "yandex_async": ystats.async_, "yandex_cached": ystats.cached,
                 "searches": sum(c.get("searches") or 0 for c in usage.calls)},
        "summary": {"groups": len(groups), "rf_found": found_rf, "abroad_found": found_ab,
                    "rf_full": sum(1 for g in groups if len(g.chosen.get("rf", [])) == 3),
                    "abroad_full": sum(1 for g in groups if len(g.chosen.get("abroad", [])) == 3)},
        "checks": checks, "errors": errors, "noco": noco,
        "groups": [{
            "positions": [r.get("№") for r in g.rows], "name": g.name, "brand_model": g.brand_model, "arts": g.arts,
            "rf_queries": g.rf_queries, "abroad_queries": g.abroad_queries, "chosen": g.chosen, "history": g.history[:5],
            "unopened": g.unopened, "notes": g.notes,
            "candidates": [{k: c.get(k) for k in ("market", "source", "url", "domain", "title", "price", "currency", "method",
                                                   "has_article", "opened", "fetch", "verdict", "reported_price",
                                                   "reported_currency")} for c in g.candidates],
        } for g in groups],
    }
    (folder / OUT_DIR).mkdir(parents=True, exist_ok=True)
    report_path(folder).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = (f"Позиций (уникальных артикулов): {len(groups)}; цены РФ — у {found_rf}, зарубеж — у {found_ab}; "
               f"Claude ≈ {money(usage.total_usd())}, Яндекс ≈ {ystats.rub:.2f} ₽"
               + (f"; {_noco_text(noco)}" if noco else ""))
    return summary, report


def _num(v) -> float | None:
    from .extract import parse_price
    return parse_price(v) if v not in (None, "") else None


def _noco_text(noco: dict) -> str:
    if noco.get("status") == "saved":
        return f"NocoDB: обновлено строк {noco.get('rows', 0)}"
    return "NocoDB: " + noco.get("text", "не записано")


def _write_noco(client, result: dict, rows: list[dict]) -> dict:
    internal = result.get("internal_number") or ""
    link = db.noco_link(client.id, internal)
    if not link or not link.get("created_by_site") or not link.get("spec_table_id"):
        return {"status": "skipped", "text": "не записано — спецификацию этого конкурса в NocoDB создал не сайт "
                                             "(или её ещё нет); цены — в Позиции_и_цены.xlsx"}
    s = client.nocodb
    nc.BASE_ID = s.base_id
    try:
        token = s.token()
        table_id = link["spec_table_id"]
        nc.ensure_spec_columns(token, table_id, only=nc.MARKET_OFFER_FIELDS)
        existing = {str(r.get("№") or "").strip(): r["Id"] for r in nc.list_all_records(token, table_id)}
        updates = []
        for r in rows:
            rid = existing.get(str(r.get("№") or "").strip())
            if not rid:
                continue
            u = {"Id": rid, "Примечание": r.get("Примечание") or ""}
            for col in MARKET_COLUMNS:
                u[col] = r.get(col)
            updates.append(u)
        for i in range(0, len(updates), 100):
            status, body = nc._request("PATCH", f"{nc.BASE_URL}/{table_id}/records", token, updates[i:i + 100])
            if status != 200:
                return {"status": "error", "text": f"HTTP {status}: {str(body)[:200]}"}
        return {"status": "saved", "rows": len(updates), "table_id": table_id}
    except Exception as exc:
        return {"status": "error", "text": str(exc)[:300]}
