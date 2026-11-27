"""
Ответы поставщиков: чтение почты и привязка писем к конкурсам (0.28.0, этап 2 почты закупщика).

Пользователь 17–18.11.2026: «сервер должен периодически проверять почту, полученные цены в письмах и пр. Если какие-то
неясности — спрашивать; нужно как-то решить, чтобы полученные письма относились к каждому конкретному тендеру».
По умолчанию (пользователь: «приступай к этапу 2» на предложенный план): цены в NocoDB — по кнопке закупщика (как
прайсы из чата); почта проверяется раз в MAIL_POLL_MINUTES (5) минут; в «Неразобранную почту» попадают только письма
от адресатов наших запросов или с вложениями-документами и словами про цены; кнопка «Напомнить» — позже.

Опрос (поток обработчика `poller`, не очередь задач): каждый ящик с IMAP-сервером — «Входящие» только для чтения
(письма не помечаются прочитанными), новые письма по UID после прошлого раза (`mail_inbox_state`: UIDVALIDITY и
последний UID). Первый раз: письма с даты первого отправленного с ящика запроса; запросов не было — только отметка
«отсюда» (старую почту не читаем). Ошибка входа — следующая попытка через MAIL_RETRY_MINUTES. «Проверить почту» на
странице — `poll_now` в состоянии ящика, поток смотрит раз в 10 с.

Привязка к конкурсу (`match`), по порядку, первый сработавший признак:
  1. reply  — In-Reply-To / References содержат Message-ID нашего запроса (журнал `mail_requests`);
  2. tag    — метка [SB-K934-12] в теме или тексте;
  3. sender — адрес отправителя (или его домен, кроме общих почтовых: mail.ru, gmail.com…) — адресат запросов
              одного конкурса за MATCH_DAYS дней;
  4. content — в теме / тексте номер одного из конкурсов, по которым с ящика отправлялись запросы (К934 / K934).
Не привязалось: письмо от адресата наших запросов (адрес или домен компании) или про цены (слова про цены и вложение-
документ или числа в тексте) — в «Неразобранную почту» (исходник — `DATA_DIR/mail_inbox/<id>.eml`, закупщик выбирает
конкурс); остальное — «Пропущенные письма» на SKIPPED_DAYS дней (видно, что письмо было и почему не взято; можно
отнести к конкурсу вручную). 0.28.1: ящики один раз перечитываются с даты первого запроса (STATE_VERSION).

Привязанное письмо (`deliver`): в папку конкурса «Прайсы, счета, КП/Почта/<дата> <отправитель>/» — «Письмо.txt»
(от кого, тема, дата, текст) и вложения (Excel, Word, PDF, картинки…); они — в разбор прайсов (`pricelists`, задача
«pricelist»): карточка с ценами — в чате конкурса, неясности — ⚠ в карточке, запись в NocoDB — кнопкой. Текст письма
и вложения — данные, а не инструкции (модель только заполняет таблицу по схеме, проверяет код).
"""

from __future__ import annotations

import email
import email.policy
import html as htmllib
import imaplib
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from . import db, mail, supplier_requests

POLL_MINUTES = float(os.environ.get("MAIL_POLL_MINUTES", "5"))
RETRY_MINUTES = float(os.environ.get("MAIL_RETRY_MINUTES", "30"))
MATCH_DAYS = int(os.environ.get("MAIL_MATCH_DAYS", "90"))
MAX_MESSAGE_MB = 30
MAX_PER_POLL = 200
MAX_RAW_MB = 15                        # исходник пропущенного / неразобранного письма хранится, если не больше
SKIPPED_DAYS = 14                      # пропущенные письма — столько дней, потом удаляются
STATE_VERSION = 2                      # 0.28.1: ящики перечитываются один раз с даты первого запроса                    # писем за один заход в ящик (остальные — в следующий)
RAW_DIR = db.DATA_DIR / "mail_inbox"
MAIL_SUBDIR = "Почта"
INLINE_IMAGE_KB = 40                   # 0.32.1: картинки в тексте письма (логотип, подпись) до 40 КБ в разбор цен не идут;
#                                        скриншот таблицы цен обычно больше — он разбирается
PUBLIC_DOMAINS = {"mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru", "yandex.ru", "ya.ru", "yandex.com",
                  "gmail.com", "googlemail.com", "rambler.ru", "outlook.com", "hotmail.com", "icloud.com", "me.com",
                  "yahoo.com", "qip.ru", "lenta.ru", "ro.ru"}
