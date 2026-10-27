"""
Стоимость вызовов моделей в долларах — для показа на странице (анализ, ответы чата).

Цены Anthropic — с официальной страницы platform.claude.com/docs/en/about-claude/pricing (проверено 27.10.2026).
Цены DeepSeek — по сторонней сводке от 18.10.2026 (официальную страницу проверить не удалось), поэтому оценка.
Точная сумма — в консоли Anthropic (console.anthropic.com → Usage / Cost) и в кабинете DeepSeek.
При смене цен — поправить таблицы ниже.
"""

from __future__ import annotations

from datetime import datetime, timezone

# $ за 1 млн токенов: вход, ответ, запись в кэш на 5 мин, запись в кэш на 1 час, чтение из кэша.
ANTHROPIC = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 4.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),
}
# Haiku 5.5: если весь запрос (вход + запись в кэш + чтение из кэша) больше 100 000 токенов —
# весь запрос по повышенным ценам.
HAIKU_LONG = (0.50, 2.50, 0.625, 1.00, 0.05)
HAIKU_THRESHOLD = 100_000

# DeepSeek: чтение из кэша, вход без кэша, ответ — вне часов пик; в часы пик (будни 01–04 и 06–10 UTC) — вдвое дороже.
DEEPSEEK = {
    "deepseek-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}


def _peak(ts: float | None) -> bool:
    t = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return t.weekday() < 5 and (1 <= t.hour < 4 or 6 <= t.hour < 10)


def cost(model: str, *, input=0, output=0, cache_write_5m=0, cache_write_1h=0, cache_read=0, at=None) -> float | None:
    """Стоимость одного вызова в $ (None — цены модели неизвестны)."""
    i, o, w5, w1, r = (int(x or 0) for x in (input, output, cache_write_5m, cache_write_1h, cache_read))
    if model in ANTHROPIC:
        price = ANTHROPIC[model]
        if model == "claude-haiku-5-5" and i + w5 + w1 + r > HAIKU_THRESHOLD:
            price = HAIKU_LONG
        p_in, p_out, p_w5, p_w1, p_r = price
        return (i * p_in + o * p_out + w5 * p_w5 + w1 * p_w1 + r * p_r) / 1_000_000
    if model in DEEPSEEK:
        p_hit, p_miss, p_out = DEEPSEEK[model]
        k = 2 if _peak(at) else 1
        miss = max(i - r, 0)   # у DeepSeek input — весь вход, из него r взято из кэша
        return k * (r * p_hit + miss * p_miss + o * p_out) / 1_000_000
    return None


def money(usd: float | None) -> str:
    if usd is None:
        return ""
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
