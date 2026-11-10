"""
Изменения и разъяснения к уже разобранному конкурсу (0.9.0) — задачи «changes» и «noco_changes» обработчика.

Пользователь 04.11.2026: «если тендер уже разобран, но к нему появились обновления или изменения — нет функции
дозагрузить документы»; как разбирать — «прочитать дозагруженные документы и понять, в каких местах были внесены
изменения, обновить данные, вывести комментарии». Остальное — рекомендованные варианты (пользователь не выбрал):
запись в NocoDB — по кнопке и только изменённого; цены — только по новым и изменённым позициям; документы — в папку
«Изменения ДД.ММ.ГГГГ».

Порядок:
  1. Новые документы — те, которых не было при прошлом анализе / разборе изменений (или файл изменился):
     `new_docs` по `result["doc_index"]` (у анализов до 0.9.0 — по списку `result["documents"]`).
  2. Модель читает ТОЛЬКО новые документы + текущий анализ (поля и позиции кратко) и отвечает по схеме: что это за
     документы, какие поля и позиции изменены (новое значение + дословная цитата), комментарии (разъяснения без
     изменения данных), нужен ли полный повторный анализ.
  3. Код: значение сверяется с текущим (то же самое — отбрасывается), цитата ищется в новых документах; не найдена —
     изменение показывается с ⚠ и не применяется. Позиции: изменённые и исключённые — только существующие №,
     добавленные — только новые №.
  4. Применение: копия прежнего анализа — AIExtract/history/; поля и позиции обновляются (у позиции — строка
     «🔄 Изменение …» в «Примечании», ТЗ дополняется, исключённая убирается из анализа), «Наименование продукции» и
     «Количество» пересобираются, xlsx и XML — заново; журнал — AIExtract/changes.json.
  5. Дальше (app/worker.py): поиск цен — только по новым позициям и позициям с другим названием / маркой (если цены
     уже искали); NocoDB — по кнопке «Записать изменения в NocoDB» (задача noco_changes, `write_noco`): только
     изменённые поля реестра; строки спецификации — изменить / добавить / пометить исключённые (не удаляются). И в
     записях, созданных агентом, — кнопка пользователя и есть подтверждение.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .. import db, jobctl
from ..docs.names import fix_path
from ..prompt_store import Prompts, for_client, or_default
from . import prompts
from .corpus import TEXT_STATUSES, QuoteIndex, build_documents_block, corpus_hash, load_docs, norm
from .fields import COMPUTED, DISPLAY_ORDER, FIELD_BY_KEY, NOT_SPECIFIED, label_of
from .llm import Claude, Usage
from .pricing import money
from .run import (ANALYSIS_MODELS, CLAUDE_MODEL, MSK, OUT_DIR, _clean_n, _compose_product, _compose_quantity,
                  _money_values, _n_int, _num, _save_files, effort_for, load_result, save_result)

MODEL = os.environ.get("CHANGES_MODEL", "").strip() or CLAUDE_MODEL
MAX_CHARS = int(os.environ.get("CHANGES_MAX_CHARS", "400000"))
LOG_FILE = OUT_DIR / "changes.json"
HISTORY_DIR = OUT_DIR / "history"
FOLDER_PREFIX = "Изменения"          # папка для дозагруженных документов: «Изменения 04.11.2026»
UPLOAD_TARGET = "@changes"           # так страница просит положить файлы в эту папку

# Графы позиций, которые может изменить документ (имена — как в analysis.json и в спецификации NocoDB).
POS_COLUMNS = ["Наименование позиции", "Кол-во", "Ед. изм.", "Марка/модель", "Аналог допускается",
               "Аналог (статус)", "Стоимость НМЦ", "НМЦК за позицию", "Техническое задание"]
PRICE_COLUMNS = {"Наименование позиции", "Марка/модель"}       # от них зависит поиск цен
FIELD_KEYS = ["product_title"] + [k for k in DISPLAY_ORDER if k not in COMPUTED]
QUOTE_OK = ("ok", "other_file", "partial")
TITLE_LABEL = "Наименование продукции (общее)"


def folder_name_for(date_text: str) -> str:
    return f"{FOLDER_PREFIX} {date_text}"


# ---------- журнал изменений (AIExtract/changes.json) ----------

@contextlib.contextmanager
def _locked(folder: Path):
    """Журнал меняют сайт (кнопка «Записать») и обработчик: блокировка — в папке данных (общая для контейнеров)."""
    lock_dir = db.DATA_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(str(folder).encode("utf-8")).hexdigest()[:16]
    with open(lock_dir / f"changes-{name}.lock", "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_log(folder: Path) -> dict:
    path = folder / LOG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    data.setdefault("sets", [])
    return data


def _save_log(folder: Path, data: dict) -> None:
    path = folder / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update_log(folder: Path, fn):
    with _locked(folder):
        data = load_log(folder)
        out = fn(data)
        _save_log(folder, data)
        return out


def _set(data: dict, change_id: str) -> dict | None:
    return next((s for s in data["sets"] if s.get("id") == change_id), None)


# ---------- новые документы ----------

def doc_index(docs) -> dict[str, str]:
    return {d.rel_path: d.sha256 for d in docs}


def _is_new(result: dict, rel_path: str, name: str, sha: str) -> bool:
    idx = result.get("doc_index")
    if isinstance(idx, dict):
        return idx.get(rel_path) != sha
    return name not in {str(x.get("name")) for x in result.get("documents") or []}


def new_docs(result: dict, docs) -> list:
    return [d for d in docs if _is_new(result, d.rel_path, d.name, d.sha256)]


def new_doc_names(client_id: str, folder_name: str, result: dict | None) -> list[str]:
    """Документы с текстом, появившиеся после анализа (для страницы — без чтения текстов, по базе)."""
    if not result or not result.get("fields"):
        return []
    out = []
    for rel, row in db.doc_rows(client_id, folder_name).items():
        if row.get("status") not in TEXT_STATUSES or not row.get("text_path"):
            continue
        name = fix_path(rel)
        if _is_new(result, rel, name, row.get("sha256") or ""):
            out.append(name)
    return sorted(out)


# ---------- задание модели ----------

def _plain(value) -> str:
    """Значение без отметок перепроверки (✅ / 🔺 и «(расхождение: …)»)."""
    t = re.sub(r"^(✅|🔺)\s*", "", str(value if value is not None else "").strip())
    t = re.sub(r"\s*\(расхождение: по версии DeepSeek.*$", "", t, flags=re.S)
    return t.strip()


def _cell(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _plain(value)


def _same(a, b) -> bool:
    return norm(_cell(a)) == norm(_cell(b))


def _short(text, n: int = 300) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def _title(result: dict) -> str:
    pn = _plain((result.get("flat") or {}).get("product_name"))
    return pn.split("Список позиций:")[0].strip() if "Список позиций:" in pn else pn


# Стандартный текст промта «changes.task» (0.18.0: у клиента может быть изменён в админ-панели, app/prompt_store.py).
# Метки {fields}, {positions}, {rules} заполняет build_prompt; остальные фигурные скобки — просто текст.
CHANGES_RULES = """Конкурс уже разобран — текущие данные ниже. Заказчик опубликовал новые документы, они в <documents>: изменения \
извещения или документации, новые редакции, разъяснения, протоколы. Задание: прочитай ТОЛЬКО эти новые документы и \
найди, что в них изменено по сравнению с текущими данными.

