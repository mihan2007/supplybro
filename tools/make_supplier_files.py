#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_supplier_files.py - builds one Word file per manufacturer from
Позиции_и_цены.xlsx, for sending price requests to suppliers
(Шаг 7.1b в CLAUDE.md, добавлено 2026-09-30, версия 0.1.6).

Each file is named after the brand (column "Производитель", text in
parentheses dropped: "ДКС (DKC)" -> "ДКС.docx", "Huawei (аналог по ТХ)" ->
"Huawei.docx") and holds a table: № позиции в конкурсе | Наименование позиции
(with the brand/model from "Марка/модель") | Кол-во (with its unit).
Rows with an empty "Производитель" get no file - they are listed in the
JSON output under "skipped" so the caller can tell the user.

Usage:
    python3 make_supplier_files.py --xlsx ".../AIExtract/Позиции_и_цены.xlsx" \
        --out-dir ".../AIExtract/Поставщикам" [--ref "К922"]

Prints JSON: {"status": "ok", "files": [{"brand", "path", "positions"}], "skipped": [...]}
Requires python-docx (pip install python-docx) and openpyxl.
"""
import argparse
import json
import os
import re
import sys

try:
    import openpyxl
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
except ImportError as e:  # pragma: no cover
    print(json.dumps({"status": "error", "error": f"missing package: {e}. "
                      f"Install: {sys.executable} -m pip install python-docx openpyxl"}, ensure_ascii=False))
    sys.exit(1)

BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]+')
# Текст обращения к поставщику перед таблицей (добавлено 2026-09-30, версия 0.1.7,
# по просьбе пользователя). Можно переопределить флагом --greeting.
GREETING = "Здравствуйте. Планируется закупка. Просим дать коммерческое предложение на этот товар."
COL_WIDTHS = (Cm(2.6), Cm(11.4), Cm(3.0))  # sums to 17 cm = A4 text width with 2 cm margins


def brand_of(manufacturer: str) -> str:
    brand = re.sub(r"\s*\([^)]*\)", "", str(manufacturer or "")).strip()
    return brand or str(manufacturer or "").strip()


def safe_filename(name: str) -> str:
    return BAD_FILENAME_CHARS.sub("_", name).strip(" .") or "Без названия"


def fmt_qty(qty, unit) -> str:
    if qty is None or str(qty).strip() == "":
        return ""
    if isinstance(qty, float) and qty.is_integer():
        qty = int(qty)
    q = str(qty).replace(".", ",")
    return f"{q} {unit}".strip() if unit else q


def item_name(row) -> str:
    name = str(row.get("Наименование позиции") or "").strip()
    model = str(row.get("Марка/модель") or "").strip()
    model = re.sub(r"^аналог по ТХ:\s*", "", model, flags=re.IGNORECASE)
    if not model or model.startswith("—") or model.lower() in name.lower():
        return name
    return f"{name} — {model}"


def shade(cell, hex_fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def build_doc(brand, rows, ref, path, greeting=GREETING):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    style.font.size = Pt(11)

    title = doc.add_paragraph()
    run = title.add_run(brand)
    run.bold = True
    run.font.size = Pt(16)
    sub = doc.add_paragraph()
    sub_text = "Перечень позиций для запроса цены и сроков поставки"
    if ref:
        sub_text += f" (запрос {ref})"
    r = sub.add_run(sub_text)
    r.font.size = Pt(11)
    r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    if greeting:
        g = doc.add_paragraph()
        g.paragraph_format.space_before = Pt(6)
        g.paragraph_format.space_after = Pt(10)
        g.add_run(greeting)

    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for i, w in enumerate(COL_WIDTHS):
        table.columns[i].width = w
    headers = ("№ позиции", "Наименование позиции", "Кол-во")
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.width = COL_WIDTHS[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(h)
        run.bold = True
        shade(cell, "D9E2F3")
    for row in rows:
        cells = table.add_row().cells
        values = (str(row.get("№") or ""), item_name(row), fmt_qty(row.get("Кол-во"), row.get("Ед. изм.")))
        for i, v in enumerate(values):
            cells[i].width = COL_WIDTHS[i]
            cells[i].text = v
    # header row repeats on each page
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    hdr = OxmlElement("w:tblHeader")
    hdr.set(qn("w:val"), "true")
    tr_pr.append(hdr)
    doc.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--greeting", default=GREETING, help="Text addressed to the supplier, printed above the table")
    ap.add_argument("--ref", default="", help="Reference printed in the subtitle, e.g. the internal tender number")
    args = ap.parse_args()

    wb = openpyxl.load_workbook(args.xlsx, data_only=True)
    ws = wb.worksheets[0]
    all_rows = list(ws.iter_rows(values_only=True))
    headers = all_rows[0]
    if "Производитель" not in headers:
        print(json.dumps({"status": "error", "error": "в xlsx нет колонки «Производитель» (Шаг 5.1)"}, ensure_ascii=False))
        sys.exit(1)

    groups, order, skipped = {}, [], []
    for values in all_rows[1:]:
        row = dict(zip(headers, values))
        if not str(row.get("Наименование позиции") or "").strip():
            continue
        brand = brand_of(row.get("Производитель"))
        if not brand:
            skipped.append({"№": row.get("№"), "item": row.get("Наименование позиции"), "reason": "не указан производитель"})
            continue
        if brand not in groups:
            groups[brand] = []
            order.append(brand)
        groups[brand].append(row)

    os.makedirs(args.out_dir, exist_ok=True)
    files = []
    for brand in order:
        path = os.path.join(args.out_dir, safe_filename(brand) + ".docx")
        build_doc(brand, groups[brand], args.ref, path, args.greeting)
        files.append({"brand": brand, "path": path, "positions": [r.get("№") for r in groups[brand]]})
    print(json.dumps({"status": "ok", "files": files, "skipped": skipped}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
