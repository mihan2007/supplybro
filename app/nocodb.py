"""
Чтение данных из NocoDB (только чтение; запись — обработчик и, с 0.34.0, смена номера и база поставщиков через
tools/nocodb_client).

Что умеет:
  - описание таблицы: колонки и представления (views);
  - какие поля и в каком порядке показывает представление — как в самой NocoDB;
  - записи таблицы (с учётом представления), одна запись по Id;
  - поиск таблицы по названию (спецификация называется внутренним номером, например «К939»).

Прокси для выхода в интернет берётся из переменных окружения HTTPS_PROXY / NO_PROXY.

Лимиты NocoDB (0.33.3; пользователь 25.11.2026: «долго грузится работа с поставщиками, иногда не подгружаются данные
от поставщиков, иногда база отказывает в доступе»). NocoDB сама сообщает лимиты в заголовках ответа (проверено
25.11.2026, app.nocodb.com): чтение данных — 5 запросов в секунду, описание таблиц (/meta/…) — 60 в минуту, общие на
ключ (сайт, обработчик и агент в приложении). Страница конкурса открывает 9 разделов сразу — несколько десятков
запросов; NocoDB отвечала 429, сайт ждал ≈ 9 с и сдавался. Теперь:
  - общая очередь на процесс (`_Limiter`): данные — не чаще NOCODB_DATA_RPS (4) в секунду, по заголовкам
    x-ratelimit-remaining / -reset сайт сам ждёт сброса окна, а не получает отказ; 429 — ждать столько, сколько
    просит NocoDB, до NOCODB_MAX_WAIT (90) секунд всего;
  - одинаковые запросы в одно время — один запрос (остальные ждут его ответ), ответ помнится RECENT_SECONDS (5 с):
    запись реестра и строки спецификации разделы страницы больше не читают по 2–3 раза;
  - описания таблиц — в памяти META_CACHE_SECONDS (15 мин; было 2), новая таблица находится сразу (`find_table_by_title`
    при промахе перечитывает список, не чаще раза в минуту);
  - одно постоянное соединение для запросов сайта (было новое на каждый запрос — через прокси ≈ 1 с на каждый).
"""

from __future__ import annotations

import asyncio
import copy
import os
import threading
import time
import weakref
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import NocoSettings

# Служебные колонки NocoDB — на странице их не показываем.
SYSTEM_UIDTS = {"ID", "CreatedTime", "LastModifiedTime", "CreatedBy", "LastModifiedBy", "Order", "Deleted", "Meta"}

CACHE_SECONDS = 120      # список записей с cache=True (реестр для панели «Конкурсы»)
META_CACHE_SECONDS = float(os.environ.get("NOCODB_META_CACHE_SECONDS", "900"))   # колонки, представления, таблицы
TABLES_MISS_SECONDS = 60  # таблицы с таким названием нет — перечитать список не чаще раза в минуту
RECENT_SECONDS = float(os.environ.get("NOCODB_RECENT_SECONDS", "5"))   # тот же запрос только что — тот же ответ
DATA_RPS = float(os.environ.get("NOCODB_DATA_RPS", "4"))   # чтение данных: NocoDB разрешает 5 в секунду
META_RESERVE = 3         # окно /meta (60 в минуту) почти исчерпано — подождать его сброса
MAX_WAIT = float(os.environ.get("NOCODB_MAX_WAIT", "90"))  # сколько всего ждать NocoDB на один запрос при 429
PAGE_SIZE = 100          # записей за один запрос (больше NocoDB не отдаёт — проверено 25.11.2026)
PARALLEL_PAGES = 1       # страниц одновременно: NocoDB отвечает 429 «Too Many Requests» уже на 10 сразу (проверено
                         # 04.11.2026); по одной через одно соединение — реестр «Наша», 940 записей, за ~5 с вместо 14
RETRY_429 = 8            # «Too Many Requests» — подождать (сколько просит NocoDB) и повторить
MAX_ROWS = 3000          # предохранитель для очень больших таблиц

_cache: dict[str, tuple[float, Any]] = {}
_tables_read: dict[str, float] = {}                   # список таблиц базы — когда читали


class NocoError(RuntimeError):
    """Ошибка обращения к NocoDB — текст показывается пользователю на странице."""


# ---------- лимиты NocoDB: общая очередь процесса (0.33.3) ----------

def _kind(path: str) -> str:
    return "meta" if path.startswith("/meta/") else "data"


def _num(headers, name: str) -> float | None:
    try:
        return float(headers.get(name))
    except (TypeError, ValueError):
        return None


