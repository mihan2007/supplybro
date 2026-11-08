"""
Таблица спецификации — только из базы NocoDB клиента (0.16.1).

Зачем: пользователь 08.11.2026 сделал для сайта копию базы NocoDB. В копии «Ссылка на спецификацию» у конкурсов
по-прежнему ведёт в таблицы СТАРОЙ базы (NocoDB при копировании ссылки не меняет), и в базе сайта (noco_links)
записаны id таблиц старой базы. Тот же ключ NocoDB видит обе базы — без проверки сайт читал бы спецификации из старой
базы и писал бы туда цены и «Отдел закупок».

own_table(s, table_id): таблица из базы клиента — та же; из другой базы — таблица с тем же названием («К934») в базе
клиента; такой нет (или таблицы нет совсем) — None: сайт считает, что спецификации нет, и в чужую базу не пишет.
"pending:…" (сайт создаёт таблицу) возвращается как есть.

Синхронно (обработчик, tools/nocodb_client) и own_table_async — для адресов сайта. Список таблиц базы — в памяти на
TTL секунд, название чужой таблицы — до перезапуска.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time

import httpx

from .config import NocoSettings
from .nocodb import NocoError

TTL = 120
# id таблицы в ссылке вида https://app.nocodb.com/<ws>/<база>/<таблица>[/<вид>] — любой базы (у tools/nocodb_client
# _table_id_from_link — только текущей, ссылка из копии базы там не находится вовсе).
TABLE_ID_RE = re.compile(r"/(m[a-z0-9]{8,})(?:[/?#]|$)")
_LOCK = threading.Lock()
_TABLES: dict[str, tuple[float, dict[str, str]]] = {}    # base_id → (годен до, {id таблицы: название})
_TITLES: dict[str, str] = {}                             # id таблицы чужой базы → название


def _get(s: NocoSettings, path: str) -> httpx.Response:
    try:
        # trust_env=True — прокси из HTTPS_PROXY, как у app/nocodb.py.
        return httpx.get(s.url.rstrip("/") + "/api/v2" + path, headers={"xc-token": s.token()}, timeout=30,
                         trust_env=True)
    except httpx.HTTPError as exc:
        raise NocoError(f"нет связи с NocoDB ({type(exc).__name__}: {exc})") from exc


def base_tables(s: NocoSettings, fresh: bool = False) -> dict[str, str]:
    """Таблицы базы клиента: {id: название}."""
    now = time.monotonic()
    with _LOCK:
        hit = _TABLES.get(s.base_id)
    if hit and hit[0] > now and not fresh:
        return hit[1]
    r = _get(s, f"/meta/bases/{s.base_id}/tables")
    if r.status_code >= 400:
        raise NocoError(f"NocoDB: список таблиц базы {s.base_id} — ответ {r.status_code}: {r.text[:200]}")
    tables = {t["id"]: str(t.get("title") or "") for t in r.json().get("list", [])}
    with _LOCK:
        _TABLES[s.base_id] = (now + TTL, tables)
    return tables


def _title_of(s: NocoSettings, table_id: str) -> str:
    """Название таблицы (любой базы, которую видит ключ); '' — таблицы нет / нет доступа."""
    with _LOCK:
        if table_id in _TITLES:
            return _TITLES[table_id]
    r = _get(s, f"/meta/tables/{table_id}")
    if r.status_code in (400, 401, 403, 404, 422):
        title = ""
    elif r.status_code >= 400:
        raise NocoError(f"NocoDB: описание таблицы {table_id} — ответ {r.status_code}: {r.text[:200]}")
    else:
        title = str(r.json().get("title") or "")
    with _LOCK:
        _TITLES[table_id] = title
    return title


def table_id_in_link(url) -> str | None:
    m = TABLE_ID_RE.search(str(url or ""))
    return m.group(1) if m else None


def own_table(s: NocoSettings, table_id: str | None) -> str | None:
    """Таблица спецификации в базе клиента (см. описание модуля)."""
    table_id = str(table_id or "").strip()
    if not table_id:
        return None
    if table_id.startswith("pending:"):
        return table_id
    tables = base_tables(s)
    if table_id in tables:
        return table_id
    with _LOCK:
        known_foreign = table_id in _TITLES
    if not known_foreign:
        tables = base_tables(s, fresh=True)      # таблицу могли создать только что
        if table_id in tables:
            return table_id
    title = _title_of(s, table_id).strip()
    if not title:
        return None
    return next((tid for tid, t in tables.items() if t.strip() == title), None)


async def own_table_async(s: NocoSettings, table_id: str | None) -> str | None:
    return await asyncio.to_thread(own_table, s, table_id)
