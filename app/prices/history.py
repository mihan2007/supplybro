"""
Своя база цен: тот же артикул в прошлых спецификациях NocoDB (таблицы, названные внутренним номером: К910, К934…).

Что берётся: цены отдела закупок (КП поставщиков, с поставщиком), цены рынков РФ / зарубеж со ссылками,
«Цены агента». Таблицы читаются один раз и кэшируются в папке данных на PRICES_HISTORY_HOURS часов.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .articles import articles, norm

from tools import nocodb_client as nc

DATA_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data"))
CACHE_HOURS = float(os.environ.get("PRICES_HISTORY_HOURS", "12"))
SPEC_TITLE = re.compile(r"^(?:[КK]-?\d{1,5}|Спецификация\s+—\s+.+)$")


def _price_fields(row: dict) -> list[dict]:
    out = []
    for s_col, p_col in nc.PROCUREMENT_PAIRS:
        price = row.get(p_col)
        if price not in (None, ""):
            out.append({"kind": "отдел закупок", "price": price, "who": str(row.get(s_col) or "").strip()})
    for prefix, market in (("Стоимость товара в РФ", "РФ"), ("Стоимость товара зарубежом", "зарубеж")):
        for i in (1, 2, 3):
            price = row.get(f"{prefix} - предложение {i}")
            if price not in (None, ""):
                out.append({"kind": market, "price": price, "url": row.get(f"{prefix} - ссылка {i}") or ""})
    for i in (1, 2, 3):
        price = row.get(f"Цена агента {i}")
        if price not in (None, ""):
            out.append({"kind": "агент", "price": price, "who": str(row.get(f"Комментарий агента {i}") or "")[:200]})
    return out


def cache_path(client) -> Path:
    """Файл своей базы цен — свой у каждой базы NocoDB клиента (0.21.0: базу переключают в админ-панели)."""
    return DATA_DIR / f"price_history_{client.id}_{client.nocodb.base_id}.json"


def load_index(client, progress=None) -> dict[str, list[dict]]:
    """{нормализованный артикул: [{tender, №, name, prices[], updated}]}."""
    cache = cache_path(client)
    if cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_HOURS * 3600:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    s = client.nocodb
    # nc.BASE_ID не трогаем: это общая настройка программы агента, её в это же время может использовать запись
    # в NocoDB другого клиента (другой поток обработчика). Здесь база указывается в адресе явно.
    token = s.token()
    status, body = nc._request("GET", f"{nc.META_URL}/bases/{s.base_id}/tables", token)
    if status != 200:
        raise RuntimeError(f"NocoDB: список таблиц не получен (HTTP {status})")
    tables = [t for t in body.get("list", []) if SPEC_TITLE.match(t.get("title", ""))]
    index: dict[str, list[dict]] = {}
    for n, t in enumerate(tables):
        if progress:
            progress(f"Своя база цен: {t['title']} ({n + 1} из {len(tables)})", n, len(tables))
        try:
            rows = nc.list_all_records(token, t["id"])
        except Exception:
            continue
        for r in rows:
            prices = _price_fields(r)
            if not prices:
                continue
            item = {"tender": t["title"], "n": str(r.get("№") or ""), "name": str(r.get("Наименование позиции") or "")[:200],
                    "brand_model": str(r.get("Марка/модель") or ""), "prices": prices,
                    "updated": str(r.get("UpdatedAt") or r.get("CreatedAt") or "")[:10]}
            for a in articles(item["brand_model"], item["name"]):
                index.setdefault(norm(a), []).append(item)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Целиком и разом (сайт читает этот файл для «Все цены», пока обработчик его пишет, — 0.10.0).
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache)
    return index


def lookup(index: dict, arts: list[str], exclude_tender: str = "") -> list[dict]:
    seen, out = set(), []
    for a in arts:
        for item in index.get(norm(a), []):
            key = (item["tender"], item["n"])
            if item["tender"] == exclude_tender or key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out
