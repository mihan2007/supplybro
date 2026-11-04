"""
Обработчик задач (отдельный процесс, сервис «worker» в compose.yml).

Берёт задачи из очереди в базе по одной и выполняет их; ход работы пишет в базу —
страница сайта показывает его. Запуск: python -m app.worker

Виды задач:
  prepare — подготовка документов без ИИ (этап 1); с параметром analyze=true после неё ставится анализ;
  analyze — анализ документов ИИ (этап 3); после него в очереди ставятся запись в NocoDB (NOCODB_WRITE=1) и поиск
            цен (AUTO_PRICES=1);
  noco    — запись анализа в NocoDB (после анализа — сама, или «Записать всё равно» при конфликте); своя очередь;
  noco_prices — дописать найденные цены в спецификацию NocoDB после записи анализа (та же очередь, следом);
  prices  — поиск цен по позициям анализа (app/prices): DeepSeek (Россия и зарубеж; Яндекс — на выбор), запись
            в xlsx и NocoDB.
            Идёт отдельной очередью (своим потоком): отложенный поиск Яндекса ждёт ответов до получаса
            и не должен задерживать подготовку и анализ других конкурсов.

Отмена и пауза (меню «В работе» на странице, app/jobctl.py): ждущая задача отменяется / откладывается сразу;
выполняющаяся — останавливается на ближайшем шаге (progress проверяет пометку не реже раза в секунду).
Приостановленная после «Продолжить» начинается заново, но готовое не повторяется: файлы подготовлены (кэш по
содержимому), ответы моделей — в кэше задачи, результаты поиска — в суточном кэше, недописанная запись в
NocoDB доделывается в ту же таблицу.
"""

from __future__ import annotations

import os
import threading
import time
import traceback

from . import db, jobctl
from .config import load_clients
from .docs.pipeline import prepare
from .tenders import folder_path

POLL_SECONDS = 1.5
NOCODB_WRITE = os.environ.get("NOCODB_WRITE", "1").strip() not in ("0", "false", "no", "")
# После анализа сразу искать цены (решение пользователя 28.10.2026): 1 — да, 0 — только по кнопке.
AUTO_PRICES = os.environ.get("AUTO_PRICES", "1").strip() not in ("0", "false", "no", "")


def _noco_text(noco: dict) -> str:
    if not noco:
        return ""
    if noco.get("status") == "saved":
        return (f"NocoDB: запись {noco['record_id']} {'создана' if noco['action'] == 'created' else 'обновлена'}"
                + (f" (отдельно от двойника {noco['conflict'].get('internal') or noco['conflict'].get('record_id')})"
                   if noco.get("separate") and noco.get("conflict") else ""))
    if noco.get("status") == "conflict":
        return "NocoDB: не записано — нужно подтверждение (см. блок «Анализ»)"
    if noco.get("status") == "error":
        return "NocoDB: ошибка записи — " + noco.get("text", "")[:200]
    return ""


def _save_noco(client, job_folder: str, folder, result: dict, force: bool, progress) -> dict:
    from .analysis import noco_write
    from .analysis.run import load_result, save_result
    try:
        noco = noco_write.save(client, job_folder, result, force=force, progress=progress)
    except Exception as exc:  # запись в NocoDB не должна терять сам анализ
        traceback.print_exc()
        noco = {"status": "error", "text": f"{exc}"[:500], "at": time.time()}
    # Пока шла запись (у нового конкурса — минуты), поиск цен мог уже дописать цены в analysis.json —
    # сохраняем только сведения о записи в NocoDB в свежую версию файла, не затирая цены.
    fresh = load_result(folder)
    if fresh and fresh.get("analyzed_at") == result.get("analyzed_at"):
        result = fresh
    result["noco"] = noco
    save_result(folder, result)
    return noco


def _chain(client_id: str, folder: str, kind: str, params: dict | None = None) -> bool:
    """Следующая задача цепочки (после подготовки — анализ, после анализа — запись и поиск цен). Если такую же
    пользователь приостановил — она остаётся на паузе (False)."""
    job_id = db.enqueue(client_id, folder, kind, params, auto=True)
    return (db.get_job(job_id) or {}).get("status") != "paused"


PAUSED_NOTE = " — на паузе, продолжите в меню «В работе»"


