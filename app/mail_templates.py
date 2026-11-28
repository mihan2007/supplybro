"""Шаблоны писем поставщикам (0.45.0).

Пользователь 27.11.2026: «сделай, чтобы ещё можно было выбирать из шаблонов, так же сделай редактор шаблонов»
(прислал два шаблона: «Запрос цен» — коммерческое предложение, и «Закупка, счёт на оплату»).

- Шаблоны — общие для сотрудников клиента (таблица mail_templates в app.db). Править — у кого есть право вносить
  цены поставщиков («purchase»); удаление — мягкое (deleted_at), прежний текст — в mail_template_history.
- «Стандартный» (id «standard») — прежнее письмо сайта с разными формулировками у разных поставщиков (0.44.0,
  supplier_requests.INTROS / ASKS): не правится и не удаляется.
- Метки в тексте: {приветствие} (у разных поставщиков — разное: Mail.ru узнаёт рассылку по одинаковому тексту),
  {поставщик}, {позиции} (обязательна), {номер}, {предмет}, {вложение}, {подпись}. Подставляет страница (письмо
  можно поправить перед отправкой); сервер проверяет шаблон при сохранении.
- «Приложить реквизиты» — карточка реквизитов юр. лица (materials.requisites в clients.json), например для
  запроса счёта.
"""
from __future__ import annotations

import re
import time

from . import db