Текущие поля конкурса (ключ — название: значение):
{fields}

Текущие позиции (№ | наименование | кол-во и ед. | марка/модель | аналог допускается | цена за ед. с НДС | НМЦК за позицию):
{positions}

Правила полей — новое значение пиши по ним, в том же виде, что текущие данные:
{rules}

Что вернуть:
1. documents — по каждому новому документу: file (имя из атрибута file), kind («изменение», «разъяснение», \
«новая редакция», «протокол», «другое»), date — дата документа ДД.ММ.ГГГГ (или пусто), summary — одно предложение, \
о чём документ.
2. summary — 1–3 предложения для тендерного специалиста: что главное изменилось (или «Изменений в данных нет: …»).
3. fields — только поля, значение которых по новым документам ДРУГОЕ, чем в текущих данных (перенесён срок подачи, \
новая НМЦ, другое место или срок поставки, новые условия оплаты…). key — ключ поля из списка; new_value — новое \
значение целиком по правилу поля; quote — дословная цитата из нового документа (подряд, 5–40 слов), file — документ; \
comment — коротко, что и где изменено («п. 12 извещения: срок подачи перенесён»). Если документ повторяет прежнее \
значение — поле не включай. Дата изменения документации — поле last_modified. Если из-за новых сроков другая стадия \
процедуры — поле stage.
4. positions — изменения перечня позиций: action «changed» (у существующей позиции другое количество, единица, \
наименование, марка, цена, допуск аналога, требования ТЗ), «added» (новая позиция), «removed» (позиция исключена). \
n — № позиции: для changed и removed — из текущего перечня, для added — № в новой редакции. changes — список \
{column, new_value} только изменённых граф: «Наименование позиции», «Кол-во» (число), «Ед. изм.», «Марка/модель», \
«Аналог допускается», «Аналог (статус)» (Да / Нет / Уточни), «Стоимость НМЦ» (за единицу с НДС, число с точкой и \
2 знаками), «НМЦК за позицию» (за позицию с НДС, так же), «Техническое задание» (только новый или изменённый текст \
требований — он будет дописан к прежнему ТЗ). Для added — все известные графы, обязательно наименование, кол-во и \
ед. изм. quote, file, comment — как у полей.
5. comments — важное из новых документов, что не меняет поля и позиции: ответы на вопросы участников, уточнения \
требований, предупреждения. text — суть одним-двумя предложениями; quote и file — откуда.
6. full_reanalysis — needed=true, если новые документы — полностью новая редакция документации или перечень позиций \
переделан так, что точечно изменения не описать (перенумерация, замена большей части позиций); reason — почему.