def run_job(job) -> str:
    client = load_clients().get(job["client_id"])
    if client is None:
        raise RuntimeError(f"клиент не найден: {job['client_id']}")
    folder = folder_path(client, job["folder"])
    params = db.job_params(job)
    last = {"t": 0.0, "check": 0.0}

    def check() -> None:
        """Не просит ли пользователь остановить задачу (из меню «В работе»). Не чаще раза в секунду."""
        now = time.monotonic()
        if now - last["check"] < 1.0:
            return
        last["check"] = now
        action = db.job_control(job["id"])
        if action:
            raise jobctl.JobStopped(action)

    def progress(step: str, fraction: float) -> None:
        check()
        now = time.monotonic()
        if now - last["t"] >= 0.5 or fraction >= 0.97:  # не чаще двух раз в секунду
            last["t"] = now
            db.update_job(job["id"], step=step[:200], progress=round(min(max(fraction, 0), 1), 3))

    progress.check = check           # для ожидания ответов моделей и поиска (jobctl.result)
    cache = jobctl.cache_dir(job["client_id"], job["folder"], job["kind"])

    if job["kind"] == "prepare":
        summary = prepare(client.id, job["folder"], folder, progress)
        if params.get("analyze"):
            ok = _chain(client.id, job["folder"], "analyze", {"model": params["model"]} if params.get("model") else None)
            summary += ". Анализ поставлен в очередь" if ok else ". Анализ" + PAUSED_NOTE
        if params.get("changes"):
            # Документы дозагрузили в уже разобранный конкурс (0.9.0): разбор изменений — следом, в этой же очереди
            # (если ещё идёт анализ — после него, и разбор сравнит с его результатом).
            ok = _chain(client.id, job["folder"], "changes", {"by": params.get("by") or ""})
            summary += ". Разбор изменений поставлен в очередь" if ok else ". Разбор изменений" + PAUSED_NOTE
        return summary

    if job["kind"] == "changes":
        from .analysis.changes import mark_prices_queued, process
        summary, entry = process(client.id, job["folder"], folder, progress, by=params.get("by") or "",
                                 cache_dir=cache)
        need = (entry or {}).get("prices", {}).get("positions") or []
        if need and AUTO_PRICES:
            from .prices.run import load_report
            if load_report(folder):
                # Цены уже искали — ищем только по новым позициям и позициям с другим названием / маркой.
                active = db.active_job(client.id, job["folder"], "prices")
                if active and not (active["status"] == "queued" and db.job_params(active).get("only")):
                    summary += ". Цены по изменённым позициям найдёт поиск цен, который уже стоит в очереди"
                else:
                    only = list(dict.fromkeys((db.job_params(active).get("only") if active else []) + need))
                    if _chain(client.id, job["folder"], "prices", {"only": only}):
                        mark_prices_queued(folder, entry["id"])
                        summary += f". Поиск цен по позициям № {', '.join(need[:15])} поставлен в очередь"
            else:
                summary += ". Цены ещё не искали — «Искать цены» найдёт и для новых позиций"
        return summary

    if job["kind"] == "analyze":
        from .analysis.run import analyze, load_result
        prev = load_result(folder)
        if prev and prev.get("positions") and not prev.get("noco") \
                and float(prev.get("analyzed_at") or 0) > float(job["created_at"] or 0):
            # Анализ этой задачи уже был сделан, обработчик перезапустили на записи в NocoDB — не платим за
            # анализ второй раз и продолжаем с записи (те же позиции — сайт узнает свою таблицу спецификации).
            result = prev
            summary = "Анализ был готов до перезапуска обработчика — продолжено с записи в NocoDB"
        else:
            summary, result = analyze(client.id, job["folder"], folder, progress, params.get("model"), cache_dir=cache)
        # Запись в NocoDB и поиск цен — в своих очередях (у нового конкурса запись идёт 10–20 минут: программа
        # агента настраивает представления спецификации по колонке на запрос), анализ следующих конкурсов не ждёт.
        if NOCODB_WRITE:
            ok = _chain(client.id, job["folder"], "noco")
            summary += ". Запись в NocoDB поставлена в очередь" if ok else ". Запись в NocoDB" + PAUSED_NOTE
        if AUTO_PRICES and result.get("positions"):
            ok = _chain(client.id, job["folder"], "prices")
            summary += ". Поиск цен запущен" if ok else ". Поиск цен" + PAUSED_NOTE
        return summary

    if job["kind"] == "noco":
        from .analysis.run import load_result
        result = load_result(folder)
        if not result:
            raise RuntimeError("анализа этого конкурса ещё нет — сначала запустите анализ")
        noco = _save_noco(client, job["folder"], folder, result, bool(params.get("force")), progress)
        if noco.get("status") == "error":
            raise RuntimeError(noco["text"])
        summary = _noco_text(noco)
        fresh = load_result(folder) or result
        if AUTO_PRICES and noco.get("status") == "saved" and fresh.get("positions") and not fresh.get("prices_at") \
                and not db.active_job(client.id, job["folder"], "prices"):
            # Цен ещё не искали — ищем и пишем в спецификацию (приостановленный пользователем поиск не трогаем).
            if _chain(client.id, job["folder"], "prices"):
                summary += ". Поиск цен поставлен в очередь"
        elif noco.get("status") == "saved" and fresh.get("prices_at") \
                and not db.active_job(client.id, job["folder"], "noco_prices"):
            # Цены нашлись, пока запись стояла на паузе или была отменена (спецификации ещё не было — в NocoDB
            # они не попали): дописываем следом.
            from .prices.run import load_report
            report = load_report(folder) or {}
            if (report.get("noco") or {}).get("status") in ("skipped", "pending", "error") \
                    and _chain(client.id, job["folder"], "noco_prices"):
                summary += ". Цены допишутся в спецификацию следом"
        return summary

    if job["kind"] == "noco_changes":
        # Изменения документации — по кнопке «Записать изменения в NocoDB» (0.9.0).
        from .analysis.changes import write_noco
        return write_noco(client, job["folder"], folder, progress)

    if job["kind"] == "noco_prices":
        # Цены нашлись, пока анализ ещё записывался в NocoDB: эта задача стоит в той же очереди после записи
        # и дописывает цены в готовую спецификацию.
        from .prices.run import write_prices_noco
        return write_prices_noco(client, job["folder"], folder)

    if job["kind"] == "pricelist":
        # Прайсы, КП, счета поставщиков (0.8.0): все файлы в очереди этого конкурса — по одному.
        from .pricelists import process
        return process(client.id, job["folder"], folder, progress, cache_dir=cache)

    if job["kind"] == "noco_purch":
        # Цены поставщиков по кнопке «Записать в спецификацию» — в колонки «Отдел закупок».
        from .pricelists import write_noco
        return write_noco(client, job["folder"], folder, progress)

    if job["kind"] == "prices":
        from .prices.run import find_prices
        summary, _report = find_prices(client, job["folder"], folder, progress, urgent=bool(params.get("urgent")),
                                       abroad=params.get("abroad", True) is not False, engine=params.get("engine"),
                                       cache_dir=cache, only=params.get("only") or None)
        return summary

    raise RuntimeError(f"неизвестный вид задачи: {job['kind']}")