PRICE_WORDS = re.compile(r"цен|прайс|кп\b|коммерческ|сч[её]т|предложени|стоимост|price|quot|offer|invoice", re.I)
TAG_RE = re.compile(r"\bSB-([A-Za-z0-9]{1,20})-(\d{1,9})\b")
MSGID_RE = re.compile(r"<[^<>\s]+>")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_inbox_state (
    mailbox_id   INTEGER PRIMARY KEY,
    uidvalidity  TEXT NOT NULL DEFAULT '',
    last_uid     INTEGER NOT NULL DEFAULT 0,
    checked_at   REAL,
    ok           INTEGER,
    error        TEXT NOT NULL DEFAULT '',
    new_count    INTEGER NOT NULL DEFAULT 0,       -- писем по делу за последний заход
    poll_now     INTEGER NOT NULL DEFAULT 0,       -- «Проверить почту» на странице
    next_try     REAL NOT NULL DEFAULT 0,
    version      INTEGER NOT NULL DEFAULT 0       -- STATE_VERSION: меньше — перечитать с даты первого запроса
);
CREATE TABLE IF NOT EXISTS mail_inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id   INTEGER NOT NULL,
    owner        TEXT NOT NULL DEFAULT '',
    uid          INTEGER,
    message_id   TEXT NOT NULL DEFAULT '',
    from_addr    TEXT NOT NULL DEFAULT '',
    from_name    TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    received_at  REAL,
    attachments  TEXT NOT NULL DEFAULT '[]',       -- JSON: имена вложений
    preview      TEXT NOT NULL DEFAULT '',         -- начало текста (для «Неразобранной почты»)
    status       TEXT NOT NULL,                    -- unmatched / delivered / ignored / error
    how          TEXT NOT NULL DEFAULT '',         -- reply / tag / sender / content / manual
    candidates   TEXT NOT NULL DEFAULT '[]',       -- JSON: [{client_id, internal}] — если подходят несколько
    request_id   INTEGER,
    client_id    TEXT NOT NULL DEFAULT '',
    internal     TEXT NOT NULL DEFAULT '',
    folder       TEXT NOT NULL DEFAULT '',
    saved_dir    TEXT NOT NULL DEFAULT '',         -- папка письма относительно папки конкурса
    files        TEXT NOT NULL DEFAULT '[]',       -- JSON: файлы, отданные в разбор прайсов
    job_id       INTEGER,
    error        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (mailbox_id, message_id)
);
CREATE INDEX IF NOT EXISTS mail_inbox_tender ON mail_inbox (client_id, internal);
CREATE INDEX IF NOT EXISTS mail_inbox_owner ON mail_inbox (owner, status);
"""

_inited = False
_LOCK = threading.Lock()


def init() -> None:
    global _inited
    if _inited:
        return
    mail.init()
    supplier_requests.init()
    with db.connect() as con:
        con.executescript(SCHEMA)
        if "body" not in {r["name"] for r in con.execute("PRAGMA table_info(mail_inbox)")}:
            try:                                   # 0.32.0: текст письма без цитаты — для переписки на сайте
                con.execute("ALTER TABLE mail_inbox ADD COLUMN body TEXT NOT NULL DEFAULT ''")
            except Exception:                      # noqa: BLE001 — колонку добавил другой процесс
                pass
        if "version" not in {r["name"] for r in con.execute("PRAGMA table_info(mail_inbox_state)")}:
            try:
                con.execute("ALTER TABLE mail_inbox_state ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            except Exception:                      # noqa: BLE001 — колонку добавил другой процесс
                pass
    _inited = True


# ---------- разбор письма ----------

def _dec(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def parse(raw: bytes) -> dict:
    """Письмо → {message_id, from_addr, from_name, to, subject, date, refs, text, attachments: [(имя, байты)]}."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_name, addr = parseaddr(str(msg.get("From") or ""))
    refs = MSGID_RE.findall(str(msg.get("In-Reply-To") or "") + " " + str(msg.get("References") or ""))
    try:
        dt = parsedate_to_datetime(str(msg.get("Date")))
        date = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError, IndexError):
        date = time.time()
    plain, html_text, attachments, inline = [], [], [], set()
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        if fname or disp == "attachment":
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:                   # noqa: BLE001 — битая часть письма
                continue
            name = _dec(fname) or f"вложение.{ctype.split('/')[-1]}"
            attachments.append((name, data))
            # Картинка внутри текста письма (логотип, подпись: image001.png с Content-ID) — не документ поставщика.
            if ctype.startswith("image/") and (disp == "inline" or part.get("Content-ID")) and len(data) <= INLINE_IMAGE_KB * 1024:
                inline.add(name)
            continue
        if ctype in ("text/plain", "text/html"):
            try:
                content = part.get_content()
            except Exception:                   # noqa: BLE001
                content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            (plain if ctype == "text/plain" else html_text).append(str(content))
    text = "\n\n".join(plain).strip() or html_to_text("\n".join(html_text))
    return {
        "message_id": _dec(msg.get("Message-ID")),
        "from_addr": addr.strip().lower(), "from_name": _dec(from_name),
        "to": [a.lower() for _, a in getaddresses([str(msg.get("To") or ""), str(msg.get("Cc") or "")]) if a],
        "subject": _dec(msg.get("Subject")), "date": date, "refs": refs,
        "text": text[:200_000], "attachments": attachments, "inline": inline,
    }


