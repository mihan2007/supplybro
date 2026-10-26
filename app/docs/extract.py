"""
Извлечение текста из документов конкурса — без ИИ.

Что получается для каждого файла:
  - части текста с метками, по которым потом можно сослаться: «стр. 5» (PDF), «лист «ТЗ»» (Excel),
    «раздел …» (Word — по заголовкам);
  - номера пунктов Word (автонумерация «3.2.1.») восстанавливаются — без них не найти «п. 3.2»;
  - таблицы — построчно, ячейки через « | », объединённые ячейки не теряются;
  - скрытый текст (скрытый шрифт, белые буквы, крошечный кегль, скрытые листы/строки) — отдельно,
    в основной текст НЕ попадает: так в нём нельзя спрятать указания для ИИ;
  - страницы PDF без текстового слоя и картинки распознаются (Tesseract, русский + английский);
  - фразы, обращённые к ИИ («игнорируй предыдущие инструкции…»), отмечаются ⚠.
"""

from __future__ import annotations

import datetime as dt
import html.parser
import re
from dataclasses import dataclass, field
from pathlib import Path

EXTRACTOR_VERSION = "1"  # при изменении правил извлечения — увеличить, файлы обработаются заново

KINDS = {
    ".docx": "docx", ".docm": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".pdf": "pdf",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".tif": "image", ".tiff": "image", ".bmp": "image",
    ".txt": "text", ".csv": "text",
    ".htm": "html", ".html": "html",
}
SKIP_KINDS = {  # файлы, которые не являются документами для чтения
    ".sig": "подпись", ".p7s": "подпись", ".sgn": "подпись", ".p7m": "подпись", ".xml": "служебный файл",
    ".exe": "программа", ".msi": "программа", ".lnk": "ярлык", ".db": "служебный файл", ".ini": "служебный файл",
}

MIN_PAGE_TEXT = 25          # меньше символов на странице PDF — считаем сканом и распознаём
OCR_LANG = "rus+eng"
OCR_DPI = 300
MAX_SHEET_ROWS = 20000


@dataclass
class DocText:
    kind: str
    parts: list[tuple[str, str]] = field(default_factory=list)   # (метка, текст)
    hidden: list[str] = field(default_factory=list)              # скрытый текст
    pages: int = 0
    ocr_pages: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(t) for _, t in self.parts)

    @property
    def hidden_chars(self) -> int:
        return sum(len(t) for t in self.hidden)


# ======================================================================
# Word (.docx)
# ======================================================================

def _w(tag: str) -> str:
    from docx.oxml.ns import qn
    return qn(tag)


def _attr(el, tag: str, default=None, conv=str):
    if el is None:
        return default
    child = el.find(_w(tag))
    if child is None:
        return default
    v = child.get(_w("w:val"))
    if v is None:
        return default
    try:
        return conv(v)
    except (TypeError, ValueError):
        return default


_RU_LOWER = "абвгдежзиклмнопрстуфхцчшщэюя"


