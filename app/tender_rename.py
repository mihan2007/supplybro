"""
Смена внутреннего номера конкурса (0.34.0).

Пользователь 25.11.2026: «сейчас нет функционала переименовывания присвоенного номера, сделай отдельный функционал»;
выбрано — менять К-номер везде. Номер конкурса (К934) живёт в нескольких местах, все они меняются одной кнопкой:
  - папка конкурса в Dropbox: «К934 Заказчик Предмет» → «К950 Заказчик Предмет» (остальная часть имени — как была);
  - «Реестр конкурсов» NocoDB базы клиента: колонка «Внутренний номер» записи конкурса;
  - таблица спецификации, названная номером («К934» → «К950»); если «Ссылка на спецификацию» ведёт в другую базу
    (копия базы «Наша»: ссылки остались на старую), ссылка переписывается на таблицу базы клиента — иначе по новому
    названию сайт её бы не нашёл;
  - база сайта (app.db): задачи, документы и поисковый индекс, чат, связь с NocoDB (noco_links), запросы поставщикам,
    почта, переписка; AIExtract/analysis.json — внутренний номер.
Метки уже отправленных писем ([SB-K934-12]) остаются прежними — ответы поставщиков сайт по-прежнему находит
(mail_inbox.match сверяет метку с запомненной у запроса).

Не меняется: база NocoDB агента в приложении (у «Нашей» сайт работает с копией), уже сделанные файлы с номером
внутри (Позиции_и_цены.xlsx, XML, письма поставщикам) и журнал действий.

Порядок: проверки (номер свободен в папках и в реестре, нет таблицы с новым названием, по конкурсу не идут задачи)
→ папка → NocoDB → база сайта. Ошибка на шаге — сделанные шаги откатываются в обратном порядке.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import db
from .config import Client
from .tenders import FolderError, client_root, list_folders, norm_number

_NUM_PREFIX = re.compile(r"^\s*(?:[КK]\s*-?\s*\d{1,5}|\d{3,4})(?=\D|$)", re.IGNORECASE)
PLAIN_TITLE = re.compile(r"^\s*[КK]\s*-?\s*\d{1,5}\s*$", re.IGNORECASE)
BUSY = ("queued", "running", "paused")
_LOCK = threading.Lock()


class RenameError(RuntimeError):
    """Причина отказа — показывается на странице."""

    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


def new_folder_name(folder: str, new: str) -> str:
    """«К934 ООО Ромашка фильтры» + К950 → «К950 ООО Ромашка фильтры»; без номера в начале — номер добавляется."""
    m = _NUM_PREFIX.match(folder)
    rest = folder[m.end():] if m else " " + folder
    return (new + rest).rstrip(". ") if rest.strip() else new


def _busy_jobs(client_id: str, folder: str) -> list[str]:
    with db.connect() as con:
        rows = con.execute(f"SELECT kind, status FROM jobs WHERE client_id=? AND folder=? AND status IN "
                           f"({','.join('?' * len(BUSY))})", (client_id, folder, *BUSY)).fetchall()
    return [f"{r['kind']} ({r['status']})" for r in rows]


# ---------- база сайта ----------

# (таблица, колонка папки, колонка номера); таблицы почты создаются, когда ими впервые пользуются, — может не быть.
DB_TABLES = [
    ("jobs", "folder", None),
    ("doc_files", "folder", None),
    ("doc_chunks", "folder", None),
    ("chat_messages", "folder", None),
    ("noco_links", "folder", "internal"),
    ("mail_requests", "folder", "internal"),
    ("mail_inbox", "folder", "internal"),
    ("mail_thread_state", None, "internal"),
]


def _db_update(con: sqlite3.Connection, client_id: str, old_folder: str | None, new_folder: str | None,
               old: str, new: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, fcol, icol in DB_TABLES:
        try:
            n = 0
            if fcol and old_folder and new_folder:
                n += con.execute(f"UPDATE {table} SET {fcol}=? WHERE client_id=? AND {fcol}=?",
                                 (new_folder, client_id, old_folder)).rowcount
            if icol:
                n += con.execute(f"UPDATE {table} SET {icol}=? WHERE client_id=? AND {icol}=?",
                                 (new, client_id, old)).rowcount
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                continue
            raise
        if n:
            counts[table] = n
    return counts


def _analysis_update(folder: Path, old: str, new: str) -> bool:
    from .analysis.run import load_result, save_result
    result = load_result(folder)
    if not result:
        return False
    changed = False
    if result.get("internal_number") in (old, "", None) or norm_number(result.get("internal_number")) == old:
        result["internal_number"] = new
        changed = True
    flat = result.get("flat")
    if isinstance(flat, dict) and norm_number(flat.get("internal_number")) in (old, None):
        flat["internal_number"] = new
        changed = True
    if changed:
        save_result(folder, result)
    return changed


# ---------- NocoDB ----------

def _own_link(client: Client, table_id: str) -> str:
    s = client.nocodb
    return f"{s.url.rstrip('/')}/{s.workspace_id}/{s.base_id}/{table_id}"


def _noco_state(client: Client, old: str, new: str) -> dict:
    """Запись реестра, таблица спецификации и проверки свободного номера в NocoDB (только чтение)."""
    from tools import nocodb_client as nc
    from .analysis import noco_write as nw
    from .noco_tables import base_tables, table_id_in_link
    from .pricelists import _spec_table
    state = {"record": None, "spec": None, "token": ""}
    with nw._WRITE_LOCK:
        token = nw._configure(client)
        state["token"] = token
        for v in nw._internal_variants(new):
            if nw._find(token, "Внутренний номер", "eq", v):
                raise RenameError(f"номер {new} уже есть в «Реестре конкурсов» NocoDB — выберите другой", 409)
        link = db.noco_link(client.id, old, base_id=client.nocodb.base_id) or {}
        rec = None
        if link.get("record_id"):
            st, body = nc._request("GET", f"{nc.RECORDS_URL}/{int(link['record_id'])}", token)
            if st == 200 and norm_number(str(body.get("Внутренний номер") or "")) == old:
                rec = body
        if rec is None:
            for v in nw._internal_variants(old):
                found = nw._find(token, "Внутренний номер", "eq", v)
                if found:
                    rec = found[0]
                    break
        state["record"] = rec
    tables = base_tables(client.nocodb, fresh=True)
    if any(t.strip() == new for t in tables.values()):
        raise RenameError(f"в базе NocoDB уже есть таблица «{new}» — выберите другой номер", 409)
    tid, _ = _spec_table(client, old)
    if tid and not str(tid).startswith("pending:") and tid in tables:
        title = tables[tid]
        state["spec"] = {"id": tid, "title": title, "rename": bool(PLAIN_TITLE.match(title)) and norm_number(title) == old,
                         "relink": bool(rec) and table_id_in_link((rec or {}).get(nc.SPEC_LINK_COLUMN)) != tid}
    return state


def _patch_record(token: str, rid, fields: dict) -> None:
    from tools import nocodb_client as nc
    st, body = nc._request("PATCH", nc.RECORDS_URL, token, {"Id": rid, **fields})
    if st not in (200, 201):
        raise RenameError(f"NocoDB не изменил запись реестра: HTTP {st} {str(body)[:200]}", 502)


def _rename_table(token: str, table_id: str, title: str) -> None:
    from tools import nocodb_client as nc
    st, body = nc._request("GET", f"{nc.META_URL}/tables/{table_id}", token)
    if st != 200:
        raise RenameError(f"NocoDB: не прочитать таблицу спецификации: HTTP {st}", 502)
    old_title, old_name = str(body.get("title") or ""), str(body.get("table_name") or "")
    name = title if (not old_name or old_name == old_title) else old_name
    st, body = nc._request("PATCH", f"{nc.META_URL}/tables/{table_id}", token, {"title": title, "table_name": name})
    if st not in (200, 201):
        raise RenameError(f"NocoDB не переименовал таблицу спецификации: HTTP {st} {str(body)[:200]}", 502)


def _forget_caches(client: Client) -> None:
    from . import nocodb, noco_tables, price_summary
    with noco_tables._LOCK:
        noco_tables._TABLES.pop(client.nocodb.base_id, None)
    nocodb._cache.clear()
    nocodb._tables_read.clear()
    with nocodb._RECENT_LOCK:
        nocodb._recent.clear()
    price_summary._SPEC_CACHE.clear()


# ---------- главное ----------

def rename(client: Client, *, folder: str | None, internal: str | None, new_number: str) -> dict:
    """Сменить внутренний номер конкурса. folder — папка (может не быть), internal — номер записи реестра (если нет
    папки). Возвращает {old, new, folder, record_id, done: [что изменено], warnings}."""
    new = norm_number(new_number)
    if not new or not re.fullmatch(r"\s*[КK]?\s*-?\s*\d{1,5}\s*", str(new_number or "")):
        raise RenameError("новый номер — буква К и цифры, например К950")
    old = norm_number(folder) if folder else norm_number(internal)
    if not old:
        raise RenameError("у конкурса нет внутреннего номера — менять нечего")
    if new == old:
        raise RenameError(f"у конкурса уже номер {new}")
    with _LOCK:
        root = None
        if folder:
            try:
                root = client_root(client)
            except FolderError as exc:
                raise RenameError(str(exc)) from exc
            if not (root / folder).is_dir():
                raise RenameError(f"папка конкурса не найдена: {folder}", 404)
            busy = _busy_jobs(client.id, folder)
            if busy:
                raise RenameError("по конкурсу идут или ждут задачи (" + ", ".join(busy) + ") — дождитесь их "
                                  "окончания или отмените их", 409)
        taken = [f["name"] for f in list_folders(client) if f["number"] == new]
        if taken:
            raise RenameError(f"номер {new} уже занят папкой «{taken[0]}»", 409)
        noco = _noco_state(client, old, new)
        rec, spec, token = noco["record"], noco["spec"], noco["token"]

        done: list[str] = []
        warnings: list[str] = []
        undo: list = []
        new_folder = new_folder_name(folder, new) if folder else None
        try:
            # 1. Папка.
            if folder:
                src, dst = root / folder, root / new_folder
                if dst.exists():
                    raise RenameError(f"папка «{new_folder}» уже есть", 409)
                try:
                    os.rename(src, dst)
                except OSError as exc:
                    raise RenameError(f"папку не переименовать ({exc.strerror or exc}): закройте файлы конкурса "
                                      "в Excel / Word / проводнике и повторите", 409) from exc
                undo.append(lambda: os.rename(dst, src))
                done.append(f"папка: «{folder}» → «{new_folder}»")
            # 2. NocoDB: запись реестра (и ссылка на спецификацию своей базы).
            from tools import nocodb_client as nc
            from .analysis import noco_write as nw
            if rec:
                fields = {"Внутренний номер": new}
                old_link = rec.get(nc.SPEC_LINK_COLUMN)
                if spec and spec["relink"]:
                    fields[nc.SPEC_LINK_COLUMN] = _own_link(client, spec["id"])
                with nw._WRITE_LOCK:
                    nw._configure(client)
                    _patch_record(token, rec["Id"], fields)
                back = {"Внутренний номер": rec.get("Внутренний номер") or old}
                if nc.SPEC_LINK_COLUMN in fields:
                    back[nc.SPEC_LINK_COLUMN] = old_link

                def undo_record(rid=rec["Id"], back=back):
                    with nw._WRITE_LOCK:
                        nw._configure(client)
                        _patch_record(token, rid, back)
                undo.append(undo_record)
                done.append(f"NocoDB: «Внутренний номер» в реестре → {new}")
                if nc.SPEC_LINK_COLUMN in fields:
                    done.append("NocoDB: «Ссылка на спецификацию» → таблица этой базы")
            else:
                warnings.append("записи конкурса в «Реестре конкурсов» NocoDB нет — там менять нечего")
            # 3. NocoDB: таблица спецификации.
            if spec and spec["rename"]:
                _rename_table(token, spec["id"], new)
                undo.append(lambda tid=spec["id"], t=spec["title"]: _rename_table(token, tid, t))
                done.append(f"NocoDB: таблица спецификации «{spec['title']}» → «{new}»")
            elif spec:
                warnings.append(f"таблица спецификации называется «{spec['title']}», а не номером — оставлена как есть")
            # 4. База сайта (одной транзакцией) и analysis.json.
            con = db.connect()
            try:
                with con:
                    counts = _db_update(con, client.id, folder, new_folder, old, new)
            finally:
                con.close()
            if counts:
                done.append("база сайта: " + ", ".join(f"{t} — {n}" for t, n in counts.items()))
        except Exception:
            for fn in reversed(undo):
                try:
                    fn()
                except Exception as exc:          # откат не удался — сказать, что осталось
                    warnings.append(f"откат не удался: {exc}")
            if warnings:
                raise RenameError("номер не изменён; " + "; ".join(warnings), 500)
            raise
        if new_folder:
            try:
                if _analysis_update(root / new_folder, old, new):
                    done.append("AIExtract/analysis.json: внутренний номер")
            except OSError as exc:
                warnings.append(f"analysis.json не обновлён: {exc}")
        _forget_caches(client)
        return {"old": old, "new": new, "folder": new_folder, "record_id": (rec or {}).get("Id"),
                "done": done, "warnings": warnings, "at": time.time()}
