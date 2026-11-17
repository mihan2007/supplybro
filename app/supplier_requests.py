"""
Запросы цен поставщикам по почте (0.27.0, этап 1).

Пользователь 17.11.2026: «нужно сделать, чтобы можно было формировать электронные письма поставщикам с запросами цен,
брать позиции из спецификации, потом отправлять письма с почтового ящика» (закупщик, свой ящик — app/mail.py).
По умолчанию (пользователь не ответил): каждое письмо закупщик видит и отправляет кнопкой; адреса поставщиков
хранятся на сайте (`supplier_emails`), структура NocoDB не меняется.

Черновики: позиции спецификации NocoDB раскладываются по поставщикам из таблицы «Поставщики» по брендам
(app/supplier_brands.py) — на поставщика одно письмо с его позициями; «Не работаем» — в конце, без галочки; плюс
письмо «Другому адресату» со всеми позициями. Номера позиций в письме и во вложении Excel — номера спецификации
(№), чтобы ответ можно было сопоставить. Тема — с меткой `[SB-K934-<id>]` и заголовок `X-SupplyBro-Tag`: по ним
(и по Message-ID / In-Reply-To) этап 2 (0.28.0) привяжет ответ к конкурсу.

Журнал — `mail_requests`: кто, с какого ящика, кому, какие позиции, Message-ID, метка, итог отправки.
"""

from __future__ import annotations

import io
import json
import re
import time

from . import db, mail

SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_emails (
    client_id     TEXT NOT NULL,
    base_id       TEXT NOT NULL,
    supplier_id   TEXT NOT NULL,              -- Id записи «Поставщиков»
    supplier_name TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL,
    updated_by    TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL,
    PRIMARY KEY (client_id, base_id, supplier_id)
);
CREATE TABLE IF NOT EXISTS mail_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,
    base_id         TEXT NOT NULL DEFAULT '',
    record_id       INTEGER,
    internal        TEXT NOT NULL DEFAULT '',     -- внутренний номер конкурса (К934)
    folder          TEXT NOT NULL DEFAULT '',
    supplier_id     TEXT NOT NULL DEFAULT '',     -- '' — другой адресат
    supplier_name   TEXT NOT NULL DEFAULT '',
    to_addr         TEXT NOT NULL,
    mailbox_id      INTEGER,
    mailbox_address TEXT NOT NULL DEFAULT '',
    owner           TEXT NOT NULL DEFAULT '',     -- логин закупщика
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    positions       TEXT NOT NULL DEFAULT '[]',   -- JSON: [№ позиции спецификации]
    attachment      TEXT NOT NULL DEFAULT '',
    message_id      TEXT NOT NULL DEFAULT '',
    tag             TEXT NOT NULL DEFAULT '',     -- SB-K934-12
    status          TEXT NOT NULL DEFAULT 'sending',   -- sending / sent / error
    error           TEXT NOT NULL DEFAULT '',
    sent_copy       TEXT NOT NULL DEFAULT '',     -- ошибка копии в «Отправленные» ('' — положена или не нужна)
    created_at      REAL NOT NULL,
    sent_at         REAL
);
CREATE INDEX IF NOT EXISTS mail_requests_tender ON mail_requests (client_id, internal);
CREATE INDEX IF NOT EXISTS mail_requests_msgid ON mail_requests (message_id);
"""

NAME_COL, BRAND_COL, MAKER_COL = "Наименование позиции", "Марка/модель", "Производитель"
QTY_COL, UNIT_COL = "Кол-во", "Ед. изм."
MAX_LETTERS = 60          # писем за одно нажатие
_inited = False


class RequestError(RuntimeError):
    pass


def init() -> None:
    global _inited
    if _inited:
        return
    with db.connect() as con:
        con.executescript(SCHEMA)
    _inited = True


def latin_tag(internal: str) -> str:
    """«К934» → «K934» (метка в теме — латиницей: так её не испортит ни одна почтовая программа)."""
    t = str(internal or "").strip().translate(str.maketrans("КкТтМмНнАаВвЕеОоРрСсХх", "KkTtMmHhAaBbEeOoPpCcXx"))
    return re.sub(r"[^0-9A-Za-z-]", "", t) or "T"


# ---------- позиции ----------

def _num(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value if value not in (None, "") else "").strip()


def positions_from_rows(rows: list[dict]) -> list[dict]:
    """Строки спецификации → [{n, name, brand, maker, qty, unit}] (n — № спецификации, иначе порядковый)."""
    out = []
    for i, r in enumerate(rows):
        name = str(r.get(NAME_COL) or "").strip()
        if not name:
            continue
        out.append({
            "n": _num(r.get("№")) or str(i + 1),
            "name": re.sub(r"\s+", " ", name)[:400],
            "brand": str(r.get(BRAND_COL) or "").strip()[:200],
            "maker": str(r.get(MAKER_COL) or "").strip()[:120],
            "qty": _num(r.get(QTY_COL)),
            "unit": str(r.get(UNIT_COL) or "").strip()[:20],
        })
    return out


def position_line(p: dict) -> str:
    extra = p["brand"] if p["brand"] and p["brand"].lower() not in p["name"].lower() else ""
    if p["maker"] and p["maker"].lower() not in (p["name"] + " " + extra).lower():
        extra = (extra + ", " if extra else "") + p["maker"]
    qty = " — " + " ".join(x for x in (p["qty"], p["unit"]) if x) if p["qty"] else ""
    return f"{p['n']}. {p['name']}" + (f" ({extra})" if extra else "") + qty


def default_signature(box: dict | None, user: dict | None) -> str:
    if box and box.get("signature"):
        return box["signature"]
    lines = []
    name = (box or {}).get("sender_name") or (user or {}).get("name") or ""
    if name:
        lines.append(name)
    if user and user.get("phone"):
        lines.append("тел. " + user["phone"])
    if box:
        lines.append(box["address"])
    return "\n".join(["С уважением,"] + lines) if lines else "С уважением"


INTRO = ("Добрый день!\n\n"
         "Просим предоставить коммерческое предложение на поставку следующих позиций:\n\n")
ASK = ("Просим указать по каждой позиции: цену за единицу с НДС (и ставку НДС), наличие на складе или срок поставки, "
       "страну происхождения, срок действия предложения. Если позиция недоступна — предложите, пожалуйста, аналог.\n")
XLSX_LINE = "\nПеречень позиций — также во вложении (Excel): цены можно указать прямо в нём.\n"


def outro(signature: str, with_xlsx: bool = True) -> str:
    return "\n\n" + ASK + (XLSX_LINE if with_xlsx else "") + "\n" + signature + "\n"


def letter_body(positions: list[dict], signature: str, with_xlsx: bool = True) -> str:
    """Текст письма; страница собирает его так же (intro + строки позиций + outro), когда меняют набор позиций."""
    return INTRO + "\n".join(position_line(p) for p in positions) + outro(signature, with_xlsx)


def subject_of(internal: str, title: str) -> str:
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    t = (t[:70].rsplit(" ", 1)[0] + "…") if len(t) > 72 else t
    return f"Запрос цен {internal}" + (f": {t}" if t else "")


def xlsx_bytes(internal: str, positions: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Запрос цен"
    head = ["№", "Наименование", "Марка / модель", "Производитель", "Кол-во", "Ед. изм.",
            "Цена за ед. с НДС, руб.", "Ставка НДС, %", "Наличие / срок поставки", "Страна происхождения",
            "Примечание (аналог и др.)"]
    ws.append([f"Запрос цен {internal}"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([])
    ws.append(head)
    fill = PatternFill("solid", fgColor="FFF2CC")
    for c in ws[3]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for p in positions:
        qty = p["qty"]
        try:
            qty = float(qty.replace(",", ".")) if qty else None
            qty = int(qty) if qty is not None and qty.is_integer() else qty
        except (ValueError, AttributeError):
            pass
        ws.append([p["n"], p["name"], p["brand"], p["maker"], qty, p["unit"], None, None, None, None, None])
        for col in range(7, 12):
            ws.cell(ws.max_row, col).fill = fill
    for col, w in zip("ABCDEFGHIJK", (6, 55, 24, 18, 8, 8, 16, 10, 20, 16, 28)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=4):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A4"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- адреса поставщиков ----------

def emails(client_id: str, base_id: str) -> dict[str, str]:
    init()
    with db.connect() as con:
        return {r["supplier_id"]: r["email"] for r in con.execute(
            "SELECT supplier_id, email FROM supplier_emails WHERE client_id=? AND base_id=?", (client_id, base_id))}


def remember_email(client_id: str, base_id: str, supplier_id: str, name: str, email: str, by: str) -> None:
    if not supplier_id or not email:
        return
    init()
    with db.connect() as con:
        con.execute("INSERT INTO supplier_emails (client_id, base_id, supplier_id, supplier_name, email, updated_by, "
                    "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (client_id, base_id, supplier_id) DO UPDATE SET "
                    "email=excluded.email, supplier_name=excluded.supplier_name, updated_by=excluded.updated_by, "
                    "updated_at=excluded.updated_at", (client_id, base_id, supplier_id, name, email, by, time.time()))


def parse_addresses(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,;\s]+", str(text or "")) if p.strip()]
    bad = [p for p in parts if not mail.EMAIL_RE.match(p)]
    if bad:
        raise RequestError("неверный адрес: " + ", ".join(bad))
    return parts


# ---------- черновики и журнал ----------

def journal(client_id: str, internal: str, limit: int = 200) -> list[dict]:
    init()
    with db.connect() as con:
        rows = con.execute("SELECT * FROM mail_requests WHERE client_id=? AND internal=? ORDER BY id DESC LIMIT ?",
                           (client_id, internal, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["positions"] = json.loads(d["positions"] or "[]")
        d.pop("body", None)
        out.append(d)
    return out


def drafts(*, client_id: str, base_id: str, internal: str, title: str, rows: list[dict], brands: dict | None,
           box: dict | None, user: dict | None) -> dict:
    """Черновики писем: по поставщикам с брендами позиций + «Другому адресату» со всеми позициями."""
    positions = positions_from_rows(rows)
    by_n = {p["n"]: p for p in positions}
    known = emails(client_id, base_id)
    sent = {}
    for j in journal(client_id, internal):
        if j["status"] == "sent" and j["supplier_id"]:
            sent.setdefault(j["supplier_id"], j["sent_at"])
    signature = default_signature(box, user)
    subject = subject_of(internal, title)
    letters = []
    sup = (brands or {}).get("suppliers") or {}
    for e in (brands or {}).get("summary", {}).get("by_supplier", []):
        s = sup.get(e["id"])
        if not s:
            continue
        pos = [by_n[n] for n in e["positions"] if n in by_n]
        if not pos:
            continue
        email = known.get(e["id"], "")
        letters.append({
            "key": "s" + e["id"], "supplier_id": e["id"], "supplier_name": s["name"], "level": s["level"],
            "status": s["status"], "method": s["method"], "site": s["site"], "brands": e["brands"], "strong": e["strong"],
            "to": email, "checked": bool(email) and s["level"] != "grey", "sent_at": sent.get(e["id"]),
            "subject": subject, "positions": [p["n"] for p in pos], "body": letter_body(pos, signature),
        })
    letters.append({
        "key": "other", "supplier_id": "", "supplier_name": "", "level": "", "status": "", "method": "", "site": "",
        "brands": [], "strong": True, "to": "", "checked": False, "sent_at": None,
        "subject": subject, "positions": [p["n"] for p in positions], "body": letter_body(positions, signature),
    })
    for p in positions:
        p["line"] = position_line(p)
    return {"positions": positions, "letters": letters, "signature": signature, "intro": INTRO,
            "outro_xlsx": outro(signature, True), "outro_plain": outro(signature, False)}


def send(*, owner: str, user: dict | None, client_id: str, base_id: str, record_id: int, internal: str, folder: str,
         box_id: int, letters: list[dict], positions: list[dict], with_xlsx: bool = True) -> list[dict]:
    """Отправить письма с ящика сотрудника. letters: [{key, supplier_id, supplier_name, to, subject, body,
    positions: [№]}]. Ответ: [{key, ok, error, id, tag}]."""
    init()
    if not letters:
        raise RequestError("не выбрано ни одного письма")
    if len(letters) > MAX_LETTERS:
        raise RequestError(f"за один раз — не больше {MAX_LETTERS} писем")
    box_row = mail._row(owner, box_id)
    by_n = {p["n"]: p for p in positions}
    prepared = []
    for L in letters:
        to = parse_addresses(L.get("to"))
        if not to:
            raise RequestError(f"не указан адрес: {L.get('supplier_name') or 'другой адресат'}")
        subject = str(L.get("subject") or "").strip()[:250]
        body = str(L.get("body") or "").strip()
        if not subject or not body:
            raise RequestError(f"пустая тема или текст письма: {L.get('supplier_name') or ', '.join(to)}")
        pos = [by_n[str(n)] for n in L.get("positions") or [] if str(n) in by_n]
        prepared.append((L, to, subject, body, pos))
    now = time.time()
    messages, ids = [], []
    for L, to, subject, body, pos in prepared:
        with db.connect() as con:
            cur = con.execute(
                "INSERT INTO mail_requests (client_id, base_id, record_id, internal, folder, supplier_id, supplier_name, "
                "to_addr, mailbox_id, mailbox_address, owner, subject, body, positions, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, base_id, record_id, internal, folder, str(L.get("supplier_id") or ""),
                 str(L.get("supplier_name") or "")[:200], ", ".join(to), box_id, box_row["address"], owner, subject,
                 body, json.dumps([p["n"] for p in pos], ensure_ascii=False), now))
            rid = cur.lastrowid
        tag = f"SB-{latin_tag(internal)}-{rid}"
        full_subject = subject if f"[{tag}]" in subject else f"{subject} [{tag}]"
        attach = []
        if with_xlsx and pos:
            attach.append((f"Запрос цен {internal}.xlsx", xlsx_bytes(internal, pos),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        msg = mail.build_message(box_row, ", ".join(to), full_subject, body, attach, {"X-SupplyBro-Tag": tag})
        with db.connect() as con:
            con.execute("UPDATE mail_requests SET subject=?, tag=?, message_id=?, attachment=? WHERE id=?",
                        (full_subject, tag, msg["Message-ID"], attach[0][0] if attach else "", rid))
        messages.append(msg)
        ids.append((rid, tag))
    results = mail.send_many(owner, box_id, messages)
    out = []
    for (L, to, *_), (rid, tag), r in zip(prepared, ids, results):
        with db.connect() as con:
            con.execute("UPDATE mail_requests SET status=?, error=?, sent_copy=?, sent_at=? WHERE id=?",
                        ("sent" if r["ok"] else "error", r["error"], r.get("sent_copy", ""),
                         time.time() if r["ok"] else None, rid))
        if r["ok"] and L.get("supplier_id"):
            remember_email(client_id, base_id, str(L["supplier_id"]), str(L.get("supplier_name") or ""),
                           ", ".join(to), owner)
        out.append({"key": L.get("key"), "ok": r["ok"], "error": r["error"], "id": rid, "tag": tag,
                    "sent_copy": r.get("sent_copy", "")})
    return out
