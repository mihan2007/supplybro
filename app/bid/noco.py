"""Запись подготовленной заявки в NocoDB — только по кнопке «Записать в NocoDB» (это и есть подтверждение).

Как у агента (PREPARE_DOCS.md, раздел NocoDB): перечень документов — в колонку «Перечень документов заявки» записи
конкурса; заполненные формы (Word) — в колонку «ДС: декларативные формы». Структуру NocoDB сайт не меняет: если
какой-то колонки нет — она пропускается, на странице видно почему (создать — только с согласия пользователя).
Файлы в «ДС: декларативные формы»: прежние файлы ячейки сохраняются, кроме файлов с теми же именами (без приставки
«ЧЕРНОВИК ») — их заменяют новые.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from .. import db
from ..analysis.run import MSK
from ..tenders import norm_number
from .run import BID_DIR, docs_list_text, load_report, save_report

LIST_COLUMN = "Перечень документов заявки"
FORMS_COLUMN = "ДС: декларативные формы"


def _record(nc, nw, token: str, client_id: str, internal: str) -> int | None:
    link = db.noco_link(client_id, internal) or {}
    if link.get("record_id"):
        return link["record_id"]
    for v in nw._internal_variants(internal):
        found = nw._find(token, nc.FIELD_MAP["internal_number"], "eq", v)
        if found:
            return found[0]["Id"]
    return None


def _plain(title: str) -> str:
    return re.sub(r"^ЧЕРНОВИК\s+", "", title or "").strip().lower()


def write(client, folder_name: str, folder: Path, progress) -> str:
    from tools import nocodb_client as nc
    from ..analysis import noco_write as nw
    report = load_report(folder)
    if not report or not report.get("documents"):
        raise RuntimeError("документы заявки ещё не готовились — нажмите «Подготовить документы заявки»")
    internal = norm_number(folder_name) or folder_name
    paths = sorted(p for p in (folder / BID_DIR).rglob("*.docx") if not p.name.startswith("~$"))
    notes, done = [], []
    with nw._WRITE_LOCK:
        token = nw._configure(client)
        progress("NocoDB: запись конкурса", 0.1)
        record_id = _record(nc, nw, token, client.id, internal)
        if not record_id:
            raise RuntimeError(f"в NocoDB нет записи конкурса {internal} — сначала запишите анализ")
        cols = nc.get_table_columns(token, nc.TABLE_ID)
        if LIST_COLUMN in cols:
            progress("NocoDB: перечень документов заявки", 0.3)
            status, body = nc._request("PATCH", nc.RECORDS_URL, token,
                                       {"Id": record_id, LIST_COLUMN: docs_list_text(report)})
            if status != 200:
                raise RuntimeError(f"NocoDB: перечень не записан: HTTP {status} {str(body)[:200]}")
            done.append("перечень документов")
        else:
            notes.append(f"колонки «{LIST_COLUMN}» в таблице нет — перечень не записан")
        if FORMS_COLUMN in cols and paths:
            progress("NocoDB: файлы форм", 0.5)
            status, rec = nc._request("GET", f"{nc.RECORDS_URL}/{record_id}", token)
            old = (rec.get(FORMS_COLUMN) if status == 200 else None) or []
            if isinstance(old, str):
                try:
                    old = json.loads(old)
                except ValueError:
                    old = []
            new_names = {_plain(p.name) for p in paths}
            keep = [a for a in old if _plain(a.get("title", "")) not in new_names]
            uploaded = []
            for k, p in enumerate(paths, 1):
                progress(f"NocoDB: файл {k} из {len(paths)}", 0.5 + 0.4 * k / len(paths))
                uploaded.extend(nc.upload_file(token, str(p)))
            status, body = nc._request("PATCH", nc.RECORDS_URL, token, {"Id": record_id, FORMS_COLUMN: keep + uploaded})
            if status != 200:
                raise RuntimeError(f"NocoDB: файлы форм не записаны: HTTP {status} {str(body)[:200]}")
            done.append(f"файлов форм: {len(uploaded)}" + (f" (прежних оставлено {len(keep)})" if keep else ""))
        elif paths:
            notes.append(f"колонки «{FORMS_COLUMN}» в таблице нет — файлы не прикреплены")
    report["noco"] = {"status": "saved" if done else "skipped", "record_id": record_id, "done": done, "notes": notes,
                      "at": time.time(), "at_text": datetime.now(MSK).strftime("%d.%m.%Y %H:%M")}
    save_report(folder, report)
    return f"NocoDB, запись {record_id}: " + ("; ".join(done) or "ничего не записано") + (
        "; " + "; ".join(notes) if notes else "")
