"""
Скачивание страниц магазинов.

* Обычный запрос (httpx) с заголовками обычного браузера; российские сайты — напрямую, зарубежные — через
  прокси Windows (OUTBOUND_PROXY), если напрямую не открываются.
* Если на странице нет цены и похоже, что её рисует JavaScript, — страница открывается встроенным браузером
  Chromium без окна (если он есть в сборке) и берётся итоговый HTML.
* Не больше FETCH_PER_DOMAIN одновременных запросов к одному сайту, всего — FETCH_PARALLEL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
MAX_BYTES = 4_000_000
PER_DOMAIN = int(os.environ.get("FETCH_PER_DOMAIN", "2"))
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
CONNECT_TIMEOUT = float(os.environ.get("FETCH_CONNECT_TIMEOUT", "6"))
READ_TIMEOUT = float(os.environ.get("FETCH_READ_TIMEOUT", "15"))
BROWSER_TIMEOUT = int(os.environ.get("FETCH_BROWSER_TIMEOUT", "25"))
# Ошибки «сайт не отвечает по этому пути» — пробуем другой путь (напрямую ↔ через прокси); «сайт отвечает медленно» —
# второй раз не ждём.
ROUTE_ERRORS = ("ConnectTimeout", "ConnectError", "ProxyError", "PoolTimeout")
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

_domain_locks: dict[str, threading.Semaphore] = {}
_lock = threading.Lock()


@dataclass
class Page:
    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    error: str = ""
    via: str = ""          # direct / proxy / browser


def _sem(domain: str) -> threading.Semaphore:
    with _lock:
        return _domain_locks.setdefault(domain, threading.Semaphore(PER_DOMAIN))


def _get(url: str, use_proxy: bool) -> Page:
    page = Page(url=url, via="proxy" if use_proxy else "direct")
    try:
        with httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT), follow_redirects=True, headers=HEADERS,
                          proxy=PROXY if (use_proxy and PROXY) else None, trust_env=False) as http:
            with http.stream("GET", url) as r:
                page.status, page.final_url = r.status_code, str(r.url)
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and "xml" not in ctype and ctype:
                    page.error = f"не страница ({ctype.split(';')[0]})"
                    return page
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                raw = b"".join(chunks)
                enc = r.encoding or "utf-8"
                try:
                    page.html = raw.decode(enc, "replace")
                except LookupError:
                    page.html = raw.decode("utf-8", "replace")
                if r.status_code >= 400:
                    page.error = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        page.error = f"{type(exc).__name__}"
    return page


def fetch(url: str, *, foreign: bool = False) -> Page:
    """Страница целиком. foreign — зарубежный сайт: сначала через прокси."""
    domain = urlparse(url).hostname or ""
    with _sem(domain):
        order = (True, False) if foreign and PROXY else (False, True) if PROXY else (False,)
        page = None
        for use_proxy in order:
            page = _get(url, use_proxy)
            if page.html and not page.error:
                return page
            if page.status in (403, 404, 410, 451):
                break           # сайт ответил отказом — через прокси не лучше
            if page.error and page.error not in ROUTE_ERRORS:
                break           # сайт отвечает, но медленно или не страницей — второй раз не ждём
        return page


def browser_html(url: str, *, foreign: bool = False, timeout: int = BROWSER_TIMEOUT) -> Page:
    """Страница, нарисованная браузером (для цен, которые подставляет JavaScript)."""
    page = Page(url=url, via="browser")
    if not CHROMIUM:
        page.error = "встроенного браузера нет в сборке"
        return page
    args = [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--blink-settings=imagesEnabled=false", f"--user-agent={UA}", "--lang=ru-RU",
            "--virtual-time-budget=8000", "--dump-dom", url]
    # Российские магазины — напрямую (через зарубежный прокси часть из них не открывается), зарубежные — через прокси.
    args.insert(1, f"--proxy-server={PROXY}" if foreign and PROXY else "--no-proxy-server")
    domain = urlparse(url).hostname or ""
    with _sem(domain):
        try:
            res = subprocess.run(args, capture_output=True, timeout=timeout)
            page.html = res.stdout.decode("utf-8", "replace")[:MAX_BYTES]
            page.status = 200 if page.html else 0
            page.final_url = url
            if not page.html:
                page.error = "браузер вернул пустую страницу"
        except subprocess.TimeoutExpired:
            page.error = "браузер не дождался страницы"
        except OSError as exc:
            page.error = f"браузер не запустился: {exc}"
    return page


def polite_pause() -> None:
    time.sleep(0.3)
