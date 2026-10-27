"""
Запись результата анализа в NocoDB — задача «noco» обработчика (и последний шаг задачи «analyze»).

Используется скрипт старого проекта tools/nocodb_client.py как библиотека (то же форматирование полей,
та же таблица спецификации с представлениями «Сбор цен», «Закупки», «Цены агента», та же проверка
записанного). Запись ищется по ВНУТРЕННЕМУ номеру (номер папки конкурса), а не по номеру закупки.

Защита чужих данных — без подтверждения пользователя («Записать всё равно» на странице) сайт НЕ меняет:
  * запись, где тот же номер закупки уже стоит у другого внутреннего номера («duplicate» — например,
    документы К934 загружены в папку К992);
  * запись с этим внутренним номером, которую создал не сайт и в которой уже есть данные («existing» —
    например, конкурс, обработанный агентом в приложении).
Даже при подтверждении у такой «чужой» записи таблица спецификации, в которой уже есть строки,
не перезаписывается (в ней цены отдела закупок, рынков, агента).
"""

from __future__ import annotations

import re
import time
import urllib.parse

from .. import db
from ..config import Client
from .fields import NOT_SPECIFIED

from tools import nocodb_client as nc

INTERNAL_COLUMN = "Внутренний номер"
NO_WRITE_KEYS = {"product_title"}


def _configure(client: Client) -> str:
    """Настраивает nocodb_client на базу клиента и возвращает ключ."""
    s = client.nocodb
    nc.WORKSPACE_ID = s.workspace_id
    nc.BASE_ID = s.base_id
    nc.TABLE_ID = s.registry_table_id
    nc.RECORDS_URL = f"{nc.BASE_URL}/{s.registry_table_id}/records"
    return s.token()


def _find(token: str, column: str, op: str, value: str) -> list[dict]:
    where = urllib.parse.quote(f"({column},{op},{value})")
    status, body = nc._request("GET", f"{nc.RECORDS_URL}?where={where}&limit=10", token)
    if status != 200:
        raise RuntimeError(f"поиск в NocoDB не удался: HTTP {status} {body}")
    return body.get("list", [])


def _internal_variants(internal: str) -> list[str]:
    m = re.search(r"(\d+)", internal or "")
    if not m:
        return [internal] if internal else []
    n = m.group(1)
    return list(dict.fromkeys([internal, f"К{n}", f"K{n}", f"К-{n}", f"K-{n}"]))


def _number_core(number: str) -> str:
    """«Лот № 0051-ЭКСП ДИТ-2026-ГП» → «0051-ЭКСП ДИТ-2026-ГП» (для поиска похожих номеров)."""
    t = re.sub(r"^(?:лот|закупка|извещение|процедура)?\s*№?\s*", "", str(number or "").strip(), flags=re.IGNORECASE)
    return t.strip(" .,")


def _has_content(rec: dict) -> bool:
    for col in ("Наименование предмета закупки", "Номер конкурса", "Начальная цена", "Заказчик"):
        v = str(rec.get(col) or "").strip()
        if v and v != NOT_SPECIFIED:
            return True
    return False


def _clean_marker(text: str) -> str:
    return re.sub(r"^(✅|🔺)\s*", "", str(text or "")).strip()


def check_conflicts(client: Client, internal: str, purchase_number: str) -> dict:
    """Что уже есть в NocoDB по этому конкурсу. Только чтение."""
    token = _configure(client)
    by_internal = None
    for v in _internal_variants(internal):
        found = _find(token, INTERNAL_COLUMN, "eq", v)
        if found:
            by_internal = found[0]
            break
    others = []
    pn = _clean_marker(purchase_number)
    if pn and pn != NOT_SPECIFIED:
        seen = set()
        for op, val in (("eq", pn), ("like", f"%{_number_core(pn)}%")):
            if op == "like" and len(_number_core(pn)) < 6:
                continue
            for rec in _find(token, nc.PURCHASE_NUMBER_COLUMN, op, val):
                if rec["Id"] in seen or (by_internal and rec["Id"] == by_internal["Id"]):
                    continue
                seen.add(rec["Id"])
                others.append({"id": rec["Id"], "internal": str(rec.get(INTERNAL_COLUMN) or ""),
                               "number": str(rec.get(nc.PURCHASE_NUMBER_COLUMN) or "")})
    link = db.noco_link(client.id, internal)
    created_by_site = bool(by_internal and link and link.get("record_id") == by_internal["Id"]
                           and link.get("created_by_site"))
    return {"token": token, "by_internal": by_internal, "duplicates": others, "created_by_site": created_by_site}


