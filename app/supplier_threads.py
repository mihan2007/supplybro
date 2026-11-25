"""
Переписка с поставщиками по конкурсу (0.32.0).

Пользователь 24.11.2026 (на примере К947): «поставщики ответили, но не назвали цены, задали вопросы — можно ли
сделать мини-диалоговое окно с каждым из поставщиков, и желательно, чтобы это не перегрузило интерфейс».

Нить переписки — один поставщик в одном конкурсе: наши письма (`mail_requests`: запросы и ответы с сайта) и его
письма (`mail_inbox`, привязанные к конкурсу). Ключ нити: «s<Id поставщика>» — поставщик из NocoDB, иначе
«a<адрес>» — адресат письма «другому адресату» / отправитель. Письмо поставщика относится к нити запроса, на который
оно пришло (`request_id`), а без него — к нити, в чьи адреса входит отправитель.

Ответ с сайта (`reply`) — с того же ящика, с которого ушёл запрос (только его владелец), тема «Re: …» с прежней
меткой [SB-…], заголовки In-Reply-To / References — поэтому следующий ответ поставщика сам привяжется к конкурсу
(app/mail_inbox.match, признак reply). В журнале запросов — строка kind='reply'. Внизу письма — цитата последнего
письма поставщика (как в обычной почте).

«Ждёт ответа» — последнее письмо нити от поставщика и закупщик не нажал «Ответ не нужен» после него
(`mail_thread_state`). Текст писем поставщика: `mail_inbox.body` (с 0.32.0), у прежних — «Письмо.txt» в папке письма.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from . import db, mail, mail_inbox, supplier_requests

MSK = timezone(timedelta(hours=3))
MAX_BODY = 20000            # символов текста нашего ответа
QUOTE_CHARS = 3000          # сколько текста письма поставщика цитировать в ответе

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_thread_state (
    client_id  TEXT NOT NULL,
    internal   TEXT NOT NULL,
    key        TEXT NOT NULL,
    done_at    REAL NOT NULL,          -- «ответ не нужен»: письма поставщика до этого времени не ждут ответа
    done_by    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (client_id, internal, key)
);
"""
_inited = False


class ThreadError(RuntimeError):
    pass


def init() -> None:
    global _inited
    if _inited:
        return
    mail_inbox.init()
    with db.connect() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(mail_requests)")}
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'request'"), ("reply_to", "TEXT NOT NULL DEFAULT ''")):
            if name not in cols:
                try:
                    con.execute(f"ALTER TABLE mail_requests ADD COLUMN {name} {ddl}")
                except Exception:                  # noqa: BLE001 — колонку добавил другой процесс
                    pass
    _inited = True


def _addrs(text: str) -> list[str]:
    return [a.strip().lower() for a in re.split(r"[,;\s]+", str(text or "")) if "@" in a]


def request_key(r: dict) -> str:
    if r.get("supplier_id"):
        return "s" + str(r["supplier_id"])
    a = _addrs(r.get("to_addr"))
    return "a" + (a[0] if a else str(r.get("id")))


def _load(client_id: str, internal: str) -> tuple[list[dict], list[dict], dict[str, float]]:
    init()
    with db.connect() as con:
        reqs = [dict(r) for r in con.execute(
            "SELECT * FROM mail_requests WHERE client_id=? AND internal=? ORDER BY id", (client_id, internal))]
        inbox = [dict(r) for r in con.execute(
            "SELECT * FROM mail_inbox WHERE client_id=? AND internal=? AND status IN ('delivered','processing','error') "
            "ORDER BY received_at", (client_id, internal))]
        done = {r["key"]: r["done_at"] for r in con.execute(
            "SELECT key, done_at FROM mail_thread_state WHERE client_id=? AND internal=?", (client_id, internal))}
    return reqs, inbox, done


def _group(reqs: list[dict], inbox: list[dict]) -> dict[str, dict]:
    by_id = {r["id"]: r for r in reqs}
    by_addr: dict[str, str] = {}
    threads: dict[str, dict] = {}
    for r in reqs:
        k = request_key(r)
        t = threads.setdefault(k, {"key": k, "reqs": [], "inbox": []})
        t["reqs"].append(r)
        for a in _addrs(r["to_addr"]):
            by_addr.setdefault(a, k)
    for m in inbox:
        r = by_id.get(m.get("request_id"))
        k = request_key(r) if r else by_addr.get(m["from_addr"]) or "a" + m["from_addr"]
        threads.setdefault(k, {"key": k, "reqs": [], "inbox": []})["inbox"].append(m)
    return threads