class _Limiter:
    """Очередь запросов к NocoDB для всего процесса (сайт: главный цикл и потоки с asyncio.run; обработчик —
    свои потоки). Потокобезопасно: каждый запрос получает время старта, ожидание — asyncio.sleep / time.sleep."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {"data": {"next": 0.0, "until": 0.0}, "meta": {"next": 0.0, "until": 0.0}}

    def delay(self, path: str) -> float:
        kind = _kind(path)
        gap = 1.0 / DATA_RPS if kind == "data" and DATA_RPS > 0 else 0.0
        with self.lock:
            now = time.monotonic()
            st = self.state[kind]
            start = max(now, st["next"], st["until"])
            st["next"] = start + gap
            return start - now

    def note(self, path: str, r: httpx.Response) -> float:
        """Заголовки ответа → сколько ждать до следующего запроса этого вида (0 — не нужно)."""
        kind = _kind(path)
        remaining, reset = _num(r.headers, "x-ratelimit-remaining"), _num(r.headers, "x-ratelimit-reset")
        wait = 0.0
        if r.status_code == 429:
            wait = _num(r.headers, "retry-after") or reset or 1.5
        elif remaining is not None and reset is not None and remaining <= (META_RESERVE if kind == "meta" else 0):
            wait = reset
        if wait <= 0:
            return 0.0
        wait = min(max(wait, 0.5), 65.0) + 0.2
        with self.lock:
            st = self.state[kind]
            st["until"] = max(st["until"], time.monotonic() + wait)
        return wait


LIMITER = _Limiter()


async def wait_turn(path: str) -> None:
    if (d := LIMITER.delay(path)) > 0:
        await asyncio.sleep(d)


def wait_turn_sync(path: str) -> None:
    if (d := LIMITER.delay(path)) > 0:
        time.sleep(d)


def busy_error() -> NocoError:
    return NocoError("NocoDB просит подождать: слишком много запросов с этого ключа (лимит NocoDB — 5 в секунду и "
                     "60 описаний таблиц в минуту). Обновите страницу через минуту.")


def get_sync(url: str, path: str, headers: dict, timeout: float = 30) -> httpx.Response:
    """Синхронный GET с той же очередью и повторами при 429 (noco_tables, noco_bases). httpx.HTTPError — наружу."""
    started = time.monotonic()
    for attempt in range(RETRY_429 + 1):
        wait_turn_sync(path)
        r = httpx.get(url, headers=headers, timeout=timeout, trust_env=True)
        wait = LIMITER.note(path, r)
        if r.status_code != 429 or attempt == RETRY_429 or time.monotonic() - started + wait > MAX_WAIT:
            return r
    return r


# ---------- одинаковые запросы и постоянное соединение ----------

_RECENT_LOCK = threading.Lock()
_recent: dict[str, tuple[float, Any]] = {}            # запрос → (годен до, ответ)
_inflight: dict[tuple[int, str], asyncio.Future] = {}  # (цикл, запрос) → ответ, который уже ждут
_shared: tuple[weakref.ref, httpx.AsyncClient] | None = None


def _recent_get(key: str):
    with _RECENT_LOCK:
        hit = _recent.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _recent_put(key: str, value: Any) -> None:
    now = time.monotonic()
    with _RECENT_LOCK:
        _recent[key] = (now + RECENT_SECONDS, value)
        if len(_recent) > 300:
            for k in [k for k, (t, _) in _recent.items() if t <= now]:
                del _recent[k]


def _shared_http() -> httpx.AsyncClient | None:
    """Постоянное соединение — только для главного цикла сайта (uvicorn в главном потоке). В потоках с asyncio.run
    цикл живёт один вызов — там соединение на запрос, как раньше."""
    global _shared
    if threading.current_thread() is not threading.main_thread():
        return None
    loop = asyncio.get_running_loop()
    if _shared and _shared[0]() is loop and not _shared[1].is_closed:
        return _shared[1]
    client = httpx.AsyncClient(timeout=30, trust_env=True,
                               limits=httpx.Limits(max_connections=20, max_keepalive_connections=10,
                                                   keepalive_expiry=60))
    _shared = (weakref.ref(loop), client)
    return client


def _cached(key: str, fresh: bool):
    if fresh:
        return None
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key: str, value: Any, seconds: float = CACHE_SECONDS) -> Any:
    _cache[key] = (time.monotonic() + seconds, value)
    return value


class NocoClient:
    def __init__(self, settings: NocoSettings):
        self.s = settings
        self.api = settings.url.rstrip("/") + "/api/v2"

    # ---------- низкоуровневый запрос ----------

    async def _get(self, path: str, params: dict | None = None, http: httpx.AsyncClient | None = None,
                   fresh: bool = False) -> Any:
        """GET к API NocoDB. Одинаковый запрос, который уже идёт или был RECENT_SECONDS назад, — тот же ответ
        (копия: вызывающие могут менять строки); fresh=True — всегда новый запрос."""
        key = self.api + path + "?" + urlencode(sorted((params or {}).items()))
        if not fresh and (hit := _recent_get(key)) is not None:
            return copy.deepcopy(hit)
        loop = asyncio.get_running_loop()
        fk = (id(loop), key)
        other = None if fresh else _inflight.get(fk)
        if other is not None:
            try:
                return copy.deepcopy(await asyncio.shield(other))
            except asyncio.CancelledError:
                if not other.cancelled():
                    raise                          # отменили нас самих
                # отменили того, кого ждали (закрыли вкладку) — спрашиваем сами
        fut = loop.create_future()
        _inflight[fk] = fut
        try:
            data = await self._fetch(path, params, http)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()                        # чтобы asyncio не писал «exception was never retrieved»
            raise
        else:
            snap = copy.deepcopy(data)
            fut.set_result(snap)
            _recent_put(key, snap)
            return data
        finally:
            if _inflight.get(fk) is fut:
                del _inflight[fk]

    async def _fetch(self, path: str, params: dict | None, http: httpx.AsyncClient | None) -> Any:
        """Сам запрос: очередь процесса, повторы при 429 (ждём, сколько просит NocoDB, всего до MAX_WAIT)."""
        headers = {"xc-token": self.s.token()}
        http = http or _shared_http()
        started = time.monotonic()
        for attempt in range(RETRY_429 + 1):
            await wait_turn(path)
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
            wait = LIMITER.note(path, r)           # следующий запрос этого вида подождёт сам
            if r.status_code != 429 or attempt == RETRY_429 or time.monotonic() - started + wait > MAX_WAIT:
                break
        if r.status_code == 429:
            raise busy_error()
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
        data = await self._get(f"/meta/bases/{self.s.base_id}/tables", fresh=fresh)
        _tables_read[key] = time.monotonic()
        return _store(key, [{"id": t["id"], "title": t["title"]} for t in data.get("list", [])], META_CACHE_SECONDS)

    async def find_table_by_title(self, title: str, fresh: bool = False) -> str | None:
        def find(tables):
            return next((t["id"] for t in tables if t["title"].strip() == title.strip()), None)

        found = find(await self.base_tables(fresh))
        if found or fresh:
            return found
        # Нет в списке из памяти — таблицу могли создать только что (спецификация нового конкурса).
        if time.monotonic() - _tables_read.get(f"tables:{self.s.base_id}", 0.0) > TABLES_MISS_SECONDS:
            found = find(await self.base_tables(True))
        return found

    async def table_meta(self, table_id: str, fresh: bool = False) -> dict:
        """Название, колонки (без служебных) и представления таблицы."""
        key = f"meta:{table_id}"
        if (hit := _cached(key, fresh)) is not None:
            return hit
        data = await self._get(f"/meta/tables/{table_id}", fresh=fresh)
        columns = [
            {"id": c["id"], "title": c["title"], "uidt": c.get("uidt"),
             # варианты выпадающего списка (0.34.0: база поставщиков на сайте)
             **({"options": [str(o.get("title")) for o in (c.get("colOptions") or {}).get("options") or []
                             if o.get("title")]}
                if c.get("uidt") in ("SingleSelect", "MultiSelect") and isinstance(c.get("colOptions"), dict) else {})}
            for c in data.get("columns", [])
            if not c.get("system") and c.get("uidt") not in SYSTEM_UIDTS
        ]
        views = [{"id": v["id"], "title": v["title"]} for v in data.get("views", [])]
        return _store(key, {"id": table_id, "title": data.get("title", ""), "columns": columns, "views": views},
                      META_CACHE_SECONDS)

    async def view_columns(self, table_id: str, view_id: str, fresh: bool = False) -> list[dict]:
        """Видимые поля представления в том порядке, как в NocoDB: [{title, uidt}]."""
        key = f"viewcols:{view_id}"
        if (hit := _cached(key, fresh)) is not None:
            return hit
        meta = await self.table_meta(table_id, fresh)
        by_id = {c["id"]: c for c in meta["columns"]}
        data = await self._get(f"/meta/views/{view_id}/columns", fresh=fresh)
        items = sorted(data.get("list", []), key=lambda vc: vc.get("order") or 0)
        cols = [{"title": by_id[vc["fk_column_id"]]["title"], "uidt": by_id[vc["fk_column_id"]]["uidt"]}
                for vc in items if vc.get("show") and vc.get("fk_column_id") in by_id]
        return _store(key, cols, META_CACHE_SECONDS)

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

        own = _shared_http() is None                       # в потоке с asyncio.run — своё соединение на все страницы
        try:
            http = httpx.AsyncClient(timeout=30, trust_env=True) if own else None
        except Exception:                                  # noqa: BLE001 — без общего соединения, как раньше
            http = None

        async def page(offset: int) -> dict:
            return await self._get(f"/tables/{table_id}/records", {**base, "offset": offset}, http, fresh=fresh)

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

    async def record(self, table_id: str, record_id: int | str, fresh: bool = False) -> dict:
        return await self._get(f"/tables/{table_id}/records/{record_id}", fresh=fresh)

    # ---------- ссылки на NocoDB ----------

    def table_link(self, table_id: str, view_id: str | None = None, row_id: int | str | None = None) -> str:
        link = f"{self.s.url.rstrip('/')}/{self.s.workspace_id}/{self.s.base_id}/{table_id}"
        if view_id:
            link += f"/{view_id}"
        if row_id is not None:
            link += f"?rowId={row_id}"
        return link
