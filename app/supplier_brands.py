"""
Наши поставщики по брендам позиций спецификации (0.26.0).

Пользователь 17.11.2026: «у части поставщиков есть бренды, которыми они торгуют — давай сделаем, чтобы в тендере
выделялись позиции спецификации с этими брендами»; выбрано — колонка на сайте (не в NocoDB), цвет строки и блок
наверху со сведениями, что есть наши бренды.

Откуда бренды: таблица «Поставщики» базы NocoDB клиента — колонка «Бренды» (текст через запятую) и связь
«Производители» (названия связанных производителей). Таблица только читается и хранится в памяти сайта CACHE_HOURS
часов (fresh=True — прочитать заново). Нет таблицы — пусто, спецификация показывается как раньше.

Сравнение — кодом, без ИИ, при каждом открытии спецификации (доли секунды):
  - бренд в колонках марки / производителя позиции → совпадение «strong»;
  - бренд только в наименовании → «weak» (на странице — с «?»);
  - перед брендом в наименовании «совместим… / аналог / compatible» → не совпадение (это не товар бренда),
    если бренда нет в колонке марки / производителя;
  - написания сводятся словарём ALIASES (Konika = Konica Minolta, «Сименс» = Siemens…), границы слов обязательны
    (HP не находится внутри «HPLC», ABB — внутри «ABBA»).

Цвет — по лучшему поставщику бренда: green — есть «Надёжный. Работаем»; yellow — «Обязательно запрос», статус не
указан или «требуется проверка / по ситуации»; поставщики «Не работаем» в подсчёт не идут (показываются в конце).
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time

from .config import Client
from .nocodb import NocoClient, NocoError

TABLE_TITLE = os.environ.get("SUPPLIERS_TABLE", "Поставщики")
CACHE_HOURS = float(os.environ.get("SUPPLIERS_CACHE_HOURS", "6"))
F_NAME, F_BRANDS, F_MAKERS = "Название", "Бренды", "Производители"
F_STATUS, F_METHOD, F_SITE, F_INFO = "Надёжность: статус", "Способ расчёта", "Сайт", "Сведения"
SUMMARY_LIMIT = 30          # поставщиков в блоке «Кому писать» (остальные — числом)

# Написание → общий ключ. Ключи и значения — в нормализованном виде (norm).
ALIASES = {
    "konika minolta": "konica minolta", "konica": "konica minolta", "konika": "konica minolta",
    "minolta": "konica minolta", "коника минолта": "konica minolta", "коника": "konica minolta",
    "schneider": "schneider electric", "шнайдер": "schneider electric", "шнейдер": "schneider electric",
    "шнайдер электрик": "schneider electric", "шнейдер электрик": "schneider electric",
    "сименс": "siemens", "ксерокс": "xerox", "самсунг": "samsung", "шарп": "sharp", "брадер": "brother",
    "бразер": "brother", "рико": "ricoh", "hewlett packard": "hp", "hewlett-packard": "hp", "абб": "abb",
    "моторола": "motorola", "мокса": "moxa",
}
# Как показывать бренд, если у поставщиков он записан по-разному или с опечаткой.
DISPLAY = {"konica minolta": "Konica Minolta", "hp": "HP", "schneider electric": "Schneider Electric"}
COMPAT_RE = re.compile(r"(совмест\w*|аналог\w*|compatible|эквивалент\w*)\W*(?:\w+\W+){0,3}$")
BRAND_COL_RE = re.compile(r"марк|модел|производител|бренд", re.I)
NAME_COLS = ("Наименование позиции", "Наименование")
WORD = "0-9a-zа-я"

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}     # client.id:base_id → (прочитано, индекс)


def norm(text) -> str:
    t = str(text or "").lower().replace("ё", "е")
    t = re.sub(r"[«»“”„\"']", "", t)
    return re.sub(r"\s+", " ", t).strip()


def spellings(brand: str) -> list[str]:
    """«HP(Hewlett Packard)» → ["hp", "hewlett packard"]: до скобок — главное написание, в скобках — ещё одно."""
    n = norm(brand)
    out = [p.strip(" .,;") for p in re.split(r"[()]", n)]
    return [p for p in out if p]


def key_of(brand: str) -> str:
    parts = spellings(brand)
    n = parts[0] if parts else ""
    return ALIASES.get(n, n)


def level_of(status) -> tuple[str, int]:
    """Надёжность → (цвет, порядок): green 0, yellow 1–2, grey 9."""
    s = norm(status)
    if "не работаем" in s:
        return "grey", 9
    if "работаем" in s:
        return "green", 0
    if "обязательно" in s:
        return "yellow", 1
    return "yellow", 2


def _split(text) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]", str(text or "")) if p.strip()]


def build_index(rows: list[dict]) -> dict:
    """Записи «Поставщиков» → {suppliers: {id: {...}}, brands: {ключ: {name, ids}}, terms: [(regex, ключ)]}."""
    suppliers: dict[str, dict] = {}
    brands: dict[str, dict] = {}
    for r in rows:
        sid = str(r.get("Id") or "")
        name = str(r.get(F_NAME) or "").strip()
        if not sid or not name:
            continue
        own = _split(r.get(F_BRANDS))
        makers = r.get(F_MAKERS)
        if isinstance(makers, list):
            own += [str(m.get("Производитель") or m.get("Title") or "").strip() for m in makers if isinstance(m, dict)]
        keys: dict[str, str] = {}
        for b in own:
            if b and (k := key_of(b)) and k not in keys:
                keys[k] = b
        if not keys:
            continue
        level, rank = level_of(r.get(F_STATUS))
        suppliers[sid] = {
            "id": sid, "name": name, "status": str(r.get(F_STATUS) or "").strip(), "level": level, "rank": rank,
            "method": str(r.get(F_METHOD) or "").strip(), "site": str(r.get(F_SITE) or "").strip(),
            "info": str(r.get(F_INFO) or "").strip()[:200], "brands": list(keys.values()),
        }
        for k, shown in keys.items():
            shown = DISPLAY.get(k) or re.sub(r"\s*\(.*?\)\s*", " ", shown).strip() or shown
            b = brands.setdefault(k, {"name": shown, "ids": []})
            b["ids"].append(sid)
    for b in brands.values():
        b["ids"].sort(key=lambda i: (suppliers[i]["rank"], suppliers[i]["name"].lower()))
    # Что искать в тексте позиции: сам ключ, исходные написания и все написания из словаря с тем же ключом.
    spell: dict[str, set[str]] = {k: {k} for k in brands}
    for s in suppliers.values():
        for b in s["brands"]:
            spell[key_of(b)].update(spellings(b))
    for alias, k in ALIASES.items():
        if k in spell:
            spell[k].add(alias)
    terms = []
    for k, words in spell.items():
        alts = "|".join(re.escape(w).replace(r"\ ", r"[\s\-]+") for w in sorted(words, key=len, reverse=True) if w)
        terms.append((re.compile(rf"(?<![{WORD}])(?:{alts})(?![{WORD}])"), k))
    return {"suppliers": suppliers, "brands": brands, "terms": terms}


async def load(client: Client, fresh: bool = False) -> tuple[dict | None, str]:
    """Индекс поставщиков клиента (кэш CACHE_HOURS). (None, причина) — таблицы нет или NocoDB не ответила."""
    s = client.nocodb
    ck = f"{client.id}:{s.base_id}"
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(ck)
    if hit and not fresh and now - hit[0] < CACHE_HOURS * 3600:
        return hit[1], ""
    noco = NocoClient(s)
    try:
        table_id = await noco.find_table_by_title(TABLE_TITLE, fresh)
        if not table_id:
            return None, f"в базе NocoDB нет таблицы «{TABLE_TITLE}»"
        rows = await noco.records(table_id)
    except (NocoError, RuntimeError) as exc:
        if hit:                                     # NocoDB не ответила — старый индекс лучше, чем ничего
            return hit[1], ""
        return None, f"не удалось прочитать «{TABLE_TITLE}»: {exc}"
    index = await asyncio.to_thread(build_index, rows)
    index["loaded_at"] = now
    index["table_id"] = table_id
    with _LOCK:
        _CACHE[ck] = (now, index)
    return index, ""


def _find(index: dict, text: str) -> list[tuple[str, int]]:
    out = []
    for rx, k in index["terms"]:
        m = rx.search(text)
        if m:
            out.append((k, m.start()))
    return out


def match_rows(index: dict, rows: list[dict], limit: int | None = SUMMARY_LIMIT) -> dict:
    """Позиции спецификации → {rows: [{i, n, level, brands: [{brand, strong, ids}]}], summary: {...}}."""
    sup = index["suppliers"]
    out_rows = []
    by_sup: dict[str, dict] = {}
    for i, r in enumerate(rows):
        brand_text = norm(" | ".join(str(v) for c, v in r.items() if v not in (None, "") and BRAND_COL_RE.search(c)
                                     and "стран" not in c.lower()))
        name_text = norm(next((r.get(c) for c in NAME_COLS if r.get(c)), ""))
        found: dict[str, bool] = {}
        for k, _ in _find(index, brand_text):
            found[k] = True
        for k, pos in _find(index, name_text):
            if k in found:
                continue
            if COMPAT_RE.search(name_text[max(0, pos - 40):pos]):
                continue                            # «совместимый с HP» — не товар бренда
            found[k] = False
        if not found:
            continue
        n = str(r.get("№") or i + 1).strip()
        items, best = [], 9
        for k, strong in found.items():
            ids = index["brands"][k]["ids"]
            items.append({"brand": index["brands"][k]["name"], "strong": strong, "ids": ids})
            for sid in ids:
                s = sup[sid]
                best = min(best, s["rank"])
                e = by_sup.setdefault(sid, {"id": sid, "positions": [], "brands": [], "strong": False})
                if n not in e["positions"]:
                    e["positions"].append(n)
                if items[-1]["brand"] not in e["brands"]:
                    e["brands"].append(items[-1]["brand"])
                e["strong"] = e["strong"] or strong
        level = "green" if best == 0 else "yellow" if best < 9 else "grey"
        out_rows.append({"i": i, "n": n, "level": level, "strong": any(found.values()), "brands": items})
    order = sorted(by_sup.values(), key=lambda e: (sup[e["id"]]["rank"], not e["strong"], -len(e["positions"]),
                                                   sup[e["id"]]["name"].lower()))
    used = {sid for e in order for sid in [e["id"]]}
    return {
        "rows": out_rows,
        "suppliers": {sid: {k: v for k, v in sup[sid].items() if k != "rank"} for sid in used},
        "summary": {
            "positions_total": len(rows),
            "positions_matched": len(out_rows),
            "positions_green": sum(1 for r in out_rows if r["level"] == "green"),
            "brands": sorted({b["brand"] for r in out_rows for b in r["brands"]}),
            "by_supplier": order[:limit] if limit else order,
            "more": max(0, len(order) - limit) if limit else 0,
        },
    }


async def for_spec(client: Client, rows: list[dict], fresh: bool = False, limit: int | None = SUMMARY_LIMIT) -> dict:
    """Ответ для страницы: совпадения или {error} (спецификация показывается в любом случае).
    limit=None — все поставщики в сводке (запросы поставщикам, 0.27.0)."""
    index, err = await load(client, fresh)
    if index is None:
        return {"error": err}
    data = await asyncio.to_thread(match_rows, index, rows, limit)
    data["loaded_at"] = index.get("loaded_at")
    data["suppliers_total"] = len(index["suppliers"])
    return data