def save(client: Client, folder_name: str, result: dict, force: bool = False, progress=None) -> dict:
    """Записывает результат анализа в NocoDB. Возвращает сведения для analysis.json["noco"]."""
    progress = progress or (lambda *_: None)
    internal = result["internal_number"]
    flat = dict(result["flat"])
    progress("NocoDB: поиск записи конкурса", 0.88)
    state = check_conflicts(client, internal, flat.get("purchase_number", ""))
    token, target = state.pop("token"), state["by_internal"]

    conflict = None
    if state["created_by_site"]:
        pass  # запись создал сайт (при необходимости — с подтверждения пользователя): обновляем без вопросов
    elif state["duplicates"]:
        d = state["duplicates"][0]
        conflict = {"kind": "duplicate", "record_id": d["id"], "internal": d["internal"],
                    "text": (f"Номер закупки «{_clean_marker(flat.get('purchase_number'))}» уже есть в NocoDB у конкурса "
                             f"{d['internal'] or 'без внутреннего номера'} (запись {d['id']}). Похоже, это тот же конкурс. "
                             f"Запись {d['internal'] or d['id']} сайт не трогал.")}
    elif target and not state["created_by_site"] and _has_content(target):
        conflict = {"kind": "existing", "record_id": target["Id"], "internal": internal,
                    "text": (f"Запись {internal} в NocoDB уже заполнена (её создал не сайт — например, агент в приложении). "
                             "Сайт её не менял.")}
    if conflict and not force:
        return {"status": "conflict", "conflict": conflict, "at": time.time()}

    # При подтверждённом «duplicate» создаётся отдельная запись с этим внутренним номером
    # (target ищется только по внутреннему номеру, запись-двойник не меняется).

    # Строка основной записи — как upsert() в nocodb_client.py.
    data = {k: v for k, v in flat.items() if k not in NO_WRITE_KEYS}
    nc.apply_dash_placeholders(data)
    if data.get("start_price"):
        data["start_price"] = nc.to_comma_decimal(str(data["start_price"]))
    if data.get("address"):
        data["address"] = nc.bold_address_except_index(str(data["address"]))
    if data.get("source_file"):
        data["source_file"] = nc.format_source_files(str(data["source_file"]))
    nc.ensure_columns(token, nc.TABLE_ID, [nc.FIELD_MAP.get(k) for k in data])
    row = {}
    for key, value in data.items():
        col = nc.FIELD_MAP.get(key)
        if col is None:
            continue
        if key in nc.STRICT_URL_FIELDS and not nc._looks_like_url(value):
            continue
        row[col] = value

    # Спецификация. Перезаполняется только таблица, которую создал сайт для этой записи, или новая.
    # Таблица со строками, которую создал не сайт (по ссылке в записи или с таким же названием), не трогается.
    progress("NocoDB: таблица спецификации", 0.91)
    spec = {"status": "skipped"}
    linked_id = nc._table_id_from_link((target or {}).get(nc.SPEC_LINK_COLUMN))
    by_title = nc.find_table_by_title(token, nc._spec_table_title(internal))
    candidate = linked_id or (by_title or {}).get("id")
    ours = bool(state["created_by_site"] and linked_id and candidate == linked_id)
    spec_rows = result.get("positions") or []
    if candidate and not ours and nc.list_all_records(token, candidate):
        spec = {"status": "kept",
                "text": "Таблица спецификации уже заполнена — не перезаписана (в ней могут быть цены и данные агента)."}
    elif spec_rows:
        url, table_id, safe_rows = nc.sync_specification(token, internal, spec_rows, existing_table_id=linked_id)
        row[nc.SPEC_LINK_COLUMN] = url
        progress("NocoDB: проверка спецификации", 0.96)
        spec = {"status": "written", "url": url, "table_id": table_id,
                "verification": nc.verify_spec_table(token, table_id, safe_rows)}

    progress("NocoDB: запись конкурса", 0.97)
    if target:
        status, body = nc._request("PATCH", nc.RECORDS_URL, token, {"Id": target["Id"], **row})
        if status != 200:
            raise RuntimeError(f"NocoDB: обновить запись не удалось: HTTP {status} {body}")
        record_id, action = target["Id"], "updated"
    else:
        status, body = nc._request("POST", nc.RECORDS_URL, token, row)
        if status != 200:
            raise RuntimeError(f"NocoDB: создать запись не удалось: HTTP {status} {body}")
        record_id, action = body.get("Id"), "created"
    created_by_site = action == "created" or state["created_by_site"]
    db.save_noco_link(client.id, internal, folder_name, record_id, created_by_site,
                      spec.get("table_id") or linked_id or "")
    verification = nc.verify_main_record(token, record_id, row)
    return {
        "status": "saved", "action": action, "record_id": record_id, "at": time.time(),
        "forced": bool(conflict), "conflict": conflict,
        "link": f"{client.nocodb.url.rstrip('/')}/{client.nocodb.workspace_id}/{client.nocodb.base_id}/"
                f"{client.nocodb.registry_table_id}?rowId={record_id}",
        "verification": verification, "spec": spec,
    }
