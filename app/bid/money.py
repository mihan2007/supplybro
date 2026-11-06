"""Суммы в рублях: «1 234 567,89» и прописью («Один миллион двести тридцать четыре тысячи … рублей 89 копеек»)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_UNITS_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
# (формы: 1, 2–4, 5–0), женский род
_SCALES = [(("", "", ""), False), (("тысяча", "тысячи", "тысяч"), True), (("миллион", "миллиона", "миллионов"), False),
           (("миллиард", "миллиарда", "миллиардов"), False), (("триллион", "триллиона", "триллионов"), False)]


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _triplet(n: int, feminine: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        words.append(_TENS[rest // 10])
        words.append((_UNITS_F if feminine else _UNITS_M)[rest % 10])
    return [w for w in words if w]


def number_words(n: int, feminine: bool = False) -> str:
    """Целое число прописью (именительный падеж)."""
    if n == 0:
        return "ноль"
    parts: list[str] = []
    i = 0
    while n > 0:
        n, chunk = divmod(n, 1000)
        if chunk:
            forms, fem = _SCALES[i]
            words = _triplet(chunk, fem if i else feminine)
            if i:
                words.append(plural(chunk, forms))
            parts = words + parts
        i += 1
    return " ".join(parts)


def to_decimal(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rub(value) -> str:
    """1234567.8 → «1 234 567,80»."""
    d = to_decimal(value)
    whole, frac = divmod(abs(d), 1)
    s = f"{int(whole):,}".replace(",", " ")
    return ("-" if d < 0 else "") + s + "," + f"{int(frac * 100):02d}"


def rub_words(value) -> str:
    """1234567.89 → «Один миллион двести тридцать четыре тысячи пятьсот шестьдесят семь рублей 89 копеек»."""
    d = to_decimal(value)
    whole = int(d)
    kop = int((d - whole) * 100)
    text = f"{number_words(whole)} {plural(whole, ('рубль', 'рубля', 'рублей'))} {kop:02d} " \
           f"{plural(kop, ('копейка', 'копейки', 'копеек'))}"
    return text[0].upper() + text[1:]