QUOTE_CUT = re.compile(r"\n[^\n]{0,120}(?:пишет|написал\(а\)|wrote)\s*:\s*\n|\n-{2,}\s*(?:Original Message|Исходное "
                       r"сообщение|Пересылаемое сообщение|Forwarded message)", re.I)


def own_text(text: str) -> str:
    """Текст письма без цитаты нашего запроса («… пишет:», «> …», «Исходное сообщение»): цены — в ответе, а в
    цитате — наши же позиции с количествами, их модели давать незачем. Полный текст — в письмо.eml."""
    m = QUOTE_CUT.search("\n" + text)
    text = text[:max(0, m.start() - 1)] if m else text
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">")).strip()


def _readable(name: str) -> bool:
    from .pricelists import EXTENSIONS
    return Path(name).suffix.lower() in EXTENSIONS


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


# ---------- привязка к конкурсу ----------

def _requests(where: str, args: tuple) -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM mail_requests WHERE status='sent' AND {where} "
                                             "ORDER BY id DESC", args)]


def match(m: dict, box: dict) -> dict:
    """Конкурс письма: {how, request} — найден; {how: '', candidates: [...]} — нет или подходят несколько."""
    init()
    # 1. Ответ на наш запрос (Message-ID в In-Reply-To / References).
    if m["refs"]:
        rows = _requests(f"message_id IN ({','.join('?' * len(m['refs']))})", tuple(m["refs"]))
        if rows:
            return {"how": "reply", "request": rows[0]}
    # 2. Метка [SB-K934-12] в теме или тексте.
    for t in TAG_RE.finditer(m["subject"] + "\n" + m["text"][:20000]):
        rows = _requests("id=?", (int(t.group(2)),))
        # Метка, запомненная у запроса, или по нынешнему номеру (0.34.0: номер конкурса можно сменить, а в уже
        # отправленных письмах осталась прежняя метка).
        if rows and (str(rows[0]["tag"] or "").lower() == t.group(0).lower()
                     or supplier_requests.latin_tag(rows[0]["internal"]).lower() == t.group(1).lower()):
            return {"how": "tag", "request": rows[0]}
    since = time.time() - MATCH_DAYS * 86400
    recent = _requests("mailbox_id=? AND sent_at>=?", (box["id"], since))
    by_tender: dict[tuple, dict] = {}
    for r in recent:
        by_tender.setdefault((r["client_id"], r["internal"]), r)

    def tenders_of(rows):
        out: dict[tuple, dict] = {}
        for r in rows:
            out.setdefault((r["client_id"], r["internal"]), r)
        return out

    # 3. Отправитель — адресат запросов (адрес; домен — если не общий почтовый).
    addr, dom = m["from_addr"], _domain(m["from_addr"])
    to_addr = tenders_of([r for r in recent if addr and addr in [a.strip().lower() for a in r["to_addr"].split(",")]])
    if not to_addr and dom and dom not in PUBLIC_DOMAINS:
        to_addr = tenders_of([r for r in recent if any(_domain(a.strip()) == dom for a in r["to_addr"].split(","))])
    if len(to_addr) == 1:
        return {"how": "sender", "request": next(iter(to_addr.values()))}
    # 4. Номер конкурса в теме или тексте (среди конкурсов, по которым с ящика были запросы).
    hay = (m["subject"] + "\n" + m["text"][:20000]).lower()
    pool = to_addr or by_tender
    found = {}
    for key, r in pool.items():
        num = r["internal"].lower()
        # Прежний номер из метки запроса (SB-K947-12 → K947 / К947): номер конкурса сменили, а поставщик пишет старый.
        old = TAG_RE.search(str(r.get("tag") or ""))
        olds = {old.group(1).lower(), old.group(1).lower().replace("k", "к", 1)} if old else set()
        for variant in {num, supplier_requests.latin_tag(r["internal"]).lower(), *olds}:
            if variant and re.search(rf"(?<![0-9a-zа-я]){re.escape(variant)}(?![0-9])", hay):
                found[key] = r
    if len(found) == 1:
        return {"how": "content", "request": next(iter(found.values()))}
    cands = found or to_addr
    return {"how": "", "candidates": [{"client_id": k[0], "internal": k[1], "folder": r["folder"]}
                                      for k, r in cands.items()], "known_sender": bool(to_addr)}


