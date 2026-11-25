"""
База поставщиков на сайте (0.34.0).

Пользователь 25.11.2026: «сделать интерфейс, кнопку, чтобы отображалась база поставщиков и в неё можно было добавлять
и удалять поставщиков». Выбрано: писать в ту же таблицу «Поставщики», откуда сайт их читает (у «Нашей» — старая база
агента, `nocodb.suppliers_table_id`; агент видит изменения), удаление — оба варианта: пометка «Не работаем» (запись
остаётся, в письма не попадает, можно вернуть) и «Удалить насовсем» с подтверждением.

Чтение — общий кэш app/supplier_brands.py (одна таблица на клиента, в памяти и на диске; там же бренды для
спецификации и писем). Запись — сразу из сайта через tools/nocodb_client._request (повторы при 429), после неё
запись заменяется в кэше (`patch_cache`) — спецификация и «Запросы» видят изменения без перечитывания таблицы.

Структуру таблицы сайт не меняет: правит только существующие колонки из FIELDS, значения выпадающих списков —
только из вариантов NocoDB (новые варианты не создаются).
"""

from __future__ import annotations

import re
import threading

from . import supplier_brands as sb
from .config import Client
from .nocodb import NocoClient, NocoError

# Колонки, которые показываются и правятся (в этом порядке); есть не у всех таблиц — берутся только существующие.
FIELDS = [sb.F_NAME, sb.F_BRANDS, "E-mail", "Телефон", "Контактное лицо", sb.F_SITE, "ИНН", "Город", "Страна",
          sb.F_METHOD, sb.F_STATUS, "Категория товаров", sb.F_INFO, "Комментарий закупщика"]
EDITABLE = {"SingleLineText", "LongText", "URL", "Email", "PhoneNumber", "SingleSelect", "Number"}
OFF_RE = re.compile(r"не\s*работаем", re.I)
CHECK_RE = re.compile(r"требуется\s*проверка|не\s*работали", re.I)
MAX_TEXT = 5000
_LOCK = threading.Lock()


class BaseError(RuntimeError):
    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


async def _columns(client: Client, table_id: str) -> list[dict]:
    meta = await NocoClient(client.nocodb).table_meta(table_id)
    cols = {c.get("title"): c for c in meta.get("columns", [])}
    out = []
    for name in FIELDS:
        c = cols.get(name)
        if not c or c.get("uidt") not in EDITABLE:
            continue
        out.append({"name": name, "type": c["uidt"], "options": list(c.get("options") or []),
                    "long": c["uidt"] == "LongText", "required": name == sb.F_NAME})
    return out


LIST_LONG = 300            # в списке длинный текст (LongText) — начало; целиком — при открытии правки


def _view(row: dict, fields: list[dict], full: bool = True) -> dict:
    out = {"id": str(row.get("Id") or "")}
    cut = []
    for f in fields:
        v = row.get(f["name"])
        v = "" if v is None else str(v)
        if not full and f["long"] and len(v) > LIST_LONG:
            v = v[:LIST_LONG] + "…"
            cut.append(f["name"])
        out[f["name"]] = v
    if cut:
        out["cut"] = cut
    makers = row.get(sb.F_MAKERS)
    out["makers"] = [str(m.get("Производитель") or m.get("Title") or "").strip() for m in makers
                     if isinstance(m, dict)] if isinstance(makers, list) else []
    out["level"], _ = sb.level_of(row.get(sb.F_STATUS))
    out["off"] = bool(OFF_RE.search(str(row.get(sb.F_STATUS) or "")))
    return out


async def listing(client: Client, fresh: bool = False) -> dict:
    index, error = await sb.load(client, fresh)
    if not index:
        return {"error": error or "поставщиков не прочитать", "suppliers": [], "fields": []}
    try:
        fields = await _columns(client, index["table_id"])
    except (NocoError, RuntimeError) as exc:
        return {"error": f"описание таблицы «Поставщики» не прочитать: {exc}", "suppliers": [], "fields": []}
    rows = [r for r in index.get("rows") or [] if str(r.get(sb.F_NAME) or "").strip()]
    rows.sort(key=lambda r: (sb.level_of(r.get(sb.F_STATUS))[1], str(r.get(sb.F_NAME) or "").lower()))
    return {"table_id": index["table_id"], "link": NocoClient(client.nocodb).table_link(index["table_id"]),
            "loaded_at": index.get("loaded_at"), "fields": fields, "error": "",
            "suppliers": [_view(r, fields, full=False) for r in rows]}


async def one(client: Client, rid: str) -> dict:
    index, fields = await _context(client)
    return {"supplier": _view(_row_of(index, rid), fields)}


# ---------- запись ----------

def _clean(data: dict, fields: list[dict], partial: bool) -> dict:
    out: dict = {}
    for f in fields:
        if f["name"] not in data:
            continue
        v = re.sub(r"[ \t]+", " ", str(data.get(f["name"]) or "")).strip()[:MAX_TEXT]
        if f["type"] != "LongText":
            v = v.replace("\n", " ")
        if f["type"] == "SingleSelect" and v and v not in f["options"]:
            raise BaseError(f"«{f['name']}»: такого варианта нет в NocoDB — выберите из списка")
        if f["name"] == "E-mail" and v:
            found = sb.EMAIL_FIND_RE.findall(v)
            if not found:
                raise BaseError("«E-mail»: не похоже на адрес почты")
            v = ", ".join(dict.fromkeys(a.strip(".") for a in found))
        if f["type"] == "URL" and v and not re.match(r"^https?://", v, re.I):
            v = "https://" + v
        if f["name"] == "ИНН" and v and not re.fullmatch(r"\d{10}|\d{12}", v):
            raise BaseError("«ИНН»: 10 или 12 цифр")
        out[f["name"]] = v or None
    if not partial and not out.get(sb.F_NAME):
        raise BaseError("укажите название поставщика")
    if partial and sb.F_NAME in out and not out[sb.F_NAME]:
        raise BaseError("название поставщика не может быть пустым")
    return out


