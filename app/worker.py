"""
Обработчик задач (отдельный процесс, сервис «worker» в compose.yml).

Берёт задачи из очереди в базе по одной и выполняет их; ход работы пишет в базу —
страница сайта показывает его. Запуск: python -m app.worker

Виды задач:
  prepare — подготовка документов без ИИ (этап 1); с параметром analyze=true после неё ставится анализ;
  analyze — анализ документов ИИ (этап 3) и запись в NocoDB (если NOCODB_WRITE=1);
  noco    — только запись готового анализа в NocoDB (например, «Записать всё равно» при конфликте).
"""

from __future__ import annotations

import os
import time
import traceback

from . import db
from .config import load_clients
from .docs.pipeline import prepare
from .tenders import folder_path

POLL_SECONDS = 1.5
NOCODB_WRITE = os.environ.get("NOCODB_WRITE", "1").strip() not in ("0", "false", "no", "")


def _noco_text(noco: dict) -> str:
    if not noco:
        return ""
    if noco.get("status") == "saved":
        return f"NocoDB: запись {noco['record_id']} {'создана' if noco['action'] == 'created' else 'обновлена'}"
    if noco.get("status") == "conflict":
        return "NocoDB: не записано — нужно подтверждение (см. блок «Анализ»)"
    if noco.get("status") == "error":
        return "NocoDB: ошибка записи — " + noco.get("text", "")[:200]
    return ""


def _save_noco(client, job_folder: str, folder, result: dict, force: bool, progress) -> dict:
    from .analysis import noco_write
    from .analysis.run import save_result
    try:
        noco = noco_write.save(client, job_folder, result, force=force, progress=progress)
    except Exception as exc:  # запись в NocoDB не должна терять сам анализ
        traceback.print_exc()
        noco = {"status": "error", "text": f"{exc}"[:500], "at": time.time()}
    result["noco"] = noco
    save_result(folder, result)
    return noco


def run_job(job) -> str:
    client = load_clients().get(job["client_id"])
    if client is None:
        raise RuntimeError(f"клиент не найден: {job['client_id']}")
    folder = folder_path(client, job["folder"])
    params = db.job_params(job)
    last = {"t": 0.0}

    def progress(step: str, fraction: float) -> None:
        now = time.monotonic()
        if now - last["t"] >= 0.5 or fraction >= 0.97:  # не чаще двух раз в секунду
            last["t"] = now
            db.update_job(job["id"], step=step[:200], progress=round(min(max(fraction, 0), 1), 3))

    if job["kind"] == "prepare":
        summary = prepare(client.id, job["folder"], folder, progress)
        if params.get("analyze"):
            db.enqueue(client.id, job["folder"], "analyze")
            summary += ". Анализ поставлен в очередь"
        return summary

    if job["kind"] == "analyze":
        from .analysis.run import analyze
        summary, result = analyze(client.id, job["folder"], folder, progress)
        if NOCODB_WRITE:
            noco = _save_noco(client, job["folder"], folder, result, False, progress)
            summary += ". " + _noco_text(noco)
        return summary

    if job["kind"] == "noco":
        from .analysis.run import load_result
        result = load_result(folder)
        if not result:
            raise RuntimeError("анализа этого конкурса ещё нет — сначала запустите анализ")
        noco = _save_noco(client, job["folder"], folder, result, bool(params.get("force")), progress)
        if noco.get("status") == "error":
            raise RuntimeError(noco["text"])
        return _noco_text(noco)

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
