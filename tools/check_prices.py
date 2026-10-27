#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_prices.py — проверка полноты поиска цен в Позиции_и_цены.xlsx (Шаг 5.1g / 5.1i CLAUDE.md).

Запускать ПЕРЕД отправкой спецификации в NocoDB (Шаг 7.1) и после перепроверки (Шаг 5.1i).
Ничего не меняет в файле — только читает и печатает отчёт.

По каждой позиции с маркой/моделью (или аналогом по ТХ) и по каждому проверяемому рынку
(РФ, Зарубеж; Avito — только с флагом --with-avito, пока поиск по Avito на паузе):
  * цены — числа > 0, по возрастанию, у каждой цены есть ссылка https://, ссылки разные;
  * если цен меньше трёх — в «Примечании» обязана быть пометка вида
        РФ: найдено 1 из 3 [запросы: «A» | «B» | «C» | «D» | «E»]
        Зарубеж: не найдено [запросы: «A» | «B» | «C» | «D» | «E» | «F»]
    с не менее чем MIN_QUERIES разными запросами (по умолчанию 5);
  * запрещены обобщения без поиска («цена по запросу у дилеров», «у бренда нет открытых цен» и т.п.),
    если по рынку не набран минимум запросов.

Выход: код 0 — всё в порядке; 1 — есть замечания (вернуться и доделать поиск, Шаг 5.1i);
2 — ошибка чтения файла. С флагом --json <файл> пишет список позиций к перепроверке.

Пример:
  python3 tools/check_prices.py --xlsx "<...>/AIExtract/Позиции_и_цены.xlsx"
  python3 tools/check_prices.py --xlsx "<...>.xlsx" --json recheck.json --min-queries 5
