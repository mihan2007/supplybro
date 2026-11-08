"""
Общая таблица цен по позициям — раздел «Цены» → «Все цены» на странице и блок <prices> для чата.

0.8.0: по каждой позиции анализа — НМЦ за единицу; интернет — проверенные цены со страниц магазинов (РФ / зарубеж, из
AIExtract/prices.json); «не проверено» — цены из выдачи поиска (решение пользователя 04.11.2026: показывать с
пометкой; в NocoDB и xlsx они не идут); поставщики — из разобранных прайсов / КП / счетов (AIExtract/pricelists.json)
и из колонок «Отдел закупок» спецификации NocoDB.

0.10.0 — пользователь 04.11.2026: «у нас проблема с тендерами, которые были загружены, — укажи цены, если они уже есть
в базе данных». Конкурсы, которые разбирал агент (К922–К939), на сайте анализа не имеют, а цены у них — в спецификации
NocoDB. Теперь из спецификации этого конкурса читаются все цены: «Цена агента 1–3» с комментариями, рынки РФ / зарубеж
со ссылками, «Рыночная цена, руб.» (старые таблицы), «Отдел закупок», доставка СДЭК; нет анализа на сайте — позиции
тоже из спецификации. «Прошлые конкурсы» — тот же артикул в других спецификациях (своя база цен,
app/prices/history.py: файл в папке данных; устарел — обновляется в фоне, страница не ждёт).

Лучшая цена — самая низкая из проверенных: интернет (сайт и NocoDB), агент, рыночная цена, поставщики (неполное
совпадение 🔴 — с пометкой; «не проверено», цены с ⚠ и прошлые конкурсы — не учитываются).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from . import db
from .analysis.run import load_result
from .noco_tables import own_table_async
from .nocodb import NocoClient, NocoError
from .prices.extract import parse_price
from .pricelists import RED, _supplier_key, supplier_prices
from .tenders import norm_number

from tools import nocodb_client as nc

SPEC_LINK = "Ссылка на спецификацию"
TABLE_ID_RE = re.compile(r"/(m[a-z0-9]{8,})(?:/|$|\?)")
SPEC_TTL = 60            # с: страница опрашивает «Цены» каждые 3–5 с, пока идут задачи — NocoDB не дёргаем каждый раз
CHAT_TTL = 1800          # с: чату годится прочитанное страницей за последние полчаса
MARKETS = (("rf", "Стоимость товара в РФ"), ("abroad", "Стоимость товара зарубежом"))
MARKET_PRICE, MARKET_SOURCE, MARKET_TEXT = "Рыночная цена, руб.", "Источник (ссылка)", "Рыночная цена (найдено в интернете)"
DELIVERY = "Стоимость доставки, руб. (СДЭК)"
SPEC_FIELDS = (["№", "Наименование позиции", "Марка/модель", "Кол-во", "Ед. изм.", nc.NMC_COST_COLUMN, "НМЦК за позицию"]
               + [c for pair in nc.AGENT_PRICE_PAIRS for c in pair] + list(nc.MARKET_OFFER_FIELDS)
               + [MARKET_PRICE, MARKET_SOURCE, MARKET_TEXT] + [c for pair in nc.PROCUREMENT_PAIRS for c in pair]
               + [nc.PROCUREMENT_COMMENT_COLUMN, DELIVERY])
_URL = re.compile(r"https?://[^\s\"'<>»)]+")


def _report(folder: Path) -> dict | None:
    from .prices.run import load_report
    return load_report(folder)


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return (s[:-3] if s.endswith(",00") else s) + " ₽"


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    return parse_price(v) if str(v or "").strip() else None


def _n(v) -> str:
    return str(v or "").strip().rstrip(".")


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _first_url(text) -> str:
    m = _URL.search(str(text or ""))
    return m.group(0).rstrip(".,;") if m else ""


# ---------- спецификация NocoDB этого конкурса ----------

_SPEC_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


def _spec_row(r: dict) -> dict:
    out = {"n": _n(r.get("№")), "name": " ".join(str(r.get("Наименование позиции") or "").split()),
           "brand_model": " ".join(str(r.get("Марка/модель") or "").split()),
           "qty": r.get("Кол-во"), "unit": r.get("Ед. изм."), "nmc": _num(r.get(nc.NMC_COST_COLUMN)),
           "agent": [], "rf": [], "abroad": [], "market": None, "purch": [], "delivery": _num(r.get(DELIVERY)),
           "purch_comment": str(r.get(nc.PROCUREMENT_COMMENT_COLUMN) or "").strip()}
    for i, (pc, cc) in enumerate(nc.AGENT_PRICE_PAIRS, 1):
        p = _num(r.get(pc))
        if p:
            comment = str(r.get(cc) or "").strip()
            out["agent"].append({"i": i, "price_rub": p, "comment": comment, "url": _first_url(comment)})
    for mkey, prefix in MARKETS:
        for i in (1, 2, 3):
            p = _num(r.get(f"{prefix} - предложение {i}"))
            if not p:
                continue
            link = str(r.get(f"{prefix} - ссылка {i}") or "").strip()
            url = _first_url(link)
            out[mkey].append({"price_rub": p, "url": url, "domain": _domain(url) or link[:60], "from_noco": True})
    mp = _num(r.get(MARKET_PRICE))
    if mp:
        src = str(r.get(MARKET_SOURCE) or "").strip()
        out["market"] = {"price_rub": mp, "url": _first_url(src), "source": src[:200],
                         "text": " ".join(str(r.get(MARKET_TEXT) or "").split())[:300]}
    for sc, pc in nc.PROCUREMENT_PAIRS:
        sup, price = str(r.get(sc) or "").strip(), _num(r.get(pc))
        if sup and price:
            out["purch"].append({"supplier": sup, "price_rub": price})
    return out


async def read_spec(client, folder_name: str, record_id: int | None = None, ttl: float = SPEC_TTL) -> tuple[dict | None, str]:
    """Цены из спецификации NocoDB конкурса: {"table_id", "rows": {№: {...}}} и текст ошибки. Таблица — по записи
    реестра (ссылка на спецификацию), иначе из noco_links, иначе по названию (внутреннему номеру)."""
    key = (client.id, folder_name)
    hit = _SPEC_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1], ""
    noco = NocoClient(client.nocodb)
    internal = norm_number(folder_name) or ""
    try:
        table_id = None
        if record_id:
            rec = await noco.record(client.nocodb.registry_table_id, record_id)
            m = TABLE_ID_RE.search(str(rec.get(SPEC_LINK) or ""))
            # Только таблица своей базы (0.16.1, app/noco_tables.py): ссылка из копии базы ведёт в старую.
            table_id = await own_table_async(client.nocodb, m.group(1)) if m else None
            internal = internal or norm_number(str(rec.get("Внутренний номер") or "")) or ""
        if not table_id and internal:
            tid = str((db.noco_link(client.id, internal) or {}).get("spec_table_id") or "")
            table_id = await own_table_async(client.nocodb, tid) if tid and not tid.startswith("pending:") else None
        if not table_id and internal:
            table_id = await noco.find_table_by_title(internal)
        if not table_id:
            return None, ""
        titles = {c["title"] for c in (await noco.table_meta(table_id))["columns"]}
        if "№" not in titles:
            return None, ""
        # Все колонки, без списка fields: NocoDB делит его по запятым, а в названиях колонок они есть
        # («Отдел закупок: цена 1, руб.», «Рыночная цена, руб.») — с fields отвечал 404 ERR_FIELD_NOT_FOUND.
        rows = await noco.records(table_id)
    except NocoError as exc:
        return None, str(exc)
    parsed: dict[str, dict] = {}
    for r in rows:
        row = _spec_row(r)
        if row["n"] and row["n"] not in parsed:
            parsed[row["n"]] = row
    data = {"table_id": table_id, "rows": parsed, "at": time.time()}
    _SPEC_CACHE[key] = (time.time(), data)
    return data, ""


def cached_spec(client_id: str, folder_name: str, ttl: float = CHAT_TTL) -> dict | None:
    hit = _SPEC_CACHE.get((client_id, folder_name))
    return hit[1] if hit and time.time() - hit[0] < ttl else None


# ---------- своя база цен: тот же артикул в прошлых конкурсах ----------

_HIST_LOCK = threading.Lock()
_HIST_BUILDING: set[str] = set()
_HIST_TRIED: dict[str, float] = {}          # когда последний раз собирали (неудача — не чаще раза в 10 минут)
_HIST_PARSED: dict[str, tuple[float, dict]] = {}
HIST_RETRY = 600


def _build_history(client) -> None:
    from .prices import history
    try:
        history.load_index(client)
    except Exception:
        traceback.print_exc()
    finally:
        with _HIST_LOCK:
            _HIST_BUILDING.discard(client.id)


def history_index(client, refresh: bool = True) -> tuple[dict, dict]:
    """Индекс своей базы цен (файл обработчика в папке данных) и сведения о нём: {at, building}. Нет файла или устарел —
    собирается заново в фоне (чтение всех спецификаций NocoDB — до минуты), страница не ждёт."""
    from .prices import history
    path = history.DATA_DIR / f"price_history_{client.id}.json"
    info = {"at": "", "building": False}
    index: dict = {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime:
        hit = _HIST_PARSED.get(client.id)
        if hit and hit[0] == mtime:
            index = hit[1]
        else:
            try:
                index = json.loads(path.read_text(encoding="utf-8"))
                _HIST_PARSED[client.id] = (mtime, index)
            except (OSError, ValueError):
                index = {}
        info["at"] = time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime))
    stale = mtime is None or time.time() - mtime > history.CACHE_HOURS * 3600
    if stale and refresh:
        with _HIST_LOCK:
            if client.id in _HIST_BUILDING:
                info["building"] = True
            elif time.time() - _HIST_TRIED.get(client.id, 0) > HIST_RETRY:
                _HIST_BUILDING.add(client.id)
                _HIST_TRIED[client.id] = time.time()
                threading.Thread(target=_build_history, args=(client,), daemon=True).start()
                info["building"] = True
    return index, info


def _history_for(index: dict, name: str, brand_model: str, internal: str) -> list[dict]:
    if not index:
        return []
    from .prices.articles import articles
    from .prices.history import lookup
    out = []
    for item in lookup(index, articles(brand_model, name), exclude_tender=internal)[:3]:
        prices = []
        for p in item.get("prices") or []:
            v = _num(p.get("price"))
            if v:
                prices.append({"kind": p.get("kind") or "", "price_rub": v, "url": _first_url(p.get("url")),
                               "who": str(p.get("who") or "")[:150]})
        if prices:
            out.append({"tender": item.get("tender"), "n": item.get("n"), "name": item.get("name", "")[:150],
                        "updated": item.get("updated", ""), "prices": prices[:4]})
    return out


# ---------- таблица ----------

def _dup(o: dict, items: list[dict]) -> bool:
    for a in items:
        if o.get("url") and a.get("url") and o["url"].rstrip("/") == a["url"].rstrip("/"):
            return True
        if o.get("domain") and o["domain"] == a.get("domain") and abs((a.get("price_rub") or 0) - o["price_rub"]) < 0.01:
            return True
    return False


def build(folder: Path, spec: dict | None = None, hist_index: dict | None = None, internal: str = "") -> dict:
    """{"rows": [...], "has_analysis", "has_positions", "positions_source", "has_report", "report_at", "abroad"}.
    spec — цены из спецификации NocoDB (read_spec), hist_index — своя база цен (history_index)."""
    result = load_result(folder) or {}
    report = _report(folder) or {}
    internal = internal or result.get("internal_number") or norm_number(folder.name) or ""
    by_pos: dict[str, dict] = {}
    for g in report.get("groups") or []:
        for n in g.get("positions") or []:
            by_pos[_n(n)] = g
    sup = supplier_prices(folder)
    spec_rows = (spec or {}).get("rows") or {}
    if result.get("positions"):
        source = "analysis"
        positions = [{"n": _n(p.get("№")), "name": " ".join(str(p.get("Наименование позиции") or "").split()),
                      "brand_model": " ".join(str(p.get("Марка/модель") or "").split()), "qty": p.get("Кол-во"),
                      "unit": p.get("Ед. изм."), "nmc": parse_price(p.get("Стоимость НМЦ"))}
                     for p in result["positions"]]
    else:
        source = "nocodb" if spec_rows else ""
        positions = [{k: s[k] for k in ("n", "name", "brand_model", "qty", "unit", "nmc")} for s in spec_rows.values()]
    rows = []
    for p in positions:
        n = p["n"]
        if not n:
            continue
        g = by_pos.get(n) or {}
        s = spec_rows.get(n) or {}
        chosen = g.get("chosen") or {}
        rf = list(chosen.get("rf") or [])
        ab = list(chosen.get("abroad") or [])
        rf += [o for o in s.get("rf") or [] if not _dup(o, rf)]
        ab += [o for o in s.get("abroad") or [] if not _dup(o, ab)]
        rf.sort(key=lambda a: a.get("price_rub") or 0)
        ab.sort(key=lambda a: a.get("price_rub") or 0)
        suppliers = [dict(x, source="прайс") for x in sup.get(n, [])]
        for slot in s.get("purch") or []:
            same = next((x for x in suppliers if _supplier_key(x["supplier"]) == _supplier_key(slot["supplier"])
                         and abs((x["price_rub"] or 0) - (slot["price_rub"] or 0)) < 0.01), None)
            if same:
                same["in_noco"] = True
            else:
                partial = RED.strip() in str(slot["supplier"])
                suppliers.append({"supplier": str(slot["supplier"]).replace(RED, "").replace(RED.strip(), "").strip(),
                                  "price_rub": slot["price_rub"], "match": "partial" if partial else "exact", "ok": True,
                                  "source": "NocoDB", "in_noco": True, "problems": []})
        suppliers.sort(key=lambda x: x["price_rub"] or 0)
        agent = s.get("agent") or []
        market = s.get("market")
        nmc = p["nmc"] or s.get("nmc")
        cands = [(a["price_rub"], f"РФ: {a.get('domain') or 'NocoDB'}", a.get("url")) for a in rf[:1]]
        cands += [(a["price_rub"], f"зарубеж: {a.get('domain') or 'NocoDB'}", a.get("url")) for a in ab[:1]]
        cands += [(a["price_rub"], f"агент (цена {a['i']})", a.get("url")) for a in agent]
        if market:
            cands.append((market["price_rub"], "рыночная цена (NocoDB)", market.get("url")))
        cands += [(x["price_rub"], f"поставщик: {x['supplier']}" + (" 🔴" if x.get("match") != "exact" else "")
                   + (", без НДС" if x.get("vat") == "без НДС" else ""), "")
                  for x in suppliers if x.get("ok") and not x.get("problems") and x.get("price_rub")]
        best = min(cands, key=lambda x: x[0]) if cands else None
        rows.append({
            "n": n, "name": p["name"] or s.get("name", ""), "brand_model": p["brand_model"] or s.get("brand_model", ""),
            "qty": p["qty"] if p["qty"] not in (None, "") else s.get("qty"), "unit": p["unit"] or s.get("unit"),
            "nmc": nmc, "rf": rf, "abroad": ab, "unverified": g.get("unverified") or [], "searched": bool(g),
            "suppliers": suppliers, "agent": agent, "market": market, "delivery": s.get("delivery"),
            "history": _history_for(hist_index or {}, p["name"], p["brand_model"], internal),
            "best": {"price_rub": best[0], "source": best[1], "url": best[2]} if best else None,
            "ratio": round(best[0] / nmc, 3) if best and nmc else None,
        })
    rows.sort(key=lambda r: (float(r["n"]) if re.fullmatch(r"\d+(\.\d+)?", r["n"]) else 1e9, r["n"]))
    return {"rows": rows, "has_analysis": bool(result.get("positions")), "has_positions": bool(rows),
            "positions_source": source, "has_report": bool(report), "report_at": report.get("at_text", ""),
            "abroad": report.get("abroad", True), "internal": internal}


def spec_for_chat(client, folder_name: str) -> dict | None:
    """Цены из NocoDB для чата: прочитанные страницей за последние полчаса, иначе — прочитать сейчас (до 15 с)."""
    spec = cached_spec(client.id, folder_name)
    if spec is not None:
        return spec
    try:
        spec, _err = asyncio.run(asyncio.wait_for(read_spec(client, folder_name), 15))
    except Exception:
        spec = None
    return spec


def chat_text(folder: Path, spec: dict | None = None, hist_index: dict | None = None) -> str:
    """Цены для чата — компактно, только позиции, где что-то нашлось (остальные — списком номеров)."""
    data = build(folder, spec, hist_index)
    if not data["has_positions"]:
        return "<prices>\nЦен пока нет: нет ни анализа документов (позиций), ни спецификации в NocoDB.\n</prices>"
    lines, empty = [], []
    for r in data["rows"]:
        parts = []
        if r["rf"]:
            parts.append("РФ — " + ", ".join(f"{_fmt(a['price_rub'])} ({a.get('domain') or ''}, {a.get('url', '')})"
                                             + (" [из NocoDB]" if a.get("from_noco") else "") for a in r["rf"]))
        if r["abroad"]:
            parts.append("зарубеж — " + ", ".join(
                f"{_fmt(a['price_rub'])} ({a.get('domain') or ''}"
                + (f", {a.get('price', '')} {a.get('currency', '')}" if a.get("currency") not in (None, "", "RUB") else "")
                + f", {a.get('url', '')})" + (" [из NocoDB]" if a.get("from_noco") else "") for a in r["abroad"]))
        if r["unverified"]:
            parts.append("из выдачи поиска, НЕ ПРОВЕРЕНО — " + ", ".join(
                f"{_fmt(a['price_rub'])} ({a['domain']}, {a.get('url', '')})" for a in r["unverified"]))
        if r["agent"]:
            parts.append("агент (NocoDB) — " + ", ".join(
                f"{_fmt(a['price_rub'])} ({' '.join(a['comment'].split())[:160]})" for a in r["agent"]))
        if r["market"]:
            m = r["market"]
            parts.append(f"рыночная цена (NocoDB) — {_fmt(m['price_rub'])} ({m.get('source') or m.get('text') or ''})"[:300])
        if r["suppliers"]:
            parts.append("поставщики — " + ", ".join(
                f"{s['supplier']}{' 🔴 не полное совпадение' if s.get('match') != 'exact' else ''} {_fmt(s['price_rub'])}"
                f" ({'прайс «' + s['file'] + '»' if s.get('file') else 'NocoDB, отдел закупок'}"
                f"{', ' + s['vat'] if s.get('vat') and s['vat'] != 'не указано' else ''}"
                f"{', ' + s['note'] if s.get('note') else ''}"
                f"{', ⚠ ' + '; '.join(s['problems']) if s.get('problems') else ''})" for s in r["suppliers"]))
        if r["delivery"]:
            parts.append(f"доставка СДЭК — {_fmt(r['delivery'])}")
        if r["history"]:
            parts.append("прошлые конкурсы (свои цены) — " + "; ".join(
                f"{h['tender']} № {h['n']}: " + ", ".join(f"{p['kind']} {_fmt(p['price_rub'])}" for p in h["prices"][:3])
                for h in r["history"]))
        if not parts:
            empty.append(r["n"])
            continue
        head = f"№ {r['n']} «{r['name'][:120]}» ({r['qty'] or ''} {r['unit'] or ''}, НМЦ за ед. {_fmt(r['nmc'])})"
        best = r["best"]
        tail = (f"; лучшая — {_fmt(best['price_rub'])} ({best['source']})"
                + (f", {round((r['ratio'] - 1) * 100):+d} % к НМЦ" if r["ratio"] else "")) if best else ""
        lines.append(head + ": " + "; ".join(parts) + tail)
    intro = ("Цены по позициям (интернет — проверенные цены со страниц магазинов"
             + (f", поиск сайта от {data['report_at']}" if data["report_at"] else ", поиск цен на сайте не запускался")
             + "; [из NocoDB] и «агент» — цены, уже записанные в спецификацию NocoDB (агентом или сайтом); «НЕ ПРОВЕРЕНО» "
               "— цена из выдачи поиска, страницу не открывали; поставщики — прайсы, КП, счета и колонки «Отдел закупок»; "
               "прошлые конкурсы — тот же артикул в других спецификациях, только для сравнения; рубли — по курсу ЦБ"
             + ("; позиции — из спецификации NocoDB, анализа на сайте нет" if data["positions_source"] == "nocodb" else "")
             + "):")
    body = "\n".join(lines) or "Ни по одной позиции цен пока нет."
    if empty:
        body += f"\nБез цен: № {', '.join(empty)}."
    return "<prices>\n" + intro + "\n" + body + "\n</prices>"