STANDARD_ID = "standard"
MARKS = {
    "приветствие": "«Добрый день!» / «Здравствуйте!» — у разных поставщиков по-разному",
    "поставщик": "название поставщика (у письма «другому адресату» — пусто)",
    "позиции": "список позиций письма: №, наименование, марка, количество (обязательно)",
    "номер": "внутренний номер конкурса (К952)",
    "предмет": "предмет закупки, коротко",
    "вложение": "строка про приложенный Excel с позициями (без вложения — пусто)",
    "подпись": "подпись из почтового ящика («✉ Мои ящики»)",
}
REQUIRED = ("позиции",)
MAX_NAME, MAX_SUBJECT, MAX_BODY = 80, 200, 8000
_MARK_RE = re.compile(r"\{([^{}\n]{1,40})\}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attach_xlsx INTEGER NOT NULL DEFAULT 1,
    attach_requisites INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 0,
    seed TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS mail_templates_client ON mail_templates (client_id);
CREATE TABLE IF NOT EXISTS mail_template_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    at REAL NOT NULL,
    by TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT ''
);
"""

# Шаблоны пользователя (27.11.2026, «Запрос цен.docx» и «Запрос счёта на оплату.docx»). Подпись — меткой {подпись}
# (в присланных — подпись одного сотрудника, а пишут с разных ящиков); «Добрый день!» — {приветствие}; во втором
# шаблоне приветствие было дважды — оставлено одно.
SEEDS = [
    {
        "seed": "kp", "name": "Запрос цен (коммерческое предложение)",
        "subject": "Запрос цен {номер}: {предмет}",
        "attach_xlsx": 1, "attach_requisites": 0,
        "body": (
            "{приветствие}\n\n"
            "Просим направить коммерческое предложение на поставку следующих позиций:\n\n"
            "{позиции}\n\n"
            "Просим указать в предложении:\n"
            "- Цену за единицу и общую стоимость по каждой позиции, а также итоговую сумму заказа. Укажите валюту и "
            "включён ли НДС в стоимость. Если НДС не применяется, отметьте это отдельно.\n"
            "- Наличие и срок поставки. Если товара нет в наличии, укажите, с какого момента отсчитывается срок: от "
            "подтверждения заказа, подписания договора или оплаты.\n"
            "- Условия доставки. Если доставка не входит в цену, укажите её стоимость отдельной строкой.\n"
            "- Условия оплаты: размер предоплаты, порядок окончательного расчёта или возможную отсрочку.\n"
            "- Гарантийный срок, если применимо, и срок действия предложения.\n\n"
            "Если запрошенной позиции нет, можно предложить аналог: укажите его отдельной строкой и опишите отличия. "
            "Если возможна только частичная поставка, укажите доступное количество и сроки поставки остатка.\n"
            "{вложение}\n"
            "Просим направить предложение ответным письмом, не меняя темы.\n\n"
            "Спасибо!\n\n"
            "{подпись}"),
    },
    {
        "seed": "invoice", "name": "Счёт на оплату",
        "subject": "Закупка, счёт на оплату {номер}",
        "attach_xlsx": 0, "attach_requisites": 1,
        "body": (
            "{приветствие}\n\n"
            "Просим выставить счёт на оплату на следующие позиции:\n\n"
            "{позиции}\n\n"
            "Реквизиты для выставления счёта во вложении.\n\n"
            "Просим указать в счёте цену за единицу, итоговую сумму к оплате, валюту и НДС (либо отметку «Без НДС»), "
            "а также реквизиты для оплаты и срок действия счёта. Если доставка оплачивается отдельно, просим "
            "выделить её стоимость отдельной строкой.\n\n"
            "Если цена, наличие или сроки поставки изменились, пожалуйста, сообщите об этом до выставления счёта.\n\n"
            "Просим направить счёт в формате PDF ответным письмом, не меняя темы.\n\n"
            "Спасибо!\n\n"
            "{подпись}"),
    },
]

_inited = False


class TemplateError(ValueError):
    pass


def init() -> None:
    global _inited
    if _inited:
        return
    with db.connect() as con:
        con.executescript(SCHEMA)
    _inited = True


def _seed(con, client_id: str) -> None:
    """Шаблоны пользователя — один раз на клиента (удалённые не возвращаются)."""
    have = con.execute("SELECT COUNT(*) FROM mail_templates WHERE client_id=?", (client_id,)).fetchone()[0]
    if have:
        return
    now = time.time()
    for i, s in enumerate(SEEDS, 1):
        con.execute("INSERT INTO mail_templates (client_id, name, subject, body, attach_xlsx, attach_requisites, sort, "
                    "seed, created_by, created_at, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (client_id, s["name"], s["subject"], s["body"], s["attach_xlsx"], s["attach_requisites"], i,
                     s["seed"], "", now, "", now))


def standard() -> dict:
    return {"id": STANDARD_ID, "name": "Стандартный (разные формулировки)", "standard": True, "subject": "",
            "body": "", "attach_xlsx": True, "attach_requisites": False,
            "note": "Письмо сайта: у каждого поставщика свои формулировки. Не правится — сделайте свой шаблон."}


def _view(r) -> dict:
    return {"id": str(r["id"]), "name": r["name"], "subject": r["subject"], "body": r["body"],
            "attach_xlsx": bool(r["attach_xlsx"]), "attach_requisites": bool(r["attach_requisites"]),
            "standard": False, "updated_by": r["updated_by"], "updated_at": r["updated_at"]}


def listing(client_id: str) -> list[dict]:
    init()
    with db.connect() as con:
        _seed(con, client_id)
        rows = con.execute("SELECT * FROM mail_templates WHERE client_id=? AND deleted_at IS NULL ORDER BY sort, id",
                           (client_id,)).fetchall()
    return [standard(), *(_view(r) for r in rows)]


def get(client_id: str, template_id) -> dict | None:
    if not template_id or str(template_id) == STANDARD_ID:
        return standard()
    init()
    try:
        tid = int(template_id)
    except (TypeError, ValueError):
        return None
    with db.connect() as con:
        r = con.execute("SELECT * FROM mail_templates WHERE id=? AND client_id=? AND deleted_at IS NULL",
                        (tid, client_id)).fetchone()
    return _view(r) if r else None


def check(name: str, subject: str, body: str) -> tuple[str, str, str]:
    name = " ".join(str(name or "").split())[:MAX_NAME]
    subject = " ".join(str(subject or "").split())
    body = str(body or "").replace("\r\n", "\n").strip()
    if not name:
        raise TemplateError("укажите название шаблона")
    if not subject:
        raise TemplateError("укажите тему письма")
    if len(subject) > MAX_SUBJECT:
        raise TemplateError(f"тема — не длиннее {MAX_SUBJECT} символов")
    if not body:
        raise TemplateError("пустой текст письма")
    if len(body) > MAX_BODY:
        raise TemplateError(f"текст — не длиннее {MAX_BODY} символов")
    unknown = sorted({m for m in _MARK_RE.findall(subject + "\n" + body) if m.strip().lower() not in MARKS})
    if unknown:
        raise TemplateError("незнакомые метки: " + ", ".join("{" + m + "}" for m in unknown)
                            + ". Можно: " + ", ".join("{" + m + "}" for m in MARKS))
    for m in REQUIRED:
        if "{" + m + "}" not in body:
            raise TemplateError(f"в тексте нужна метка {{{m}}} — на её место встанет список позиций")
    if "{позиции}" in subject:
        raise TemplateError("метка {позиции} — только в тексте, не в теме")
    return name, subject, body


def save(client_id: str, data: dict, by: str) -> dict:
    init()
    name, subject, body = check(data.get("name"), data.get("subject"), data.get("body"))
    xlsx = 1 if data.get("attach_xlsx", True) else 0
    req = 1 if data.get("attach_requisites") else 0
    tid = str(data.get("id") or "")
    now = time.time()
    with db.connect() as con:
        _seed(con, client_id)
        dup = con.execute("SELECT id FROM mail_templates WHERE client_id=? AND deleted_at IS NULL AND lower(name)=lower(?)"
                          " AND id != ?", (client_id, name, int(tid) if tid.isdigit() else -1)).fetchone()
        if dup:
            raise TemplateError(f"шаблон «{name}» уже есть — выберите другое название")
        if tid and tid != STANDARD_ID:
            if not tid.isdigit():
                raise TemplateError("шаблон не найден")
            old = con.execute("SELECT * FROM mail_templates WHERE id=? AND client_id=? AND deleted_at IS NULL",
                              (int(tid), client_id)).fetchone()
            if not old:
                raise TemplateError("шаблон не найден — обновите страницу")
            con.execute("INSERT INTO mail_template_history (template_id, at, by, action, name, subject, body) "
                        "VALUES (?,?,?,?,?,?,?)", (old["id"], now, by, "до изменения", old["name"], old["subject"],
                                                   old["body"]))
            con.execute("UPDATE mail_templates SET name=?, subject=?, body=?, attach_xlsx=?, attach_requisites=?, "
                        "updated_by=?, updated_at=? WHERE id=?", (name, subject, body, xlsx, req, by, now, old["id"]))
            new_id = old["id"]
        else:
            sort = (con.execute("SELECT COALESCE(MAX(sort), 0) FROM mail_templates WHERE client_id=?",
                                (client_id,)).fetchone()[0] or 0) + 1
            cur = con.execute("INSERT INTO mail_templates (client_id, name, subject, body, attach_xlsx, "
                              "attach_requisites, sort, created_by, created_at, updated_by, updated_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (client_id, name, subject, body, xlsx, req, sort, by, now, by, now))
            new_id = cur.lastrowid
    return get(client_id, new_id)


def delete(client_id: str, template_id: str, by: str) -> None:
    init()
    if str(template_id) == STANDARD_ID:
        raise TemplateError("стандартный шаблон не удаляется")
    if not str(template_id).isdigit():
        raise TemplateError("шаблон не найден")
    now = time.time()
    with db.connect() as con:
        old = con.execute("SELECT * FROM mail_templates WHERE id=? AND client_id=? AND deleted_at IS NULL",
                          (int(template_id), client_id)).fetchone()
        if not old:
            raise TemplateError("шаблон не найден — обновите страницу")
        con.execute("INSERT INTO mail_template_history (template_id, at, by, action, name, subject, body) "
                    "VALUES (?,?,?,?,?,?,?)", (old["id"], now, by, "удалён", old["name"], old["subject"], old["body"]))
        con.execute("UPDATE mail_templates SET deleted_at=?, updated_by=? WHERE id=?", (now, by, old["id"]))


def requisites_file(client, entity_id: str | None):
    """Карточка реквизитов юр. лица (materials.requisites, как у подготовки заявки) → (Path | None, имя юр. лица)."""
    from .bid.participant import _glob
    from .tenders import client_root
    entity = client.entity(entity_id) if entity_id else None
    entity = entity or (client.legal_entities[0] if client.legal_entities else None)
    if not entity:
        return None, ""
    mat = dict(getattr(entity, "materials", None) or {})
    if not mat.get("requisites"):
        return None, entity.name
    try:
        base = client_root(client) / mat.get("dir", "")
    except Exception:                                  # noqa: BLE001
        return None, entity.name
    return (_glob(base, mat["requisites"]) if base.is_dir() else None), entity.name
