"""
Зарубежные предложения — встроенный веб-поиск Claude (web_search, $10 за 1000 поисков + токены).

Модель получает артикул(ы) и название, сама делает до PRICES_ABROAD_SEARCHES поисков (голый артикул,
бренд + артикул, английское название детали + артикул, «price») и возвращает ссылки на страницы именно этого
товара в зарубежных магазинах (приоритет: Германия, Китай, США). Цену с этих страниц потом проверяет программа
(app/prices/fetch.py + extract.py); то, что не удалось открыть, в цены не идёт — только в «Примечание».
"""

from __future__ import annotations

import json
import os
import time

from ..analysis.llm import (ANTHROPIC_VERSION, CLAUDE_KEY_FILE, CLAUDE_URL, Usage, _claude_usage, _extract_json,
                            _post, conform, read_key)

SEARCHES = int(os.environ.get("PRICES_ABROAD_SEARCHES", "5"))
SEARCH_USD = 0.01

PROMPT = """Найди в интернете, где за рубежом продаётся ИМЕННО этот товар, и верни ссылки на страницы товара.

Товар: {name}
Марка / модель / артикул: {brand_model}
Артикулы для поиска: {articles}

Правила:
- Ищи магазины и дистрибьюторов за пределами России: в приоритете Германия, Китай (AliExpress, Alibaba, \
Made-in-China, магазины .cn), США (eBay, Amazon, дистрибьюторы); другие страны — если в этих мало.
- Сделай не меньше 5 разных запросов (если 3 магазина не нашлись раньше): голый артикул; бренд + артикул; \
английское название детали + бренд + артикул; артикул + price; артикул + Preis / 价格. Каждый альтернативный \
артикул — отдельным запросом.
- Нужны только страницы конкретного товара с этим артикулом (карточка товара), не каталоги, не форумы, не поиск.
- Цену и валюту указывай, только если видишь её в результатах поиска; не придумывай. Если цены не видно — price пустая.
- Российские сайты (.ru) не включай.

Ответ — только один JSON-объект без пояснений:
{{"offers": [{{"url": "https://…", "shop": "название", "country": "DE/CN/US/…", "title": "заголовок страницы", \
"price": "число или пусто", "currency": "EUR/USD/CNY/… или пусто"}}], "notes": "кратко, что не нашлось"}}"""

SCHEMA = {"type": "object", "required": ["offers"], "properties": {
    "offers": {"type": "array", "items": {"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string"}, "shop": {"type": "string"}, "country": {"type": "string"},
        "title": {"type": "string"}, "price": {"type": "string"}, "currency": {"type": "string"}}}},
    "notes": {"type": "string"}}}


def search_abroad(model: str, name: str, brand_model: str, arts: list[str], usage: Usage) -> dict:
    """{'offers': [...], 'queries': [...], 'notes': str}. Ошибка API — LLMError."""
    key = read_key(CLAUDE_KEY_FILE, "Claude")
    body = {
        "model": model, "max_tokens": 6000,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": SEARCHES}],
        "messages": [{"role": "user", "content": PROMPT.format(name=name, brand_model=brand_model or "—",
                                                               articles=", ".join(arts) or "—")}],
    }
    if "haiku" in model:
        body["output_config"] = {"effort": "low"}
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    t0 = time.monotonic()
    data = _post(CLAUDE_URL, headers, body, 300, "Claude")
    u = _claude_usage(model, data.get("usage") or {})
    searches = int(((data.get("usage") or {}).get("server_tool_use") or {}).get("web_search_requests") or 0)
    u["usd"] = round((u.get("usd") or 0) + searches * SEARCH_USD, 5)
    u.pop("model")
    usage.add(model=model, label=f"Зарубеж: {arts[0] if arts else name[:30]}", searches=searches,
              seconds=round(time.monotonic() - t0, 1), **u)
    queries, found_urls = [], []
    for b in data.get("content") or []:
        if b.get("type") == "server_tool_use" and b.get("name") == "web_search":
            q = (b.get("input") or {}).get("query")
            if q:
                queries.append(q)
        if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            found_urls += [x.get("url") for x in b["content"] if isinstance(x, dict) and x.get("url")]
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    try:
        ans = conform(_extract_json(text), SCHEMA)
    except (ValueError, json.JSONDecodeError):
        ans = {"offers": [], "notes": "ответ модели не разобран"}
    seen_urls = set(found_urls)
    offers = []
    for o in ans.get("offers") or []:
        url = (o.get("url") or "").strip()
        if not url.startswith("http") or ".ru/" in url + "/" and "aliexpress" not in url:
            continue
        o["in_search_results"] = url in seen_urls      # ссылку дал поиск, а не модель «из памяти»
        offers.append(o)
    return {"offers": offers, "queries": queries, "notes": ans.get("notes") or "", "searches": searches}