def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _letters(n: int, alphabet: str) -> str:
    # a, b, … z, aa, bb, … — как в Word
    k = len(alphabet)
    return alphabet[(n - 1) % k] * ((n - 1) // k + 1)


def _fmt_number(n: int, fmt: str) -> str:
    if fmt in ("bullet",):
        return "•"
    if fmt in ("none",):
        return ""
    if fmt == "lowerLetter":
        return _letters(n, "abcdefghijklmnopqrstuvwxyz")
    if fmt == "upperLetter":
        return _letters(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if fmt == "lowerRoman":
        return _roman(n).lower()
    if fmt == "upperRoman":
        return _roman(n)
    if fmt == "russianLower":
        return _letters(n, _RU_LOWER)
    if fmt == "russianUpper":
        return _letters(n, _RU_LOWER.upper())
    if fmt == "decimalZero":
        return f"{n:02d}"
    return str(n)  # decimal и всё прочее


class _Numbering:
    """Восстанавливает текст автонумерации Word («1.», «3.2.1.», «а)») по описанию списков в файле."""

    def __init__(self, doc):
        self.abstract: dict[str, dict[int, tuple[int, str, str, bool]]] = {}
        self.num_to_abs: dict[str, str] = {}
        self.overrides: dict[str, dict[int, int]] = {}
        self.counters: dict[str, list] = {}
        self.style_numpr: dict[str, tuple[str, int] | None] = {}
        try:
            root = doc.part.numbering_part.element
        except Exception:
            root = None
        if root is not None:
            for an in root.findall(_w("w:abstractNum")):
                levels = {}
                for lvl in an.findall(_w("w:lvl")):
                    il = int(lvl.get(_w("w:ilvl"), "0"))
                    levels[il] = (_attr(lvl, "w:start", 1, int), _attr(lvl, "w:numFmt", "decimal"),
                                  _attr(lvl, "w:lvlText", ""), lvl.find(_w("w:isLgl")) is not None)
                self.abstract[an.get(_w("w:abstractNumId"))] = levels
            for num in root.findall(_w("w:num")):
                nid = num.get(_w("w:numId"))
                self.num_to_abs[nid] = _attr(num, "w:abstractNumId", None)
                ov = {}
                for lo in num.findall(_w("w:lvlOverride")):
                    so = lo.find(_w("w:startOverride"))
                    if so is not None:
                        ov[int(lo.get(_w("w:ilvl"), "0"))] = int(so.get(_w("w:val"), "1"))
                if ov:
                    self.overrides[nid] = ov
        # Нумерация, заданная стилем (например, стилем заголовка), — с учётом наследования стилей.
        try:
            for st in doc.styles:
                self.style_numpr[st.style_id] = None
                s = st
                while s is not None:
                    ppr = s.element.find(_w("w:pPr"))
                    numpr = ppr.find(_w("w:numPr")) if ppr is not None else None
                    if numpr is not None:
                        nid = _attr(numpr, "w:numId", None)
                        self.style_numpr[st.style_id] = (nid, _attr(numpr, "w:ilvl", 0, int)) if nid else None
                        break
                    s = s.base_style
        except Exception:
            pass

    def label(self, p) -> str:
        """Текст номера абзаца (с пробелом в конце) или пустая строка."""
        ppr = p._p.pPr
        numpr = ppr.numPr if ppr is not None else None
        nid, ilvl = None, 0
        if numpr is not None:
            nid = _attr(numpr, "w:numId", None)
            ilvl = _attr(numpr, "w:ilvl", 0, int)
        if nid is None:
            try:
                from_style = self.style_numpr.get(p.style.style_id) if p.style is not None else None
            except Exception:
                from_style = None
            if from_style:
                nid, style_lvl = from_style
                ilvl = ilvl if numpr is not None and _attr(numpr, "w:ilvl", None, int) is not None else style_lvl
        if not nid or nid == "0":
            return ""
        abs_id = self.num_to_abs.get(nid)
        levels = self.abstract.get(abs_id)
        if not levels or ilvl not in levels:
            return ""
        key = nid if nid in self.overrides else f"a{abs_id}"
        cnt = self.counters.setdefault(key, [None] * 9)

        def start(level: int) -> int:
            return self.overrides.get(nid, {}).get(level, levels.get(level, (1,))[0])

        cnt[ilvl] = start(ilvl) if cnt[ilvl] is None else cnt[ilvl] + 1
        for deeper in range(ilvl + 1, 9):
            cnt[deeper] = None
        _start, fmt, text, is_lgl = levels[ilvl]
        if fmt == "bullet":
            return "• "
        if not text:
            return ""

        def sub(m):
            lv = int(m.group(1)) - 1
            n = cnt[lv] if cnt[lv] is not None else start(lv)
            lv_fmt = "decimal" if is_lgl else levels.get(lv, (1, "decimal"))[1]
            return _fmt_number(n, lv_fmt)

        out = re.sub(r"%(\d)", sub, text).strip()
        return out + " " if out else ""


def _run_hidden(r) -> bool:
    rpr = r._r.rPr
    if rpr is None:
        return False
    for tag in ("w:vanish", "w:specVanish"):
        el = rpr.find(_w(tag))
        if el is not None and el.get(_w("w:val"), "true").lower() not in ("0", "false", "off"):
            return True
    color = _attr(rpr, "w:color", "")
    if color and color.upper() == "FFFFFF":
        return True
    size = _attr(rpr, "w:sz", None, int)  # в половинках пункта
    return size is not None and size < 4


def _para_text(p, hidden: list[str]) -> str:
    visible = []
    try:
        items = list(p.iter_inner_content())
    except Exception:
        items = list(p.runs)
    for item in items:
        runs = item.runs if hasattr(item, "runs") and not hasattr(item, "font") else [item]
        for r in runs:
            t = r.text
            if not t:
                continue
            (hidden if _run_hidden(r) else visible).append(t)
    text = "".join(visible)
    if not text.strip() and not hidden:
        # Текст в полях форм и элементах управления содержимым — берём все w:t абзаца.
        text = "".join(t.text or "" for t in p._p.iter(_w("w:t")))
    return _FIELD_CODE_RE.sub(" ", text)


# Служебные коды полей Word, попадающие в текст: « REF _Ref170138224 \r \h ».
_FIELD_CODE_RE = re.compile(r"\s*\b(?:REF|PAGEREF|NOTEREF)\s+_Ref\d+(?:\s+\\[a-zA-Z*])*\s*")


def _iter_blocks(parent_el, doc):
    """Абзацы и таблицы в порядке документа (включая содержимое элементов управления w:sdt)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in parent_el.iterchildren():
        if child.tag == _w("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == _w("w:tbl"):
            yield Table(child, doc)
        elif child.tag == _w("w:sdt"):
            content = child.find(_w("w:sdtContent"))
            if content is not None:
                yield from _iter_blocks(content, doc)


def _cell_text(s: str) -> str:
    return " / ".join(x.strip() for x in s.replace("|", "/").splitlines() if x.strip())


def extract_docx(path: Path) -> DocText:
    from docx import Document
    from docx.table import Table
    doc = Document(str(path))
    numbering = _Numbering(doc)
    out = DocText(kind="docx")  # страниц в Word без вёрстки не посчитать
    section_label, lines = "начало документа", []

    def flush():
        text = "\n".join(lines).strip()
        if text:
            out.parts.append((section_label, text))
        lines.clear()

    def table_lines(table: Table) -> list[str]:
        # Ключ — сам элемент ячейки (не id(): объекты-обёртки библиотеки пересоздаются, id может повториться).
        result, seen = [], {}
        for row in table.rows:
            cells, prev = [], None
            for cell in row.cells:
                tc = cell._tc
                if tc is prev:
                    continue  # горизонтально объединённая ячейка — один раз
                prev = tc
                if tc not in seen:  # вертикально объединённая — текст считаем один раз
                    parts = [numbering.label(cp) + _para_text(cp, out.hidden) for cp in cell.paragraphs]
                    for nested in cell.tables:
                        parts.extend(table_lines(nested))
                    seen[tc] = _cell_text("\n".join(parts))
                cells.append(seen[tc])
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                result.append("| " + " | ".join(cells) + " |")
        return result

    for block in _iter_blocks(doc.element.body, doc):
        if isinstance(block, Table):
            rows = table_lines(block)
            if rows:
                lines.append("")
                lines.extend(rows)
                lines.append("")
            continue
        text = _para_text(block, out.hidden)
        label = numbering.label(block)
        if not text.strip():
            continue
        style = ""
        try:
            style = (block.style.name or "").lower() if block.style is not None else ""
        except Exception:
            pass
        if style.startswith(("heading", "заголовок", "title", "название")):
            flush()
            section_label = ("раздел " + (label + text).strip())[:80]
            lines.append("## " + (label + text).strip())
        else:
            lines.append((label + text).strip())
    flush()
    return out


# ======================================================================
# Excel (.xlsx)
# ======================================================================

def _cell_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%d.%m.%Y") if not isinstance(v, dt.datetime) or (v.hour, v.minute) == (0, 0) \
            else v.strftime("%d.%m.%Y %H:%M")
    return _cell_text(str(v))


def extract_xlsx(path: Path) -> DocText:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True)
    out = DocText(kind="xlsx", pages=len(wb.worksheets))
    for ws in wb.worksheets:
        merged = {}
        for rng in ws.merged_cells.ranges:
            top = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    merged[(r, c)] = top
        hidden_cols = {k for k, d in ws.column_dimensions.items() if d.hidden}
        sheet_hidden = ws.sheet_state != "visible"
        visible_rows, hidden_rows, count = [], [], 0
        for row in ws.iter_rows():
            if not row:
                continue
            r = row[0].row
            row_hidden = sheet_hidden or bool(ws.row_dimensions[r].hidden)
            vis, hid = [], []
            for c in row:
                if not hasattr(c, "column_letter"):
                    continue
                v = _cell_value(merged.get((r, c.column), c.value))
                white = False
                try:
                    rgb = c.font.color.rgb if c.font and c.font.color is not None else None
                    white = isinstance(rgb, str) and rgb.upper().endswith("FFFFFF")
                except Exception:
                    pass
                if row_hidden or c.column_letter in hidden_cols or (white and v):
                    hid.append(v)
                    vis.append("")
                else:
                    vis.append(v)
            while vis and not vis[-1]:
                vis.pop()
            if any(vis):
                visible_rows.append(f"[строка {r}] " + " | ".join(vis))
                count += 1
            if any(hid):
                hidden_rows.append(f"[лист «{ws.title}», строка {r}] " + " | ".join(x for x in hid if x))
            if count >= MAX_SHEET_ROWS:
                out.notes.append(f"лист «{ws.title}»: показаны первые {MAX_SHEET_ROWS} строк")
                break
        if visible_rows:
            out.parts.append((f"лист «{ws.title}»", f"## Лист «{ws.title}»\n" + "\n".join(visible_rows)))
        out.hidden.extend(hidden_rows)
        if sheet_hidden:
            out.notes.append(f"скрытый лист «{ws.title}»")
    return out


# ======================================================================
# PDF
# ======================================================================

def _is_white(color) -> bool:
    if color is None or isinstance(color, str):
        return False
    vals = list(color) if isinstance(color, (list, tuple)) else [color]
    try:
        vals = [float(v) for v in vals]
    except (TypeError, ValueError):
        return False
    if len(vals) in (1, 3):
        return all(v >= 0.98 for v in vals)
    if len(vals) == 4:  # CMYK: белый — все нули
        return all(v <= 0.02 for v in vals)
    return False


def _hidden_char(obj) -> bool:
    return obj.get("object_type") == "char" and (
        (obj.get("size") or 10) < 1.5 or _is_white(obj.get("non_stroking_color")))


def _ocr_image(img) -> str:
    import pytesseract
    return pytesseract.image_to_string(img, lang=OCR_LANG)


def extract_pdf(path: Path) -> DocText:
    import pdfplumber
    out = DocText(kind="pdf")
    with pdfplumber.open(str(path)) as pdf:
        out.pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            hidden = [c["text"] for c in page.chars if _hidden_char(c)]
            visible_page = page.filter(lambda o: not _hidden_char(o)) if hidden else page
            text = visible_page.extract_text() or ""
            if hidden:
                out.hidden.append(f"[стр. {i}] " + "".join(hidden))
            if len(re.sub(r"\s", "", text)) < MIN_PAGE_TEXT:
                try:
                    img = page.to_image(resolution=OCR_DPI).original
                    ocr = _ocr_image(img)
                    if len(re.sub(r"\s", "", ocr)) > len(re.sub(r"\s", "", text)):
                        text = ocr
                        out.ocr_pages += 1
                except Exception as exc:
                    out.notes.append(f"стр. {i}: не удалось распознать ({type(exc).__name__})")
            text = text.strip()
            if text:
                out.parts.append((f"стр. {i}", text))
    return out


# ======================================================================
# Картинки, текст, HTML
# ======================================================================

def extract_image(path: Path) -> DocText:
    from PIL import Image, ImageSequence
    out = DocText(kind="image")
    with Image.open(str(path)) as im:
        for i, frame in enumerate(ImageSequence.Iterator(im), 1):
            text = _ocr_image(frame.convert("RGB")).strip()
            out.pages += 1
            out.ocr_pages += 1
            if text:
                out.parts.append((f"стр. {i}", text))
    return out


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text_file(path: Path) -> DocText:
    out = DocText(kind="text")
    text = _read_text_file(path).strip()
    if text:
        out.parts.append(("текст", text))
    return out


class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "tr", "li", "div", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def extract_html(path: Path) -> DocText:
    parser = _HTMLText()
    parser.feed(_read_text_file(path))
    out = DocText(kind="html")
    text = re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()
    if text:
        out.parts.append(("текст", text))
    return out


EXTRACTORS = {"docx": extract_docx, "xlsx": extract_xlsx, "pdf": extract_pdf, "image": extract_image,
              "text": extract_text_file, "html": extract_html}


def extract(path: Path, kind: str) -> DocText:
    return EXTRACTORS[kind](path)


# ======================================================================
# Проверка на обращения к ИИ (возможная инъекция)
# ======================================================================

_INJECTION_PATTERNS = [
    r"(игнорир\w*|проигнорир\w*|забудь\w*|не\s+учитывай\w*)\s+(все\s+|всё\s+)?(предыдущ|прежн|выше|ранее|инструкц)",
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above)\s+(instructions|prompts?|rules)",
    r"(system|системн\w*)\s+(prompt|промпт\w*)",
    r"\b(ты|вы)\s*(—|-|являешься|являетесь)?\s*(искусственн\w+\s+интеллект\w*|языков\w+\s+модел\w+|чат-?бот\w*|ии-?ассистент\w*)",
    r"(инструкц\w+|указани\w+|сообщени\w+)\s+для\s+(ии|нейросет\w+|языков\w+\s+модел\w+|ии-?ассистент\w+|chatgpt|claude|gpt|deepseek)\b",
    r"\b(you\s+are\s+(an?\s+)?(ai|assistant|language\s+model)|as\s+an\s+ai)\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)


def injection_snippets(text: str, limit: int = 3) -> list[str]:
    found = []
    for m in _INJECTION_RE.finditer(text):
        a, b = max(0, m.start() - 40), min(len(text), m.end() + 40)
        found.append(" ".join(text[a:b].split()))
        if len(found) >= limit:
            break
    return found


# ======================================================================
# Текст для файла и фрагменты для поиска
# ======================================================================

def render(doc: DocText, display_name: str, flags: list[str]) -> str:
    head = [f"# {display_name}",
            f"(тип: {doc.kind}; страниц/листов: {doc.pages}; распознано страниц: {doc.ocr_pages}; "
            f"символов: {doc.chars})"]
    if flags:
        head.append("Пометки: " + "; ".join(flags))
    body = []
    for label, text in doc.parts:
        body.append(f"=== {label} ===\n{text}")
    tail = []
    if doc.hidden:
        tail.append("=== ⚠ Скрытый текст (в анализ и чат не передаётся) ===\n" + "\n".join(doc.hidden))
    return "\n".join(head) + "\n\n" + "\n\n".join(body + tail) + "\n"


def chunks(doc: DocText, size: int = 1500, overlap: int = 200) -> list[tuple[str, str]]:
    """Фрагменты ~size символов по границам строк, с меткой места в документе (для поиска и ссылок)."""
    result = []
    for label, text in doc.parts:
        lines, buf = text.splitlines(), ""
        for line in lines:
            if buf and len(buf) + len(line) + 1 > size:
                result.append((label, buf.strip()))
                buf = buf[-overlap:] + "\n" + line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf.strip():
            result.append((label, buf.strip()))
    return result
