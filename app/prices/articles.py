"""
Артикулы позиции и обязательный набор поисковых запросов (правила Шага 5.1g старого проекта).

Артикул — «слово» из букв и цифр, где есть и цифры, и буквы (или длинное число): A0P0R74100, DK-8350,
Z358080031, 402-881.229. Кириллица, похожая на латиницу (А, С, М, Р…), в артикулах — частая опечатка ТЗ:
для поиска делается вариант с латиницей.
"""

from __future__ import annotations

import re

# Кириллица, которая выглядит как латиница (для артикулов вида «АСМ1РР6600»).
LOOKALIKE = str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O", "Р": "P",
                           "Т": "T", "Х": "X", "У": "Y", "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x",
                           "у": "y", "к": "k", "м": "m", "т": "t", "н": "h", "в": "b", "З": "3"})
_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё\-./]*[0-9A-Za-zА-Яа-яЁё]")
_SEP = re.compile(r"[\s\-./_]")


def norm(text: str) -> str:
    """Для сравнения артикулов: латиница вместо похожей кириллицы, без разделителей, в верхнем регистре."""
    return _SEP.sub("", str(text or "").translate(LOOKALIKE)).upper()


def _looks_like_article(tok: str, lenient: bool = False) -> bool:
    """lenient — для поля «Марка/модель»: там почти всё — обозначения (ACVF0RD, PF-06)."""
    t = tok.translate(LOOKALIKE)
    if any(c.isalpha() and not c.isascii() for c in t):
        return False                                # осталась «настоящая» кириллица — это слово, не артикул
    letters = sum(c.isalpha() for c in t)
    digits = sum(c.isdigit() for c in t)
    n = len(norm(t))
    if lenient and n >= 4 and digits >= 1:
        return True
    if n >= 5 and digits >= 3 and letters >= 1:
        return True
    if n >= 6 and digits >= 1 and letters >= 2:     # ACVF0RD, A0XPWY3
        return True
    return digits >= 7 and letters == 0             # длинный числовой артикул: 35800803500, 402881229


def articles(brand_model: str = "", *texts: str) -> list[str]:
    """Артикулы из марки/модели (первыми) и из наименования, без повторов (в исходном написании)."""
    seen, out = set(), []
    for i, text in enumerate((brand_model, *texts)):
        for tok in _TOKEN.findall(str(text or "")):
            tok = tok.strip(".-/")
            if _looks_like_article(tok, lenient=(i == 0)) and norm(tok) not in seen:
                seen.add(norm(tok))
                out.append(tok)
    return out


def _variants(article: str) -> list[str]:
    """Варианты написания: как в ТЗ, латиница вместо кириллицы, без разделителей."""
    latin = article.translate(LOOKALIKE)
    plain = _SEP.sub("", latin)
    return list(dict.fromkeys(v for v in (article, latin, plain) if v))


_BRANDS = re.compile(r"\b(konica(?:\s+minolta)?|kyocera|ricoh|canon|kip|xerox|hp|brother|samsung|sharp|epson|"
                     r"siemens|schneider|phoenix|abb|omron|festo|danfoss|testo|endress\+?hauser|samson|sulzer|ksb|"
                     r"rexroth|bosch|agilent|mettler(?:-toledo)?|thermo|катюша)\b", re.IGNORECASE)


def brand_of(*texts: str) -> str:
    for text in texts:
        m = _BRANDS.search(str(text or ""))
        if m:
            return m.group(1)
    return ""


def _name_without_codes(name: str, arts: list[str]) -> str:
    out = str(name or "")
    for a in arts:
        out = out.replace(a, " ")
    return re.sub(r"\s+", " ", out).strip(" ,;/-")


def queries_rf(name: str, brand_model: str) -> tuple[list[str], list[str]]:
    """(артикулы, запросы для российского рынка). Без артикула — запрос по наименованию."""
    arts = articles(brand_model, name)
    brand = brand_of(brand_model, name)
    plain_name = _name_without_codes(name, arts)
    qs: list[str] = []
    if arts:
        main = arts[0]
        qs += _variants(main)                                   # голый артикул и варианты написания
        if brand:
            qs.append(f"{brand} {main.translate(LOOKALIKE)}")      # бренд + артикул
        if plain_name:
            qs.append(f"{plain_name} {main.translate(LOOKALIKE)}")  # название детали + артикул
        qs.append(f"{main.translate(LOOKALIKE)} купить цена")
        for alt in arts[1:4]:                                    # альтернативные артикулы
            qs.append(alt.translate(LOOKALIKE))
            if brand:
                qs.append(f"{brand} {alt.translate(LOOKALIKE)}")
    elif plain_name:
        qs.append(f"{plain_name} купить цена")
        if brand_model and brand_model not in plain_name:
            qs.append(f"{brand_model} купить цена")
            qs.append(f"{plain_name} {brand_model}")
    # Шаг 5.1g: при < 3 цен в пометке должно быть ≥ 5 разных запросов — добираем до пяти.
    main = arts[0].translate(LOOKALIKE) if arts else (brand_model or plain_name)
    for extra in (f"{main} цена", f"{main} в наличии", f"{brand} {plain_name} {main}".strip() if brand else "",
                  f"{plain_name} {main} интернет-магазин" if plain_name and main != plain_name else f"{main} интернет-магазин",
                  f"{main} оптом"):
        if len(dict.fromkeys(q for q in qs if q.strip())) >= 5:
            break
        if extra.strip():
            qs.append(extra)
    return arts, list(dict.fromkeys(" ".join(q.split())[:400] for q in qs if q.strip()))


