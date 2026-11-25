"""
Удаление конкурса из NocoDB (0.37.0) — только администратор сайта.

Пользователь 25.11.2026: «сделай, чтобы можно было удалять спецификации, только admin»; уточнение: «и спецификацию, и
запись в базе данных… спецификация и запись с тендером должны удаляться вместе». Удаляются таблица спецификации
конкурса (если есть) и запись «Реестра конкурсов», у сайта — связь (noco_links). Папка конкурса в Dropbox и данные
сайта (документы, анализ, чат, письма) остаются — конкурс становится «папкой без записи в NocoDB».
Перед удалением запись целиком и все строки спецификации сохраняются копией: DATA_DIR/spec_backups/ и, если у
конкурса есть папка, AIExtract/history/ — по ней конкурс можно восстановить.

Защита от удаления не той таблицы: только таблица базы клиента, не реестр и не «Поставщики», и только с названием
спецификации («К948» — внутренний номер конкурса, или «Спецификация — <номер>»); на странице — ввод номера конкурса.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import Client
from .tenders import norm_number

MSK = timezone(timedelta(hours=3))


class SpecDeleteError(RuntimeError):
    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


def _spec_title_ok(title: str, internal: str, number: str) -> bool:
    t = (title or "").strip()
    if internal and norm_number(t) == norm_number(internal) and re.fullmatch(r"[КK]\s*-?\s*\d{1,5}", t, re.I):
        return True
    return bool(number) and t == f"Спецификация — {number}"


def delete(client: Client, *, record: dict, table_id: str | None, internal: str, number: str, folder_path: Path | None,
           rows: list[dict], columns: list[str], actor: str) -> dict:
    """Синхронно (поток): копия → удаление таблицы спецификации (если есть) → удаление записи реестра → связь сайта."""
    from tools import nocodb_client as nc
    from .analysis import noco_write as nw
    from .noco_tables import base_tables
    s = client.nocodb
    title = ""
    if table_id:
        tables = base_tables(s, fresh=True)
        title = tables.get(table_id)
        if title is None:
            raise SpecDeleteError("таблица спецификации не в базе этого клиента — удалять её отсюда нельзя", 409)
        if table_id in (s.registry_table_id, getattr(s, "suppliers_table_id", "")) or not _spec_title_ok(title, internal, number):
            raise SpecDeleteError(f"таблица «{title}» не похожа на спецификацию конкурса {internal} — не удаляю", 409)
    if not record.get("Id"):
        raise SpecDeleteError("запись конкурса в реестре не найдена", 404)
    # 1. Копия.
    stamp = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")
    backup = {"client": client.id, "base_id": s.base_id, "registry_table_id": s.registry_table_id, "table_id": table_id,
              "title": title, "internal": internal, "record_id": record.get("Id"), "record": record,
              "deleted_by": actor, "deleted_at": stamp, "columns": columns, "rows": rows}
    text = json.dumps(backup, ensure_ascii=False, indent=1)
    saved = []
    safe = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]", "_", f"{client.id}_{internal or table_id}_{stamp}")
    for path in ([db.DATA_DIR / "spec_backups" / f"{safe}.json"] +
                 ([folder_path / "AIExtract" / "history" / f"nocodb_{internal or record.get('Id')}_{stamp}.json"] if folder_path else [])):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            saved.append(str(path))
        except OSError:
            pass
    if not saved:
        raise SpecDeleteError("не удалось сохранить копию — удаление отменено", 500)
    token = s.token()
    done, warnings = [], []
    # 2. Таблица спецификации.
    if table_id:
        st, body = nc._request("DELETE", f"{nc.META_URL}/tables/{table_id}", token)
        if st not in (200, 201):
            raise SpecDeleteError(f"NocoDB не удалил спецификацию — ничего не удалено: HTTP {st} {str(body)[:200]}", 502)
        done.append(f"спецификация «{title}» удалена ({len(rows)} строк)")
    # 3. Запись реестра.
    with nw._WRITE_LOCK:
        nw._configure(client)
        st, body = nc._request("DELETE", nc.RECORDS_URL, token, {"Id": record["Id"]})
    if st in (200, 201):
        done.append(f"запись конкурса {internal} в реестре удалена")
    else:
        warnings.append(f"запись в реестре удалить не удалось (HTTP {st}) — удалите её в NocoDB вручную")
    # 4. Связь сайта и кэши.
    with db.connect() as con:
        con.execute("DELETE FROM noco_links WHERE client_id=? AND internal=?", (client.id, internal))
    from .tender_rename import _forget_caches
    _forget_caches(client)
    return {"done": done, "warnings": warnings, "backup": saved, "at": time.time()}


# ---------- папка конкурса — в «#Удалённые конкурсы» (0.37.0) ----------
# Пользователь 26.11.2026 (К999 — папка без записи в NocoDB): «я вот эту запись хочу удалить». Файлы не стираются:
# папка переезжает в «#Удалённые конкурсы» внутри папки клиента (служебные «#…» сайт в списке не показывает), к имени
# добавляется время — вернуть можно, перетащив её обратно. Идущие задачи по конкурсу — отказ.

TRASH_FOLDER = "#Удалённые конкурсы"


def trash_folder(client: Client, folder: str) -> str:
    """Переместить папку конкурса в корзину клиента. Возвращает путь в корзине (относительно папки клиента)."""
    import os
    from .tenders import FolderError, client_root, folder_path
    try:
        src = folder_path(client, folder)
        root = client_root(client)
    except FolderError as exc:
        raise SpecDeleteError(str(exc), 404) from exc
    with db.connect() as con:
        busy = con.execute("SELECT kind FROM jobs WHERE client_id=? AND folder=? AND status IN ('queued','running','paused')",
                           (client.id, folder)).fetchall()
    if busy:
        raise SpecDeleteError("по конкурсу идут или ждут задачи (" + ", ".join(r[0] for r in busy) + ") — дождитесь "
                              "окончания или отмените их", 409)
    stamp = datetime.now(MSK).strftime("%Y-%m-%d %H-%M")
    dst = root / TRASH_FOLDER / f"{folder} (удалён {stamp})"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        raise SpecDeleteError(f"папку не переместить ({exc.strerror or exc}): закройте файлы конкурса в Excel / Word / "
                              "проводнике и повторите", 409) from exc
    return f"{TRASH_FOLDER}/{dst.name}"
