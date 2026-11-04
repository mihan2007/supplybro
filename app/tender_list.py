"""Краткие сведения о конкурсе для панели «Конкурсы» (0.13.0).

Пользователь 04.11.2026: «сейчас при входе предлагается выбрать конкурс из выпадающего списка — давай сделаем панель,
где будут отображаться тендеры выбранного лица, причём чтобы их можно было открыть в отдельной вкладке. В панели —
номер тендера, заказчик и наименование оборудования, ещё какая-нибудь минимальная информация».

В «Реестре конкурсов» агент пишет поля развёрнуто («✅ 09.11.2026 12:00 (МСК)», «Заказчик: АО «…» (полное название),
груз…», «9 045 801,66 руб. без НДС (с НДС 22% — …)»). Здесь из них — коротко для строки панели; полный текст
страница показывает во всплывающей подсказке. Ничего не пишет и не меняет — только разбор текста.
"""
from __future__ import annotations

import re

MARKS = re.compile(r"[✅🔺⚠️❗🔴🟢]+")
# Так агент пишет, когда в документах значения нет — в панели это пусто.
NOT_FOUND = re.compile(r"^(?:не указан|не найден|документ по ссылке не найден|нет данных|отсутству)", re.I)
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
          "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}


def _plain(value) -> str:
    """Текст поля NocoDB без отметок проверки (✅ / 🔺) и лишних пробелов, только первая строка."""
    if value is None:
        return ""
    text = MARKS.sub("", str(value))
    for line in text.splitlines():
        line = " ".join(line.split())
        if line:
            return "" if NOT_FOUND.match(line) else line
    return ""


def _cut(text: str, seps: tuple[str, ...], min_len: int = 4) -> str:
    """Обрезать на первом разделителе, если до него осталось не меньше min_len символов."""
    best = len(text)
    for sep in seps:
        i = text.find(sep)
        if min_len <= i < best:
            best = i
    return text[:best].strip(" ,;—–-")


def short_customer(value) -> str:
    """«Заказчик: АО «Интер РАО – Электрогенерация» (Группа…), груз…» → «АО «Интер РАО – Электрогенерация»».
    «ООО УК "…" (организатор); заказчик — ПАО "Уралмаш…"» → «ПАО "Уралмаш…"»."""
    text = _plain(value)
    if not text:
        return ""
    m = re.search(r"заказчик\s*[—–:-]\s*(.+)", text, re.I)
    if m and re.search(r"организатор", text[:m.start()], re.I):
        text = m.group(1)                                   # сначала организатор, потом заказчик
    text = re.sub(r"^\s*заказчик\s*[:—–-]\s*", "", text, flags=re.I)
    return _cut(text, (" (", ";", " — заказчик", ", груз"))


def short_subject(value) -> str:
    """Наименование оборудования: без кодов ОКПД2 / КТРУ и без «Список позиций: …»."""
    text = _plain(value)
    if not text:
        return ""
    text = re.sub(r"^\s*(?:код\s+)?(?:ОКПД\s*2?|КТРУ)[\s:]*[\d.,\s-]+[—–-]\s*", "", text, flags=re.I)
    text = re.sub(r"\s*\((?:[^()]*?\b(?:ОКПД|КТРУ)[^()]*)\)?", "", text, flags=re.I)
    text = re.split(r"\s*Список позиций", text, maxsplit=1, flags=re.I)[0]
    return text.strip(" ,;:—–-")


def short_stage(value) -> str:
    """«Приём заявок (до 09.11.2026 12:00 МСК)» → «Приём заявок»."""
    return _cut(_plain(value), (" (", ";", ". "), 3)


def parse_deadline(value) -> dict | None:
    """Срок подачи заявок → {"date": "2026-11-09", "time": "12:00", "local": True/False}. Не разобрать — None.
    Понимает «09.11.2026 12:00 (МСК)», «09:00 (МСК) «06» октября 2026 г.», «06.10.2026 09:00 (время местное; …)»."""
    text = _plain(value)
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        rest = text[m.end():]
    else:
        m = re.search(r"«?(\d{1,2})»?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", text, re.I)
        if not m:
            return None
        d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
        rest = text
    if not (1 <= d <= 31 and 1 <= mo <= 12 and 2000 <= y <= 2100):
        return None
    t = re.search(r"\b(\d{1,2}):(\d{2})\b", rest) or re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    hh, mm = (int(t.group(1)), int(t.group(2))) if t else (23, 59)
    if hh > 23 or mm > 59:
        hh, mm = 23, 59
    return {"date": f"{y:04d}-{mo:02d}-{d:02d}", "time": f"{hh:02d}:{mm:02d}" if t else "",
            "local": bool(re.search(r"местн", text, re.I))}


def parse_price(value, currency=None) -> dict | None:
    """Начальная цена → {"value": 9045801.66, "currency": "RUB", "vat": "без НДС" | "с НДС" | ""}. Нет числа — None."""
    text = _plain(value)
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)", text)
    if not m:
        return None
    try:
        num = float(re.sub(r"[   ]", "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
    except ValueError:
        return None
    if num < 1:
        return None
    low = text.lower()
    cur = (str(currency or "").strip().upper() or "RUB")
    for code, signs in (("EUR", ("eur", "€", "евро")), ("USD", ("usd", "$", "доллар")), ("CNY", ("cny", "юан", "¥")),
                        ("RUB", ("руб", "₽"))):
        if any(s in low for s in signs):
            cur = code
            break
    vat = ""
    head = low[:m.end() + 40]
    if re.search(r"без\s+(?:учета\s+|учёта\s+)?ндс", head):
        vat = "без НДС"
    elif re.search(r"(?:с|включая|в т\.?\s?ч\.?)\s+(?:учетом\s+|учётом\s+)?ндс", head):
        vat = "с НДС"
    return {"value": round(num, 2), "currency": cur, "vat": vat}


def short_result(value) -> str:
    """«Статус конкурса (выиграли - проиграли)»: «Выиграли» / «Проиграли» / «Не участвовали» (как в NocoDB)."""
    return _plain(value)[:40]


def _full(value, limit: int = 600) -> str:
    """Полный текст поля для подсказки: без отметок проверки, строки через « · »."""
    if value is None:
        return ""
    lines = [" ".join(x.split()) for x in MARKS.sub("", str(value)).splitlines()]
    text = " · ".join(x for x in lines if x)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def brief(customer, title, stage, deadline, price, currency, result) -> dict:
    """Поля строки панели «Конкурсы» (добавляются к записи списка конкурсов в main.api_tenders)."""
    return {
        "customer": short_customer(customer),
        "customer_full": _full(customer),
        "subject": short_subject(title),
        "subject_full": _full(title),
        "stage_short": short_stage(stage),
        "stage_full": _full(stage),
        "deadline_at": parse_deadline(deadline),
        "deadline_full": _full(deadline),
        "price": parse_price(price, currency),
        "price_full": _full(price),
        "result": short_result(result),
    }
