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
MAX_LETTERS = 25          # писем за одно нажатие (0.44.0: было 60 — Mail.ru счёл пачку одинаковых писем спамом)
HOURLY_LIMIT = 30         # писем с одного ящика за час (запросы и ответы в переписке)
SEND_PAUSE = (4.0, 8.0)   # пауза между письмами, с — случайная в этих пределах
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


# 0.44.0: ссылки на мессенджеры и сокращатели ссылок — частый признак спама у Mail.ru (ответ поставщика на запрос
# с подписью «http://t.me/+7911…» вернулся «550 spam message rejected»). В письме — обычным текстом.
_MESSENGER_RE = re.compile(r"(?:https?://)?(?:www\.)?(t\.me|telegram\.me|wa\.me|api\.whatsapp\.com/send\?phone=)/?([+\w.\-]+)",
                           re.I)
_SHORT_RE = re.compile(r"(?:,\s*)?(?:https?://)?(?:bit\.ly|clck\.ru|vk\.cc|tinyurl\.com|goo\.su|cutt\.ly)/\S+", re.I)
_SCHEME_RE = re.compile(r"\bhttps?://(?=[^\s/]+\.[^\s/]+)", re.I)


def clean_links(text: str) -> str:
    """Ссылки в тексте письма — без «http://», мессенджеры — словами, сокращённые ссылки убираются."""
    def messenger(m):
        name = "WhatsApp" if "wa" in m.group(1).lower() else "Telegram"
        return f"{name}: {m.group(2)}"
    text = _MESSENGER_RE.sub(messenger, str(text or ""))
    text = _SHORT_RE.sub("", text)
    return _SCHEME_RE.sub("", text)


def default_signature(box: dict | None, user: dict | None) -> str:
    if box and box.get("signature"):
        return clean_links(box["signature"])
    lines = []
    name = (box or {}).get("sender_name") or (user or {}).get("name") or ""
    if name:
        lines.append(name)
    if user and user.get("phone"):
        lines.append("тел. " + user["phone"])
    if box:
        lines.append(box["address"])
    return clean_links("\n".join(["С уважением,"] + lines) if lines else "С уважением")


INTRO = ("Добрый день!\n\n"
         "Просим предоставить коммерческое предложение на поставку следующих позиций:\n\n")
ASK = ("Просим указать по каждой позиции: цену за единицу с НДС (и ставку НДС), наличие на складе или срок поставки, "
       "страну происхождения, срок действия предложения. Если позиция недоступна — предложите, пожалуйста, аналог.\n")
XLSX_LINE = "\nПеречень позиций — также во вложении (Excel): цены можно указать прямо в нём.\n"

# 0.44.0: письма разным поставщикам не должны быть одинаковыми — фильтр Mail.ru узнаёт рассылку по тексту и
# отклоняет потом и ответы с цитатой. У каждого письма — обращение с названием поставщика и свой вариант
# формулировок (выбор — по ключу письма, у одного поставщика он не меняется).
GREETINGS = ("Добрый день!", "Здравствуйте!", "Добрый день, коллеги!")
INTROS = (
    "Просим предоставить коммерческое предложение на поставку следующих позиций:",
    "Рассматриваем закупку оборудования и просим {to}прислать цены на позиции ниже:",
    "Подскажите, пожалуйста, {to}стоимость и сроки поставки по следующим позициям:",
    "Прошу {to}направить предложение по цене и срокам на позиции из списка:",
)
ASKS = (
    ASK,
    "По каждой позиции нужны: цена за единицу с НДС (ставка НДС), наличие или срок поставки, страна происхождения "
    "и срок действия цены. Если чего-то нет — можно предложить аналог.\n",
    "Пожалуйста, укажите цену за штуку с НДС и ставку НДС, срок поставки (или наличие на складе), страну "
    "происхождения и до какой даты действует предложение. Аналоги тоже рассмотрим.\n",
)
XLSX_LINES = (
    XLSX_LINE,
    "\nВо вложении — тот же список в Excel, цены можно вписать прямо туда.\n",
    "\nСписок продублирован в файле Excel во вложении — так будет удобнее заполнить.\n",
)


def _pick(key: str, options: tuple, salt: str = "") -> str:
    import hashlib
    h = int(hashlib.sha1((salt + "|" + str(key or "")).encode("utf-8")).hexdigest()[:8], 16)
    return options[h % len(options)]


