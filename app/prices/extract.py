"""
Цена товара со страницы магазина — без ИИ.

Где ищется (по надёжности):
  1. JSON-LD (schema.org Product / Offer / AggregateOffer) — его размещает почти каждый магазин для поисковиков;
  2. микроразметка: itemprop="price" / "priceCurrency" / "sku" / "name";
  3. мета-теги: product:price:amount / og:price:amount и валюта;
  4. запасной путь — числа рядом с «₽», «руб», «€», «$» в видимом тексте (менее надёжно — дальше решает модель).
Также — видимый текст страницы (для проверки артикула) и фрагмент вокруг артикула/цены для модели.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

from .articles import mentions, norm

CURRENCY_SIGNS = {"₽": "RUB", "руб": "RUB", "р.": "RUB", "rub": "RUB", "€": "EUR", "eur": "EUR", "$": "USD",
                  "usd": "USD", "¥": "CNY", "cny": "CNY", "rmb": "CNY", "£": "GBP", "gbp": "GBP"}


@dataclass
class Offer:
    price: float | None = None
    currency: str = ""
    name: str = ""
    sku: str = ""
    availability: str = ""
    condition: str = ""
    method: str = ""          # jsonld / microdata / meta / text


@dataclass
class PageInfo:
    title: str = ""
    text: str = ""
    offers: list[Offer] = field(default_factory=list)
    has_article: bool = False
    context: str = ""          # фрагмент текста вокруг артикула и цены — для модели


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.items: dict[str, list[str]] = {}
        self.title = ""
        self._skip = 0
        self._in_ld = False
        self._in_title = False
        self._buf: list[str] = []
        self._pending_prop: str | None = None   # itemprop без content — значение в тексте элемента

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg"):
            if tag == "script" and "ld+json" in a.get("type", "").lower():
                self._in_ld, self._buf = True, []
            else:
                self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        prop = a.get("itemprop", "").lower()
        if prop:
            val = a.get("content") or a.get("value") or ""
            if val:
                self.items.setdefault(prop, []).append(val)
                self._pending_prop = None
            else:
                self._pending_prop = prop
        if tag in ("br", "p", "div", "li", "tr", "td", "h1", "h2", "h3", "span"):
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.jsonld.append("".join(self._buf))
            return
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
        self.text.append(data)
        if self._pending_prop and data.strip():
            self.items.setdefault(self._pending_prop, []).append(data.strip())
            self._pending_prop = None


def parse_price(raw) -> float | None:
    """«1 234,50» / «1234.5» / «12 340 ₽» / 1234 → число."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    t = unescape(str(raw)).replace(" ", " ").replace(" ", " ")
    m = re.search(r"\d[\d\s.,']*", t)
    if not m:
        return None
    s = re.sub(r"[\s']", "", m.group(0)).rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif s.count(".") == 1 and len(s.split(".")[-1]) == 3:
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _walk(obj, out: list):
    if isinstance(obj, list):
        for x in obj:
            _walk(x, out)
    elif isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _walk(v, out)


def _types(d: dict) -> set[str]:
    t = d.get("@type")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t]) if x}


def _from_jsonld(blocks: list[str]) -> list[Offer]:
    offers = []
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        nodes: list[dict] = []
        _walk(data, nodes)
        for prod in [n for n in nodes if "product" in _types(n)]:
            name = str(prod.get("name") or "")
            sku = " ".join(str(prod.get(k) or "") for k in ("sku", "mpn", "productID", "gtin13")).strip()
            cond = str(prod.get("itemCondition") or "")
            raw_offers = prod.get("offers")
            sub: list[dict] = []
            _walk(raw_offers, sub)
            for o in sub or [{}]:
                ps = o.get("priceSpecification")
                price = parse_price(o.get("price") or o.get("lowPrice") or (ps.get("price") if isinstance(ps, dict) else None))
                if price is None:
                    continue
                offers.append(Offer(price=price, currency=str(o.get("priceCurrency") or "").upper(), name=name, sku=sku,
                                    availability=str(o.get("availability") or "").split("/")[-1],
                                    condition=(str(o.get("itemCondition") or cond)).split("/")[-1], method="jsonld"))
        for o in [n for n in nodes if "offer" in _types(n) and not offers]:
            price = parse_price(o.get("price") or o.get("lowPrice"))
            if price:
                offers.append(Offer(price=price, currency=str(o.get("priceCurrency") or "").upper(),
                                    availability=str(o.get("availability") or "").split("/")[-1], method="jsonld"))
    return offers


_TEXT_PRICE = re.compile(r"(\d[\d\s .,]{1,14}\d|\d)\s*(₽|руб\.?|р\.|€|\$|eur|usd|¥)|(€|\$|¥|USD|EUR)\s*(\d[\d\s.,]{0,14}\d)",
                         re.IGNORECASE)


def _from_text(text: str, around: str) -> list[Offer]:
    """Запасной путь: суммы с валютой в видимом тексте (ближе к артикулу — первыми)."""
    found = []
    for m in _TEXT_PRICE.finditer(around or text[:20000]):
        num = m.group(1) or m.group(4)
        sign = (m.group(2) or m.group(3) or "").lower().rstrip(".")
        cur = next((v for k, v in CURRENCY_SIGNS.items() if sign.startswith(k)), "")
        p = parse_price(num)
        if p and p >= 10:
            found.append(Offer(price=p, currency=cur, method="text"))
        if len(found) >= 5:
            break
    return found


def analyze_page(html: str, arts: list[str]) -> PageInfo:
    p = _Parser()
    try:
        p.feed(html)
    except Exception:  # битый HTML — берём, что успели
        pass
    text = re.sub(r"\s+", " ", unescape("".join(p.text))).strip()
    info = PageInfo(title=" ".join((p.meta.get("og:title") or p.title).split())[:300], text=text)
    info.offers = _from_jsonld(p.jsonld)
    if not info.offers and p.items.get("price"):
        cur = (p.items.get("pricecurrency") or [""])[0].upper()
        for v in p.items["price"][:3]:
            pr = parse_price(v)
            if pr:
                info.offers.append(Offer(price=pr, currency=cur, name=(p.items.get("name") or [""])[0],
                                         sku=" ".join(p.items.get("sku", []) + p.items.get("mpn", [])),
                                         availability=(p.items.get("availability") or [""])[0].split("/")[-1],
                                         method="microdata"))
    if not info.offers:
        for k in ("product:price:amount", "og:price:amount", "price"):
            pr = parse_price(p.meta.get(k))
            if pr:
                cur = (p.meta.get("product:price:currency") or p.meta.get("og:price:currency") or "").upper()
                info.offers.append(Offer(price=pr, currency=cur, method="meta"))
                break
    info.has_article = mentions(info.title + " " + text + " " + " ".join(o.sku + " " + o.name for o in info.offers), arts)
    # Фрагмент для модели: вокруг первого упоминания артикула (или начало текста) + заголовок.
    pos = -1
    if arts:
        nt = norm(text)
        for a in arts:
            i = nt.find(norm(a))
            if i >= 0:
                # перевод позиции из «нормализованного» текста приблизительно — берём по доле длины
                pos = int(i / max(len(nt), 1) * len(text))
                break
    start = max(0, pos - 700) if pos >= 0 else 0
    info.context = text[start:start + 1600]
    if not info.offers:
        info.offers = _from_text(text, info.context)
    return info
