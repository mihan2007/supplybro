"""Курс ЦБ РФ на сегодня (cbr.ru, напрямую; запасной — cbr-xml-daily.ru). Кэш на день в папке данных."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import httpx

DATA_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data"))


def _cbr_xml() -> dict[str, float]:
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=True) as http:
        r = http.get("https://www.cbr.ru/scripts/XML_daily.asp", headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = {}
    for v in root.iter("Valute"):
        code = v.findtext("CharCode")
        nominal = float((v.findtext("Nominal") or "1").replace(",", "."))
        value = float((v.findtext("Value") or "0").replace(",", "."))
        if code and value:
            out[code] = value / nominal
    out["_date"] = root.get("Date", "")
    return out


def _cbr_json() -> dict[str, float]:
    with httpx.Client(timeout=20, trust_env=True, follow_redirects=True) as http:
        r = http.get("https://www.cbr-xml-daily.ru/daily_json.js")
    r.raise_for_status()
    d = r.json()
    out = {k: v["Value"] / v["Nominal"] for k, v in d.get("Valute", {}).items()}
    out["_date"] = d.get("Date", "")[:10]
    return out


def rates() -> dict[str, float]:
    """{'USD': 83.7, 'EUR': 93.8, 'CNY': 12.5, …, '_date': '27.10.2026'}; пусто — курс получить не удалось."""
    cache = DATA_DIR / f"cbr_{date.today().isoformat()}.json"
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    for source in (_cbr_xml, _cbr_json):
        try:
            data = source()
            if data.get("USD"):
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data), encoding="utf-8")
                return data
        except Exception:
            continue
    return {}


def to_rub(price: float, currency: str, fx: dict) -> float | None:
    cur = (currency or "").upper().strip()
    if cur in ("", "RUB", "RUR"):
        return price
    rate = fx.get(cur)
    return round(price * rate, 2) if rate else None
