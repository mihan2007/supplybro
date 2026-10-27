"""
Позиции_и_цены.xlsx — таблица позиций конкурса (первый лист «Позиции и цены», как у агента старого проекта).

Колонки — те, что сайт заполняет на этапе 3 (без поиска в интернете); их названия совпадают с колонками
спецификации в NocoDB (SPEC_COLUMNS в tools/nocodb_client.py). Цены по рынкам, цены агента, аналоги,
размеры упаковки и доставка — этап 4.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

COLUMNS = [
    ("№", 6), ("Наименование позиции", 40), ("Техническое задание", 60), ("Ошибки в документации", 40),
    ("Марка/модель", 22), ("Производитель", 18), ("Страна происхождения", 14), ("Аналог допускается", 28),
    ("Аналог (статус)", 10), ("Кол-во", 8), ("Ед. изм.", 8), ("НМЦК за позицию", 18), ("Стоимость НМЦ", 14),
    ("Примечание", 50),
]
STATUS_FILL = {"Да": "C6EFCE", "Нет": "FFC7CE", "Уточни": "FFEB9C"}


def write_spec_xlsx(path: Path, rows: list[dict]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Позиции и цены"
    ws.append([c for c, _ in COLUMNS])
    for i, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = width
        ws.cell(1, i).font = Font(bold=True)
        ws.cell(1, i).alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append([r.get(c, "") for c, _ in COLUMNS])
        row = ws.max_row
        for i in range(1, len(COLUMNS) + 1):
            ws.cell(row, i).alignment = Alignment(wrap_text=True, vertical="top")
        status = ws.cell(row, [c for c, _ in COLUMNS].index("Аналог (статус)") + 1)
        if status.value in STATUS_FILL:
            status.fill = PatternFill("solid", fgColor=STATUS_FILL[status.value])
    ws.freeze_panes = "C2"
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(path)