def relevant(m: dict, box: dict) -> str:
    """Почему письмо может быть ответом поставщика ('' — не похоже): адресат наших запросов (адрес или домен
    компании), или про цены (слова про цены + вложение-документ или числа в тексте — 18.11.2026: поставщик ответил
    ценой прямо в тексте письма, без вложения, и письмо было пропущено)."""
    addr = m["from_addr"]
    if not addr or addr == box["address"]:
        return ""
    dom = _domain(addr)
    with db.connect() as con:
        rows = con.execute("SELECT to_addr FROM mail_requests WHERE status='sent' AND mailbox_id=?", (box["id"],)).fetchall()
    sent_to = {a.strip().lower() for r in rows for a in r["to_addr"].split(",") if a.strip()}
    if addr in sent_to:
        return "адресат наших запросов"
    if dom and dom not in PUBLIC_DOMAINS and dom in {_domain(a) for a in sent_to}:
        return "домен адресата наших запросов"
    words = PRICE_WORDS.search(m["subject"] + " " + m["text"][:5000])
    docs = [n for n, _ in m["attachments"] if _readable(n)]
    if words and (docs or re.search(r"\d", own_text(m["text"]))):
        return "про цены"
    return ""


# ---------- доставка в папку конкурса ----------

def _write_unique(path: Path, data: bytes) -> Path:
    stem, suf, k = path.stem, path.suffix, 1
    while path.exists():
        k += 1
        path = path.with_name(f"{stem} ({k}){suf}")
    path.write_bytes(data)
    return path


