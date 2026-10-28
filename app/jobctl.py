"""
Остановка задачи по просьбе пользователя (меню «В работе»: «Пауза», «Отменить») и кэш ответов моделей задачи.

Как останавливается задача, которая уже идёт: страница ставит в базе пометку jobs.control ('cancel' / 'pause'),
обработчик проверяет её при каждом сообщении о ходе работы (progress) и пока ждёт ответов моделей и поиска
(не реже раза в секунду), и бросает JobStopped. Прерывать посреди запроса нельзя — остановка наступает на
ближайшем шаге: обычно за секунды.

Кэш ответов моделей: ответы Claude / DeepSeek одной задачи (анализ конкурса, проверка цен) сохраняются на диск.
Если задачу приостановили, отменили, она упала с ошибкой или обработчик перезапустили — при следующем запуске
те же вопросы с теми же документами повторно не оплачиваются. После успешного окончания кэш удаляется
(«Повторить анализ» спрашивает модель заново).
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .db import DATA_DIR

CACHE_ROOT = DATA_DIR / "jobcache"
CACHE_DAYS = 3


class JobStopped(BaseException):
    """Задачу просят остановить (action: 'cancel' или 'pause').

    BaseException, а не Exception — чтобы по пути её не перехватили обработчики ошибок вида `except Exception`
    (ошибка одной страницы, одного артикула, повтор запроса к NocoDB): остановка должна дойти до очереди."""

    def __init__(self, action: str):
        super().__init__(action)
        self.action = action


def checker(progress):
    """Функция проверки «не просят ли остановиться» из progress обработчика (у прочих вызовов — пустая)."""
    return getattr(progress, "check", None) or (lambda: None)


def result(fut: cf.Future, check, poll: float = 1.0):
    """fut.result(), но пока ждём — раз в poll секунд проверяем, не просят ли остановить задачу."""
    while not fut.done():
        cf.wait([fut], timeout=poll)
        if not fut.done():
            check()
    return fut.result()


def stop_pool(pool: cf.ThreadPoolExecutor) -> None:
    """Задачу остановили: ждущие в пуле вызовы не запускать (идущие доработают сами и сохранятся в кэш)."""
    pool.shutdown(wait=False, cancel_futures=True)


# ---------- кэш ответов моделей задачи ----------

def cache_dir(client_id: str, folder: str, kind: str) -> Path:
    """Папка кэша для «вида задачи по конкурсу» (а не для одной задачи): приостановленная и запущенная заново,
    упавшая и повторённая — пользуются одними ответами."""
    h = hashlib.sha1(f"{client_id}\0{folder}".encode("utf-8")).hexdigest()[:16]
    return CACHE_ROOT / f"{h}-{kind}"


def drop_cache(client_id: str, folder: str, kind: str) -> None:
    shutil.rmtree(cache_dir(client_id, folder, kind), ignore_errors=True)


def prune_cache(days: float = CACHE_DAYS) -> int:
    """Старые кэши (задача так и не была доделана) — удалить. Вызывается при запуске обработчика."""
    if not CACHE_ROOT.is_dir():
        return 0
    n, limit = 0, time.time() - days * 86400
    for d in CACHE_ROOT.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < limit:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


class AnswerCache:
    """Ответы моделей на диске: ключ — хеш всего запроса (модель, системный текст, документы, задание)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def key(*parts) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(json.dumps(p, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def get(self, key: str):
        f = self.path / f"{key}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))["answer"]
        except (OSError, ValueError, KeyError):
            return None

    def put(self, key: str, answer) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            tmp = self.path / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps({"at": time.time(), "answer": answer}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path / f"{key}.json")
            os.utime(self.path)            # папка «свежая» — prune_cache её не удалит
        except OSError:
            pass                           # кэш — только экономия: без него задача всё равно выполнится