"""
import argparse
import json
import re
import sys

try:
    import openpyxl
except ImportError:  # pragma: no cover
    print("Нужен пакет openpyxl: pip install openpyxl", file=sys.stderr)
    sys.exit(2)

MARKETS = {
    "РФ": "Стоимость товара в РФ",
    "Avito": "Стоимость товара на Avito",
    "Зарубеж": "Стоимость товара зарубежом",
}
NO_BRAND_VALUES = {"", "не удалось определить", "не указано в документах", "—", "-"}
SKIP_MARKERS = ("пропущено (предварительный анализ)",)
NOTE_RE = re.compile(
    r"(?P<market>РФ|Avito|Зарубеж)\s*:\s*(?P<status>найдено\s*(?P<n>\d)\s*из\s*3|не\s+найдено)"
    r"\s*\[\s*запросы\s*:\s*(?P<queries>[^\]]*)\]",
    re.IGNORECASE,
)
GENERALIZATION_RE = re.compile(
    r"цена по запросу у (дилеров|продавцов)|цены в открытом доступе не найдены|"
    r"нет открытых цен|у бренда .{0,30}(нет|отсутству)",
    re.IGNORECASE,
)


def to_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return "bad"


def parse_queries(text):
    qs = re.findall(r"«([^»]+)»", text)
    return {q.strip().lower() for q in qs if q.strip()}


def check(path, markets, min_queries):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Позиции и цены"] if "Позиции и цены" in wb.sheetnames else wb.active
    header = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    idx = {h: i for i, h in enumerate(header)}
    need = ["№", "Марка/модель", "Примечание"]
    for m in markets:
        for k in (1, 2, 3):
            need += [f"{MARKETS[m]} - предложение {k}", f"{MARKETS[m]} - ссылка {k}"]
    missing = [h for h in need if h not in idx]
    if missing:
        print("ОШИБКА: в файле нет колонок: " + "; ".join(missing))
        return None, None

    problems, recheck = [], []
    total = checked = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        num = row[idx["№"]]
        if num is None or str(num).strip() == "":
            continue
        total += 1
        brand = str(row[idx["Марка/модель"]] or "").strip()
        note = str(row[idx["Примечание"]] or "")
        if brand.lower() in NO_BRAND_VALUES or any(s in note.lower() for s in SKIP_MARKERS):
            continue
        checked += 1
        notes = {}
        for mt in NOTE_RE.finditer(note):
            notes[mt.group("market").capitalize() if mt.group("market") != "РФ" else "РФ"] = mt
        row_probs, weak_markets = [], []
        for m in markets:
            prices, links = [], []
            for k in (1, 2, 3):
                p = to_number(row[idx[f"{MARKETS[m]} - предложение {k}"]])
                l = str(row[idx[f"{MARKETS[m]} - ссылка {k}"]] or "").strip()
                if p == "bad":
                    row_probs.append(f"{m}: «предложение {k}» не число")
                    continue
                if p is None and not l:
                    continue
                if p is None or p <= 0:
                    row_probs.append(f"{m}: ссылка {k} без цены")
                    continue
                if not l.startswith("https://"):
                    row_probs.append(f"{m}: у цены {k} нет ссылки https://")
                prices.append(p)
                links.append(l)
            if prices != sorted(prices):
                row_probs.append(f"{m}: цены не по возрастанию")
            if len(set(links)) != len(links):
                row_probs.append(f"{m}: одинаковые ссылки")
            if len(prices) < 3:
                weak_markets.append(m)
                key = m
                mt = notes.get(key)
                if mt is None:
                    row_probs.append(
                        f"{m}: найдено {len(prices)} из 3, а пометки «{m}: … [запросы: «…» | …]» в Примечании нет"
                    )
                else:
                    nq = len(parse_queries(mt.group("queries")))
                    if nq < min_queries:
                        row_probs.append(
                            f"{m}: найдено {len(prices)} из 3, но запросов только {nq} (нужно ≥ {min_queries})"
                        )
        if GENERALIZATION_RE.search(note) or GENERALIZATION_RE.search(
            " ".join(str(row[i] or "") for i, h in enumerate(header) if h.startswith("Комментарий агента"))
        ):
            if any(p.startswith(tuple(weak_markets)) for p in row_probs):
                row_probs.append("обобщение вместо поиска («цена по запросу у дилеров» и т.п.) — искать по набору запросов")
        if row_probs:
            problems.append((num, brand, row_probs))
        if weak_markets:
            recheck.append({"№": str(num), "Марка/модель": brand, "рынки": weak_markets})
    return (total, checked, problems), recheck


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", required=True, help="путь к Позиции_и_цены.xlsx")
    ap.add_argument("--min-queries", type=int, default=5, help="минимум разных запросов на рынок, если цен < 3")
    ap.add_argument("--with-avito", action="store_true", help="проверять и Avito (пока поиск по Avito на паузе)")
    ap.add_argument("--json", help="записать список позиций с < 3 ценами (для Шага 5.1i)")
    a = ap.parse_args()
    markets = ["РФ", "Зарубеж"] + (["Avito"] if a.with_avito else [])
    try:
        res, recheck = check(a.xlsx, markets, a.min_queries)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА чтения {a.xlsx}: {e}")
        sys.exit(2)
    if res is None:
        sys.exit(2)
    total, checked, problems = res
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(recheck, f, ensure_ascii=False, indent=1)
    print(f"Позиций: {total}, проверено (с маркой/аналогом): {checked}; рынки: {', '.join(markets)}")
    print(f"Позиций, где по какому-то рынку < 3 цен: {len(recheck)}")
    if not problems:
        print("OK: замечаний нет")
        sys.exit(0)
    print(f"ЗАМЕЧАНИЯ — {len(problems)} поз. (вернуться к Шагу 5.1i и доделать поиск):")
    for num, brand, probs in problems:
        print(f"  № {num} {brand[:60]}")
        for p in probs:
            print(f"     - {p}")
    sys.exit(1)


if __name__ == "__main__":
    main()