def letter_text(m: dict, body: str) -> str:
    when = datetime.fromtimestamp(m["date"], tz=timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    att = ", ".join(n for n, _ in m["attachments"]) or "нет"
    cut = "" if body == m["text"].strip() else "\n\n(цитата прежней переписки — в файле письмо.eml)"
    return (f"От: {m['from_name']} <{m['from_addr']}>\nТема: {m['subject']}\nДата: {when} (МСК)\nВложения: {att}\n\n"
            f"{body}{cut}\n")


def deliver(m: dict, raw: bytes, box: dict, request: dict, how: str, inbox_id: int | None = None) -> dict:
    """Письмо → папка конкурса и разбор прайсов. Возвращает поля для mail_inbox (status, saved_dir, files, job_id)."""
    from .config import load_clients
    from .pricelists import MAX_FILE_MB, add_files, price_dir, update
    from .tenders import FolderError, folder_path, safe_name
    client = load_clients().get(request["client_id"])
    if not client:
        return {"status": "error", "error": f"клиент {request['client_id']} не найден"}
    if not request.get("folder"):
        return {"status": "error", "error": "у конкурса нет папки с документами — письмо некуда сохранить"}
    try:
        base = folder_path(client, request["folder"])
    except FolderError as exc:
        return {"status": "error", "error": str(exc)}
    when = datetime.fromtimestamp(m["date"], tz=timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H-%M")
    who = safe_name(request.get("supplier_name") or m["from_name"] or m["from_addr"], 60) or "поставщик"
    dest = price_dir(base) / MAIL_SUBDIR / safe_name(f"{when} {who}", 100)
    k = 1
    while dest.exists():
        k += 1
        dest = dest.with_name(safe_name(f"{when} {who} ({k})", 100))
    dest.mkdir(parents=True)
    (dest / "письмо.eml").write_bytes(raw)
    files = []
    body = own_text(m["text"])
    letter = _write_unique(dest / "Письмо.txt", letter_text(m, body).encode("utf-8"))
    if re.search(r"\d", body):                     # в тексте есть числа — может быть цена; иначе разбирать нечего
        files.append(letter)
    skipped = []
    for name, data in m["attachments"]:
        fname = safe_name(Path(name).name, 120) or "вложение"
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            skipped.append(f"{fname} (больше {MAX_FILE_MB} МБ)")
            continue
        p = _write_unique(dest / fname, data)
        if name in m.get("inline", ()):
            continue                               # логотип / подпись — сохранён, но не разбирается
        if _readable(fname):
            files.append(p)
        else:
            skipped.append(fname)
    rels = [p.relative_to(base).as_posix() for p in files]
    job_id = None
    if rels:
        added = add_files(base, rels, request.get("owner") or "")
        info = {"inbox_id": inbox_id, "request_id": request.get("id"), "from": m["from_addr"], "subject": m["subject"],
                "supplier": request.get("supplier_name") or "", "how": how, "from_name": m["from_name"]}
        ids = {e["id"] for e in added}

        def tag(data):
            for e in data["files"]:
                if e.get("id") in ids:
                    e["mail"] = info
        update(base, tag)
        job_id = db.enqueue(client.id, request["folder"], "pricelist", {}, by=request.get("owner") or "")
    return {"status": "delivered", "saved_dir": dest.relative_to(base).as_posix(), "files": rels, "job_id": job_id,
            "error": ("не разбирались: " + ", ".join(skipped)) if skipped else ""}


# ---------- журнал писем ----------

def _save_row(box: dict, uid: int | None, m: dict, **fields) -> int:
    now = time.time()
    row = {
        "mailbox_id": box["id"], "owner": box["owner"], "uid": uid, "message_id": m["message_id"] or f"uid:{box['id']}:{uid}",
        "from_addr": m["from_addr"], "from_name": m["from_name"][:200], "subject": m["subject"][:500],
        "received_at": m["date"], "attachments": json.dumps([n for n, _ in m["attachments"]], ensure_ascii=False),
        "preview": m["text"][:600], "body": own_text(m["text"])[:20000], "created_at": now, "updated_at": now, **fields,
    }
    for k in ("candidates", "files"):
        if not isinstance(row.get(k, "[]"), str):
            row[k] = json.dumps(row[k], ensure_ascii=False)
    with db.connect() as con:
        cur = con.execute(f"INSERT OR IGNORE INTO mail_inbox ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))
        return cur.lastrowid if cur.rowcount else 0


def _update_row(inbox_id: int, **fields) -> None:
    for k in ("candidates", "files"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = time.time()
    with db.connect() as con:
        con.execute(f"UPDATE mail_inbox SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                    (*fields.values(), inbox_id))


def handle(raw: bytes, box: dict, uid: int | None = None) -> str:
    """Одно новое письмо: привязать и доставить / в «Неразобранную» / пропустить. Возвращает итог (для журнала)."""
    m = parse(raw)
    if m["from_addr"] == box["address"]:
        return "своё письмо"
    with db.connect() as con:
        if m["message_id"] and con.execute("SELECT 1 FROM mail_inbox WHERE mailbox_id=? AND message_id=?",
                                           (box["id"], m["message_id"])).fetchone():
            return "уже было"
    found = match(m, box)
    if found["how"]:
        r = found["request"]
        inbox_id = _save_row(box, uid, m, status="processing", how=found["how"], request_id=r["id"],
                             client_id=r["client_id"], internal=r["internal"], folder=r["folder"])
        if not inbox_id:
            return "уже было"
        try:
            res = deliver(m, raw, box, r, found["how"], inbox_id)
        except Exception as exc:                   # noqa: BLE001
            res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:400]}
        _update_row(inbox_id, **res)
        return f"{r['internal']}: {res['status']}"
    why = relevant(m, box)
    # Не похоже на ответ поставщика — «Пропущенные письма» (последние SKIPPED_DAYS дней): закупщик видит, что сайт
    # письмо получил и почему не взял, и может отнести его к конкурсу сам.
    status = "unmatched" if why else "skipped"
    inbox_id = _save_row(box, uid, m, status=status, candidates=found.get("candidates") or [],
                         error="" if why else "не похоже на ответ поставщика: не ответ на запрос, отправителю запросов "
                                              "не было, слов про цены с числами или вложений нет")
    if inbox_id and len(raw) <= MAX_RAW_MB * 1024 * 1024:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{inbox_id}.eml").write_bytes(raw)
    return "неразобранная" if why else "пропущено"


def assign(owner: str, inbox_id: int, client_id: str, internal: str) -> dict:
    """«Неразобранная почта» → выбранный закупщиком конкурс (из тех, по которым он отправлял запросы)."""
    init()
    with db.connect() as con:
        row = con.execute("SELECT * FROM mail_inbox WHERE id=? AND owner=?", (inbox_id, owner)).fetchone()
    if not row:
        raise mail.MailError("письмо не найдено")
    if row["status"] not in ("unmatched", "error", "skipped"):
        raise mail.MailError("письмо уже разобрано")
    reqs = _requests("owner=? AND client_id=? AND internal=?", (owner, client_id, internal))
    if not reqs:
        raise mail.MailError("по этому конкурсу вы запросов не отправляли")
    raw_path = RAW_DIR / f"{inbox_id}.eml"
    if not raw_path.exists():
        raise mail.MailError("исходник письма не сохранился — перешлите письмо себе ещё раз")
    raw = raw_path.read_bytes()
    m = parse(raw)
    from_addr = m["from_addr"]
    r = next((x for x in reqs if from_addr in x["to_addr"].lower()), reqs[0])
    box = dict(mail._row(owner, row["mailbox_id"]))
    _update_row(inbox_id, status="processing", how="manual", request_id=r["id"], client_id=r["client_id"],
                internal=r["internal"], folder=r["folder"])
    res = deliver(m, raw, box, r, "manual", inbox_id)
    _update_row(inbox_id, **res)
    if res["status"] == "delivered":
        raw_path.unlink(missing_ok=True)
    return res


def ignore(owner: str, inbox_id: int) -> None:
    init()
    with db.connect() as con:
        n = con.execute("UPDATE mail_inbox SET status='ignored', preview='', updated_at=? WHERE id=? AND owner=? "
                        "AND status IN ('unmatched','error','skipped')", (time.time(), inbox_id, owner)).rowcount
    if not n:
        raise mail.MailError("письмо не найдено")
    (RAW_DIR / f"{inbox_id}.eml").unlink(missing_ok=True)


def _public(r) -> dict:
    d = dict(r)
    for k in ("attachments", "candidates", "files"):
        d[k] = json.loads(d.get(k) or "[]")
    return d


def unmatched(owner: str) -> list[dict]:
    init()
    with db.connect() as con:
        rows = con.execute("SELECT * FROM mail_inbox WHERE owner=? AND status IN ('unmatched','error') "
                           "ORDER BY received_at DESC LIMIT 100", (owner,)).fetchall()
    return [_public(r) for r in rows]


def skipped(owner: str) -> list[dict]:
    """Пропущенные письма (не похожи на ответ поставщика) — за SKIPPED_DAYS дней, новые сверху."""
    init()
    with db.connect() as con:
        rows = con.execute("SELECT * FROM mail_inbox WHERE owner=? AND status='skipped' AND received_at>=? "
                           "ORDER BY received_at DESC LIMIT 50", (owner, time.time() - SKIPPED_DAYS * 86400)).fetchall()
    return [_public(r) for r in rows]


def purge() -> None:
    """Пропущенные письма старше SKIPPED_DAYS — из базы и исходники с диска."""
    with db.connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM mail_inbox WHERE status='skipped' AND created_at<?",
                                            (time.time() - SKIPPED_DAYS * 86400,))]
        con.execute("DELETE FROM mail_inbox WHERE status='skipped' AND created_at<?",
                    (time.time() - SKIPPED_DAYS * 86400,))
    for i in ids:
        (RAW_DIR / f"{i}.eml").unlink(missing_ok=True)


def owner_tenders(owner: str) -> list[dict]:
    """Конкурсы, по которым сотрудник отправлял запросы (для выбора в «Неразобранной почте»)."""
    init()
    with db.connect() as con:
        rows = con.execute("SELECT client_id, internal, folder, MAX(sent_at) AS last FROM mail_requests "
                           "WHERE owner=? AND status='sent' GROUP BY client_id, internal ORDER BY last DESC LIMIT 100",
                           (owner,)).fetchall()
    return [dict(r) for r in rows]


def for_tender(client_id: str, internal: str) -> list[dict]:
    """Письма поставщиков по конкурсу (для журнала запросов)."""
    init()
    with db.connect() as con:
        rows = con.execute("SELECT * FROM mail_inbox WHERE client_id=? AND internal=? AND status IN "
                           "('delivered','processing','error') ORDER BY received_at DESC LIMIT 200",
                           (client_id, internal)).fetchall()
    out = []
    for r in rows:
        d = _public(r)
        d.pop("preview", None)
        d.pop("body", None)
        out.append(d)
    return out


def states(owner: str) -> dict[int, dict]:
    init()
    with db.connect() as con:
        rows = con.execute("SELECT s.* FROM mail_inbox_state s JOIN mailboxes b ON b.id=s.mailbox_id WHERE b.owner=?",
                           (owner,)).fetchall()
    return {r["mailbox_id"]: dict(r) for r in rows}


def request_poll(owner: str) -> int:
    init()
    now = time.time()
    with db.connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM mailboxes WHERE owner=? AND imap_host!=''", (owner,))]
        for i in ids:
            con.execute("INSERT INTO mail_inbox_state (mailbox_id, poll_now) VALUES (?, 1) ON CONFLICT (mailbox_id) "
                        "DO UPDATE SET poll_now=1, next_try=?", (i, now))
    return len(ids)


# ---------- опрос ящиков ----------

def _first_since(box_id: int) -> float | None:
    with db.connect() as con:
        r = con.execute("SELECT MIN(sent_at) AS t FROM mail_requests WHERE mailbox_id=? AND status='sent'",
                        (box_id,)).fetchone()
    return r["t"] if r and r["t"] else None


def _set_state(box_id: int, **fields) -> None:
    with db.connect() as con:
        con.execute("INSERT OR IGNORE INTO mail_inbox_state (mailbox_id) VALUES (?)", (box_id,))
        con.execute(f"UPDATE mail_inbox_state SET {', '.join(f'{k}=?' for k in fields)} WHERE mailbox_id=?",
                    (*fields.values(), box_id))


def poll_box(box_row) -> str:
    """Новые письма одного ящика. Возвращает итог для журнала обработчика."""
    box = dict(box_row)
    with db.connect() as con:
        st = con.execute("SELECT * FROM mail_inbox_state WHERE mailbox_id=?", (box["id"],)).fetchone()
    st = dict(st) if st else {"uidvalidity": "", "last_uid": 0}
    try:
        password = mail.decrypt(box["secret"])
        imap = mail._imap(box, password)
    except Exception as exc:                       # noqa: BLE001
        err = str(exc) if isinstance(exc, mail.MailError) else mail._explain(exc, "Чтение почты (IMAP)")
        _set_state(box["id"], checked_at=time.time(), ok=0, error=err, poll_now=0,
                   next_try=time.time() + RETRY_MINUTES * 60)
        return err
    handled, results = 0, []
    try:
        typ, data = imap.select("INBOX", readonly=True)       # только чтение — письма не помечаются прочитанными
        if typ != "OK":
            raise mail.MailError(f"папка «Входящие» не открылась: {typ}")
        uv = imap.response("UIDVALIDITY")[1]
        uidvalidity = (uv[0].decode() if uv and uv[0] else "")
        # Первый раз, сменилась UIDVALIDITY или ящик читался старым правилом (STATE_VERSION) — с даты первого запроса;
        # уже разобранные письма не повторяются (сверка по Message-ID в handle).
        rescan = st.get("version", 0) < STATE_VERSION
        if rescan:
            st["last_uid"] = 0
        if st["uidvalidity"] != uidvalidity or not st["last_uid"]:
            since = _first_since(box["id"])
            if since:
                day = datetime.fromtimestamp(since - 86400).strftime("%d-%b-%Y")
                typ, data = imap.uid("SEARCH", None, "SINCE", day)
            else:                                  # запросов с ящика не было — старую почту не читаем
                typ, data = imap.uid("SEARCH", None, "ALL")
                uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
                _set_state(box["id"], uidvalidity=uidvalidity, last_uid=max(uids or [0]), checked_at=time.time(),
                           ok=1, error="", new_count=0, poll_now=0, next_try=0, version=STATE_VERSION)
                return "отмечено «отсюда» (запросов с ящика ещё не было)"
        else:
            typ, data = imap.uid("SEARCH", None, f"UID {st['last_uid'] + 1}:*")
        uids = sorted(int(x) for x in (data[0] or b"").split()) if typ == "OK" else []
        base_uid = st["last_uid"] if st["uidvalidity"] == uidvalidity else 0
        uids = [u for u in uids if u > base_uid]
        last = base_uid
        for uid in uids[:MAX_PER_POLL]:
            typ, size = imap.uid("FETCH", str(uid), "(RFC822.SIZE)")
            m_size = re.search(rb"RFC822\.SIZE (\d+)", size[0] if size and isinstance(size[0], bytes) else b"")
            if m_size and int(m_size.group(1)) > MAX_MESSAGE_MB * 1024 * 1024:
                results.append(f"{uid}: больше {MAX_MESSAGE_MB} МБ — пропущено")
                last = uid
                continue
            typ, msg = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in msg or [] if isinstance(p, tuple) and len(p) > 1), None)
            if raw:
                try:
                    res = handle(raw, box, uid)
                except Exception as exc:           # noqa: BLE001 — одно битое письмо не останавливает остальные
                    res = f"ошибка: {type(exc).__name__}: {exc}"
                    traceback.print_exc()
                results.append(f"{uid}: {res}")
                if res not in ("своё письмо", "пропущено", "уже было"):
                    handled += 1
            last = uid
        _set_state(box["id"], uidvalidity=uidvalidity, last_uid=last, checked_at=time.time(), ok=1, error="",
                   new_count=handled, poll_now=0, next_try=0, version=STATE_VERSION)
    except Exception as exc:                       # noqa: BLE001
        err = str(exc) if isinstance(exc, mail.MailError) else mail._explain(exc, "Чтение почты (IMAP)")
        _set_state(box["id"], checked_at=time.time(), ok=0, error=err, poll_now=0,
                   next_try=time.time() + RETRY_MINUTES * 60)
        return err
    finally:
        try:
            imap.logout()
        except Exception:                          # noqa: BLE001
            pass
    return f"писем по делу: {handled}" + (f" ({'; '.join(results[-5:])})" if results else "")


