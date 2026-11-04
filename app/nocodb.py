"""
Чтение данных из NocoDB (только чтение — сайт ничего в NocoDB не пишет).

Что умеет:
  - описание таблицы: колонки и представления (views);
  - какие поля и в каком порядке показывает представление — как в самой NocoDB;
  - записи таблицы (с учётом представления), одна запись по Id;
  - поиск таблицы по названию (спецификация называется внутренним номером, например «К939»).

Ответы описаний таблиц кэшируются на 2 минуты, чтобы не упираться в лимит запросов NocoDB.
Прокси для выхода в интернет берётся из переменных окружения HTTPS_PROXY / NO_PROXY.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import NocoSettings

# Служебные колонки NocoDB — на странице их не показываем.
SYSTEM_UIDTS = {"ID", "CreatedTime", "LastModifiedTime", "CreatedBy", "LastModifiedBy", "Order", "Deleted", "Meta"}

CACHE_SECONDS = 120
PAGE_SIZE = 100          # записей за один запрос
PARALLEL_PAGES = 1       # страниц одновременно: NocoDB отвечает 429 «Too Many Requests» уже на 10 сразу (проверено
                         # 04.11.2026); по одной через одно соединение — реестр «Наша», 940 записей, за ~5 с вместо 14
RETRY_429 = 3            # «Too Many Requests» — подождать и повторить
MAX_ROWS = 3000          # предохранитель для очень больших таблиц

_cache: dict[str, tuple[float, Any]] = {}


class NocoError(RuntimeError):
    """Ошибка обращения к NocoDB — текст показывается пользователю на странице."""


def _cached(key: str, fresh: bool):
    if fresh:
        return None
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key: str, value: Any) -> Any:
    _cache[key] = (time.monotonic() + CACHE_SECONDS, value)
    return value


class NocoClient:
    def __init__(self, settings: NocoSettings):
        self.s = settings
        self.api = settings.url.rstrip("/") + "/api/v2"

    # ---------- низкоуровневый запрос ----------

    async def _get(self, path: str, params: dict | None = None, http: httpx.AsyncClient | None = None) -> Any:
        """GET к API NocoDB. http — уже открытое соединение (records: страницы одной таблицы — через одно соединение;
        новое соединение через прокси — около секунды на каждый запрос)."""
        headers = {"xc-token": self.s.token()}
        for attempt in range(RETRY_429 + 1):
            try:
                if http is not None:
                    r = await http.get(self.api + path, params=params, headers=headers)
                else:
                    # trust_env=True — прокси берётся из HTTPS_PROXY (на компьютере это прокси Windows).
                    async with httpx.AsyncClient(timeout=30, trust_env=True) as one:
                        r = await one.get(self.api + path, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise NocoError(f"нет связи с NocoDB ({type(exc).__name__}: {exc}). "
                                "Проверьте интернет и прокси (OUTBOUND_PROXY в .env).") from exc
            if r.status_code != 429 or attempt == RETRY_429:
                break
            await asyncio.sleep(1.5 * (attempt + 1))   # NocoDB ограничивает частоту запросов — переждать
        if r.status_code == 401:
            raise NocoError("NocoDB не принял ключ (401). Проверьте файл ключа (NOCODB_TOKEN_FILE в .env).")
        if r.status_code >= 400:
            raise NocoError(f"NocoDB ответил {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------- описание таблиц ----------

    async def base_tables(self, fresh: bool = False) -> list[dict]:
        key = f"tables:{self.s.base_id}"
        if (hit := _cached(key, fresh)) is not None:
            return hit
        data = await self._get(f"/meta/bases/{self.s.base_id}/tables")
        return _store(key, [{"id": t["id"], "title": t["title"]} for t in data.get("list", [])])

    async def find_table_by_title(self, title: str, fresh: bool = False) -> str | None:
        for t in await self.base_tables(fresh):
            if t["title"].strip() == title.strip():
                return t["id"]
        return None

    async def table_meta(self, table_id: str, fresh: bool = False) -> dict:
        """Название, колонки (без служебных) и представления таблицы."""
        key = f"meta:{table_id}"
        if (hit := _cached(key, fresh)) is not None:
            return hit
        data = await self._get(f"/meta/tables/{table_id}")
        columns = [
            {"id": c["id"], "title": c["title"], "uidt": c.get("uidt")}
            for c in data.get("columns", [])
            if not c.get("system") and c.get("uidt") not in SYSTEM_UIDTS
        ]
        views = [{"id": v["id"], "title": v["title"]} for v in data.get("views", [])]
        return _store(key, {"id": table_id, "title": data.get("title", ""), "columns": columns, "views": views})

    async def view_columns(self, table_id: str, view_id: str, fresh: bool = False) -> list[dict]:
        """Видимые поля представления в том порядке, как в NocoDB: [{title, uidt}]."""
        key = f"viewcols:{view_id}"
        if (hit := _cached(key, fresh)) is not None:
            return hit
        meta = await self.table_meta(table_id, fresh)
        by_id = {c["id"]: c for c in meta["columns"]}
        data = await self._get(f"/meta/views/{view_id}/columns")
        items = sorted(data.get("list", []), key=lambda vc: vc.get("order") or 0)
        cols = [{"title": by_id[vc["fk_column_id"]]["title"], "uidt": by_id[vc["fk_column_id"]]["uidt"]}
                for vc in items if vc.get("show") and vc.get("fk_column_id") in by_id]
        return _store(key, cols)

    @staticmethod
    def pick_view(meta: dict, wanted: str | None, default_title: str) -> dict | None:
        """Представление по id/названию; иначе — по умолчанию (по названию); иначе — первое."""
        views = meta["views"]
        for key in (wanted, default_title):
            if key:
                for v in views:
                    if v["id"] == key or v["title"] == key:
                        return v
        return views[0] if views else None

    # ---------- записи ----------

    async def records(self, table_id: str, *, view_id: str | None = None, fields: list[str] | None = None,
                      sort: str | None = None, cache: bool = False, fresh: bool = False) -> list[dict]:
        """Все записи таблицы (постранично), с учётом представления и списка полей.

        Страницы — через одно соединение (0.13.1: с новым соединением на каждую страницу реестр «Наша» читался 14 с, и
        новая вкладка конкурса ждала его весь); по pageInfo.totalRows можно читать и по PARALLEL_PAGES одновременно.
        cache=True — список хранится CACHE_SECONDS (fresh=True — прочитать заново)."""
        key = f"records:{self.s.url}:{table_id}:{view_id}:{','.join(fields or [])}:{sort}"
        if cache and (hit := _cached(key, fresh)) is not None:
            return hit
        base: dict[str, Any] = {"limit": PAGE_SIZE}
        if view_id:
            base["viewId"] = view_id
        if fields:
            base["fields"] = ",".join(fields)
        if sort:
            base["sort"] = sort

        def last(data: dict) -> bool:
            return (data.get("pageInfo") or {}).get("isLastPage", True) or not data.get("list")

        try:
            http = httpx.AsyncClient(timeout=30, trust_env=True)   # одно соединение на все страницы
        except Exception:                                  # noqa: BLE001 — без общего соединения, как раньше
            http = None

        async def page(offset: int) -> dict:
            return await self._get(f"/tables/{table_id}/records", {**base, "offset": offset}, http)

        try:
            data = await page(0)
            rows: list[dict] = list(data.get("list", []))
            total = (data.get("pageInfo") or {}).get("totalRows")
            step = len(rows)                               # сколько NocoDB на самом деле отдаёт за раз
            if not last(data) and isinstance(total, int) and total > step:
                sem = asyncio.Semaphore(PARALLEL_PAGES)

                async def limited(offset: int) -> dict:
                    async with sem:
                        return await page(offset)

                pages = await asyncio.gather(*(limited(o) for o in range(step, min(total, MAX_ROWS), step)))
                for d in pages:
                    rows.extend(d.get("list", []))
                data = pages[-1] if pages else data
            while not last(data) and len(rows) < MAX_ROWS:  # без totalRows или записей прибавилось, пока читали
                data = await page(len(rows))
                rows.extend(data.get("list", []))
        finally:
            if http is not None:
                await http.aclose()
        if rows and "Id" in rows[0]:                       # запись могла сдвинуться между страницами — без повторов
            seen: set = set()
            rows = [r for r in rows if not (r.get("Id") in seen or seen.add(r.get("Id")))]
        rows = rows[:MAX_ROWS]
        return _store(key, rows) if cache else rows

    async def record(self, table_id: str, record_id: int | str) -> dict:
        return await self._get(f"/tables/{table_id}/records/{record_id}")

    # ---------- ссылки на NocoDB ----------

    def table_link(self, table_id: str, view_id: str | None = None, row_id: int | str | None = None) -> str:
        link = f"{self.s.url.rstrip('/')}/{self.s.workspace_id}/{self.s.base_id}/{table_id}"
        if view_id:
            link += f"/{view_id}"
        if row_id is not None:
            link += f"?rowId={row_id}"
        return link