def _norm_name(text) -> str:
    t = sb.norm(text)
    t = re.sub(r"\b(ооо|ао|зао|оао|пао|ип|llc|ltd|gmbh|inc|co)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def duplicates(rows: list[dict], data: dict, skip_id: str = "") -> list[dict]:
    """Похожие записи: то же название (без ООО / ИП …), тот же ИНН или общая почта."""
    name = _norm_name(data.get(sb.F_NAME))
    inn = str(data.get("ИНН") or "").strip()
    mails = {a.lower() for a in sb.EMAIL_FIND_RE.findall(str(data.get("E-mail") or ""))}
    out = []
    for r in rows:
        rid = str(r.get("Id") or "")
        if rid == skip_id:
            continue
        why = []
        if name and _norm_name(r.get(sb.F_NAME)) == name:
            why.append("название")
        if inn and str(r.get("ИНН") or "").strip() == inn:
            why.append("ИНН")
        if mails and mails & {a.lower() for a in sb.emails_of(r)}:
            why.append("почта")
        if why:
            out.append({"id": rid, "name": str(r.get(sb.F_NAME) or ""), "why": ", ".join(why)})
    return out[:10]


def _request(method: str, url: str, token: str, body=None):
    from tools import nocodb_client as nc
    st, resp = nc._request(method, url, token, body)
    if st not in (200, 201):
        raise BaseError(f"NocoDB не принял изменение: HTTP {st} {str(resp)[:300]}", 502)
    return resp


def _records_url(table_id: str) -> str:
    from tools import nocodb_client as nc
    return f"{nc.BASE_URL}/{table_id}/records"


def _fetch(client: Client, table_id: str, rid) -> dict:
    return _request("GET", f"{_records_url(table_id)}/{int(rid)}", client.nocodb.token())


async def _context(client: Client) -> tuple[dict, list[dict]]:
    index, error = await sb.load(client)
    if not index:
        raise BaseError(error or "таблица «Поставщики» не прочитана", 502)
    try:
        fields = await _columns(client, index["table_id"])
    except (NocoError, RuntimeError) as exc:
        raise BaseError(f"описание таблицы «Поставщики» не прочитать: {exc}", 502) from exc
    return index, fields


def _row_of(index: dict, rid: str) -> dict:
    row = next((r for r in index.get("rows") or [] if str(r.get("Id")) == str(rid)), None)
    if row is None:
        raise BaseError("поставщик не найден — обновите список", 404)
    return row


async def add(client: Client, data: dict, confirm: bool = False) -> dict:
    index, fields = await _context(client)
    values = _clean(data, fields, partial=False)
    dups = duplicates(index.get("rows") or [], values)
    if dups and not confirm:
        return {"need_confirm": True, "duplicates": dups}
    import asyncio
    return await asyncio.to_thread(_add_sync, client, index["table_id"], values, fields)


def _add_sync(client: Client, table_id: str, values: dict, fields: list[dict]) -> dict:
    with _LOCK:
        resp = _request("POST", _records_url(table_id), client.nocodb.token(), values)
        rid = (resp or {}).get("Id") if isinstance(resp, dict) else None
        if not rid:
            raise BaseError(f"NocoDB не вернул номер новой записи: {str(resp)[:200]}", 502)
        row = _fetch(client, table_id, rid)
    sb.patch_cache(client, str(rid), row)
    return {"supplier": _view(row, fields)}


async def edit(client: Client, rid: str, data: dict) -> dict:
    index, fields = await _context(client)
    _row_of(index, rid)
    values = _clean(data, fields, partial=True)
    if not values:
        raise BaseError("нечего сохранять")
    import asyncio
    return await asyncio.to_thread(_patch_sync, client, index["table_id"], rid, values, fields)


def _patch_sync(client: Client, table_id: str, rid: str, values: dict, fields: list[dict]) -> dict:
    with _LOCK:
        _request("PATCH", _records_url(table_id), client.nocodb.token(), {"Id": int(rid), **values})
        row = _fetch(client, table_id, rid)
    sb.patch_cache(client, str(rid), row)
    return {"supplier": _view(row, fields)}


async def set_off(client: Client, rid: str, off: bool) -> dict:
    """«Не работаем» — вариант статуса с этими словами; «Вернуть» — «Не работали. Требуется проверка» (или пусто)."""
    index, fields = await _context(client)
    _row_of(index, rid)
    f = next((x for x in fields if x["name"] == sb.F_STATUS), None)
    if not f:
        raise BaseError(f"в таблице нет колонки «{sb.F_STATUS}»")
    if off:
        value = next((o for o in f["options"] if OFF_RE.search(o)), None)
        if not value:
            raise BaseError(f"в «{sb.F_STATUS}» нет варианта «Не работаем»")
    else:
        value = next((o for o in f["options"] if CHECK_RE.search(o) and not OFF_RE.search(o)), None)
    import asyncio
    return await asyncio.to_thread(_patch_sync, client, index["table_id"], rid, {sb.F_STATUS: value}, fields)


async def delete(client: Client, rid: str) -> dict:
    index, _ = await _context(client)
    row = _row_of(index, rid)
    import asyncio

    def run():
        with _LOCK:
            _request("DELETE", _records_url(index["table_id"]), client.nocodb.token(), {"Id": int(rid)})
        sb.patch_cache(client, str(rid), None)
    await asyncio.to_thread(run)
    return {"deleted": str(rid), "name": str(row.get(sb.F_NAME) or "")}
