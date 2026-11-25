"""
Почтовые ящики сотрудников и отправка писем (0.27.0).

Пользователь 17.11.2026: «свой ящик у каждого закупщика, причём закупщик может сам выбирать, какой ящик использовать…
у закупщика должна быть возможность добавлять, выбирать почтовые ящики, с которых нужно отправлять». Логины и пароли
ящиков вводит сам сотрудник на сайте («Мои почтовые ящики»); в чат и в файлы проекта они не попадают.

Хранение — таблица `mailboxes` в app.db: хозяин (логин сотрудника), адрес, имя отправителя, серверы SMTP / IMAP,
логин и пароль. Пароль зашифрован (Fernet, библиотека cryptography — уже есть в образе как зависимость pdfminer.six);
ключ — файл `DATA_DIR/mail.key` (создаётся сам, права 600). Потерян ключ — пароли ящиков нужно ввести заново.
Пароль никогда не отдаётся странице (только «задан»), его видит только SMTP / IMAP.

Серверы по домену — PRESETS (Яндекс, Mail.ru, Gmail, Rambler…); для почты на своём домене (Яндекс 360 и т. п.)
сотрудник вписывает серверы сам. Проверка ящика — вход по SMTP и по IMAP (IMAP понадобится для чтения ответов, 0.28.0).

Сеть: SMTP / IMAP идут напрямую (прокси Windows на ноутбуке — HTTP, почта через него не ходит): .ru-серверы с ноутбука
доступны, Gmail — может не отвечать; на сервере — смотря что разрешает хостинг (порт 465 / 587).
"""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
import socket
import sqlite3
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from . import db

TIMEOUT = float(os.environ.get("MAIL_TIMEOUT", "25"))
# Ключ шифрования паролей: файл из MAIL_KEY_FILE (.env → secrets/, в контейнере — /run/secrets/mail_key, создаёт
# run.sh) — отдельно от базы с паролями. Нет его — запасной ключ в DATA_DIR (так было до появления MAIL_KEY_FILE).
SECRET_KEY_PATH = Path(os.environ.get("MAIL_KEY_FILE_IN_CONTAINER", "/run/secrets/mail_key"))
KEY_PATH = db.DATA_DIR / "mail.key"
EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")

