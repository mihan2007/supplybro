"""Задача «bid» обработчика — подготовка заявки по кнопке «Подготовить документы заявки» (0.15.0).

Порядок (PARTICIPATION.md и PREPARE_DOCS.md старого агента):
1. Участник и его материалы (participant.load).
2. Модель читает документацию (тот же блок документов, что анализ) и сведения участника: требования к участнику со
   статусами, как подаётся заявка, полный состав заявки и что из библиотеки участника подходит (цитаты проверяет код).
3. Где в файлах Word документации формы (по оглавлениям файлов — дёшево).
4. Папка AIExtract/Заявка (прежняя — в AIExtract/history): копии документов из библиотеки (сканы — в PDF), формы,
   заполненные по правкам модели (по форме — свой вызов, параллельно), опись — последней.
5. Цены, НДС, суммы прописью и паспортные данные — метками, их подставляет код (по цене, которую задал пользователь;
   нет цены — «<нет данных>»). Новая цена — задача bid с reapply: формы собираются заново по сохранённым правкам, без
   модели.
6. Самопроверка: пометки «<нет данных>» → приставка «ЧЕРНОВИК », «Нет данных.txt»; запрещённые значения (известные
   ошибки реквизитов); первая часть без сведений об участнике; цена не выше НМЦ.
7. Отчёт AIExtract/bid.json (страница) и «Перечень документов заявки.txt» в папке пакета; файлы поставщикам.
В NocoDB — по кнопке (задача noco_bid).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .. import db, jobctl
from ..prompt_store import for_client
from ..analysis.corpus import QuoteIndex, build_documents_block, load_docs
from ..analysis.llm import Claude, DeepSeekJSON, Usage
from ..analysis.run import CORPUS_LIMIT, MSK, OUT_DIR, effort_for, load_result
from ..docs.names import fix_path
from ..docs.pipeline import CONV_DIR
from . import forms, money, participant as participant_mod, prompts

BID_MODEL = os.environ.get("BID_MODEL", "deepseek-flash")
# Модели на выбор (администратору на странице). DeepSeek — в 15–30 раз дешевле Claude (0.15.1, пользователь 05.11.2026:
# «модели Claude оказались очень прожорливые… давай попробуем добавить туда DeepSeek»).
BID_MODELS = [m.strip() for m in os.environ.get(
    "BID_MODELS", "deepseek-flash,deepseek-v4-pro,claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5").split(",") if m.strip()]
if BID_MODEL not in BID_MODELS:
    BID_MODELS.insert(0, BID_MODEL)
BID_DIR = OUT_DIR / "Заявка"
REPORT_FILE = OUT_DIR / "bid.json"
HISTORY_DIR = OUT_DIR / "history"
GAPS_FILE = "Нет данных.txt"
LIST_FILE = "Перечень документов заявки.txt"
PARALLEL = int(os.environ.get("BID_PARALLEL", "4"))
MAX_FORMS = int(os.environ.get("BID_MAX_FORMS", "30"))
DRAFT = "ЧЕРНОВИК "
KEEP_BACKUPS = 5
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
IMAGE_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}
SKIP_LIBRARY_EXT = {".p7s", ".sig", ".xml", ".zip"}
FILLED_KINDS = ("form", "tech", "price", "inventory")


class BidError(RuntimeError):
    pass


# ---------- отчёт ----------

def report_path(folder: Path) -> Path:
    return folder / REPORT_FILE


def load_report(folder: Path) -> dict | None:
    p = report_path(folder)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        return None


def save_report(folder: Path, report: dict) -> None:
    p = report_path(folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def set_price(folder: Path, price: dict | None) -> dict:
    """Цена заявки от пользователя: {"mode": "percent", "value": 5} (снижение от НМЦ, %) или {"mode": "total",
    "value": 1234567.89, "vat": "with" | "without"}; None — убрать цену."""
    report = load_report(folder) or {}
    report["price"] = price
    save_report(folder, report)
    return report


# ---------- данные конкурса ----------

def _tender_text(result: dict | None, noco_fields: dict | None) -> str:
    flat = (result or {}).get("flat") or {}
    labels = {"purchase_number": "Номер закупки", "procurement_method": "Способ закупки", "procedure_type": "Тип процедуры",
              "platform": "Площадка", "organizer": "Организатор / заказчик", "inn": "ИНН заказчика",
              "address": "Адрес заказчика", "start_price": "НМЦ", "currency": "Валюта",
              "delivery_place": "Место поставки", "delivery_term": "Срок поставки", "payment_terms": "Условия оплаты",
              "bid_security": "Обеспечение заявки", "contract_security": "Обеспечение договора",
              "publication_date": "Дата публикации", "submission_deadline": "Окончание подачи заявок",
              "results_date": "Подведение итогов", "sme": "Для МСП", "national_regime_applied": "Нацрежим",
              "national_regime_measure": "Мера нацрежима", "price_reduction": "Переторжка / уторговывание",
              "status": "Новизна товара", "tender_url": "Ссылка на закупку"}
    lines = []
    title = str(flat.get("product_name") or "").split("\n")[0].strip()
    if title:
        lines.append(f"Предмет закупки: {title}")
    for key, label in labels.items():
        v = str(flat.get(key) or "").strip()
        if v and not v.startswith("Не указано"):
            lines.append(f"{label}: {v[:400]}")
    for k, v in (noco_fields or {}).items():
        if v and not any(line.startswith(k) for line in lines):
            lines.append(f"{k}: {str(v)[:400]}")
    return "\n".join(lines) or "Сведения о закупке — только в документации выше."


def _positions(result: dict | None, client, folder_name: str) -> tuple[list[dict], str]:
    rows = (result or {}).get("positions") or []
    if rows:
        return rows, "analysis"
    try:
        from ..prices.run import noco_positions
        rows = noco_positions(client, folder_name)
        return rows, "nocodb" if rows else ""
    except Exception:
        return [], ""


def _num(v) -> float | None:
    from ..prices.extract import parse_price
    try:
        return parse_price(v) if v not in (None, "") else None
    except Exception:
        return None


def _positions_text(rows: list[dict], with_tz: bool) -> str:
    out = []
    for r in rows:
        parts = [f"№ {r.get('№')}", str(r.get("Наименование позиции") or "").strip()]
        for key, label in (("Марка/модель", "марка/модель"), ("Производитель", "производитель"),
                           ("Страна происхождения", "страна бренда (не производства)")):
            v = str(r.get(key) or "").strip()
            if v and v not in ("—", "-"):
                parts.append(f"{label}: {v}")
        parts.append(f"кол-во: {r.get('Кол-во') or ''} {r.get('Ед. изм.') or ''}".strip())
        nmc = _num(r.get("Стоимость НМЦ"))
        if nmc:
            parts.append(f"НМЦ за ед.: {money.rub(nmc)}")
        if with_tz and r.get("Техническое задание"):
            parts.append("ТЗ: " + " ".join(str(r["Техническое задание"]).split())[:300])
        out.append(" | ".join(parts))
    return "\n".join(out)


# ---------- цена ----------

def _rub_num(text: str) -> float:
    return float(text.replace(" ", "").replace(",", "."))


def price_values(price: dict | None, rows: list[dict], basis: str, vat_rate: float | None,
                 nmc_total: float | None) -> tuple[dict, list[str]]:
    """Значения меток цены. basis — в каком базисе НМЦ и сравнение цен («без НДС» / «с НДС»).

    Цена — от пользователя: снижение от НМЦ в % или общая сумма (с НДС / без НДС). Если у позиций есть НМЦ за единицу,
    цены позиций — НМЦ × тот же коэффициент (сумма — по позициям, чтобы формы сходились копейка в копейку); иначе
    только итоговые суммы. Возвращает (метки, проблемы)."""
    vals: dict = {}
    problems: list[str] = []
    if vat_rate is not None:
        vals["СТАВКА_НДС"] = f"{vat_rate:g} %" if vat_rate else "НДС не облагается"
    if not price or not price.get("value"):
        return vals, problems
    if vat_rate is None:
        problems.append("ставка НДС участника неизвестна — цены в формах не подставлены (vat_rate в config/clients.json)")
        return vals, problems
    k = 1 + vat_rate / 100
    with_vat_basis = basis == "с НДС"
    to_pair = (lambda x: (x / k, x)) if with_vat_basis else (lambda x: (x, x * k))     # → (без НДС, с НДС)
    # Цена заявки в базисе сравнения.
    if price.get("mode") == "percent":
        pct = float(price["value"])
        vals["СНИЖЕНИЕ_ПРОЦ"] = f"{pct:g}"
        factor = 1 - pct / 100
        target = nmc_total * factor if nmc_total else None
    else:
        value = float(price["value"])
        given_with_vat = price.get("vat") != "without"
        if given_with_vat == with_vat_basis:
            target = value
        else:
            target = value / k if given_with_vat else value * k
        factor = target / nmc_total if nmc_total else None
        if factor is not None:
            vals["СНИЖЕНИЕ_ПРОЦ"] = f"{(1 - factor) * 100:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    # По позициям — если у них есть НМЦ за единицу.
    total_wo = total_w = 0.0
    priced = 0
    if factor is not None:
        for r in rows:
            n = str(r.get("№") or "").strip()
            unit_nmc, qty = _num(r.get("Стоимость НМЦ")), _num(r.get("Кол-во"))
            if not n or not unit_nmc or not qty:
                continue
            unit = float(money.to_decimal(unit_nmc * factor))
            unit_wo, unit_w = (float(money.to_decimal(x)) for x in to_pair(unit))
            s_wo, s_w = float(money.to_decimal(unit_wo * qty)), float(money.to_decimal(unit_w * qty))
            total_wo, total_w = total_wo + s_wo, total_w + s_w
            priced += 1
            vals[f"ЦЕНА_ЕД_БЕЗ_НДС:{n}"], vals[f"ЦЕНА_ЕД_С_НДС:{n}"] = money.rub(unit_wo), money.rub(unit_w)
            vals[f"СУММА_БЕЗ_НДС:{n}"], vals[f"СУММА_С_НДС:{n}"] = money.rub(s_wo), money.rub(s_w)
    named = [r for r in rows if str(r.get("№") or "").strip()]
    if priced and priced == len(named):
        if target is not None:
            mine = total_w if with_vat_basis else total_wo
            if abs(mine - target) >= 1:
                problems.append(f"сумма по позициям {money.rub(mine)} руб. отличается от заданной "
                                f"{money.rub(target)} руб. — из-за округления цен за единицу")
    else:
        if priced:
            problems.append(f"НМЦ за единицу есть не у всех позиций ({priced} из {len(named)}) — цены позиций в формах "
                            "не подставлены, только итог")
            for key in [x for x in vals if ":" in x]:
                vals.pop(key)
        elif rows:
            problems.append("в документации нет НМЦ по позициям — цены за единицу в формах остаются «<нет данных>»")
        if target is None:
            problems.append("НМЦ закупки неизвестна — снижение в % не пересчитать в цену; задайте общую сумму")
            return vals, problems
        total_wo, total_w = to_pair(target)
    vals["ЦЕНА_БЕЗ_НДС"], vals["ЦЕНА_С_НДС"] = money.rub(total_wo), money.rub(total_w)
    vals["НДС"] = money.rub(_rub_num(vals["ЦЕНА_С_НДС"]) - _rub_num(vals["ЦЕНА_БЕЗ_НДС"]))
    for key in ("ЦЕНА_БЕЗ_НДС", "ЦЕНА_С_НДС", "НДС"):
        vals[key + "_ПРОПИСЬЮ"] = money.rub_words(_rub_num(vals[key]))
    if nmc_total:
        mine = _rub_num(vals["ЦЕНА_С_НДС"] if with_vat_basis else vals["ЦЕНА_БЕЗ_НДС"])
        if mine > nmc_total + 0.01:
            problems.append(f"цена заявки {money.rub(mine)} руб. выше НМЦ {money.rub(nmc_total)} руб. ({basis}) — "
                            "заявку отклонят")
    return vals, problems


# ---------- файлы ----------

def safe_name(name: str, limit: int = 110) -> str:
    name = BAD_CHARS.sub("_", " ".join(str(name).split())).strip(" ._")
    return (name[:limit].rstrip(" ._") or "Документ")


def library_numbers(text) -> list[str]:
    """Номера документов библиотеки из ответа модели: «301», «304, 306, 303», «501–504» (диапазон — до 10 номеров)."""
    out: list[str] = []
    for m in re.finditer(r"(\d{3})(?:\s*[–—-]\s*(\d{3}))?", str(text or "")):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out += [f"{n:03d}" for n in range(a, b + 1)] if 0 <= b - a <= 10 else [m.group(1)]
    return list(dict.fromkeys(out))


def doc_file_name(d: dict) -> str:
    """Имя файла документа: как требует документация (file_name), иначе «Форма 3 Письмо-согласие…» или название."""
    title = str(d.get("title") or "Документ")
    form = str(d.get("form") or "").strip()
    name = d.get("file_name") or (f"{form} {title}" if form and len(form) <= 20 and form.lower() not in title.lower()
                                  else title)
    return safe_name(name)


def _part_dir(structure: dict, part: str) -> str:
    parts = structure.get("parts") or "single"
    if part == "after_win":
        return "После победы"
    if parts == "single":
        return ""
    return {"1": "1 Первая часть", "2": "2 Вторая часть", "price": "3 Ценовое предложение"}.get(part, "")


def _backup(folder: Path) -> str:
    """Прежний пакет — в AIExtract/history/Заявка <дата время> (правки пользователя в Word не теряются)."""
    src = folder / BID_DIR
    if not src.exists() or not any(src.iterdir()):
        return ""
    stamp = datetime.now(MSK).strftime("%Y-%m-%d %H-%M-%S")
    dst = folder / HISTORY_DIR / f"Заявка {stamp}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    old = sorted(p for p in dst.parent.glob("Заявка *") if p.is_dir())
    for p in old[:-KEEP_BACKUPS]:                  # хранятся последние KEEP_BACKUPS пакетов
        shutil.rmtree(p, ignore_errors=True)
    return str(dst.relative_to(folder))


def _copy_library(src: Path, dst_dir: Path, base_name: str) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    if ext in IMAGE_EXT:                                   # скан — в PDF (площадки принимают PDF)
        from PIL import Image
        dst = dst_dir / (base_name + ".pdf")
        with Image.open(src) as im:
            frames = []
            try:
                while True:
                    frames.append(im.convert("RGB").copy())
                    im.seek(im.tell() + 1)
            except EOFError:
                pass
        frames[0].save(dst, save_all=True, append_images=frames[1:])
        return dst
    dst = dst_dir / (base_name + ext)
    shutil.copy2(src, dst)
    return dst


def _word_sources(client_id: str, folder_name: str, folder: Path) -> list[dict]:
    """Файлы Word документации: исходный .docx или его копия .docx из .doc (AIExtract/_converted)."""
    out = []
    conv = folder / CONV_DIR
    for rel, row in sorted(db.doc_rows(client_id, folder_name).items()):
        if row.get("kind") != "docx" or row.get("status") not in ("ok", "ocr"):
            continue
        path = folder / rel
        if path.suffix.lower() != ".docx":
            sha = (row.get("sha256") or "")[:8]
            hits = sorted(conv.glob(f"{sha}_*.docx")) if sha and conv.is_dir() else []
            if not hits:
                continue
            path = hits[0]
        if path.is_file():
            out.append({"name": fix_path(rel), "rel": rel, "path": path})
    return out


# ---------- задача ----------

def prepare_bid(client, folder_name: str, folder: Path, progress, entity: str | None = None, model: str | None = None,
                cache_dir: Path | None = None, reapply: bool = False) -> tuple[str, dict]:
    started = time.time()
    check = jobctl.checker(progress)
    old = load_report(folder) or {}
    price = old.get("price")
    if reapply:
        if not old.get("forms") and not old.get("documents"):
            raise BidError("документы заявки ещё не готовились — нажмите «Подготовить документы заявки»")
        return _assemble(client, folder_name, folder, progress, old, price, reapply=True, started=started)

    model = model if model in BID_MODELS else BID_MODEL
    progress("Участник и его документы", 0.02)
    part = participant_mod.load(client, entity or old.get("entity"))
    docs = load_docs(client.id, folder_name, folder)
    if not docs:
        raise BidError("нет документов с извлечённым текстом — сначала загрузите и обработайте документацию")
    docs_block, used, skipped = build_documents_block(docs, CORPUS_LIMIT)
    result = load_result(folder)
    rows, pos_source = _positions(result, client, folder_name)
    usage = Usage()
    # Одинаковые задания и проверки ответа для обеих: DeepSeekJSON повторяет Claude.json_call.
    claude = DeepSeekJSON(model, usage) if model.startswith("deepseek") else Claude(model, usage, effort_for(model))
    if cache_dir:
        claude.cache = jobctl.AnswerCache(cache_dir)
    today = datetime.now(MSK).strftime("%d.%m.%Y")
    tender = _tender_text(result, None)
    ps = for_client(client.id, part.id)           # промты клиента и участника (0.18.0, юр. лицо — 0.19.0)
    system = ps.text("bid.system")
    pool = ThreadPoolExecutor(max_workers=PARALLEL)
    try:
        # 1. Требования к участнику и состав заявки.
        progress("Требования к участнику и состав заявки (модель читает документацию)", 0.06)
        # Два вызова параллельно (в одном ответе всё не помещалось): требования и порядок подачи; состав заявки.
        args = (today, part.profile, part.library_text(), tender)
        f_docs = pool.submit(claude.json_call, system=system, documents=docs_block,
                             prompt=prompts.docs_prompt(*args, ps=ps), schema=prompts.DOCS_SCHEMA, label="Состав заявки",
                             max_tokens=64000)
        f_req = pool.submit(claude.json_call, system=system, documents=docs_block,
                            prompt=prompts.req_prompt(*args, ps=ps), schema=prompts.REQ_SCHEMA,
                            label="Требования к участнику и порядок подачи", max_tokens=32000)
        plan = {**jobctl.result(f_req, check), **jobctl.result(f_docs, check)}
        quotes = QuoteIndex(used)
        for item in (plan.get("requirements") or []) + (plan.get("documents") or []):
            item["quote_status"], _ = quotes.find(item.get("quote") or "", item.get("file") or "")
        st = plan.get("structure") or {}
        st["quote_status"], _ = quotes.find(st.get("quote") or "", st.get("file") or "")

        # 2. Где формы в файлах Word.
        progress("Поиск форм в документации", 0.3)
        sources = _word_sources(client.id, folder_name, folder)
        need = [d for d in plan.get("documents") or [] if d.get("kind") in FILLED_KINDS
                and d.get("required") == "yes"]
        # Формы документации сайт заполняет всегда, даже если модель отнесла их к участнику (цена, суды, решения):
        # что известно — вписано, остальное — «<нет данных>» и строка в «Нужно от вас».
        located: list[dict] = []
        if need and sources:
            outlines, counts = [], {}
            for s in sources:
                try:
                    ol = forms.outline(s["path"])
                    counts[s["name"]] = forms.count_elements(s["path"])
                except Exception as exc:
                    s["error"] = str(exc)
                    continue
                lines = [f"[{o['i']}] {o['text']}" for o in ol]
                outlines.append(f'<file name="{s["name"]}" элементов: {counts[s["name"]]}>\n'
                                + ("\n".join(lines) or "(заголовков нет)") + "\n</file>")
            listing = "\n".join(f"{d['n']}. {d['title']}" + (f" ({d['form']})" if d.get("form") else "")
                                + f" — {d['kind']}" for d in need)
            ans = jobctl.result(pool.submit(
                claude.json_call, system=system, documents=None,
                prompt=prompts.forms_prompt(listing, "\n\n".join(outlines), ps), schema=prompts.FORMS_SCHEMA,
                label="Где формы", max_tokens=8000), check)
            by_name = {s["name"]: s for s in sources}
            for f in ans.get("forms") or []:
                src = by_name.get(f.get("source")) or next(
                    (s for n, s in by_name.items() if f.get("source") and (n.endswith(f["source"]) or f["source"] in n)), None)
                if not src:
                    continue
                total = counts.get(src["name"], 0)
                a, b = int(f.get("start") or 0), int(f.get("end") or 0)
                if b < a or b >= total:
                    b = total - 1
                if not (0 <= a < total):
                    continue
                located.append({"doc_n": str(f.get("doc_n") or ""), "title": f.get("title") or "",
                                "kind": f.get("kind") or "form", "source": src["name"], "rel": src["rel"],
                                "path": str(src["path"].relative_to(folder)) if str(src["path"]).startswith(str(folder))
                                else str(src["path"]), "start": a, "end": b})
        # Одна и та же форма для двух документов (оферта и «откорректированная оферта») — заполняется один раз.
        uniq: dict[tuple, dict] = {}
        for f in located:
            key = (f["source"], f["start"])
            if key in uniq:
                uniq[key].setdefault("also", []).append(f["doc_n"])
            else:
                uniq[key] = f
        located = list(uniq.values())[:MAX_FORMS]

        # 3. Заполнение форм (опись — последней, со списком файлов пакета).
        doc_by_n = {str(d.get("n")): d for d in plan.get("documents") or []}
        part_text = part.profile[:60000]
        tokens = ", ".join("{{" + k + "}}" for k in forms.TOKENS)

        def fill(form: dict, extra: str = "") -> dict:
            doc = forms.extract(folder / form["path"], form["start"], form["end"])
            d = doc_by_n.get(form["doc_n"]) or {}
            kind = form["kind"]
            pos = _positions_text(rows, with_tz=(kind == "tech")) if kind in ("tech", "price") else ""
            ans = claude.json_call(
                system=system, documents=None,
                prompt=prompts.fill_prompt(d.get("title") or form["title"], kind, forms.markup(doc), part_text,
                                           tender, pos, tokens, extra, ps),
                schema=prompts.FILL_SCHEMA, label=f"Форма: {(d.get('title') or form['title'])[:60]}",
                max_tokens=32000)
            return {"edits": ans.get("edits") or [], "note": ans.get("note") or ""}

        main_forms = [f for f in located if f["kind"] != "inventory"]
        inv_forms = [f for f in located if f["kind"] == "inventory"]
        futs = [(f, pool.submit(fill, f)) for f in main_forms]
        for k, (f, fut) in enumerate(futs, 1):
            progress(f"Заполнение форм: {k} из {len(located)}", 0.35 + 0.5 * k / max(len(located), 1))
            try:
                f.update(jobctl.result(fut, check))
            except jobctl.JobStopped:
                raise
            except Exception as exc:
                f["error"] = str(exc)[:300]
        report = {
            "entity": part.id, "model": model, "plan": plan, "forms": main_forms, "inventory_forms": inv_forms,
            "positions_source": pos_source, "skipped_docs": [d.base for d in skipped], "price": price,
            "started": started,
        }
        # опись: модель получает список будущих файлов пакета
        if inv_forms:
            progress("Опись документов", 0.88)
            names = _planned_names(plan, main_forms)
            for f in inv_forms:
                try:
                    f.update(jobctl.result(pool.submit(fill, f, "Файлы пакета заявки по порядку (для описи):\n"
                                                       + "\n".join(names)), check))
                except jobctl.JobStopped:
                    raise
                except Exception as exc:
                    f["error"] = str(exc)[:300]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    report["usage"] = {"totals": usage.totals(), "usd": usage.total_usd(), "calls": usage.calls}
    report["usd"] = round(usage.total_usd(), 4)
    return _assemble(client, folder_name, folder, progress, report, price, reapply=False, started=started,
                     participant=part, rows=rows, result=result)


def _planned_names(plan: dict, located: list[dict]) -> list[str]:
    have = {f["doc_n"] for f in located}
    out = []
    for d in plan.get("documents") or []:
        if d.get("required") == "not_needed":
            continue
        mark = "" if d.get("kind") in ("library",) or str(d.get("n")) in have else " (готовит участник)"
        out.append(f"{d.get('n')}. {d.get('title')}{mark}")
    return out


def _assemble(client, folder_name: str, folder: Path, progress, report: dict, price: dict | None, reapply: bool,
              started: float, participant=None, rows=None, result=None) -> tuple[str, dict]:
    """Собрать папку пакета по отчёту: копии из библиотеки, формы по правкам, проверки, перечень, отчёт."""
    progress("Сборка пакета заявки", 0.9)
    part = participant or participant_mod.load(client, report.get("entity"))
    if result is None:
        result = load_result(folder)
    if rows is None:
        rows, _ = _positions(result, client, folder_name)
    plan = report.get("plan") or {}
    structure = plan.get("structure") or {}
    basis = structure.get("price_basis") or "не указано"
    nmc_total = _num(((result or {}).get("flat") or {}).get("start_price"))
    if basis == "не указано":
        basis = "без НДС"
    values, price_problems = price_values(price, rows, basis, part.vat_rate, nmc_total)
    if part.passport:
        values["ПАСПОРТ_РУКОВОДИТЕЛЯ"] = part.passport

    backup = _backup(folder)
    out = folder / BID_DIR
    out.mkdir(parents=True, exist_ok=True)
    gaps: list[tuple[str, str]] = []
    checks: list[str] = []
    errors: list[str] = []
    files_by_doc: dict[str, list[dict]] = {}
    lib_by_no = {it["no"]: it for it in part.library if it.get("no")}
    used_names: set[str] = set()

    def unique(dst_dir: Path, name: str, d: dict) -> str:
        """Два документа с одним именем файла (документация назвала оба «Техническое предложение») — к имени
        второго добавляется его название / номер."""
        key = str(dst_dir / name).lower()
        if key not in used_names:
            used_names.add(key)
            return name
        for cand in (safe_name(f"{name} — {d.get('title') or ''}", 120), f"{name} ({d.get('n') or ''})"):
            if str(dst_dir / cand).lower() not in used_names:
                used_names.add(str(dst_dir / cand).lower())
                return cand
        k = 2
        while str(dst_dir / f"{name} {k}").lower() in used_names:
            k += 1
        used_names.add(str(dst_dir / f"{name} {k}").lower())
        return f"{name} {k}"

    # 1. Документы из библиотеки участника («301» или несколько: «304, 306», «501–504»).
    for d in plan.get("documents") or []:
        if d.get("required") == "not_needed" or d.get("kind") != "library" or d.get("library_status") != "ok":
            continue
        items = [lib_by_no[no] for no in library_numbers(d.get("library")) if no in lib_by_no]
        items = [it for it in items if Path(it["path"]).suffix.lower() not in SKIP_LIBRARY_EXT]
        base = re.sub(r"\.(pdf|docx?|xlsx?|tiff?|jpe?g|png)$", "", doc_file_name(d), flags=re.I)
        dst_dir = out / _part_dir(structure, d.get("part") or "single")
        for it in items:
            short = re.sub(r"^\d{3}\s+", "", Path(it["file"]).stem)
            name = base if len(items) == 1 else safe_name(f"{base} — {short}")
            name = unique(dst_dir, name, d)
            try:
                dst = _copy_library(Path(it["path"]), dst_dir, name)
                files_by_doc.setdefault(str(d.get("n")), []).append(
                    {"file": str(dst.relative_to(folder)), "marks": 0, "from_library": it["file"]})
            except Exception as exc:
                errors.append(f"копия «{it['file']}»: {exc}")

    # 2. Формы по правкам модели.
    all_forms = (report.get("forms") or []) + (report.get("inventory_forms") or [])
    doc_by_n = {str(d.get("n")): d for d in plan.get("documents") or []}
    for f in all_forms:
        if f.get("error") and not f.get("edits"):
            continue
        d = doc_by_n.get(f["doc_n"]) or {}
        try:
            doc = forms.extract(folder / f["path"], f["start"], f["end"])
            applied, problems, form_gaps = forms.apply(doc, f.get("edits") or [], values)
        except Exception as exc:
            f["error"] = f"форма не собралась: {exc}"[:300]
            continue
        name = doc_file_name(d or {"title": f["title"]})
        name = re.sub(r"\.(docx?|pdf)$", "", name, flags=re.I)
        dst_dir = out / _part_dir(structure, d.get("part") or "single")
        dst_dir.mkdir(parents=True, exist_ok=True)
        name = unique(dst_dir, name, d or {"title": f["title"], "n": f["doc_n"]})
        tmp = dst_dir / (name + ".docx")
        doc.save(str(tmp))
        marks = forms.count_marks(tmp)
        final = dst_dir / ((DRAFT if marks else "") + name + ".docx")
        if final != tmp:
            os.replace(tmp, final)
        f.update(applied=applied, problems=problems, gaps=form_gaps, marks=marks, file=str(final.relative_to(folder)))
        for g in form_gaps:
            gaps.append((final.name, g))
        for n in [f["doc_n"]] + list(f.get("also") or []):
            files_by_doc.setdefault(n, []).append({"file": f["file"], "marks": marks, "form": True})

    # 3. Самопроверка.
    docx_files = sorted(p for p in out.rglob("*.docx") if not p.name.startswith("~$"))
    texts = {}
    for p in docx_files:
        try:
            texts[p] = "\n".join(forms.doc_texts(p))
        except Exception as exc:
            errors.append(f"{p.name}: не открылся ({exc})")
    for p, t in texts.items():
        for bad in part.wrong_values:
            if bad and bad in t:
                checks.append(f"⚠ {p.name}: «{bad}» — известная ошибка реквизитов участника, исправьте")
        for m in re.finditer(r"ИНН[\s:№]*(\d{12}|\d{10})\b", t):
            inn = m.group(1)
            if inn not in (part.inn, (result or {}).get("flat", {}).get("inn")) and inn not in part.profile:
                checks.append(f"⚠ {p.name}: ИНН {inn} — нет ни в сведениях участника, ни у заказчика")
        if "{{" in t or "[[" in t:
            checks.append(f"⚠ {p.name}: осталась незаполненная метка «{{{{…}}}}» или «[[…]]»")
    if structure.get("parts") in ("two_parts", "three_parts") and structure.get("part1_anonymous"):
        req = part.requisites or {}
        keys = [k for k in [part.inn, req.get("short_name"), req.get("full_name"), req.get("ogrn")] if k and len(k) > 4]
        for p, t in texts.items():
            if "1 Первая часть" in str(p):
                hits = [k for k in keys if k in t]
                if hits:
                    checks.append(f"⚠ {p.name}: в первой части сведения об участнике ({', '.join(hits)}) — "
                                  "документация это запрещает")
    checks += [f"⚠ {x}" for x in price_problems]
    for f in all_forms:
        for pr in (f.get("problems") or [])[:5]:
            checks.append(f"{Path(f.get('file') or f['title']).name}: правка не внесена — {pr}")

    # 4. Состояние документов.
    docs_out = []
    for d in plan.get("documents") or []:
        n = str(d.get("n"))
        files = files_by_doc.get(n, [])
        if d.get("required") == "not_needed":
            status, todo = "not_needed", ""
        elif files and all(x["marks"] == 0 for x in files):
            status, todo = ("copied" if any(x.get("from_library") for x in files) else "ready"), ""
        elif files:
            status, todo = "draft", f"пометок «<нет данных>»: {sum(x['marks'] for x in files)}"
        elif d.get("kind") == "library":
            status = {"update": "update", "urgent": "urgent", "missing": "urgent"}.get(d.get("library_status"), "urgent")
            todo = "обновить" if status == "update" else "запросить"
        elif d.get("required") == "conditional":
            status, todo = "conditional", ("нужен, если: " + d["condition"]) if d.get("condition") else "нужен при условии"
        elif d.get("kind") in FILLED_KINDS and re.search(r"\.xlsx?\b|excel|xlsx", f"{d.get('form')} {d.get('title')}", re.I):
            status, todo = "manual", "заполнить файл Excel заказчика (цены — по цене заявки)"
        elif d.get("kind") in FILLED_KINDS:
            status, todo = "manual", "форма в документации не найдена — подготовить вручную"
        elif d.get("kind") == "third_party" or d.get("who") == "third_party":
            status, todo = "third_party", "запросить у производителя / поставщика"
        elif d.get("who") == "accountant":
            status, todo = "accountant", "подготовит бухгалтер"
        else:
            status, todo = "user", "готовит участник"
        docs_out.append({**d, "files": files, "status": status, "todo": todo})
    required = [d for d in docs_out if d["status"] != "not_needed" and d.get("required") != "conditional"]
    ready = [d for d in required if d["status"] in ("ready", "copied")]
    drafts = [d for d in docs_out if d["status"] == "draft"]
    urgent = [d["title"] for d in docs_out if d["status"] == "urgent"]

    # 5. Нет данных.txt, перечень, файлы поставщикам.
    if gaps:
        (out / GAPS_FILE).write_text("".join(f"{f} | {g}\n" for f, g in gaps), encoding="utf-8")
    summary = {"ready": len(ready), "total": len(required), "drafts": len(drafts), "urgent": urgent,
               "files": len(list(p for p in out.rglob("*") if p.is_file()))}
    report.update(documents=docs_out, structure=structure, checks=checks, errors=errors, gaps=[
        {"file": f, "need": g} for f, g in gaps], summary=summary, participant={
        "id": part.id, "name": part.name, "inn": part.inn, "problems": part.problems, "vat_rate": part.vat_rate},
        price=price, price_values={k: v for k, v in values.items() if ":" not in k and k != "ПАСПОРТ_РУКОВОДИТЕЛЯ"},
        backup=backup, at=time.time(), at_text=datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
        seconds=round(time.time() - started), reapplied=reapply)
    if not reapply:
        report["supplier_files"] = _supplier_files(folder, rows, (result or {}).get("internal_number") or "")
    (out / LIST_FILE).write_text(docs_list_text(report), encoding="utf-8")
    report.pop("started", None)
    save_report(folder, report)
    text = (f"Документы заявки: готово {summary['ready']} из {summary['total']}, черновиков {summary['drafts']}"
            + (f", запросить: {len(urgent)}" if urgent else "") + f"; файлов в пакете {summary['files']}"
            + (f"; ≈ ${report['usd']:.2f}" if report.get("usd") and not reapply else "")
            + ("; цены пересчитаны" if reapply else ""))
    return text, report


def _supplier_files(folder: Path, rows: list[dict], ref: str) -> dict:
    """Файлы поставщикам (Шаг 7.1b): по производителям. Папку агента «Поставщикам» не трогаем — тогда «Поставщикам
    (сайт)»."""
    from tools import make_supplier_files as msf
    groups: dict[str, list] = {}
    for r in rows:
        brand = msf.brand_of(r.get("Производитель"))
        if brand and str(r.get("Наименование позиции") or "").strip():
            groups.setdefault(brand, []).append(r)
    # «HYDAC International» и «HYDAC» — один производитель: одно письмо (по короткому названию).
    for brand in sorted(groups, key=len, reverse=True):
        base_brand = next((b for b in groups if b != brand and brand.lower().startswith(b.lower() + " ")), None)
        if base_brand:
            groups[base_brand].extend(groups.pop(brand))
    if not groups:
        return {"files": [], "note": "производители позиций не указаны — файлы поставщикам не созданы"}
    base = folder / OUT_DIR / "Поставщикам"
    if base.exists() and not (base / ".supplybro").exists():
        base = folder / OUT_DIR / "Поставщикам (сайт)"
    base.mkdir(parents=True, exist_ok=True)
    (base / ".supplybro").write_text("создано сайтом SupplyBro", encoding="utf-8")
    files = []
    for brand, items in groups.items():
        path = base / (msf.safe_filename(brand) + ".docx")
        msf.build_doc(brand, items, ref, str(path))
        files.append(str(path.relative_to(folder)))
    skipped = [str(r.get("№")) for r in rows if not msf.brand_of(r.get("Производитель"))]
    return {"files": files, "skipped": skipped}


STATUS_MARK = {"ready": "✅ готово", "copied": "🟢 есть", "draft": "ЧЕРНОВИК", "update": "⚠ обновить",
               "urgent": "🚩 запросить срочно", "manual": "⚠ не готово", "third_party": "🚩 запросить у третьих лиц",
               "accountant": "🚩 бухгалтер", "user": "⚠ готовит участник", "not_needed": "не требуется",
               "conditional": "при условии"}


def docs_list_text(report: dict) -> str:
    """«Перечень документов заявки» — как у агента (Шаг 7.1d / PREPARE_DOCS.md, раздел NocoDB)."""
    p = report.get("participant") or {}
    st = report.get("structure") or {}
    sm = report.get("summary") or {}
    vat = p.get("vat_rate")
    lines = [f"Участник: {p.get('name', '')} (ИНН {p.get('inn', '')})"
             + (f", НДС {vat:g} %" if isinstance(vat, (int, float)) and vat else "") + "."]
    lines.append(f"Готовность заявки: {sm.get('ready', 0)} из {sm.get('total', 0)} готовы; черновиков "
                 f"{sm.get('drafts', 0)}" + (f"; 🚩 запросить срочно: {', '.join(sm.get('urgent') or [])}"
                                            if sm.get("urgent") else "")
                 + (f"; срок подачи {st['deadline']}" if st.get("deadline") else "") + ".")
    basis = [x for x in (st.get("submission"), f"обеспечение заявки — {st['bid_security']}" if st.get("bid_security")
                         else "", st.get("naming")) if x]
    if basis:
        lines.append("Порядок подачи: " + "; ".join(basis) + ".")
    current = None
    names = {"single": "", "1": "Первая часть", "2": "Вторая часть", "price": "Ценовое предложение",
             "after_win": "Представляет победитель"}
    for d in report.get("documents") or []:
        part = names.get(d.get("part") or "single", "")
        if part != current and part:
            lines.append(f"\n{part}:")
        current = part
        files = ", ".join(Path(f["file"]).name for f in d.get("files") or [])
        tail = files or d.get("todo") or ""
        cond = f" ({d['condition']})" if d.get("required") == "conditional" and d.get("condition") else ""
        lines.append(f"{d.get('n')}. {d.get('title')}{' (' + d['form'] + ')' if d.get('form') else ''}{cond} — "
                     f"{STATUS_MARK.get(d.get('status'), '')}" + (f": {tail}" if tail else ""))
    for n in (report.get("plan") or {}).get("notes") or []:
        lines.append(f"Замечание: {n}")
    return "\n".join(lines).strip() + "\n"
