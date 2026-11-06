"""Формы Word из документации: разметка для модели, вырезание формы, правки модели, пометки «<нет данных>».

Основа — помощники старого агента (tools/declarative_forms.py: extract_range, set_par, fill_cell, MARK, GAPS). Модель
не пишет документ сама: она получает разметку формы с номерами элементов ([А12] — абзац, [Т15] — таблица, ячейки
r/c) и отвечает списком правок; правки вносит код, форматирование берётся из самой формы.

В тексте правки:
- «[[что нужно]]» — сведений нет: в документ — ровно «<нет данных>» (жёлтая подсветка), подсказка — в «Нет данных.txt»;
- «{{ИМЯ}}» — значение, которое подставит код (цена, НДС, сумма прописью, паспорт руководителя — персональные данные
  модели не передаются); нет значения — «<нет данных>» с подсказкой из TOKENS.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from tools import declarative_forms as df

MARK = df.MARK
TOKEN_RE = re.compile(r"\{\{\s*([A-ZА-ЯЁ_0-9]+)(?::\s*([^{}]+?))?\s*\}\}")
# Подставляет код (персональные данные и цены модели не видит и не считает).
TOKENS = {
    "ЦЕНА_БЕЗ_НДС": "цена заявки без НДС",
    "НДС": "сумма НДС",
    "ЦЕНА_С_НДС": "цена заявки с НДС",
    "ЦЕНА_БЕЗ_НДС_ПРОПИСЬЮ": "цена заявки без НДС прописью",
    "НДС_ПРОПИСЬЮ": "сумма НДС прописью",
    "ЦЕНА_С_НДС_ПРОПИСЬЮ": "цена заявки с НДС прописью",
    "СТАВКА_НДС": "ставка НДС участника",
    "СНИЖЕНИЕ_ПРОЦ": "снижение цены от НМЦ, %",
    "ЦЕНА_ЕД_БЕЗ_НДС": "цена за единицу без НДС (позиция)",
    "ЦЕНА_ЕД_С_НДС": "цена за единицу с НДС (позиция)",
    "СУММА_БЕЗ_НДС": "стоимость позиции без НДС",
    "СУММА_С_НДС": "стоимость позиции с НДС",
    "ПАСПОРТ_РУКОВОДИТЕЛЯ": "паспортные данные руководителя",
}
OUTLINE_RE = re.compile(r"^\s*(форма|образец|приложение|раздел|часть|глава|\d+(\.\d+)*\.?\s+(форма|образец))\b", re.I)


# ---------- разметка ----------

def _cells(row) -> list:
    """Ячейки строки без повторов объединённых по горизонтали."""
    seen, out = set(), []
    for c in row.cells:
        if id(c._tc) in seen:
            continue
        seen.add(id(c._tc))
        out.append(c)
    return out


AUTO_NUM = "(номер — автоматически)"
SERVICE_RE = re.compile(r"(начало|конец) формы\.?")


def _auto_numbered(cell) -> bool:
    """Ячейка с автонумерацией Word («1.», «2.» рисует сам Word, в тексте её нет)."""
    return not cell.text.strip() and any(True for _ in cell._tc.iter(qn("w:numPr")))


def elements(doc) -> list:
    """Элементы тела документа (абзацы и таблицы) — номера, как их видит модель."""
    return [el for el in doc.element.body.iterchildren() if el.tag in (qn("w:p"), qn("w:tbl"))]


def outline(path: Path, limit: int = 400) -> list[dict]:
    """Заголовки документа — где искать формы: абзацы «Форма…», «Образец…», «Приложение…», стили заголовков.
    Номера — по elements(): те же, что у extract()."""
    d = docx.Document(str(path))
    out = []
    after_annex = False          # «Приложение … к письму о подаче оферты» — следующий абзац обычно название формы
    for i, el in enumerate(elements(d)):
        if el.tag != qn("w:p"):
            continue
        p = Paragraph(el, d)
        text = " ".join(p.text.split())
        if not text:
            continue
        if len(text) > 220:
            after_annex = False
            continue
        style = (p.style.name if p.style is not None else "") or ""
        if OUTLINE_RE.match(text) or "heading" in style.lower() or "заголов" in style.lower():
            out.append({"i": i, "text": text[:180]})
            after_annex = text.lower().startswith("приложение")
        elif after_annex:
            out.append({"i": i, "text": text[:180]})
            after_annex = False
        if len(out) >= limit:
            break
    return out


def count_elements(path: Path) -> int:
    return len(elements(docx.Document(str(path))))


def extract(path: Path, first: int, last: int):
    """Документ, в котором оставлены только элементы first..last (номера elements()), с оформлением и полями."""
    d = docx.Document(str(path))
    els = elements(d)
    keep = set(els[first:last + 1])
    body = d.element.body
    for el in list(body.iterchildren()):
        if el.tag == qn("w:sectPr"):
            continue
        if el.tag in (qn("w:p"), qn("w:tbl")) and el in keep:
            continue
        body.remove(el)
    # Разрывы разделов внутри куска — убрать (иначе пустые страницы); первый абзац — без разрыва страницы.
    inner = [ppr.find(qn("w:sectPr")) for ppr in body.iter(qn("w:pPr")) if ppr.find(qn("w:sectPr")) is not None]
    final = body.find(qn("w:sectPr"))
    if inner and final is not None:
        new = copy.deepcopy(inner[-1])
        for ch in list(new):
            if ch.tag == qn("w:type"):
                new.remove(ch)
        body.replace(final, new)
    for ppr in body.iter(qn("w:pPr")):
        s = ppr.find(qn("w:sectPr"))
        if s is not None:
            ppr.remove(s)
    first_p = body.find(qn("w:p"))
    if first_p is not None:
        for br in list(first_p.iter(qn("w:br"))):
            if br.get(qn("w:type")) == "page":
                br.getparent().remove(br)
        ppr = first_p.find(qn("w:pPr"))
        if ppr is not None and ppr.find(qn("w:pageBreakBefore")) is not None:
            ppr.remove(ppr.find(qn("w:pageBreakBefore")))
    return d


def markup(doc, max_cell: int = 300) -> str:
    """Разметка формы для модели: [А5] текст абзаца; [Т7] таблица — строки r0.. и ячейки c0..
    Пустые абзацы показываются как [А3] (пусто) — в них тоже можно писать."""
    lines = []
    for i, el in enumerate(elements(doc)):
        if el.tag == qn("w:p"):
            text = Paragraph(el, doc).text
            lines.append(f"[А{i}] " + (text.replace("\n", " ⏎ ") if text.strip() else "(пусто)"))
        else:
            t = Table(el, doc)
            lines.append(f"[Т{i}] таблица, строк {len(t.rows)}")
            for r, row in enumerate(t.rows):
                cells = _cells(row)
                parts = []
                for c, cell in enumerate(cells):
                    text = " ".join(cell.text.split())
                    parts.append(f"c{c}: " + (AUTO_NUM if _auto_numbered(cell) else f"«{text[:max_cell]}»"))
                lines.append(f"   r{r}: " + " | ".join(parts))
    return "\n".join(lines)


# ---------- правки ----------

def substitute(text: str, values: dict) -> str:
    """{{ИМЯ}} / {{ИМЯ:№}} → значение; нет значения — [[подсказка]] (станет «<нет данных>»)."""
    def rep(m):
        name, arg = m.group(1).upper(), (m.group(2) or "").strip()
        key = f"{name}:{arg}" if arg else name
        v = values.get(key)
        if v not in (None, ""):
            return str(v)
        hint = TOKENS.get(name, name.lower().replace("_", " "))
        return f"[[{hint}{' № ' + arg if arg else ''}]]"
    out = TOKEN_RE.sub(rep, text or "")
    # «{{СТАВКА_НДС}} %» при значении «22 %» / «НДС не облагается» — без второго знака процента.
    return re.sub(r"(%|облагается)\s*%", r"\1", out)


def _norm_marks(text: str) -> str:
    """Модель могла написать пометку словами — привести к [[…]]."""
    text = text.replace(MARK, "[[нет данных]]")
    return re.sub(r"<\s*нет данных\s*:?\s*([^<>]*)>", lambda m: f"[[{m.group(1).strip() or 'нет данных'}]]", text)


_QUOTES = "«»\"“”„'"


def _fuzzy(find: str) -> re.Pattern:
    """Образец для поиска фрагмента формы, если модель переписала его неточно: любая длина подчёркиваний и пробелов,
    кавычки любого вида."""
    out = []
    for ch in re.split(r"(_+|\s+|[«»\"“”„'])", find):
        if not ch:
            continue
        if ch.startswith("_"):
            out.append(r"_+")
        elif ch.isspace():
            out.append(r"\s*")
        elif ch in _QUOTES:
            out.append("[" + re.escape(_QUOTES) + "]")
        else:
            out.append(re.escape(ch))
    return re.compile(r"\s*".join(x for x in out if x != r"\s*"))


def replace_once(text: str, find: str, repl: str) -> str | None:
    """Заменить первый find в text (точно, иначе — по _fuzzy). None — не найдено."""
    if find and find in text:
        return text.replace(find, repl, 1)
    if not find or not find.strip(" _"):
        # только подчёркивания — первое поле для заполнения
        m = re.search(r"_{3,}", text)
    else:
        m = _fuzzy(find).search(text)
    if not m:
        return None
    return text[:m.start()] + repl + text[m.end():]


_KEEP = "\x00"     # пометка, которая уже стояла в абзаце (до правки) — снова с подсветкой, но без второй строки в GAPS


def _keep_marks(write, target, text: str) -> None:
    n = len(df.GAPS)
    write(target, text.replace(MARK, f"[[{_KEEP}]]"))
    df.GAPS[n:] = [g for g in df.GAPS[n:] if g != _KEEP]


def _set_par(p, text: str) -> None:
    _keep_marks(df.set_par, p, text)


def _set_cell(cell, text: str) -> None:
    _keep_marks(lambda c, t: df.fill_cell(c, t, align=None), cell, text)


def _add_rows(table: Table, after: int, rows: list[list[str]], values: dict) -> int:
    """Строки по образцу строки after (оформление копируется), значения — по ячейкам."""
    if not rows:
        return 0
    template = table.rows[after]._tr
    anchor = template
    added = 0
    for values_row in rows:
        new = copy.deepcopy(template)
        anchor.addnext(new)
        anchor = new
        added += 1
    # заполнить по ячейкам
    trs = list(table._tbl.iterchildren(qn("w:tr")))
    start = trs.index(template) + 1
    from docx.table import _Row
    for k, values_row in enumerate(rows):
        row = _Row(trs[start + k], table)
        cells = _cells(row)
        for c, cell in enumerate(cells):
            text = values_row[c] if c < len(values_row) else ""
            if _auto_numbered(cell) and re.fullmatch(r"\s*\d*[.)]?\s*", str(text)):
                continue
            _set_cell(cell, _norm_marks(substitute(str(text), values)))
    return added


def apply(doc, edits: list[dict], values: dict) -> tuple[int, list[str], list[str]]:
    """Вносит правки модели. Возвращает (внесено, проблемы, подсказки «чего нет»)."""
    els = elements(doc)
    tables = {}
    applied, problems = 0, []
    df.GAPS.clear()
    delete: list = []
    row_adds: list[tuple] = []
    for e in edits:
        op = e.get("op")
        i = e.get("i")
        if not isinstance(i, int) or not (0 <= i < len(els)):
            problems.append(f"правка {op}: нет элемента {i}")
            continue
        el = els[i]
        if op in ("cell", "cell_replace") and el.tag == qn("w:p"):
            op = "par" if op == "cell" else "replace"          # модель ошиблась видом элемента — правка та же
        elif op in ("par", "replace") and el.tag == qn("w:tbl") and e.get("r") is not None:
            op = "cell" if op == "par" else "cell_replace"
        text = _norm_marks(substitute(str(e.get("text") or ""), values))
        try:
            if op in ("par", "replace", "delete"):
                if el.tag != qn("w:p"):
                    problems.append(f"правка {op}: [{i}] — не абзац")
                    continue
                p = Paragraph(el, doc)
                if op == "par":
                    _set_par(p, text)
                elif op == "replace":
                    find = str(e.get("find") or "")
                    new_text = replace_once(p.text, find, text)
                    if new_text is None:
                        problems.append(f"[А{i}]: не найдено «{find[:40]}»")
                        continue
                    _set_par(p, new_text)
                else:
                    delete.append(el)
            elif op in ("cell", "cell_replace", "add_rows"):
                if el.tag != qn("w:tbl"):
                    problems.append(f"правка {op}: [{i}] — не таблица")
                    continue
                t = tables.setdefault(i, Table(el, doc))
                r = int(e.get("r") or 0)
                if not (0 <= r < len(t.rows)):
                    problems.append(f"[Т{i}]: нет строки r{r}")
                    continue
                if op == "add_rows":
                    row_adds.append((t, r, e.get("rows") or []))
                    continue
                cells = _cells(t.rows[r])
                c = int(e.get("c") or 0)
                if not (0 <= c < len(cells)):
                    problems.append(f"[Т{i}] r{r}: нет ячейки c{c}")
                    continue
                if op == "cell":
                    if _auto_numbered(cells[c]) and re.fullmatch(r"\s*\d+[.)]?\s*", text):
                        continue                      # номер уже рисует автонумерация Word — не дублировать
                    _set_cell(cells[c], text)
                else:
                    find = str(e.get("find") or "")
                    new_text = replace_once(cells[c].text, find, text)
                    if new_text is None:
                        problems.append(f"[Т{i}] r{r} c{c}: не найдено «{find[:40]}»")
                        continue
                    _set_cell(cells[c], new_text)
            else:
                problems.append(f"неизвестная правка «{op}»")
                continue
            applied += 1
        except Exception as exc:                      # одна неудачная правка не останавливает остальные
            problems.append(f"правка {op} [{i}]: {exc}")
    for t, r, rows in sorted(row_adds, key=lambda x: -x[1]):     # снизу вверх — номера строк не сдвигаются
        try:
            applied += 1 if _add_rows(t, r, [[str(x) for x in row] for row in rows], values) else 0
        except Exception as exc:
            problems.append(f"добавление строк: {exc}")
    for el in delete:
        if el.getparent() is not None:
            el.getparent().remove(el)
    # Служебные строки шаблона («начало формы», «конец формы») — убрать, даже если модель их оставила.
    for el in elements(doc):
        if el.tag == qn("w:p") and SERVICE_RE.fullmatch(" ".join(Paragraph(el, doc).text.split()).lower()):
            el.getparent().remove(el)
    gaps = list(dict.fromkeys(g.strip() for g in df.GAPS if g.strip()))
    df.GAPS.clear()
    return applied, problems, gaps


# ---------- проверки готового файла ----------

def doc_texts(path: Path) -> list[str]:
    """Весь текст .docx: абзацы, таблицы, колонтитулы."""
    d = docx.Document(str(path))
    texts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            texts += [c.text for c in _cells(row)]
    for sec in d.sections:
        texts += [p.text for p in sec.header.paragraphs] + [p.text for p in sec.footer.paragraphs]
    return texts


def count_marks(path: Path) -> int:
    try:
        return "\n".join(doc_texts(path)).count(MARK)
    except Exception:
        return 0
