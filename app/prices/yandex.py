"""
Yandex Search API (текстовый поиск) — российская выдача для поиска цен.

Два режима:
  * отложенный (по умолчанию, ~30 ₽ за 1000 запросов): запрос → номер операции → ответ через 5 минут — несколько часов;
  * мгновенный (~488 ₽ за 1000): ответ сразу. Используется для «Срочно» и для запросов, отложенный ответ на которые
    не пришёл за PRICES_ASYNC_TIMEOUT_MIN минут.
Ключ — файл YANDEX_KEY_FILE_IN_CONTAINER (заголовок «Authorization: Api-Key …»), каталог указывать не нужно.
Яндекс — российский сервис: запросы идут напрямую, без прокси Windows (если напрямую нет связи — через прокси).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from ..analysis.llm import LLMError, read_key

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/"
KEY_FILE = os.environ.get("YANDEX_KEY_FILE_IN_CONTAINER", "/run/secrets/yandex")
REGION = os.environ.get("YANDEX_REGION", "225")     # 225 — Россия
PER_QUERY = int(os.environ.get("YANDEX_RESULTS_PER_QUERY", "20"))
PARALLEL = int(os.environ.get("YANDEX_PARALLEL", "4"))        # одновременных запросов к Яндексу
POLL_SEC = int(os.environ.get("YANDEX_POLL_SEC", "30"))       # как часто спрашивать готовность отложенных
# Ответы Яндекса хранятся сутки: повторный поиск цен (и продолжение после перезапуска) не платит второй раз;
# номера ещё не готовых отложенных операций — тоже, чтобы после перезапуска дождаться их, а не спрашивать заново.
CACHE_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data")) / "yandex"
CACHE_HOURS = float(os.environ.get("YANDEX_CACHE_HOURS", "24"))

# ₽ за один запрос, с НДС (aistudio.yandex.ru/docs/search-api/pricing, 27.10.2026; дневной тариф).
RUB_SYNC = 0.488
RUB_ASYNC = 0.0305


@dataclass
class Hit:
    url: str
    domain: str
    title: str
    snippet: str
    query: str
    position: int


@dataclass
class Stats:
    sync: int = 0
    async_: int = 0
    cached: int = 0
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def rub(self) -> float:
        return round(self.sync * RUB_SYNC + self.async_ * RUB_ASYNC, 2)


def _body(query: str) -> dict:
    return {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query, "familyMode": "FAMILY_MODE_NONE",
                      "fixTypoMode": "FIX_TYPO_MODE_OFF"},
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(PER_QUERY), "docsInGroup": "1"},
            "maxPassages": "3", "region": REGION, "l10n": "LOCALIZATION_RU", "responseFormat": "FORMAT_XML"}


def _client(direct: bool) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60, connect=15), trust_env=not direct)


_route = {"direct": True}       # куда пошёл последний удачный запрос — туда и следующие


def _request(method: str, url: str, key: str, body: dict | None = None) -> dict:
    """Сначала напрямую, при отсутствии связи — через прокси из настроек; 429 и 5xx — повтор с паузой."""
    last: Exception | str | None = None
    for attempt in range(5):
        for direct in (_route["direct"], not _route["direct"]):
            try:
                with _client(direct) as http:
                    r = http.request(method, url, json=body, headers={"Authorization": f"Api-Key {key}"})
            except httpx.HTTPError as exc:
                last = exc
                continue
            _route["direct"] = direct
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                raise LLMError(f"Яндекс не принял ключ ({r.status_code}): {r.text[:200]}")
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                break
            raise LLMError(f"Яндекс ответил {r.status_code}: {r.text[:300]}")
        else:
            break               # оба пути без связи — дальше пробовать бесполезно
        time.sleep(2 * (attempt + 1))
    raise LLMError(f"нет связи с Яндексом ({last})")


def parse_xml(raw: str, query: str) -> list[Hit]:
    """Результаты из XML Яндекса: ссылка, домен, заголовок, фрагменты текста."""
    hits = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return hits
    err = root.find(".//response/error")
    if err is not None and (err.text or "").strip():
        code = err.get("code", "")
        if code == "15":            # «ничего не найдено»
            return hits
        raise LLMError(f"Яндекс: {err.text.strip()} (код {code})")
    for i, doc in enumerate(root.iter("doc")):
        url = (doc.findtext("url") or "").strip()
        if not url:
            continue
        title = "".join(doc.find("title").itertext()) if doc.find("title") is not None else ""
        passages = [" ".join(p.itertext()) for p in doc.iter("passage")]
        headline = doc.findtext("headline") or ""
        hits.append(Hit(url=url, domain=(doc.findtext("domain") or "").strip(), title=" ".join(title.split()),
                        snippet=" ".join(" ".join(passages + [headline]).split()), query=query, position=i + 1))
    return hits


def _decode(data: dict) -> str:
    raw = data.get("rawData") or (data.get("response") or {}).get("rawData") or ""
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return str(raw)


def search_sync(query: str, stats: Stats) -> list[Hit]:
    key = read_key(KEY_FILE, "Яндекс")
    data = _request("POST", SEARCH_URL, key, _body(query))
    with stats.lock:
        stats.sync += 1
    return parse_xml(_decode(data), query)


def _cache_file(q: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(f"{REGION}|{PER_QUERY}|{q}".encode("utf-8")).hexdigest() + ".json")


def _cache_get(q: str) -> dict:
    try:
        d = json.loads(_cache_file(q).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if time.time() - d.get("at", 0) < CACHE_HOURS * 3600 else {}


def _cache_put(q: str, **data) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(q).write_text(json.dumps({"q": q, "at": time.time(), **data}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _parse_op(op: dict, q: str, stats: Stats) -> list[Hit]:
    if op.get("error"):
        stats.errors.append(f"«{q}»: {op['error']}")
        return []
    try:
        return parse_xml(_decode(op.get("response") or {}), q)
    except LLMError as exc:
        stats.errors.append(f"«{q}»: {exc}")
        return []


def search_many(queries: list[str], *, urgent: bool, timeout_min: float, stats: Stats, progress=None) -> dict[str, list[Hit]]:
    """Все запросы разом. urgent — сразу мгновенный режим. Иначе — отложенный; ответы, не пришедшие за timeout_min,
    переспрашиваются мгновенным. Возвращает {запрос: результаты}."""
    progress = progress or (lambda *_: None)
    key = read_key(KEY_FILE, "Яндекс")
    out: dict[str, list[Hit]] = {}
    pending: dict[str, str] = {}
    total = len(queries)
    for q in queries:
        c = _cache_get(q)
        if "hits" in c:
            out[q] = [Hit(**h) for h in c["hits"]]
        elif c.get("op") and not urgent:
            pending[q] = c["op"]            # отложенный запрос, отправленный до перезапуска
    stats.cached = len(out)
    halt = threading.Event()            # задачу остановили — оставшиеся запросы не отправлять
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        try:
            if not urgent and queries:
                def submit(q: str):
                    if halt.is_set():
                        return q, {}, LLMError("остановлено")
                    try:
                        return q, _request("POST", ASYNC_URL, key, _body(q)), None
                    except LLMError as exc:
                        return q, {}, exc
                for q, op, exc in pool.map(submit, [q for q in queries if q not in out and q not in pending]):
                    if exc:
                        stats.errors.append(f"отложенный запрос «{q}»: {exc}")
                    elif op.get("id"):
                        pending[q] = op["id"]
                        stats.async_ += 1
                        _cache_put(q, op=op["id"])
                if not pending and stats.errors and "ключ" in stats.errors[-1]:
                    raise LLMError(stats.errors[-1])
                started = time.monotonic()

                def poll(item):
                    q, op_id = item
                    if halt.is_set():
                        return q, {}, LLMError("остановлено")
                    try:
                        return q, _request("GET", OPERATION_URL + op_id, key), None
                    except LLMError as exc:
                        return q, {}, exc
                while pending and time.monotonic() - started < timeout_min * 60:
                    pause_until = time.monotonic() + min(POLL_SEC, max(5, timeout_min * 60 - (time.monotonic() - started)))
                    while True:             # ждём по секунде: счётчик на странице идёт, остановка задачи — сразу
                        waited = int(time.monotonic() - started)
                        progress(f"Яндекс, отложенный режим: готово {total - len(pending)} из {total} "
                                 f"(ждём {waited // 60} мин {waited % 60:02d} с, не дольше {timeout_min:g} мин)",
                                 total - len(pending), total)
                        if time.monotonic() >= pause_until:
                            break
                        time.sleep(min(1.0, max(pause_until - time.monotonic(), 0)))
                    for q, op, exc in pool.map(poll, list(pending.items())):
                        if exc:
                            continue                    # операция ещё есть у Яндекса — спросим в следующий раз
                        if op.get("done"):
                            pending.pop(q, None)
                            out[q] = _parse_op(op, q, stats)
                            if not op.get("error"):
                                _cache_put(q, hits=[asdict(h) for h in out[q]])
            rest = [q for q in queries if q not in out]

            def sync(q: str):
                if halt.is_set():
                    return q, [], LLMError("остановлено")
                try:
                    hits = search_sync(q, stats)
                    _cache_put(q, hits=[asdict(h) for h in hits])
                    return q, hits, None
                except LLMError as exc:
                    return q, [], exc
            for i, (q, hits, exc) in enumerate(pool.map(sync, rest)):
                if exc:
                    stats.errors.append(f"«{q}»: {exc}")
                out[q] = hits
                progress(f"Яндекс, мгновенный режим: {i + 1} из {len(rest)}", total - len(rest) + i + 1, total)
        except BaseException:
            halt.set()
            raise
    return out
