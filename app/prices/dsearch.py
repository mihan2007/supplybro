"""
Поиск DeepSeek (Search API, с 17.10.2026) и «открывалка страниц» DeepSeek (Fetch API) — для поиска цен.

* Поиск: POST https://api.deepseek.com/v1/tools/search — до 50 результатов на запрос за ~1 с (заголовок, ссылка,
  короткий фрагмент и выжимка ~700 знаков), $0,75 за 1000 успешных запросов (≈ 0,06 ₽); пустая выдача и ошибки
  не оплачиваются. Ключ — тот же, что для DeepSeek в анализе (DEEPSEEK_KEY_FILE_IN_CONTAINER).
  Российские запросы — с «localization» (IP в России), зарубежные — без него.
* Открыть страницу: POST https://api.deepseek.com/v1/tools/fetch — страница в виде текста (Markdown), та же цена.
  Нужна, когда магазин не пускает программу (проверено: Ситилинк открылся, Ozon и eBay — нет).
Ответы поиска хранятся сутки в DATA_DIR/dsearch (повторный поиск не платит второй раз).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..analysis.llm import DEEPSEEK_KEY_FILE, BillingError, LLMError, _fail, _post, read_key
from .yandex import Hit

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
USD_PER_CALL = 0.00075                      # за успешный запрос поиска или открытия страницы (27.10.2026)
COUNT = int(os.environ.get("DEEPSEEK_SEARCH_COUNT", "50"))
PARALLEL = int(os.environ.get("DEEPSEEK_SEARCH_PARALLEL", "8"))
RU_IP = os.environ.get("DEEPSEEK_SEARCH_RU_IP", "77.88.8.8")     # IP в России — выдача «как из России»
CACHE_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data")) / "dsearch"
CACHE_HOURS = float(os.environ.get("DEEPSEEK_SEARCH_CACHE_HOURS", "24"))


@dataclass
class Stats:
    searches: int = 0
    fetches: int = 0
    cached: int = 0
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def usd(self) -> float:
        return round((self.searches + self.fetches) * USD_PER_CALL, 5)


def _headers() -> dict:
    return {"Authorization": f"Bearer {read_key(DEEPSEEK_KEY_FILE, 'DeepSeek')}", "Content-Type": "application/json"}


def _cache_file(q: str, loc: str | None) -> Path:
    return CACHE_DIR / (hashlib.sha1(f"{loc or ''}|{COUNT}|{q}".encode("utf-8")).hexdigest() + ".json")


def _cache_get(q: str, loc: str | None) -> list[Hit] | None:
    try:
        d = json.loads(_cache_file(q, loc).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - d.get("at", 0) > CACHE_HOURS * 3600:
        return None
    return [Hit(**h) for h in d.get("hits", [])]


def _cache_put(q: str, loc: str | None, hits: list[Hit]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(q, loc).write_text(json.dumps({"q": q, "loc": loc, "at": time.time(),
                                                   "hits": [asdict(h) for h in hits]}, ensure_ascii=False),
                                       encoding="utf-8")
    except OSError:
        pass


def search(query: str, stats: Stats, localization: str | None = None) -> list[Hit]:
    body = {"query": query[:128], "count": COUNT}
    if localization:
        body["localization"] = localization
    data = _post(SEARCH_URL, _headers(), body, 60, "DeepSeek (поиск)")
    results = data.get("results") or []
    if results:
        with stats.lock:
            stats.searches += 1
    hits = []
    for i, r in enumerate(results):
        url = (r.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        host = (urlparse(url).hostname or "").lower()
        text = " ".join(f"{r.get('snippet') or ''} {r.get('summary') or ''}".split())
        hits.append(Hit(url=url, domain=host[4:] if host.startswith("www.") else host, title=" ".join((r.get("title") or "").split()),
                        snippet=text[:900], query=query, position=i + 1))
    return hits


def search_many(queries: dict[str, str | None], stats: Stats, progress=None) -> dict[str, list[Hit]]:
    """{запрос: IP для локализации или None} → {запрос: результаты}. Ошибка по одному запросу не останавливает
    остальные; закончились деньги на счёте DeepSeek — остальные запросы не отправляются."""
    progress = progress or (lambda *_: None)
    out: dict[str, list[Hit]] = {}
    todo = []
    for q, loc in queries.items():
        cached = _cache_get(q, loc)
        if cached is not None:
            out[q] = cached
            stats.cached += 1
        else:
            todo.append((q, loc))
    stop = threading.Event()

    def one(item):
        q, loc = item
        if stop.is_set():
            return q, [], None
        try:
            hits = search(q, stats, loc)
            _cache_put(q, loc, hits)
            return q, hits, None
        except BillingError as exc:
            stop.set()
            return q, [], exc
        except LLMError as exc:
            return q, [], exc

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        try:
            for i, (q, hits, exc) in enumerate(pool.map(one, todo)):
                out[q] = hits
                if exc:
                    stats.errors.append(f"DeepSeek, «{q}»: {exc}")
                if i % 10 == 0 or i == len(todo) - 1:
                    progress(f"DeepSeek, поиск: {i + 1} из {len(todo)}", len(out), len(queries))
        except BaseException:
            stop.set()                  # задачу остановили — оставшиеся запросы не отправлять
            raise
    return out


def _page_cache(url: str) -> Path:
    return CACHE_DIR / "pages" / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def fetch_text(url: str, stats: Stats, timeout: float = 60) -> tuple[str, str]:
    """(заголовок, текст страницы в Markdown). Страница не открылась — LLMError. Без долгих повторов
    (в отличие от поиска): не открылась — значит, не открылась; одна повторная попытка — при перегрузке.
    Открытые страницы хранятся сутки (повторный поиск цен не платит за них второй раз)."""
    cache = _page_cache(url)
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - d.get("at", 0) < CACHE_HOURS * 3600:
            return d.get("title") or "", d.get("content") or ""
    except (OSError, ValueError):
        pass
    title, content = _fetch_text(url, stats, timeout)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"url": url, "at": time.time(), "title": title, "content": content},
                                    ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return title, content


def _fetch_text(url: str, stats: Stats, timeout: float) -> tuple[str, str]:
    last = ""
    for attempt in range(2):
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=20), trust_env=True) as http:
                r = http.post(FETCH_URL, headers=_headers(), json={"url": url})
        except httpx.HTTPError as exc:
            last = f"нет связи с DeepSeek ({type(exc).__name__})"
            continue
        if r.status_code == 200:
            data = r.json()
            content = data.get("content") or ""
            if content:
                with stats.lock:
                    stats.fetches += 1
            return data.get("title") or "", content
        if r.status_code in (429, 500, 502, 503) and attempt == 0:
            time.sleep(3)
            continue
        if r.status_code in (403, 404):
            raise LLMError("DeepSeek не смог открыть страницу")
        raise _fail(r.status_code, r.text[:300], "DeepSeek (страница)")
    raise LLMError(last or "DeepSeek не смог открыть страницу")