def _summary(t: dict, done: dict) -> dict:
    sent = [r for r in t["reqs"] if r["status"] == "sent"]
    last_out = max((r["sent_at"] or 0 for r in sent), default=0)
    # Когда письмо поставщика увидел сайт (created_at), а не дата в письме: у поставщика часы могут отставать.
    last_in = max((max(m["received_at"] or 0, m["created_at"] or 0) for m in t["inbox"]), default=0)
    root = (sent or t["reqs"] or [{}])[-1]
    name = root.get("supplier_name") or ""
    if not name and t["inbox"]:
        m = t["inbox"][-1]
        name = m["from_name"] or m["from_addr"]
    return {
        "key": t["key"], "supplier_id": root.get("supplier_id") or "", "supplier_name": name or t["key"][1:],
        "to": root.get("to_addr") or (t["inbox"][-1]["from_addr"] if t["inbox"] else ""),
        "mailbox_id": root.get("mailbox_id"), "mailbox_address": root.get("mailbox_address") or "",
        "owner": root.get("owner") or "",
        "out": len(sent), "in": len(t["inbox"]), "last_at": max(last_out, last_in),
        "last_from": "supplier" if last_in > last_out else "us",
        "waiting": last_in > last_out and last_in > done.get(t["key"], 0),
        "positions": sorted({str(n) for r in t["reqs"] for n in json.loads(r["positions"] or "[]")},
                            key=lambda x: (len(x), x)),
    }


def threads(client_id: str, internal: str) -> list[dict]:
    """Нити с письмами поставщика (только кто ответил), сначала ждущие ответа, потом по последнему письму."""
    reqs, inbox, done = _load(client_id, internal)
    out = [_summary(t, done) for t in _group(reqs, inbox).values() if t["inbox"]]
    out.sort(key=lambda s: (not s["waiting"], -s["last_at"]))
    return out


def _fmt(ts) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=MSK).strftime("%d.%m.%Y %H:%M")


def _inbox_text(client, m: dict) -> str:
    if m.get("body"):
        return m["body"]
    if m.get("saved_dir") and m.get("folder"):
        from .tenders import FolderError, folder_path
        try:
            p = folder_path(client, m["folder"]) / m["saved_dir"] / "Письмо.txt"
            text = p.read_text(encoding="utf-8", errors="replace")
            body = text.split("\n\n", 1)[1] if "\n\n" in text else text
            return re.sub(r"\n*\(цитата прежней переписки — в файле письмо\.eml\)\s*$", "", body).strip()
        except (FolderError, OSError):
            pass
    return m.get("preview") or ""


def messages(client, client_id: str, internal: str, key: str) -> dict:
    """Письма нити по времени: [{dir in|out, at, who, to, subject, text, attachments, status, error, kind}]."""
    reqs, inbox, done = _load(client_id, internal)
    t = _group(reqs, inbox).get(key)
    if not t:
        raise ThreadError("переписка не найдена")
    msgs = []
    for r in t["reqs"]:
        msgs.append({"dir": "out", "id": r["id"], "at": r["sent_at"] or r["created_at"], "kind": r.get("kind") or "request",
                     "who": r["mailbox_address"] + (f" ({r['owner']})" if r["owner"] else ""), "to": r["to_addr"],
                     "subject": r["subject"], "text": r["body"], "status": r["status"], "error": r["error"],
                     "attachments": [r["attachment"]] if r["attachment"] else []})
    for m in t["inbox"]:
        msgs.append({"dir": "in", "id": m["id"], "at": m["received_at"], "kind": "letter", "addr": m["from_addr"],
                     "_seen": max(m["received_at"] or 0, m["created_at"] or 0),
                     "who": f"{m['from_name']} <{m['from_addr']}>" if m["from_name"] else m["from_addr"],
                     "to": "", "subject": m["subject"], "text": _inbox_text(client, m), "status": m["status"],
                     "error": m["error"], "attachments": json.loads(m["attachments"] or "[]")})
    msgs.sort(key=lambda x: (x.pop("_seen", None) or x["at"] or 0, x["dir"] == "in"))
    return {"thread": _summary(t, done), "messages": msgs}


def mark_done(client_id: str, internal: str, key: str, by: str) -> None:
    init()
    with db.connect() as con:
        con.execute("INSERT INTO mail_thread_state (client_id, internal, key, done_at, done_by) VALUES (?,?,?,?,?) "
                    "ON CONFLICT (client_id, internal, key) DO UPDATE SET done_at=excluded.done_at, "
                    "done_by=excluded.done_by", (client_id, internal, key, time.time(), by))