def due_boxes() -> list:
    """Ящики, которые пора проверить: «Проверить почту» или прошло POLL_MINUTES; после ошибки — не раньше next_try."""
    init()
    now = time.time()
    with db.connect() as con:
        return con.execute(
            "SELECT b.* FROM mailboxes b LEFT JOIN mail_inbox_state s ON s.mailbox_id=b.id "
            "WHERE b.imap_host!='' AND b.secret!='' AND (s.mailbox_id IS NULL OR s.poll_now=1 "
            "OR (COALESCE(s.next_try,0)<=? AND COALESCE(s.checked_at,0)<=?))",
            (now, now - POLL_MINUTES * 60)).fetchall()


def poller() -> None:
    """Поток обработчика: раз в 10 с смотрит, какие ящики пора проверить; раз в час чистит пропущенные письма."""
    purged = 0.0
    if POLL_MINUTES <= 0:
        # 0.44.4: MAIL_POLL_MINUTES=0 — почту читает только сервер. Локальная копия с общей папкой Dropbox иначе
        # клала бы те же ответы поставщиков в те же папки второй раз.
        print("Почта: опрос ящиков выключен (MAIL_POLL_MINUTES=0)", flush=True)
        return
    while True:
        if time.time() - purged > 3600:
            try:
                purge()
            except Exception:                      # noqa: BLE001
                traceback.print_exc()
            purged = time.time()
        try:
            for box in due_boxes():
                with _LOCK:
                    res = poll_box(box)
                print(f"Почта {box['address']}: {res}", flush=True)
        except Exception:                          # noqa: BLE001 — поток не должен умирать
            traceback.print_exc()
        time.sleep(10)