def _company(name: str) -> str:
    """«ООО «Ромашка»» → «ООО «Ромашка»» для обращения «просим ООО «Ромашка» …»; пусто — без обращения."""
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    return (n + " ") if n and len(n) <= 60 else ""


def intro_of(key: str = "", supplier_name: str = "") -> str:
    if not key:
        return INTRO
    to = _company(supplier_name)
    intro = _pick(key, INTROS, "intro").replace("{to}", to)
    if not to:
        intro = intro.replace("  ", " ")
    return _pick(key, GREETINGS, "hello") + "\n\n" + intro + "\n\n"


def outro(signature: str, with_xlsx: bool = True, key: str = "") -> str:
    ask = _pick(key, ASKS, "ask") if key else ASK
    xl = _pick(key, XLSX_LINES, "xlsx") if key else XLSX_LINE
    return "\n\n" + ask + (xl if with_xlsx else "") + "\n" + signature + "\n"


def letter_body(positions: list[dict], signature: str, with_xlsx: bool = True, key: str = "",
                supplier_name: str = "") -> str:
    """Текст письма; страница собирает его так же (intro + строки позиций + outro), когда меняют набор позиций."""
    return (intro_of(key, supplier_name) + "\n".join(position_line(p) for p in positions)
            + outro(signature, with_xlsx, key))


def _letter_parts(key: str, supplier_name: str, signature: str) -> dict:
    return {"intro": intro_of(key, supplier_name), "outro_xlsx": outro(signature, True, key),
            "outro_plain": outro(signature, False, key)}


def sent_last_hour(address: str) -> int:
    """Сколько писем ушло с ящика за последний час (у всех сотрудников, кто им пользуется)."""
    with db.connect() as con:
        row = con.execute("SELECT COUNT(*) FROM mail_requests WHERE lower(mailbox_address)=lower(?) AND status='sent' "
                          "AND sent_at > ?", (str(address or ""), time.time() - 3600)).fetchone()
    return int(row[0] or 0)


def check_rate(address: str, count: int) -> None:
    done = sent_last_hour(address)
    if done + count > HOURLY_LIMIT:
        left = max(0, HOURLY_LIMIT - done)
        raise RequestError(
            f"с ящика {address} за последний час уже отправлено {done} писем; не больше {HOURLY_LIMIT} в час — "
            f"иначе Mail.ru и другие почтовые службы принимают запросы за рассылку спама и потом отклоняют ответы "
            f"поставщиков. Сейчас можно отправить ещё {left}" + ("" if left else " — подождите немного") + ".")


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
        # Адрес: сохранённый на сайте (последний, на который отправляли / который ввели руками), иначе — все почты
        # поставщика из NocoDB («Поставщики», колонка E-mail; несколько адресов — письмо уйдёт на все).
        base_emails = s.get("emails") or []
        email = known.get(e["id"]) or ", ".join(base_emails)
        letters.append({
            "key": "s" + e["id"], "supplier_id": e["id"], "supplier_name": s["name"], "level": s["level"],
            "status": s["status"], "method": s["method"], "site": s["site"], "brands": e["brands"], "strong": e["strong"],
            # 0.33.1: ничего не отмечено заранее — с почтами из NocoDB адрес есть почти у всех (К947: 146 из 150),
            # и одно нажатие «Отправить» ушло бы всем. Выбор — фильтром брендов и «выбрать всех с адресом».
            "to": email, "emails": base_emails, "checked": False,
            "sent_at": sent.get(e["id"]),
            "subject": subject, "positions": [p["n"] for p in pos],
            "body": letter_body(pos, signature, key="s" + e["id"], supplier_name=s["name"]),
            **_letter_parts("s" + e["id"], s["name"], signature),
        })
    letters.append({
        "key": "other", "supplier_id": "", "supplier_name": "", "level": "", "status": "", "method": "", "site": "",
        "brands": [], "strong": True, "to": "", "emails": [], "checked": False, "sent_at": None,
        "subject": subject, "positions": [p["n"] for p in positions], "body": letter_body(positions, signature),
        **_letter_parts("", "", signature),
    })
    for p in positions:
        p["line"] = position_line(p)
    # Бренды позиций (0.33.0): {№: [бренд]} — фильтр «по брендам» на странице сужает письма до этих позиций.
    pos_brands = {}
    for r in (brands or {}).get("rows", []):
        if r.get("n") in by_n:
            pos_brands[r["n"]] = [b["brand"] for b in r.get("brands", [])]
    return {"positions": positions, "letters": letters, "pos_brands": pos_brands, "signature": signature, "intro": INTRO,
            "outro_xlsx": outro(signature, True), "outro_plain": outro(signature, False),
            "limits": {"per_click": MAX_LETTERS, "per_hour": HOURLY_LIMIT, "pause": list(SEND_PAUSE)}}


