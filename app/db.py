"""
База сайта (SQLite в папке данных): очередь задач, сведения об обработанных документах
и поисковый индекс по их тексту.

Сайт и обработчик (worker) — два процесса, работают с одной базой; режим WAL позволяет
читать, пока другой процесс пишет.
"""

from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data"))
DB_PATH = DATA_DIR / "app.db"

# Кто ставит задачу (0.16.0, jobs.started_by — логин): на сайте — вошедший сотрудник (app/access.py, проходная),
# в обработчике — тот, кто запустил текущую задачу (следующие задачи цепочки — от его имени). Пусто — вход выключен
# или задачу поставила программа сама.
ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("supplybro_actor", default="")

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Очередь задач: подготовка документов, позже — анализ и т.п.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    folder      TEXT NOT NULL,              -- папка конкурса внутри папки клиента
    kind        TEXT NOT NULL,              -- 'prepare' / 'analyze' / 'noco'
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued / running / done / error / paused / canceled
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

-- Чат по документации конкурса: вопросы, ответы (с моделью, токенами, проверкой источников) и ошибки.
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    folder      TEXT NOT NULL,
    role        TEXT NOT NULL,              -- user / assistant / error
    content     TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL
);
CREATE INDEX IF NOT EXISTS chat_folder ON chat_messages(client_id, folder, id);

-- Очереди обработчика: когда каждая последний раз подавала признаки жизни и какую задачу выполняет.
CREATE TABLE IF NOT EXISTS worker_lanes (
    lane    TEXT PRIMARY KEY,                   -- docs / nocodb / prices
    pid     INTEGER,
    job_id  INTEGER,
    at      REAL
);

-- Какая запись NocoDB относится к конкурсу и создал ли её сайт (чужие записи без подтверждения не меняются).
CREATE TABLE IF NOT EXISTS noco_links (
    client_id       TEXT NOT NULL,
    internal        TEXT NOT NULL,              -- внутренний номер (К992)
    folder          TEXT NOT NULL DEFAULT '',
    record_id       INTEGER,
    created_by_site INTEGER NOT NULL DEFAULT 0,
    spec_table_id   TEXT DEFAULT '',
    updated_at      REAL,
    PRIMARY KEY (client_id, internal)
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
        cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("params", "TEXT NOT NULL DEFAULT ''"),   # с 0.2.0: параметры задачи (JSON)
                         ("updated_at", "REAL"),                    # с 0.5.6: последнее действие задачи
                         ("control", "TEXT NOT NULL DEFAULT ''"),   # с 0.5.8: просьба остановить: cancel / pause
                         ("started_by", "TEXT NOT NULL DEFAULT ''")):  # с 0.16.0: кто запустил (логин)
            if col not in cols:
                try:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass  # колонку только что добавил другой процесс (сайт и обработчик стартуют одновременно)
        # 0.20.0: личный чат — автор сообщения (логин; '' — вход выключен или автор старого сообщения не нашёлся;
        # NULL — сообщение до 0.20.0, автора ещё не искали: assign_chat_owners).
        if "user_login" not in {r["name"] for r in con.execute("PRAGMA table_info(chat_messages)")}:
            try:
                con.execute("ALTER TABLE chat_messages ADD COLUMN user_login TEXT")
            except sqlite3.OperationalError:
                pass


# Какая очередь обработчика выполняет какую задачу (app/worker.py) и как это называть на странице.
LANES = {"prepare": "docs", "analyze": "docs", "noco": "nocodb", "noco_prices": "nocodb", "prices": "prices",
         "pricelist": "pricelists", "noco_purch": "nocodb", "changes": "docs", "noco_changes": "nocodb", "bid": "bid", "noco_bid": "nocodb"}
LANE_TITLES = {"docs": "подготовка и анализ документов", "nocodb": "запись в NocoDB", "prices": "поиск цен",
               "pricelists": "прайсы поставщиков", "bid": "подготовка заявок"}
KIND_TITLES = {"prepare": "подготовка документов", "analyze": "анализ документов", "noco": "запись в NocoDB",
               "noco_prices": "запись цен в NocoDB", "prices": "поиск цен", "pricelist": "разбор прайсов поставщиков",
               "noco_purch": "запись цен поставщиков в NocoDB", "changes": "разбор изменений документации",
               "noco_changes": "запись изменений в NocoDB", "bid": "подготовка документов заявки",
               "noco_bid": "запись заявки в NocoDB"}