def _quote(m: dict, text: str) -> str:
    text = text.strip()
    if len(text) > QUOTE_CHARS:
        text = text[:QUOTE_CHARS].rsplit(" ", 1)[0] + " …"
    who = m["from_name"] or m["from_addr"]
    return f"\n\n{_fmt(m['received_at'])}, {who} пишет:\n" + "\n".join("> " + line for line in text.splitlines())


def reply(client, *, owner: str, client_id: str, internal: str, key: str, to: str, body: str,
          box_id: int | None = None) -> dict:
    """Ответить поставщику в нити. Возвращает {ok, error, id, sent_copy}."""
    reqs, inbox, _ = _load(client_id, internal)
    t = _group(reqs, inbox).get(key)
    if not t:
        raise ThreadError("переписка не найдена")
    body = str(body or "").strip()
    if not body:
        raise ThreadError("пустой ответ")
    if len(body) > MAX_BODY:
        raise ThreadError(f"ответ длиннее {MAX_BODY} символов")
    addrs = supplier_requests.parse_addresses(to)
    if not addrs:
        raise ThreadError("не указан адрес")
    sent = [r for r in t["reqs"] if r["status"] == "sent"]
    root = sent[-1] if sent else None
    last_in = t["inbox"][-1] if t["inbox"] else None
    if root is None and last_in is None:
        raise ThreadError("в переписке нет писем")
    box_id = int(box_id or (root and root["mailbox_id"]) or (last_in and last_in["mailbox_id"]) or 0)
    try:
        box = mail._row(owner, box_id)
    except mail.MailError:
        who = (root or {}).get("owner") or (last_in or {}).get("owner") or ""
        raise ThreadError("запрос отправлен с ящика " + ((root or {}).get("mailbox_address") or "другого сотрудника") +
                          (f" ({who})" if who else "") + " — ответить с сайта может только его владелец") from None
    base = (last_in or {}).get("subject") or (root or {}).get("subject") or f"Запрос цен {internal}"
    subject = base if re.match(r"(?i)\s*(re|ответ)\s*:", base) else "Re: " + base
    tag = ""
    if (m := mail_inbox.TAG_RE.search(subject)) is None and root and root.get("tag"):
        subject = f"{subject} [{root['tag']}]"
        tag = root["tag"]
    elif m:
        tag = m.group(0)
    subject = subject[:250]
    full = body + (_quote(last_in, _inbox_text(client, last_in)) if last_in else "")
    refs = [x for x in ((root or {}).get("message_id"), (last_in or {}).get("message_id")) if x]
    in_reply = (last_in or root or {}).get("message_id") or ""
    headers = {"X-SupplyBro-Tag": tag} if tag else {}
    if in_reply:
        headers["In-Reply-To"] = in_reply
    if refs:
        headers["References"] = " ".join(dict.fromkeys(refs))
    src = root or {}
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO mail_requests (client_id, base_id, record_id, internal, folder, supplier_id, supplier_name, "
            "to_addr, mailbox_id, mailbox_address, owner, subject, body, positions, created_at, kind, reply_to, tag) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client_id, src.get("base_id") or "", src.get("record_id"), internal,
             src.get("folder") or (last_in or {}).get("folder") or "", src.get("supplier_id") or "",
             src.get("supplier_name") or (last_in or {}).get("from_name") or "", ", ".join(addrs), box_id,
             box["address"], owner, subject, full, src.get("positions") or "[]", time.time(), "reply", in_reply, tag))
        rid = cur.lastrowid
    msg = mail.build_message(box, ", ".join(addrs), subject, full, (), headers)
    with db.connect() as con:
        con.execute("UPDATE mail_requests SET message_id=? WHERE id=?", (msg["Message-ID"], rid))
    r = mail.send_many(owner, box_id, [msg])[0]
    with db.connect() as con:
        con.execute("UPDATE mail_requests SET status=?, error=?, sent_copy=?, sent_at=? WHERE id=?",
                    ("sent" if r["ok"] else "error", r["error"], r.get("sent_copy", ""),
                     time.time() if r["ok"] else None, rid))
    if r["ok"] and src.get("supplier_id"):
        supplier_requests.remember_email(client_id, src.get("base_id") or "", str(src["supplier_id"]),
                                         str(src.get("supplier_name") or ""), ", ".join(addrs), owner)
    return {"ok": r["ok"], "error": r["error"], "id": rid, "sent_copy": r.get("sent_copy", "")}
