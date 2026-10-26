"""
База сайта (SQLite в папке данных): очередь задач, сведения об обработанных документах
и поисковый индекс по их тексту.

Сайт и обработчик (worker) — два процесса, работают с одной базой; режим WAL позволяет
читать, пока другой процесс пишет.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data"))
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Очередь задач: подготовка документов, позже — анализ и т.п.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    folder      TEXT NOT NULL,              -- папка конкурса внутри папки клиента
    kind        TEXT NOT NULL,              -- 'prepare'
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued / running / done / error
    step        TEXT NOT NULL DEFAULT '',
    progress    REAL NOT NULL DEFAULT 0,    -- 0..1
    message     TEXT NOT NULL DEFAULT '',
    created_at  REAL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, id);
CREATE INDEX IF NOT EXISTS jobs_folder ON jobs(client_id, folder, id);

-- Документы конкурса: что извлечено из каждого файла (кэш по хешу содержимого).
CREATE TABLE IF NOT EXISTS doc_files (
    client_id    TEXT NOT NULL,
    folder       TEXT NOT NULL,
    rel_path     TEXT NOT NULL,             -- путь внутри папки конкурса, как на диске
    sha256       TEXT, size INTEGER, mtime REAL,
    kind         TEXT,                      -- docx / xlsx / pdf / image / text / archive / other
    status       TEXT,                      -- ok / ocr / skipped / error / archive
    pages        INTEGER DEFAULT 0,         -- страниц / листов
    chars        INTEGER DEFAULT 0,         -- символов текста
    ocr_pages    INTEGER DEFAULT 0,         -- распознано страниц
    hidden_chars INTEGER DEFAULT 0,         -- символов скрытого текста
    flags        TEXT DEFAULT '',           -- пометки через «; »
    text_path    TEXT DEFAULT '',           -- файл с текстом внутри папки конкурса
    error        TEXT DEFAULT '',
    extractor    TEXT DEFAULT '',           -- версия обработки (при смене — файл обрабатывается заново)
    updated_at   REAL,
    PRIMARY KEY (client_id, folder, rel_path)
);

-- Поисковый индекс по фрагментам текста (для чата и анализа).
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks USING fts5(
    content,
    client_id UNINDEXED, folder UNINDEXED, rel_path UNINDEXED, label UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


# ---------- очередь задач ----------

def enqueue(client_id: str, folder: str, kind: str = "prepare") -> int:
    """Ставит задачу в очередь. Если такая же уже ждёт — возвращает её (не плодит дубли)."""
    with connect() as con:
        row = con.execute(
            "SELECT id FROM jobs WHERE client_id=? AND folder=? AND kind=? AND status='queued' ORDER BY id DESC LIMIT 1",
            (client_id, folder, kind)).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO jobs(client_id, folder, kind, status, created_at) VALUES (?,?,?,?,?)",
            (client_id, folder, kind, "queued", time.time()))
        return cur.lastrowid


def claim_next() -> sqlite3.Row | None:
    """Берёт самую старую ждущую задачу и помечает её «выполняется» (одним действием)."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if row:
            con.execute("UPDATE jobs SET status='running', started_at=?, step='', progress=0 WHERE id=?",
                        (time.time(), row["id"]))
        con.execute("COMMIT")
        return row
    finally:
        con.close()


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def latest_job(client_id: str, folder: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE client_id=? AND folder=? ORDER BY id DESC LIMIT 1",
                          (client_id, folder)).fetchone()
        return dict(row) if row else None


def recover_interrupted() -> int:
    """После перезапуска обработчика: задачи, оборванные на середине, — снова в очередь."""
    with connect() as con:
        return con.execute("UPDATE jobs SET status='queued' WHERE status='running'").rowcount


# ---------- документы ----------

def doc_rows(client_id: str, folder: str) -> dict[str, dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM doc_files WHERE client_id=? AND folder=?", (client_id, folder)).fetchall()
        return {r["rel_path"]: dict(r) for r in rows}
