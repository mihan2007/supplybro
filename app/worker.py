"""
Обработчик задач (отдельный процесс, сервис «worker» в compose.yml).

Берёт задачи из очереди в базе по одной и выполняет их; ход работы пишет в базу —
страница сайта показывает его. Запуск: python -m app.worker
"""

from __future__ import annotations

import time
import traceback

from . import db
from .config import load_clients
from .docs.pipeline import prepare
from .tenders import folder_path

POLL_SECONDS = 1.5


def run_job(job) -> str:
    client = load_clients().get(job["client_id"])
    if client is None:
        raise RuntimeError(f"клиент не найден: {job['client_id']}")
    folder = folder_path(client, job["folder"])
    last = {"t": 0.0}

    def progress(step: str, fraction: float) -> None:
        now = time.monotonic()
        if now - last["t"] >= 0.5 or fraction >= 0.97:  # не чаще двух раз в секунду
            last["t"] = now
            db.update_job(job["id"], step=step[:200], progress=round(min(max(fraction, 0), 1), 3))

    if job["kind"] == "prepare":
        return prepare(client.id, job["folder"], folder, progress)
    raise RuntimeError(f"неизвестный вид задачи: {job['kind']}")


def main() -> None:
    db.init()
    restored = db.recover_interrupted()
    print(f"Обработчик запущен. Возвращено в очередь прерванных задач: {restored}", flush=True)
    while True:
        job = db.claim_next()
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        print(f"Задача {job['id']}: {job['kind']} — {job['folder']}", flush=True)
        try:
            summary = run_job(job)
            db.update_job(job["id"], status="done", step="Готово", progress=1, message=summary,
                          finished_at=time.time())
            print(f"Задача {job['id']} готова: {summary}", flush=True)
        except Exception as exc:
            traceback.print_exc()
            db.update_job(job["id"], status="error", message=f"{exc}"[:500], finished_at=time.time())


if __name__ == "__main__":
    main()