# Домен → (SMTP-сервер, порт, IMAP-сервер, порт). Порт 465 — SSL сразу, 587 — STARTTLS.
_YANDEX = ("smtp.yandex.ru", 465, "imap.yandex.ru", 993)
_MAILRU = ("smtp.mail.ru", 465, "imap.mail.ru", 993)
PRESETS = {
    **{d: _YANDEX for d in ("yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz", "narod.ru")},
    **{d: _MAILRU for d in ("mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru", "mail.ua")},
    # Почта компании на своём домене (пользователь 17.11.2026: agent@prostor01.ru — ящик на Mail.ru для бизнеса).
    "prostor01.ru": _MAILRU,
    "gmail.com": ("smtp.gmail.com", 465, "imap.gmail.com", 993),
    "googlemail.com": ("smtp.gmail.com", 465, "imap.gmail.com", 993),
    **{d: ("smtp.rambler.ru", 465, "imap.rambler.ru", 993) for d in ("rambler.ru", "lenta.ru", "ro.ru", "autorambler.ru")},
    **{d: ("smtp-mail.outlook.com", 587, "outlook.office365.com", 993) for d in ("outlook.com", "hotmail.com", "live.com")},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,                 -- логин сотрудника ('' — вход на сайт выключен)
    address     TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',      -- имя в поле «От кого» и в подписи
    signature   TEXT NOT NULL DEFAULT '',      -- подпись письма (пусто — имя, телефон, адрес)
    login       TEXT NOT NULL DEFAULT '',      -- пусто — адрес
    secret      TEXT NOT NULL DEFAULT '',      -- пароль, зашифрован
    smtp_host   TEXT NOT NULL DEFAULT '',
    smtp_port   INTEGER NOT NULL DEFAULT 465,
    imap_host   TEXT NOT NULL DEFAULT '',
    imap_port   INTEGER NOT NULL DEFAULT 993,
    is_default  INTEGER NOT NULL DEFAULT 0,
    check_ok    INTEGER,                       -- последняя проверка: 1 — вход удался, 0 — нет, NULL — не проверяли
    check_text  TEXT NOT NULL DEFAULT '',
    checked_at  REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (owner, address)
);
"""

_KEY_LOCK = threading.Lock()
_inited = False


class MailError(RuntimeError):
    """Ошибка, понятная сотруднику (показывается на странице)."""


def init() -> None:
    global _inited
    if _inited:
        return
    with db.connect() as con:
        con.executescript(SCHEMA)
    _inited = True


# ---------- шифрование пароля ----------

def _key_from(path: Path) -> bytes | None:
    try:
        key = path.read_bytes().strip()
    except OSError:
        return None
    return key or None


def _fernet():
    """Шифр паролей. Шифрует ключом из secrets/ (MAIL_KEY_FILE); расшифровывает и им, и запасным ключом из DATA_DIR
    (пароли, сохранённые до появления ключа в secrets/, продолжают работать и перешифровываются при сохранении)."""
    from cryptography.fernet import Fernet, MultiFernet
    with _KEY_LOCK:
        keys = []
        main = _key_from(SECRET_KEY_PATH)
        if main:
            keys.append(main)
        old = _key_from(KEY_PATH)
        if not main and not old:                    # ни одного ключа — запасной в DATA_DIR
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = KEY_PATH.with_suffix(".tmp")
            tmp.write_bytes(Fernet.generate_key())
            os.chmod(tmp, 0o600)
            os.replace(tmp, KEY_PATH)
            old = _key_from(KEY_PATH)
        if old and old not in keys:
            keys.append(old)
        try:
            return MultiFernet([Fernet(k) for k in keys])
        except ValueError as exc:
            raise MailError("ключ шифрования паролей ящиков повреждён (MAIL_KEY_FILE) — обратитесь к администратору") from exc


def key_source() -> str:
    """Где ключ шифрования (для проверки): secrets — файл MAIL_KEY_FILE, data — запасной в DATA_DIR."""
    return "secrets" if _key_from(SECRET_KEY_PATH) else "data"


def encrypt(password: str) -> str:
    return _fernet().encrypt(password.encode("utf-8")).decode("ascii") if password else ""


def decrypt(secret: str) -> str:
    if not secret:
        return ""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(secret.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise MailError("пароль ящика не расшифровывается (ключ сайта сменился) — введите пароль заново") from exc


# ---------- ящики ----------

def preset(address: str) -> dict:
    """Серверы по домену адреса: {smtp_host, smtp_port, imap_host, imap_port, known}."""
    domain = address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""
    p = PRESETS.get(domain)
    if not p:
        return {"smtp_host": "", "smtp_port": 465, "imap_host": "", "imap_port": 993, "known": False}
    return {"smtp_host": p[0], "smtp_port": p[1], "imap_host": p[2], "imap_port": p[3], "known": True}


def _public(row) -> dict:
    d = dict(row)
    d["has_password"] = bool(d.pop("secret", ""))
    d["is_default"] = bool(d["is_default"])
    d["check_ok"] = None if d["check_ok"] is None else bool(d["check_ok"])
    return d


def list_boxes(owner: str) -> list[dict]:
    init()
    with db.connect() as con:
        rows = con.execute("SELECT * FROM mailboxes WHERE owner=? ORDER BY is_default DESC, address", (owner,)).fetchall()
    return [_public(r) for r in rows]


def _row(owner: str, box_id: int):
    init()
    with db.connect() as con:
        row = con.execute("SELECT * FROM mailboxes WHERE id=? AND owner=?", (int(box_id), owner)).fetchone()
    if not row:
        raise MailError("ящик не найден")
    return row


def get_box(owner: str, box_id: int) -> dict:
    return _public(_row(owner, box_id))


def _port(value, default: int) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError):
        return default
    return p if 0 < p < 65536 else default


def _host(value) -> str:
    h = str(value or "").strip().lower()
    if h and not re.fullmatch(r"[a-z0-9.-]{3,253}", h):
        raise MailError(f"неверное имя сервера: {value}")
    return h


def save_box(owner: str, data: dict) -> dict:
    """Добавить ящик (без id) или изменить свой. Пустой пароль при изменении — прежний."""
    init()
    address = str(data.get("address") or "").strip().lower()
    if not EMAIL_RE.match(address):
        raise MailError("укажите адрес почты, например zakupki@yandex.ru")
    p = preset(address)
    fields = {
        "address": address,
        "sender_name": str(data.get("sender_name") or "").strip()[:120],
        "signature": str(data.get("signature") or "").strip()[:2000],
        "login": str(data.get("login") or "").strip()[:200],
        "smtp_host": _host(data.get("smtp_host")) or p["smtp_host"],
        "smtp_port": _port(data.get("smtp_port"), p["smtp_port"]),
        "imap_host": _host(data.get("imap_host")) or p["imap_host"],
        "imap_port": _port(data.get("imap_port"), p["imap_port"]),
    }
    if not fields["smtp_host"]:
        raise MailError("для этого домена серверы неизвестны — укажите сервер исходящей почты (SMTP)")
    # Пробелы по краям — от копирования (пароли приложений их не содержат; у Gmail пробелы внутри — можно).
    password = str(data.get("password") or "").strip()
    now = time.time()
    with db.connect() as con:
        if data.get("id"):
            old = con.execute("SELECT * FROM mailboxes WHERE id=? AND owner=?", (int(data["id"]), owner)).fetchone()
            if not old:
                raise MailError("ящик не найден")
            if password:
                fields["secret"] = encrypt(password)
            changed_login = any(str(old[k]) != str(fields[k]) for k in ("address", "login", "smtp_host", "smtp_port",
                                                                         "imap_host", "imap_port"))
            if password or changed_login:              # старая проверка больше не про эти настройки
                fields.update(check_ok=None, check_text="", checked_at=None)
            sets = ", ".join(f"{k}=?" for k in fields)
            try:
                con.execute(f"UPDATE mailboxes SET {sets}, updated_at=? WHERE id=? AND owner=?",
                            (*fields.values(), now, int(data["id"]), owner))
            except sqlite3.IntegrityError as exc:
                raise MailError("такой ящик у вас уже есть") from exc
            box_id = int(data["id"])
        else:
            if not password:
                raise MailError("укажите пароль приложения для ящика")
            fields["secret"] = encrypt(password)
            first = not con.execute("SELECT 1 FROM mailboxes WHERE owner=?", (owner,)).fetchone()
            cols = ", ".join(fields) + ", owner, is_default, created_at, updated_at"
            try:
                cur = con.execute(f"INSERT INTO mailboxes ({cols}) VALUES ({', '.join('?' * (len(fields) + 4))})",
                                  (*fields.values(), owner, int(first), now, now))
            except sqlite3.IntegrityError as exc:
                raise MailError("такой ящик у вас уже есть") from exc
            box_id = cur.lastrowid
    return get_box(owner, box_id)


def delete_box(owner: str, box_id: int) -> None:
    init()
    with db.connect() as con:
        row = con.execute("SELECT is_default FROM mailboxes WHERE id=? AND owner=?", (int(box_id), owner)).fetchone()
        if not row:
            raise MailError("ящик не найден")
        con.execute("DELETE FROM mailboxes WHERE id=? AND owner=?", (int(box_id), owner))
        if row["is_default"]:
            con.execute("UPDATE mailboxes SET is_default=1 WHERE id=(SELECT id FROM mailboxes WHERE owner=? "
                        "ORDER BY id LIMIT 1)", (owner,))


def set_default(owner: str, box_id: int) -> None:
    _row(owner, box_id)
    with db.connect() as con:
        con.execute("UPDATE mailboxes SET is_default=(id=?) WHERE owner=?", (int(box_id), owner))


# ---------- SMTP / IMAP ----------

def _explain(exc: Exception, what: str) -> str:
    text = str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError) or "AUTHENTICATIONFAILED" in text.upper() \
            or "authentication failed" in text.lower() or "invalid credentials" in text.lower():
        # Ответ сервера дословно (17.11.2026: без него не понять, что именно не так — пароль приложения нужен,
        # доступ программам выключен или пароль неверен). Пароля в ответе сервера нет.
        raw = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc, smtplib.SMTPAuthenticationError) \
            and isinstance(exc.smtp_error, bytes) else text
        raw = re.sub(r"\s+", " ", raw).strip()[:200]
        return (f"{what}: сервер не принял логин или пароль (ответ сервера: «{raw}»). Нужен пароль приложения "
                "(создаётся в настройках безопасности почты), а не обычный пароль; у Яндекса и Mail.ru ещё должен "
                "быть разрешён доступ почтовыми программами.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"{what}: сервер не ответил за {TIMEOUT:.0f} с — нет связи (порт закрыт сетью или хостингом?)"
    if isinstance(exc, socket.gaierror):
        return f"{what}: сервер не найден — проверьте имя сервера"
    if isinstance(exc, ConnectionRefusedError):
        return f"{what}: сервер отказал в соединении — проверьте порт"
    if isinstance(exc, ssl.SSLError):
        return f"{what}: ошибка защищённого соединения ({text[:120]}) — проверьте порт (465 / 587 / 993)"
    return f"{what}: {type(exc).__name__}: {text[:200]}"


def _smtp(row, password: str) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    host, port = row["smtp_host"], int(row["smtp_port"])
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=TIMEOUT)
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
    s.login(row["login"] or row["address"], password)
    return s


def _imap(row, password: str) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    host, port = row["imap_host"], int(row["imap_port"])
    if port == 143:
        m = imaplib.IMAP4(host, port, timeout=TIMEOUT)
        m.starttls(ssl_context=ctx)
    else:
        m = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=TIMEOUT)
    m.login(row["login"] or row["address"], password)
    return m


def check_box(owner: str, box_id: int) -> dict:
    """Войти по SMTP и по IMAP. Результат пишется в ящик и возвращается: {ok, smtp, imap, text}."""
    row = _row(owner, box_id)
    password = decrypt(row["secret"])
    res = {"smtp": "", "imap": ""}
    try:
        s = _smtp(row, password)
        try:
            s.noop()
        finally:
            try:
                s.quit()
            except Exception:                       # noqa: BLE001
                pass
        res["smtp"] = "ok"
    except Exception as exc:                        # noqa: BLE001
        res["smtp"] = _explain(exc, "Отправка (SMTP)")
    if row["imap_host"]:
        try:
            m = _imap(row, password)
            try:
                m.select("INBOX", readonly=True)
            finally:
                try:
                    m.logout()
                except Exception:                   # noqa: BLE001
                    pass
            res["imap"] = "ok"
        except Exception as exc:                    # noqa: BLE001
            res["imap"] = _explain(exc, "Чтение (IMAP)")
    else:
        res["imap"] = "Чтение (IMAP): сервер не указан — ответы поставщиков сайт читать не сможет"
    ok = res["smtp"] == "ok"
    parts = ["отправка — работает" if ok else res["smtp"],
             "чтение — работает" if res["imap"] == "ok" else res["imap"]]
    text = "; ".join(parts)
    with db.connect() as con:
        con.execute("UPDATE mailboxes SET check_ok=?, check_text=?, checked_at=? WHERE id=?",
                    (int(ok), text, time.time(), int(box_id)))
    return {"ok": ok, "imap_ok": res["imap"] == "ok", "text": text}


def build_message(row, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = (),
                  headers: dict | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((row["sender_name"], row["address"])) if row["sender_name"] else row["address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=row["address"].rsplit("@", 1)[-1])
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    for name, data, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


def _append_sent(row, password: str, msg: EmailMessage) -> str:
    """Копия в «Отправленные» по IMAP (Яндекс и Mail.ru сами её не кладут при отправке через SMTP). '' — получилось."""
    if not row["imap_host"] or "gmail.com" in row["smtp_host"]:   # Gmail кладёт копию сам
        return ""
    try:
        m = _imap(row, password)
    except Exception as exc:                        # noqa: BLE001
        return _explain(exc, "копия в «Отправленные»")
    try:
        folder = None
        typ, data = m.list()
        for line in data or []:
            s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if "\\Sent" in s:
                folder = s.rsplit(' "', 1)[-1].strip('"') if ' "' in s else s.split()[-1]
                break
        folder = folder or "Sent"
        typ, _ = m.append(f'"{folder}"' if " " in folder else folder, "\\Seen",
                          imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        return "" if typ == "OK" else f"копия в «Отправленные»: ответ {typ}"
    except Exception as exc:                        # noqa: BLE001
        return _explain(exc, "копия в «Отправленные»")
    finally:
        try:
            m.logout()
        except Exception:                           # noqa: BLE001
            pass


def send_many(owner: str, box_id: int, messages: list[EmailMessage], pause: float = 1.0) -> list[dict]:
    """Отправить письма одним соединением SMTP. [{ok, error, message_id, sent_copy}] — по порядку."""
    row = _row(owner, box_id)
    password = decrypt(row["secret"])
    try:
        s = _smtp(row, password)
    except Exception as exc:                        # noqa: BLE001
        err = _explain(exc, "Отправка (SMTP)")
        with db.connect() as con:
            con.execute("UPDATE mailboxes SET check_ok=0, check_text=?, checked_at=? WHERE id=?",
                        (err, time.time(), int(box_id)))
        return [{"ok": False, "error": err, "message_id": m["Message-ID"]} for m in messages]
    out = []
    try:
        for i, msg in enumerate(messages):
            if i:
                time.sleep(pause)                   # почтовые серверы не любят пачку писем за секунду
            try:
                refused = s.send_message(msg)
                if refused:
                    out.append({"ok": False, "error": "сервер не принял адрес: " + ", ".join(refused),
                                "message_id": msg["Message-ID"]})
                    continue
                out.append({"ok": True, "error": "", "message_id": msg["Message-ID"]})
            except smtplib.SMTPServerDisconnected:
                try:                                # соединение оборвалось — один повтор
                    s = _smtp(row, password)
                    s.send_message(msg)
                    out.append({"ok": True, "error": "", "message_id": msg["Message-ID"]})
                except Exception as exc:            # noqa: BLE001
                    out.append({"ok": False, "error": _explain(exc, "Отправка"), "message_id": msg["Message-ID"]})
            except Exception as exc:                # noqa: BLE001
                out.append({"ok": False, "error": _explain(exc, "Отправка"), "message_id": msg["Message-ID"]})
    finally:
        try:
            s.quit()
        except Exception:                           # noqa: BLE001
            pass
    for msg, r in zip(messages, out):
        if r["ok"]:
            r["sent_copy"] = _append_sent(row, password, msg)
    with db.connect() as con:
        con.execute("UPDATE mailboxes SET check_ok=1, check_text=?, checked_at=? WHERE id=? AND check_ok IS NOT 1",
                    ("отправка — работает", time.time(), int(box_id)))
    return out


def owner_key() -> str:
    """Хозяин ящиков — вошедший сотрудник ('' — вход на сайт выключен). С 0.36.0 — или сотрудник, чьи ящики у него
    общие («Почтовые ящики как у …» в админ-панели): тогда те же ящики, ответы поставщиков и неразобранная почта."""
    from . import access
    u = access.current_user()
    if not u:
        return ""
    return (u.get("mail_owner") or "").strip() or u["login"]


def merge_owner(src: str, dst: str) -> str:
    """Ящики, письма и ответы сотрудника src — к общим ящикам dst (0.36.0). Ящик с тем же адресом у dst уже есть —
    остаётся ящик dst, двойник src убирается (иначе сайт читал бы одну почту дважды). Возвращает, что сделано."""
    if not src or not dst or src == dst:
        return ""
    init()
    moved = same = 0
    with db.connect() as con:
        have = {r["address"].lower() for r in con.execute("SELECT address FROM mailboxes WHERE owner=?", (dst,))}
        has_default = bool(con.execute("SELECT 1 FROM mailboxes WHERE owner=? AND is_default=1", (dst,)).fetchone())
        for r in con.execute("SELECT id, address FROM mailboxes WHERE owner=?", (src,)).fetchall():
            if r["address"].lower() in have:
                # тот же адрес уже есть в общих — второй ящик убрать, иначе почта читалась бы дважды
                con.execute("DELETE FROM mailboxes WHERE id=?", (r["id"],))
                try:
                    con.execute("DELETE FROM mail_inbox_state WHERE mailbox_id=?", (r["id"],))
                except sqlite3.OperationalError:
                    pass
                same += 1
                continue
            con.execute("UPDATE mailboxes SET owner=?, is_default=CASE WHEN ? THEN 0 ELSE is_default END WHERE id=?",
                        (dst, has_default, r["id"]))
            have.add(r["address"].lower())
            moved += 1
        for table in ("mail_inbox", "mail_requests"):
            try:
                con.execute(f"UPDATE {table} SET owner=? WHERE owner=?", (dst, src))
            except sqlite3.OperationalError:
                pass                                 # таблицы ещё нет (почтой не пользовались)
    parts = []
    if moved:
        parts.append(f"ящиков перенесено в общие: {moved}")
    if same:
        parts.append(f"двойников с тем же адресом убрано: {same}")
    return ", ".join(parts)