def heartbeat(lane: str, job_id: int | None = None) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO worker_lanes(lane, pid, job_id, at) VALUES (?,?,?,?)",
                    (lane, os.getpid(), job_id, time.time()))


def lanes_state() -> dict[str, dict]:
    with connect() as con:
        return {r["lane"]: dict(r) for r in con.execute("SELECT * FROM worker_lanes")}


def active_jobs(client_id: str | None = None) -> list[dict]:
    """Ждущие, выполняющиеся и приостановленные задачи (по порядку очереди)."""
    q, args = "SELECT * FROM jobs WHERE status IN ('queued','running','paused')", []
    if client_id:
        q += " AND client_id=?"
        args.append(client_id)
    with connect() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def recent_jobs(client_id: str, seconds: float = 600) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM jobs WHERE client_id=? AND status IN ('done','error','canceled') AND finished_at>? "
            "ORDER BY finished_at DESC "
            "LIMIT 10", (client_id, time.time() - seconds))]


# ---------- очередь задач ----------

def enqueue(client_id: str, folder: str, kind: str = "prepare", params: dict | None = None, auto: bool = False,
            by: str | None = None) -> int:
    """Ставит задачу в очередь. Если такая же уже ждёт — возвращает её (не плодит дубли),
    дополнив её параметры новыми (например, «после подготовки — анализ»). Такая же приостановленная —
    продолжается (снова в очереди) с новыми параметрами, если задачу ставит пользователь (кнопкой);
    auto=True — задачу ставит сам обработчик по цепочке (после анализа — запись, поиск цен): приостановленную
    пользователем он не трогает.
    by — кто запустил (логин); по умолчанию — ACTOR. У задачи, которая уже ждала, остаётся прежний (кто поставил
    первым), если он был записан."""
    who = (ACTOR.get() if by is None else by) or ""
    with connect() as con:
        row = con.execute(
            "SELECT id, params, status FROM jobs WHERE client_id=? AND folder=? AND kind=? AND status IN ('queued','paused') "
            "ORDER BY id DESC LIMIT 1", (client_id, folder, kind)).fetchone()
        if row and auto and row["status"] == "paused":
            return row["id"]
        if row:
            merged = {**job_params(row), **(params or {})}
            con.execute("UPDATE jobs SET params=?, status='queued', control='', created_at=CASE WHEN status='paused' "
                        "THEN ? ELSE created_at END, started_by=CASE WHEN started_by='' THEN ? ELSE started_by END "
                        "WHERE id=?",
                        (json.dumps(merged, ensure_ascii=False), time.time(), who, row["id"]))
            return row["id"]
        cur = con.execute(
            "INSERT INTO jobs(client_id, folder, kind, status, created_at, params, started_by) VALUES (?,?,?,?,?,?,?)",
            (client_id, folder, kind, "queued", time.time(), json.dumps(params or {}, ensure_ascii=False), who))
        return cur.lastrowid