Ничего не выдумывай: только то, что прямо написано в новых документах. Нет изменений — пустые fields и positions."""

SCHEMA = prompts._obj({
    "documents": {"type": "array", "items": prompts._obj({"file": prompts.STR, "kind": prompts.STR,
                                                          "date": prompts.STR, "summary": prompts.STR})},
    "summary": prompts.STR,
    "fields": {"type": "array", "items": prompts._obj({
        "key": {"type": "string", "enum": FIELD_KEYS}, "new_value": prompts.STR, "quote": prompts.STR,
        "file": prompts.STR, "comment": prompts.STR})},
    "positions": {"type": "array", "items": prompts._obj({
        "action": {"type": "string", "enum": ["changed", "added", "removed"]}, "n": prompts.STR,
        "changes": {"type": "array", "items": prompts._obj({"column": {"type": "string", "enum": POS_COLUMNS},
                                                            "new_value": prompts.STR})},
        "quote": prompts.STR, "file": prompts.STR, "comment": prompts.STR})},
    "comments": {"type": "array", "items": prompts._obj({"text": prompts.STR, "quote": prompts.STR,
                                                         "file": prompts.STR})},
    "full_reanalysis": prompts._obj({"needed": {"type": "boolean"}, "reason": prompts.STR}),
})


def build_prompt(result: dict, today: str, ps: Prompts | None = None) -> str:
    ps = or_default(ps)
    by_key = {f["key"]: f for f in result.get("fields") or []}
    lines = [f"- product_title — «{TITLE_LABEL}»: {_short(_title(result), 300)}"]
    for key in DISPLAY_ORDER:
        if key in COMPUTED:
            continue
        lines.append(f"- {key} — «{label_of(key)[1]}»: {_short(_plain((by_key.get(key) or {}).get('value')), 400)}")
    pos = []
    for r in result.get("positions") or []:
        pos.append(" | ".join([str(r.get("№") or ""), _short(r.get("Наименование позиции"), 150),
                               f"{_cell(r.get('Кол-во'))} {r.get('Ед. изм.') or ''}".strip(),
                               _short(r.get("Марка/модель"), 80), _short(r.get("Аналог допускается"), 60),
                               _cell(r.get("Стоимость НМЦ")), _cell(r.get("НМЦК за позицию"))]))
    rules = [f"- {k}: {prompts.field_rule(k, ps)}" for k in FIELD_KEYS if k in FIELD_BY_KEY]
    return f"Сегодня: {today}.\n\n" + ps.render("changes.task", fields="\n".join(lines),
                                                  positions="\n".join(pos) or "(позиций нет)", rules="\n".join(rules))


# ---------- проверки и применение ----------

def _add_note(note, line: str) -> str:
    note = str(note or "").strip()
    if line in note:
        return note
    return f"{note}; {line}" if note else line


def _sort_key(row: dict):
    n = _n_int(row.get("№"))
    return (n if n is not None else 10 ** 9, str(row.get("№") or ""))


def _analog_status(text: str) -> str | None:
    t = str(text or "").strip().lower()
    if t.startswith("да"):
        return "Да"
    if t.startswith("нет"):
        return "Нет"
    return None


def _new_row(n: str, cols: dict, note: str) -> dict:
    qty = _num(cols.get("Кол-во"))
    analog = cols.get("Аналог допускается") or NOT_SPECIFIED
    return {
        "№": n,
        "Наименование позиции": cols.get("Наименование позиции", ""),
        "Техническое задание": cols.get("Техническое задание", ""),
        "Ошибки в документации": "",
        "Марка/модель": cols.get("Марка/модель", ""),
        "Производитель": "",
        "Страна происхождения": "",
        "Аналог допускается": analog,
        "Аналог (статус)": cols.get("Аналог (статус)") or _analog_status(analog) or "Уточни",
        "Кол-во": qty if qty is not None else cols.get("Кол-во", ""),
        "Ед. изм.": cols.get("Ед. изм.", ""),
        "НМЦК за позицию": cols.get("НМЦК за позицию") or NOT_SPECIFIED,
        "Стоимость НМЦ": cols.get("Стоимость НМЦ", ""),
        "Примечание": note,
    }


def _sum_check(rows: list[dict], start_price: str) -> str | None:
    """Сумма НМЦК по позициям к начальной цене — после изменений (как в анализе)."""
    from tools.nocodb_client import parse_ru_number
    sums = [parse_ru_number(re.sub(r"\(.*?\)", "", str(r.get("НМЦК за позицию") or ""))) for r in rows]
    if not rows or not all(s is not None for s in sums):
        return None
    total = round(sum(sums), 2)
    prices = _money_values(_plain(start_price))
    if not prices:
        return None
    fmt = f"{total:,.2f}".replace(",", " ").replace(".", ",")
    if any(p and abs(total - p) / p <= 0.005 for p in prices):
        return f"✅ После изменений сумма НМЦК по позициям {fmt} совпадает с начальной ценой."
    return f"⚠ После изменений сумма НМЦК по позициям {fmt} не совпадает с начальной ценой ({_plain(start_price)})."


def _src(item: dict) -> str:
    return (item.get("file") or "").split("/")[-1] or "новые документы"


def _backup(folder: Path, result: dict, stamp: str) -> str:
    d = folder / HISTORY_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = f"analysis_{stamp}.json"
    (d / name).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return (HISTORY_DIR / name).as_posix()


def process(client_id: str, folder_name: str, folder: Path, progress, by: str = "",
            cache_dir: Path | None = None, entity: str | None = None) -> tuple[str, dict | None]:
    """Задача «changes»: разобрать документы, появившиеся после анализа, и применить изменения.
    Возвращает (итог одной строкой, запись журнала или None, если новых документов нет)."""
    result = load_result(folder)
    if not result or not result.get("fields"):
        raise RuntimeError("анализа этого конкурса ещё нет — запустите анализ (новые документы войдут в него)")
    docs = load_docs(client_id, folder_name, folder)
    fresh = new_docs(result, docs)
    if not fresh:
        return "Новых документов после анализа нет — сравнивать нечего", None
    block, used, skipped = build_documents_block(fresh, MAX_CHARS)
    model = MODEL if (MODEL in ANALYSIS_MODELS or MODEL.startswith("claude")) else CLAUDE_MODEL
    usage = Usage()
    claude = Claude(model, usage, effort_for(model))
    if cache_dir:
        claude.cache = jobctl.AnswerCache(cache_dir)
    ps = for_client(client_id, entity)            # промты клиента и юр. лица (0.18.0 / 0.19.0)
    check = jobctl.checker(progress)
    now_dt = datetime.now(MSK)
    today = now_dt.strftime("%d.%m.%Y")
    progress(f"Новые документы ({len(used)}): что изменилось", 0.1)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        ans = jobctl.result(pool.submit(lambda: claude.json_call(
            system=prompts.system(ps), documents=block, prompt=build_prompt(result, today, ps), schema=SCHEMA,
            label="Изменения: что изменилось", max_tokens=32000)), check)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    progress("Проверка цитат и применение изменений", 0.8)
    qi = QuoteIndex(used)
    now = time.time()
    at_text = now_dt.strftime("%d.%m.%Y %H:%M")
    cid = now_dt.strftime("%Y%m%d-%H%M%S")

    def quote_of(item: dict) -> tuple[str, str, str]:
        quote, file = str(item.get("quote") or "").strip(), str(item.get("file") or "").strip()
        status, doc = qi.find(quote, file)
        return quote, doc or file, status

    # Поля.
    by_key = {f["key"]: f for f in result.get("fields") or []}
    flat = result.setdefault("flat", {})
    field_changes, seen = [], set()
    for c in ans.get("fields") or []:
        key = c.get("key")
        new = str(c.get("new_value") or "").strip()
        if key not in FIELD_KEYS or key in seen or not new:
            continue
        old = _title(result) if key == "product_title" else (by_key.get(key) or {}).get("value", NOT_SPECIFIED)
        if _same(old, new):
            continue                                   # документ повторяет прежнее значение
        seen.add(key)
        quote, file, q_status = quote_of(c)
        ok = q_status in QUOTE_OK
        field_changes.append({"key": key, "label": TITLE_LABEL if key == "product_title" else label_of(key)[1],
                              "old": _plain(old), "new": new, "quote": quote, "file": file, "quote_status": q_status,
                              "comment": str(c.get("comment") or "").strip(), "applied": ok,
                              "problem": "" if ok else "цитата не найдена в новых документах — не применено"})

    # Позиции.
    rows = list(result.get("positions") or [])
    by_n = {_clean_n(r.get("№")): r for r in rows}
    pos_changes = []
    for p in ans.get("positions") or []:
        action, n = p.get("action"), _clean_n(p.get("n"))
        if action not in ("changed", "added", "removed") or not n:
            continue
        cols: dict[str, str] = {}
        for ch in p.get("changes") or []:
            col, v = ch.get("column"), str(ch.get("new_value") or "").strip()
            if col in POS_COLUMNS and v and col not in cols:
                cols[col] = v
        quote, file, q_status = quote_of(p)
        item = {"action": action, "n": n, "name": "", "changes": [], "quote": quote, "file": file,
                "quote_status": q_status, "comment": str(p.get("comment") or "").strip(), "applied": False,
                "problem": "", "note": ""}
        row = by_n.get(n)
        if action in ("changed", "removed"):
            if row is None:
                item["problem"] = f"позиции № {n} в текущем перечне нет — проверьте вручную"
                item["changes"] = [{"column": k, "old": "", "new": v} for k, v in cols.items()]
            else:
                item["name"] = row.get("Наименование позиции") or ""
                if action == "changed":
                    for col, v in cols.items():
                        old = row.get(col)
                        if col == "Кол-во":
                            nv, ov = _num(v), (old if isinstance(old, (int, float)) else _num(old))
                            if nv is not None and ov is not None and abs(nv - ov) < 1e-9:
                                continue
                        elif col != "Техническое задание" and _same(old, v):
                            continue
                        item["changes"].append({"column": col, "old": _cell(old) if col != "Техническое задание" else "",
                                                "new": v})
                    if not item["changes"]:
                        continue                       # на деле ничего не изменилось
        else:  # added
            item["name"] = cols.get("Наименование позиции", "")
            item["changes"] = [{"column": k, "old": "", "new": v} for k, v in cols.items()]
            if row is not None:
                item["problem"] = f"№ {n} уже есть в перечне — проверьте вручную"
            elif not cols.get("Наименование позиции") or not cols.get("Кол-во"):
                item["problem"] = "у новой позиции нет наименования или количества — не добавлена"
        if not item["problem"] and q_status not in QUOTE_OK:
            item["problem"] = "цитата не найдена в новых документах — не применено"
        item["applied"] = not item["problem"]
        pos_changes.append(item)

    comments = []
    for c in ans.get("comments") or []:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        quote, file, q_status = quote_of(c)
        comments.append({"text": text, "quote": quote, "file": file, "quote_status": q_status})

    applied_f = [c for c in field_changes if c["applied"]]
    applied_p = [c for c in pos_changes if c["applied"]]
    backup = _backup(folder, result, cid) if (applied_f or applied_p) else ""

    # Применение: поля.
    for c in applied_f:
        if c["key"] == "product_title":
            continue                                   # войдёт в «Наименование продукции» ниже
        f = by_key.get(c["key"])
        if f is not None:
            f.update(value=c["new"], quote=c["quote"], file=c["file"], quote_status=c["quote_status"], check=None,
                     changed={"at": now, "date": today, "old": c["old"], "change_id": cid, "comment": c["comment"]})
        flat[c["key"]] = c["new"]

    # Применение: позиции.
    need_prices, positions_changed = [], False
    for c in applied_p:
        src = _src(c)
        if c["action"] == "changed":
            row = by_n[c["n"]]
            parts = []
            for d in c["changes"]:
                col = d["column"]
                if col == "Техническое задание":
                    row[col] = (str(row.get(col) or "").rstrip() + f"\n⚠ Изменение {today} ({src}): {d['new']}").strip()
                    parts.append("ТЗ дополнено")
                    continue
                if col == "Кол-во":
                    q = _num(d["new"])
                    row[col] = q if q is not None else d["new"]
                else:
                    row[col] = d["new"]
                if col == "Аналог допускается" and not any(x["column"] == "Аналог (статус)" for x in c["changes"]):
                    row["Аналог (статус)"] = _analog_status(d["new"]) or "Уточни"
                parts.append(f"{col}: {_short(d['old'], 60) or '—'} → {_short(d['new'], 60)}")
                if col in PRICE_COLUMNS:
                    need_prices.append(c["n"])
            c["note"] = f"🔄 Изменение {today} ({src}): " + "; ".join(parts)
            row["Примечание"] = _add_note(row.get("Примечание"), c["note"])
        elif c["action"] == "removed":
            rows = [r for r in rows if _clean_n(r.get("№")) != c["n"]]
            c["note"] = f"❌ Исключена изменением {today} ({src})"
        else:
            c["note"] = f"🆕 Добавлена изменением {today} ({src})"
            row = _new_row(c["n"], {d["column"]: d["new"] for d in c["changes"]}, c["note"])
            rows.append(row)
            by_n[c["n"]] = row
            need_prices.append(c["n"])
        positions_changed = True
    rows.sort(key=_sort_key)

    # «Наименование продукции» и «Количество» собирает код из позиций (как при анализе).
    computed = []
    title_change = next((c for c in applied_f if c["key"] == "product_title"), None)
    if positions_changed or title_change:
        title = title_change["new"] if title_change else _title(result)
        main_source = (result.get("inventory") or {}).get("main_source") or ""
        for key, val in (("product_name", _compose_product(title, rows)),
                         ("quantity", _compose_quantity(rows, main_source))):
            old = flat.get(key, NOT_SPECIFIED)
            if _same(old, val):
                continue
            flat[key] = val
            f = by_key.get(key)
            if f is not None:
                f.update(value=val, check=None, changed={"at": now, "date": today, "old": _plain(old),
                                                        "change_id": cid, "comment": "собрано из позиций"})
            computed.append({"key": key, "label": label_of(key)[1], "old": _plain(old), "new": val, "quote": "",
                             "file": "", "quote_status": "computed", "comment": "собрано из позиций после изменений",
                             "applied": True, "problem": "", "computed": True})

    checks = []
    if skipped:
        checks.append("⚠ Не поместились (слишком большой объём): " + ", ".join(d.base for d in skipped))
    if positions_changed or any(c["key"] == "start_price" for c in applied_f):
        line = _sum_check(rows, flat.get("start_price", ""))
        if line:
            checks.append(line)
    full = ans.get("full_reanalysis") or {}
    if full.get("needed"):
        checks.append("⚠ Модель советует повторить анализ целиком: " + (str(full.get("reason") or "").strip() or
                                                                         "документация сильно переделана"))

    # Сохранение: анализ (новая версия), xlsx и XML, журнал.
    summary_text = str(ans.get("summary") or "").strip()
    known = {str(x.get("name")) for x in result.get("documents") or []}
    result["documents"] = list(result.get("documents") or []) + [
        {"name": d.name, "chars": len(d.text), "used": True, "added_at": now} for d in used if d.name not in known]
    result["doc_index"] = doc_index(docs)
    result["corpus_hash"] = corpus_hash(docs)
    if applied_f or applied_p:
        sources = [s for s in str(flat.get("source_file") or "").split(" + ") if s]
        flat["source_file"] = " + ".join(dict.fromkeys(sources + [d.base for d in used]))
        result["positions"] = rows
        if positions_changed:
            result["analyzed_at"] = now      # поиск цен, начатый по прежним позициям, не запишет их поверх новых
        result["outputs"] = _save_files(folder, flat, rows, result.get("outputs") or {})
    result["changes_at_text"] = at_text
    result["changes"] = (result.get("changes") or []) + [{
        "id": cid, "at_text": at_text, "files": [d.name for d in used], "summary": summary_text,
        "applied": len(applied_f) + len(applied_p),
        "comments": [_short(c["text"], 300) for c in comments[:10]]}]
    save_result(folder, result)

    entry = {
        "id": cid, "at": now, "at_text": at_text, "by": by, "model": model,
        "files": [d.name for d in used], "skipped": [d.name for d in skipped],
        "documents": [{k: str(x.get(k) or "") for k in ("file", "kind", "date", "summary")}
                      for x in ans.get("documents") or []],
        "summary": summary_text, "fields": field_changes + computed, "positions": pos_changes, "comments": comments,
        "full_reanalysis": {"needed": bool(full.get("needed")), "reason": str(full.get("reason") or "")},
        "checks": checks, "backup": backup,
        "usage": {"usd": usage.total_usd(), "totals": usage.totals()},
        "prices": {"positions": list(dict.fromkeys(need_prices)), "queued": False},
        "noco": None,
    }
    update_log(folder, lambda data: data["sets"].append(entry))
    not_applied = sum(1 for c in field_changes + pos_changes if not c["applied"])
    summary = (f"Новых документов: {len(used)}; изменено полей: {len(applied_f)}, позиций: {len(applied_p)}"
               + (f"; не применено (проверить): {not_applied}" if not_applied else "")
               + (f"; комментариев: {len(comments)}" if comments else "")
               + ("; ⚠ советует повторить анализ целиком" if full.get("needed") else "")
               + f"; стоимость ≈ {money(usage.total_usd())}")
    return summary, entry


def mark_prices_queued(folder: Path, change_id: str) -> None:
    def fn(data):
        s = _set(data, change_id)
        if s:
            s["prices"]["queued"] = True
    update_log(folder, fn)


# ---------- запись изменений в NocoDB (по кнопке) ----------

def request_write(folder: Path, change_id: str, by: str) -> None:
    def fn(data):
        s = _set(data, change_id)
        if not s:
            raise ValueError("такого разбора изменений нет")
        if not any(c.get("applied") for c in (s.get("fields") or []) + (s.get("positions") or [])):
            raise ValueError("в этом разборе нет применённых изменений — записывать нечего")
        if (s.get("noco") or {}).get("status") in ("requested", "running"):
            return
        s["noco"] = {"status": "requested", "at": time.time(), "by": by}
    update_log(folder, fn)


def write_noco(client, folder_name: str, folder: Path, progress) -> str:
    """Задача «noco_changes»: записать в NocoDB все разборы изменений, ждущие записи."""
    todo = [s["id"] for s in load_log(folder)["sets"] if (s.get("noco") or {}).get("status") == "requested"]
    if not todo:
        return "Записывать нечего"
    texts = []
    for cid in todo:
        set_noco(folder, cid, status="running")
        try:
            info = _write_one(client, folder_name, folder, cid, progress)
        except Exception as exc:
            set_noco(folder, cid, status="error", text=str(exc)[:400], finished_at=time.time())
            raise
        set_noco(folder, cid, status="saved", finished_at=time.time(), **info)
        texts.append(info["text"])
    return "; ".join(texts)


def set_noco(folder: Path, cid: str, **kw) -> None:
    def fn(data):
        s = _set(data, cid)
        if s is not None:
            s["noco"] = {**(s.get("noco") or {}), **kw}
    update_log(folder, fn)


def _registry_row(nc, flat: dict, keys: list[str], reg_cols) -> dict:
    """Поля реестра в том же виде, как их пишет запись анализа (noco_write._save)."""
    tmp = dict(flat)
    nc.apply_dash_placeholders(tmp)
    data = {k: tmp.get(k) for k in keys}
    if data.get("start_price"):
        data["start_price"] = nc.to_comma_decimal(str(data["start_price"]))
    if data.get("address"):
        data["address"] = nc.bold_address_except_index(str(data["address"]))
    if data.get("source_file"):
        data["source_file"] = nc.format_source_files(str(data["source_file"]))
    row = {}
    for key, value in data.items():
        col = nc.FIELD_MAP.get(key)
        if not col or col not in reg_cols:
            continue
        if key in nc.STRICT_URL_FIELDS and not nc._looks_like_url(value):
            continue
        row[col] = value
    return row


def _write_one(client, folder_name: str, folder: Path, cid: str, progress) -> dict:
    from tools import nocodb_client as nc
    from . import noco_write as nw
    from ..pricelists import _spec_table
    s = _set(load_log(folder), cid)
    result = load_result(folder) or {}
    internal = result.get("internal_number") or ""
    flat = result.get("flat") or {}
    fields = [c for c in s.get("fields") or [] if c.get("applied") and c["key"] != "product_title"]
    poss = [c for c in s.get("positions") or [] if c.get("applied")]
    with nw._WRITE_LOCK:                 # программа агента держит базу в общих настройках — по очереди с записью анализа
        token = nw._configure(client)
        progress("NocoDB: запись конкурса", 0.1)
        link = db.noco_link(client.id, internal) or {}
        record_id, created_by_site = link.get("record_id"), bool(link.get("created_by_site"))
        if not record_id:
            for v in nw._internal_variants(internal):
                found = nw._find(token, nc.FIELD_MAP["internal_number"], "eq", v)
                if found:
                    record_id = found[0]["Id"]
                    break
        if not record_id:
            raise RuntimeError(f"в NocoDB нет записи конкурса {internal} — сначала запишите анализ («Записать в NocoDB»)")
        keys = list(dict.fromkeys([c["key"] for c in fields] + ["source_file"]))
        row = _registry_row(nc, flat, keys, nc.get_table_columns(token, nc.TABLE_ID))
        issues = []
        if row:
            status, body = nc._request("PATCH", nc.RECORDS_URL, token, {"Id": record_id, **row})
            if status != 200:
                raise RuntimeError(f"NocoDB: обновить запись не удалось: HTTP {status} {str(body)[:200]}")
            issues += (nc.verify_main_record(token, record_id, row) or {}).get("issues") or []

        updated = added = marked = 0
        spec_note = ""
        if poss:
            progress("NocoDB: строки спецификации", 0.5)
            table_id, token = _spec_table(client, internal)
            if not table_id:
                spec_note = "спецификации в NocoDB нет — позиции не записаны"
            else:
                cols = nc.get_table_columns(token, table_id)
                spec_rows = nc.list_all_records(token, table_id)
                by_n: dict[str, list[dict]] = {}
                for r in spec_rows:
                    by_n.setdefault(_clean_n(r.get("№")), []).append(r)
                cur = {_clean_n(r.get("№")): r for r in result.get("positions") or []}
                patches, posts = [], []

                def note_with(r: dict, line: str) -> str:
                    return _add_note(r.get("Примечание"), line)

                for c in poss:
                    targets = by_n.get(c["n"], [])
                    if c["action"] == "removed":
                        for r in targets:
                            if "Примечание" in cols:
                                patches.append({"Id": r["Id"], "Примечание": note_with(r, c["note"])})
                                marked += 1
                        continue
                    now_row = cur.get(c["n"])
                    if now_row is None:              # позицию потом исключили ещё одним изменением
                        continue
                    full = nc.sanitize_spec_row(dict(now_row))
                    if c["action"] == "added" or not targets:
                        if targets:                  # строка с таким № уже есть (записывали раньше) — обновить
                            for r in targets:
                                patches.append({"Id": r["Id"], **{k: v for k, v in full.items() if k in cols and k != "Id"}})
                                updated += 1
                        else:
                            posts.append({k: v for k, v in full.items() if k in cols and k != "Id"})
                            added += 1
                        continue
                    changed_cols = [d["column"] for d in c["changes"]]
                    if "Аналог допускается" in changed_cols:
                        changed_cols.append("Аналог (статус)")
                    for r in targets:
                        u = {"Id": r["Id"], **{k: full.get(k) for k in changed_cols if k in cols}}
                        if "Примечание" in cols:
                            u["Примечание"] = note_with(r, c["note"])
                        patches.append(u)
                        updated += 1
                for i in range(0, len(patches), 100):
                    status, body = nc._request("PATCH", f"{nc.BASE_URL}/{table_id}/records", token, patches[i:i + 100])
                    if status != 200:
                        raise RuntimeError(f"NocoDB: строки спецификации не обновились: HTTP {status} {str(body)[:200]}")
                for i in range(0, len(posts), 100):
                    status, body = nc._request("POST", f"{nc.BASE_URL}/{table_id}/records", token, posts[i:i + 100])
                    if status != 200:
                        raise RuntimeError(f"NocoDB: новые строки спецификации не добавились: HTTP {status} {str(body)[:200]}")
        link_url = (f"{client.nocodb.url.rstrip('/')}/{client.nocodb.workspace_id}/{client.nocodb.base_id}/"
                    f"{client.nocodb.registry_table_id}?rowId={record_id}")
    written = [label_of(k)[1] for k in keys if k != "source_file" and nc.FIELD_MAP.get(k) in row]
    parts = [f"запись {record_id}: " + (f"полей {len(written)}" if written else "поля конкурса не менялись")]
    if poss:
        parts.append(spec_note or f"строк спецификации: изменено {updated}, добавлено {added}, помечено исключённых {marked}")
    return {"record_id": record_id, "link": link_url, "agent_record": not created_by_site,
            "fields_written": written,
            "rows_updated": updated, "rows_added": added, "rows_marked": marked, "spec_note": spec_note,
            "issues": issues[:20], "text": "Изменения записаны в NocoDB — " + "; ".join(parts)}


def chat_lines(result: dict) -> list[str]:
    """Изменения документации — для чата (в <analysis>)."""
    out = []
    for c in (result or {}).get("changes") or []:
        line = f"- {c.get('at_text', '')}: {c.get('summary') or 'изменения разобраны'}"
        if c.get("files"):
            line += f" (документы: {', '.join(x.split('/')[-1] for x in c['files'][:6])})"
        out.append(line)
        out.extend(f"  · {t}" for t in (c.get("comments") or [])[:6])
    return out