PRICE_KINDS = ("prices",)
NOCO_KINDS = ("noco", "noco_prices", "noco_purch", "noco_changes")
PRICELIST_KINDS = ("pricelist",)          # прайсы поставщиков — своя очередь: не ждут долгий поиск цен в интернете


_current: dict[str, int | None] = {}     # очередь → выполняемая задача (для сигнала «обработчик жив»)
HEARTBEAT_SECONDS = 10


def heartbeats() -> None:
    """Раз в 10 с каждая очередь отмечается в базе: страница видит, что обработчик жив, даже когда задача
    долго не меняет шаг (например, NocoDB ограничивает частоту запросов)."""
    while True:
        for name, job_id in list(_current.items()):
            try:
                db.heartbeat(name, job_id)
            except Exception:
                traceback.print_exc()
        time.sleep(HEARTBEAT_SECONDS)


def _stopped(job, action: str) -> None:
    """Задачу остановили по просьбе пользователя: отменена или на паузе (с шага, где остановилась)."""
    cur = db.get_job(job["id"]) or {}
    where = f" на шаге «{cur['step']}»" if cur.get("step") else ""
    if action == "pause":
        db.update_job(job["id"], status="paused", control="", message=f"Приостановлена{where}")
        print(f"Задача {job['id']} приостановлена{where}", flush=True)
    else:
        db.update_job(job["id"], status="canceled", control="", message=f"Отменена{where}", finished_at=time.time())
        print(f"Задача {job['id']} отменена{where}", flush=True)


def lane(name: str, only: tuple[str, ...] = (), skip: tuple[str, ...] = ()) -> None:
    """Очередь задач: берёт задачи своих видов по одной и выполняет их."""
    _current[name] = None
    while True:
        try:
            job = db.claim_next(only=only, skip=skip)
        except Exception:
            traceback.print_exc()
            time.sleep(5)
            continue
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        _current[name] = job["id"]
        db.heartbeat(name, job["id"])
        print(f"Задача {job['id']}: {job['kind']} — {job['folder']}", flush=True)
        try:
            summary = run_job(job)
            db.update_job(job["id"], status="done", step="Готово", progress=1, message=summary,
                          finished_at=time.time(), control="")
            jobctl.drop_cache(job["client_id"], job["folder"], job["kind"])   # следующий запуск — заново
            print(f"Задача {job['id']} готова: {summary}", flush=True)
        except jobctl.JobStopped as stop:
            _stopped(job, stop.action)
        except Exception as exc:
            traceback.print_exc()
            db.update_job(job["id"], status="error", message=f"{exc}"[:500], finished_at=time.time(), control="")
        _current[name] = None
        db.heartbeat(name, None)


def main() -> None:
    db.init()
    restored = db.recover_interrupted()
    print(f"Обработчик запущен. Возвращено в очередь прерванных задач: {restored}", flush=True)
    jobctl.prune_cache()
    # Четыре очереди: подготовка и анализ; запись в NocoDB (долгая у новых конкурсов); поиск цен (ждёт ответов поиска);
    # прайсы поставщиков (0.8.0).
    for name in ("docs", "nocodb", "prices", "pricelists"):
        _current[name] = None
        db.heartbeat(name, None)
    threading.Thread(target=heartbeats, name="heartbeat", daemon=True).start()
    threading.Thread(target=lane, args=("prices",), kwargs={"only": PRICE_KINDS}, name="prices", daemon=True).start()
    threading.Thread(target=lane, args=("nocodb",), kwargs={"only": NOCO_KINDS}, name="nocodb", daemon=True).start()
    threading.Thread(target=lane, args=("pricelists",), kwargs={"only": PRICELIST_KINDS}, name="pricelists",
                     daemon=True).start()
    lane("docs", skip=PRICE_KINDS + NOCO_KINDS + PRICELIST_KINDS)


if __name__ == "__main__":
    main()