def job_by(job) -> str:
    """Кто запустил задачу (логин; '' — неизвестно: вход выключен, задача старше 0.16.0)."""
    try:
        return str(job["started_by"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def job_params(job) -> dict:
    try:
        return json.loads(job["params"] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def claim_next(only: tuple[str, ...] = (), skip: tuple[str, ...] = ()) -> sqlite3.Row | None:
    """Берёт самую старую ждущую задачу и помечает её «выполняется» (одним действием).
    only — только задачи этих видов; skip — кроме этих видов (поиск цен идёт отдельной очередью)."""
    where, args = "status='queued'", []
    if only:
        where += f" AND kind IN ({','.join('?' * len(only))})"
        args += list(only)
    if skip:
        where += f" AND kind NOT IN ({','.join('?' * len(skip))})"
        args += list(skip)
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY id LIMIT 1", args).fetchone()
        if row:
            con.execute("UPDATE jobs SET status='running', started_at=?, updated_at=?, step='', progress=0, control='' "
                        "WHERE id=?",
                        (time.time(), time.time(), row["id"]))
        con.execute("COMMIT")
        return row
    finally:
        con.close()


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    fields.setdefault("updated_at", time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def latest_job(client_id: str, folder: str, kinds: tuple[str, ...] = ("prepare",)) -> dict | None:
    marks = ",".join("?" * len(kinds))
    with connect() as con:
        row = con.execute(f"SELECT * FROM jobs WHERE client_id=? AND folder=? AND kind IN ({marks}) "
                          "ORDER BY id DESC LIMIT 1", (client_id, folder, *kinds)).fetchone()
        return dict(row) if row else None


def active_job(client_id: str, folder: str, kind: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE client_id=? AND folder=? AND kind=? AND status IN ('queued','running') "
                          "ORDER BY id DESC LIMIT 1", (client_id, folder, kind)).fetchone()
        return dict(row) if row else None


def recover_interrupted() -> int:
    """После перезапуска обработчика: задачи, оборванные на середине, — снова в очередь (кроме тех, что
    пользователь просил отменить или приостановить: они — отменены / на паузе)."""
    now = time.time()
    with connect() as con:
        con.execute("UPDATE jobs SET status='canceled', finished_at=?, control='', message='Отменена пользователем' "
                    "WHERE status='running' AND control='cancel'", (now,))
        con.execute("UPDATE jobs SET status='paused', control='', message='Приостановлена пользователем' "
                    "WHERE status='running' AND control='pause'")
        return con.execute("UPDATE jobs SET status='queued' WHERE status='running'").rowcount


# ---------- отмена и пауза (меню «В работе») ----------

def get_job(job_id: int) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def job_control(job_id: int) -> str:
    """Просит ли пользователь остановить выполняющуюся задачу: 'cancel' / 'pause' / ''."""
    with connect() as con:
        row = con.execute("SELECT control, status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return "cancel"                 # задачу удалили из базы — дальше выполнять незачем
    return row["control"] or ""


def request_stop(job_id: int, action: str) -> str:
    """action: 'cancel' / 'pause'. Ждущая задача отменяется / откладывается сразу; выполняющаяся — получает
    пометку и останавливается обработчиком на ближайшем шаге. Возвращает, что сделано:
    canceled / paused / stopping / finished (задача уже закончилась) / missing."""
    assert action in ("cancel", "pause")
    now = time.time()
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")       # чтобы обработчик не взял задачу в работу между чтением и записью
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            out = "missing"
        elif row["status"] == "queued" or (row["status"] == "paused" and action == "cancel"):
            if action == "cancel":
                con.execute("UPDATE jobs SET status='canceled', finished_at=?, updated_at=?, control='', "
                            "message=? WHERE id=?",
                            (now, now, "Отменена до начала" if row["status"] == "queued" else "Отменена (была на паузе)",
                             job_id))
                out = "canceled"
            else:
                con.execute("UPDATE jobs SET status='paused', updated_at=?, message='Приостановлена до начала' "
                            "WHERE id=?", (now, job_id))
                out = "paused"
        elif row["status"] == "paused":
            out = "paused"
        elif row["status"] == "running":
            con.execute("UPDATE jobs SET control=? WHERE id=?", (action, job_id))
            out = "stopping"
        else:
            out = "finished"
        con.execute("COMMIT")
        return out
    finally:
        con.close()


def resume(job_id: int) -> str:
    """Продолжить приостановленную задачу (снова в очередь) или передумать останавливать выполняющуюся.
    Возвращает: queued / running / finished / missing."""
    now = time.time()
    with connect() as con:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return "missing"
        if row["status"] == "paused":
            con.execute("UPDATE jobs SET status='queued', control='', message='', step='', progress=0, created_at=?, "
                        "updated_at=? WHERE id=? AND status='paused'", (now, now, job_id))
            return "queued"
        if row["status"] == "running":
            con.execute("UPDATE jobs SET control='' WHERE id=? AND status='running'", (job_id,))
            return "running"
        return "queued" if row["status"] == "queued" else "finished"


# ---------- чат по документации ----------

# Личный чат (0.20.0, пользователь 10.11.2026: «у логиста и закупщика должны быть чаты… вопросы друг друга они не
# должны видеть»; бэклог: «у каждого пользователя свой чат, который не видят другие пользователи»). owner — логин:
# сотрудник видит только свои вопросы и ответы; with_unowned — ещё и сообщения без автора (до 0.20.0, автор не нашёлся
# в журнале) — администратор сайта и сайт без входа. owner=None — все сообщения (только для оценки кэша модели).

def _chat_where(owner: str | None, with_unowned: bool) -> tuple[str, tuple]:
    if owner is None:
        return "", ()
    extra = " OR user_login='' OR user_login IS NULL" if with_unowned else ""
    return f" AND (user_login=?{extra})", (owner,)


def chat_add(client_id: str, folder: str, role: str, content: str, meta: dict | None = None, owner: str = "") -> int:
    with connect() as con:
        cur = con.execute("INSERT INTO chat_messages(client_id, folder, role, content, meta, created_at, user_login) "
                          "VALUES (?,?,?,?,?,?,?)", (client_id, folder, role, content,
                                                     json.dumps(meta or {}, ensure_ascii=False), time.time(), owner or ""))
        return cur.lastrowid


def chat_history(client_id: str, folder: str, limit: int = 200, owner: str | None = None,
                 with_unowned: bool = False) -> list[dict]:
    """Последние сообщения чата конкурса, по порядку: сотрудника owner (None — всех)."""
    where, args = _chat_where(owner, with_unowned)
    with connect() as con:
        rows = con.execute(f"SELECT * FROM chat_messages WHERE client_id=? AND folder=?{where} ORDER BY id DESC LIMIT ?",
                           (client_id, folder, *args, limit)).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except ValueError:
            d["meta"] = {}
        out.append(d)
    return out


def chat_clear(client_id: str, folder: str, owner: str | None = None, with_unowned: bool = False) -> int:
    """Очистить чат конкурса: только сообщения owner (и без автора — with_unowned); None — весь."""
    where, args = _chat_where(owner, with_unowned)
    with connect() as con:
        return con.execute(f"DELETE FROM chat_messages WHERE client_id=? AND folder=?{where}",
                           (client_id, folder, *args)).rowcount


CHAT_EVENT = "задал вопрос в чате"          # app/access.py RULES: журнал пишется, когда ответ отдан


def assign_chat_owners() -> int:
    """0.20.0: у сообщений чата до личных чатов автора нет. Автор вопроса — из журнала (user_events: «задал вопрос
    в чате», «клиент · папка», время — конец ответа, через 0–15 мин после вопроса); ответ и ошибка — автора вопроса
    перед ними. Не нашёлся (вход был выключен) — '' (такие видит администратор сайта). Повторно не ищет."""
    with connect() as con:
        if not con.execute("SELECT 1 FROM chat_messages WHERE user_login IS NULL LIMIT 1").fetchone():
            return 0
        events = []
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_events'").fetchone():
            events = [dict(r) for r in con.execute(
                "SELECT id, at, actor, details FROM user_events WHERE action=? ORDER BY at", (CHAT_EVENT,))]
        used: set[int] = set()
        owner: dict[tuple, str] = {}
        updates = []
        for r in con.execute("SELECT id, client_id, folder, role, created_at FROM chat_messages WHERE user_login IS NULL "
                             "ORDER BY client_id, folder, id").fetchall():
            key = (r["client_id"], r["folder"])
            if r["role"] == "user":
                detail, t = f"{r['client_id']} · {r['folder']}", float(r["created_at"] or 0)
                ev = next((e for e in events if e["id"] not in used and e["details"] == detail
                           and t - 5 <= float(e["at"] or 0) <= t + 900), None)
                if ev:
                    used.add(ev["id"])
                owner[key] = (ev or {}).get("actor") or ""
            updates.append((owner.get(key, ""), r["id"]))
        con.executemany("UPDATE chat_messages SET user_login=? WHERE id=?", updates)
        return len(updates)


# ---------- записи NocoDB ----------

def noco_link(client_id: str, internal: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM noco_links WHERE client_id=? AND internal=?", (client_id, internal)).fetchone()
        return dict(row) if row else None


def save_noco_link(client_id: str, internal: str, folder: str, record_id, created_by_site: bool,
                   spec_table_id: str = "") -> None:
    with connect() as con:
        con.execute("""INSERT OR REPLACE INTO noco_links(client_id, internal, folder, record_id, created_by_site,
                       spec_table_id, updated_at) VALUES (?,?,?,?,?,?,?)""",
                    (client_id, internal, folder, record_id, int(bool(created_by_site)), spec_table_id, time.time()))


# ---------- документы ----------

def doc_rows(client_id: str, folder: str) -> dict[str, dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM doc_files WHERE client_id=? AND folder=?", (client_id, folder)).fetchall()
        return {r["rel_path"]: dict(r) for r in rows}