def send(*, owner: str, user: dict | None, client_id: str, base_id: str, record_id: int, internal: str, folder: str,
         box_id: int, letters: list[dict], positions: list[dict], with_xlsx: bool = True,
         extra_attachments: list | None = None) -> list[dict]:
    """Отправить письма с ящика сотрудника. letters: [{key, supplier_id, supplier_name, to, subject, body,
    positions: [№]}]. Ответ: [{key, ok, error, id, tag}]."""
    init()
    if not letters:
        raise RequestError("не выбрано ни одного письма")
    box_row = mail._row(owner, box_id)
    by_n = {p["n"]: p for p in positions}
    prepared = []
    for L in letters:
        to = parse_addresses(L.get("to"))
        if not to:
            raise RequestError(f"не указан адрес: {L.get('supplier_name') or 'другой адресат'}")
        subject = str(L.get("subject") or "").strip()[:250]
        body = clean_links(str(L.get("body") or "").strip())
        if not subject or not body:
            raise RequestError(f"пустая тема или текст письма: {L.get('supplier_name') or ', '.join(to)}")
        pos = [by_n[str(n)] for n in L.get("positions") or [] if str(n) in by_n]
        # 0.44.5: на каждый адрес — своё письмо, в «Кому» только этот адрес (пользователь 27.11.2026: К951 ушёл
        # одним письмом девяти поставщикам — все видели друг друга). Ни списка, ни скрытой копии.
        for addr in dict.fromkeys(to):
            prepared.append((L, [addr], subject, body, pos))
    if len(prepared) > MAX_LETTERS:
        raise RequestError(f"за один раз — не больше {MAX_LETTERS} писем (каждый адрес — отдельное письмо, "
                           f"выбрано адресов: {len(prepared)})")
    check_rate(box_row["address"], len(prepared))
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
        if L.get("with_xlsx", with_xlsx) and pos:
            attach.append((f"Запрос цен {internal}.xlsx", xlsx_bytes(internal, pos),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        if L.get("with_requisites"):                 # реквизиты по шаблону письма (0.45.0, с 0.45.2 — у письма)
            attach.extend(extra_attachments or [])
        msg = mail.build_message(box_row, ", ".join(to), full_subject, body, attach, {"X-SupplyBro-Tag": tag})
        with db.connect() as con:
            con.execute("UPDATE mail_requests SET subject=?, tag=?, message_id=?, attachment=? WHERE id=?",
                        (full_subject, tag, msg["Message-ID"], ", ".join(a[0] for a in attach), rid))
        messages.append(msg)
        ids.append((rid, tag))
    results = mail.send_many(owner, box_id, messages, pause=SEND_PAUSE)
    # Итог — по карточке письма (key): ок, если ушло на все её адреса; ошибки — с адресом.
    by_key: dict = {}
    for (L, to, *_), (rid, tag), r in zip(prepared, ids, results):
        with db.connect() as con:
            con.execute("UPDATE mail_requests SET status=?, error=?, sent_copy=?, sent_at=? WHERE id=?",
                        ("sent" if r["ok"] else "error", r["error"], r.get("sent_copy", ""),
                         time.time() if r["ok"] else None, rid))
        k = L.get("key")
        o = by_key.setdefault(k, {"L": L, "key": k, "ok": True, "errors": [], "id": rid, "tag": tag, "ids": [],
                                  "tags": [], "sent_to": [], "sent_copy": []})
        o["ids"].append(rid)
        o["tags"].append(tag)
        if r["ok"]:
            o["sent_to"].append(to[0])
            if r.get("sent_copy"):
                o["sent_copy"].append(f"{to[0]}: {r['sent_copy']}")
        else:
            o["ok"] = False
            o["errors"].append(f"{to[0]}: {r['error']}")
    out = []
    for o in by_key.values():
        L = o.pop("L")
        if o["sent_to"] and L.get("supplier_id"):
            remember_email(client_id, base_id, str(L["supplier_id"]), str(L.get("supplier_name") or ""),
                           ", ".join(o["sent_to"]), owner)
        o["error"] = "; ".join(o.pop("errors"))
        o["sent_copy"] = "; ".join(o["sent_copy"])
        out.append(o)
    return out