def queries_abroad(name: str, brand_model: str, arts: list[str], en: str = "") -> list[str]:
    """Запросы для зарубежного рынка — набор Шага 5.1g: голый артикул, бренд + артикул, английское название детали +
    бренд + артикул (en — перевод наименования), eBay / Amazon, AliExpress, «Preis» (Германия), «price» (США),
    каждый альтернативный артикул. Фильтр языка (lang:) не ставится: на редких артикулах он отсекает и нужные
    страницы (проверено 27.10.2026)."""
    brand = brand_of(brand_model, name)
    en = " ".join(str(en or "").split())[:60]
    if not arts:
        # Без артикула за рубежом ищем только по латинской марке/модели (по русскому названию — бессмысленно).
        base = brand_model.strip()[:80] if re.search(r"[A-Za-z]{2}", brand_model or "") else ""
        if not base:
            return []
        qs = [base, f"{en} {base}" if en else f"{base} buy", f"{base} price", f"{base} Preis",
              f"{base} site:ebay.com OR site:ebay.de OR site:amazon.com", f"aliexpress {base}"]
    else:
        main = arts[0].translate(LOOKALIKE)
        qs = [main, f"{brand} {main}" if brand else f"{main} buy",
              f"{en} {brand} {main}".strip() if en else f"{main} spare part",
              f"{main} price", f"{main} Preis",
              f"{main} site:ebay.com OR site:ebay.de OR site:amazon.com", f"aliexpress {main}"]
        qs += [alt.translate(LOOKALIKE) for alt in arts[1:3]]
    return list(dict.fromkeys(" ".join(q.split())[:128] for q in qs if q.strip()))


def recheck_queries(name: str, brand_model: str, arts: list[str], en: str = "") -> tuple[list[str], list[str]]:
    """Шаг 5.1i — второй проход по артикулам без трёх цен: другие запросы (Россия, зарубеж)."""
    brand = brand_of(brand_model, name)
    plain_name = _name_without_codes(name, arts)
    en = " ".join(str(en or "").split())[:60]
    if arts:
        main = arts[0].translate(LOOKALIKE)
        named = plain_name if not brand or brand.lower() in plain_name.lower() else f"{plain_name} {brand}"
        rf = [f"{main} lang:ru", f"{brand} {main} купить".strip(), f"{named} {main} цена".strip(), f"{main} в наличии"]
        rf += [f"{alt.translate(LOOKALIKE)} купить" for alt in arts[1:3]]
        ab = [f"{main} kaufen", f"{main} 价格", f"{en} {main} price".strip() if en else f"{main} buy",
              f"{main} site:alibaba.com OR site:made-in-china.com", f"{main} Preis lang:de", f"{main} price lang:en"]
        ab += [f"{alt.translate(LOOKALIKE)} price" for alt in arts[1:3]]
    else:
        base = (brand_model or plain_name).strip()[:80]
        rf = [f"{plain_name} {brand_model} lang:ru".strip(), f"{base} в наличии", f"{plain_name} купить"] if base else []
        latin = brand_model.strip()[:80] if re.search(r"[A-Za-z]{2}", brand_model or "") else ""
        ab = [f"{latin} kaufen", f"{latin} 价格", f"{en} {latin} price".strip(), f"{latin} price lang:en"] if latin else []
    clean = lambda qs: list(dict.fromkeys(" ".join(q.split())[:128] for q in qs if q.strip()))
    return clean(rf), clean(ab)


def mentions(text: str, arts: list[str]) -> bool:
    """Есть ли в тексте один из артикулов (без учёта разделителей, регистра, кириллицы-двойников)."""
    if not arts:
        return False
    t = norm(text)
    return any(norm(a) in t for a in arts)
