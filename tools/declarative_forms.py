"""Шаг 7.1c — заполнение декларативных форм участника (Word) реквизитами компании.

Декларативные формы — формы из документации тендера, в которых участник
сообщает сведения о себе и декларирует соответствие требованиям: заявка /
письмо о подаче заявки, анкета участника, декларация о соответствии,
цепочка собственников, согласие на обработку ПДн, справка о деловой
репутации, письмо о (не)крупности сделки и т.п. Технико-коммерческие формы
(техническое предложение, КП, таблицы цен) и формы, которые заполняет третье
лицо (свидетельство изготовителя), сюда НЕ относятся.

Источник реквизитов — файл участника из COMPANY.md (по умолчанию —
tools/requisites_path.txt). Чего в реквизитах и документации нет (цена,
паспортные данные и т.п.) — в документ вписывается РОВНО единая пометка
«<нет данных>» (MARK, жёлтая подсветка), значения не выдумываются. Что именно
нужно — подсказка из ph("…") — собирается в список GAPS и записывается в
«AIExtract/Заявка/Нет данных.txt» функцией write_gaps() (версия 0.1.36).

Модуль — набор помощников; сами формы у каждого тендера разные, поэтому
заполнение конкретного тендера пишется коротким скриптом поверх этих функций
(см. CLAUDE.md, Шаг 7.1c):

    from declarative_forms import *
    R = load_requisites()                       # dict реквизитов
    doc = extract_range(src_docx, first_idx, last_idx)   # вырезать одну форму
    fill_cell(cell, "ИНН " + ph("ИНН заказчика"))   # в документе: «ИНН <нет данных>»
    write_gaps(".../AIExtract/Заявка/Нет данных.txt", "Анкета участника.docx")
    set_par(paragraph, "текст")
    build_declaration(path, title, intro, items, R, tender)  # своя декларация

CLI: `python3 declarative_forms.py --dump <file.docx>` — вывести элементы
документа с индексами (чтобы найти границы форм), `--requisites` — вывести
распознанные реквизиты JSON, `--find-marks <папка>` — найти во всех .docx/.xlsx
папки пометки «<нет данных>» и старые варианты пометок (самопроверка пакета).
"""
import argparse
import copy
import json
import os
import re
import sys

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.shared import Pt, Cm
from docx.table import Table
from docx.text.paragraph import Paragraph

HERE = os.path.dirname(os.path.abspath(__file__))
REQUISITES_PATH_FILE = os.path.join(HERE, "requisites_path.txt")
PLACEHOLDER_RE = re.compile(r"\[\[(.+?)\]\]")
# Единая пометка «нет данных» — пишется в документы ровно так, без вариантов (поиск по ней)
MARK = "<нет данных>"
# Старые / посторонние варианты пометок — в пакете их быть не должно (заменять на MARK)
# (линии «______» не проверяются: это обычные места для подписи и даты в формах)
OLD_MARKS_RE = re.compile(r"\[уточнить|\[\[|‹добавить›|XXX|<нет данных:|\[нет данных|\(нет данных\)")
GAPS = []  # подсказки «что нужно» для полей с MARK (заполняет set_par)

# Канонические ключи реквизитов -> варианты подписи в файле реквизитов
_REQ_KEYS = {
    "full_name": ["полное наименование"],
    "short_name": ["сокращенное наименование", "сокращённое наименование"],
    "inn": ["инн"],
    "kpp": ["кпп"],
    "account": ["расчётный счёт", "расчетный счет", "расчётный счет"],
    "bank": ["банк"],
    "corr_account": ["корреспондентский"],
    "bik": ["бик"],
    "legal_address": ["юридический адрес"],
    "postal_address": ["корреспонденцию отправлять", "почтовый адрес"],
    "actual_address": ["фактический адрес"],
    "ogrn": ["огрн"],
    "okpo": ["окпо"],
    "okved": ["оквэд"],
    "okato": ["окато"],
    "oktmo": ["октмо"],
    "okfs": ["окфс"],
    "okopf": ["окопф"],
    "okogu": ["окогу"],
    "phone": ["телефон"],
    "fax": ["факс"],
    "director": ["генеральный директор"],
    "director_gen": ["в род. падеже", "родительном"],
    "accountant": ["главный бухгалтер"],
    "email": ["e-mail", "email", "эл. почта", "электронная почта"],
    "edo_id": ["идентификатор эдо"],
    "edo_system": ["система"],
    "edo_contact": ["контактное лицо по эдо"],
}


def requisites_path():
    if os.path.exists(REQUISITES_PATH_FILE):
        p = open(REQUISITES_PATH_FILE, encoding="utf-8").read().strip()
        if p:
            return p
    raise FileNotFoundError("tools/requisites_path.txt не найден или пуст")


def load_requisites(path=None):
    """Читает таблицу «поле | значение» из файла реквизитов -> dict с ключами _REQ_KEYS
    (+ 'raw': {подпись: значение}). Пустые значения -> ''."""
    d = docx.Document(path or requisites_path())
    raw = {}
    for t in d.tables:
        for r in t.rows:
            cells = [c.text.strip() for c in r.cells]
            if len(cells) >= 2 and cells[0]:
                raw[cells[0].rstrip(":").strip()] = cells[-1].strip()
    out = {"raw": raw}
    for key, variants in _REQ_KEYS.items():
        val = ""
        for label, v in raw.items():
            low = label.lower()
            if key == "director" and "род" in low:
                continue
            if key == "bank" and "банк" != low.strip():
                continue
            if key in ("inn", "kpp", "ogrn") and low.split()[0] not in variants:
                continue
            if any(x in low for x in variants):
                val = v
                break
        out[key] = val
    # нормализация
    out["corr_account"] = out["corr_account"].replace(" ", "")
    out["is_ip"] = out["full_name"].lower().startswith("индивидуальный предприниматель")
    fio = out["director"] or re.sub(r"(?i)^(индивидуальный предприниматель|ип)\s+", "", out["short_name"])
    out["fio"] = fio
    parts = fio.split()
    out["fio_short"] = f"{parts[0]} {parts[1][0]}.{parts[2][0]}." if len(parts) >= 3 else fio
    out["position"] = "Индивидуальный предприниматель" if out["is_ip"] else "Генеральный директор"
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", out.get("edo_contact", ""))
    out["edo_email"] = m.group(0) if m else ""
    return out


def ph(text):
    """Поле без данных: в документе будет ровно MARK («<нет данных>», жёлтая подсветка);
    text — что нужно для поля, попадает в GAPS (а оттуда в «Нет данных.txt»), не в документ."""
    return f"[[{text}]]"


def write_gaps(txt_path, file_label):
    """Дописывает накопленные подсказки GAPS в «Нет данных.txt» (строки «файл | что нужно») и очищает GAPS.
    Возвращает число записанных строк."""
    items = list(dict.fromkeys(g.strip() for g in GAPS if g.strip()))
    GAPS.clear()
    if not items:
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(txt_path)), exist_ok=True)
    with open(txt_path, "a", encoding="utf-8") as f:
        for it in items:
            f.write(f"{file_label} | {it}\n")
    return len(items)


def find_marks(folder):
    """Самопроверка: по всем .docx/.xlsx папки (с подпапками) — число пометок MARK и найденные старые варианты.
    Возвращает список dict(file, marks, old)."""
    out = []
    for root, _, files in os.walk(folder):
        for fn in sorted(files):
            path = os.path.join(root, fn); low = fn.lower()
            if fn.startswith("~$") or not low.endswith((".docx", ".xlsx", ".xlsm")):
                continue
            texts = []
            try:
                if low.endswith(".docx"):
                    d = docx.Document(path)
                    texts += [p.text for p in d.paragraphs]
                    for t in d.tables:
                        for r in t.rows:
                            texts += [c.text for c in r.cells]
                    for sec in d.sections:
                        texts += [p.text for p in sec.header.paragraphs] + [p.text for p in sec.footer.paragraphs]
                else:
                    import openpyxl
                    wb = openpyxl.load_workbook(path, data_only=False)
                    for ws in wb.worksheets:
                        for row in ws.iter_rows(values_only=True):
                            texts += [str(v) for v in row if isinstance(v, str)]
            except Exception as e:  # повреждённый файл — тоже показать
                out.append({"file": os.path.relpath(path, folder), "marks": None, "old": [f"не открылся: {e}"]})
                continue
            joined = "\n".join(texts)
            old = sorted(set(m.group(0) for m in OLD_MARKS_RE.finditer(joined)))
            out.append({"file": os.path.relpath(path, folder), "marks": joined.count(MARK), "old": old})
    return out


def val(v, hint):
    """Значение или жёлтая пометка, если пусто."""
    return v if (v and v.strip() and v.strip() != "-") else ph(hint)


# ---------- низкоуровневые помощники Word ----------

def _first_rpr(p_el):
    r = p_el.find(qn("w:r"))
    if r is not None and r.find(qn("w:rPr")) is not None:
        return copy.deepcopy(r.find(qn("w:rPr")))
    ppr = p_el.find(qn("w:pPr"))
    if ppr is not None and ppr.find(qn("w:rPr")) is not None:
        return copy.deepcopy(ppr.find(qn("w:rPr")))
    return None


def _clear_runs(p_el):
    for child in list(p_el):
        if child.tag in (qn("w:r"), qn("w:hyperlink"), qn("w:ins"), qn("w:del"),
                         qn("w:smartTag"), qn("w:fldSimple"), qn("w:proofErr")):
            p_el.remove(child)


def set_par(paragraph, text, bold=None, keep_format=True):
    """Заменяет текст абзаца (форматирование берётся с первого run). [[…]] -> жёлтая подсветка,
    '\n' -> перенос строки."""
    rpr = _first_rpr(paragraph._p) if keep_format else None
    _clear_runs(paragraph._p)
    for i, chunk in enumerate(PLACEHOLDER_RE.split(text)):
        if not chunk:
            continue
        is_ph = i % 2 == 1
        lines = chunk.split("\n")
        for j, line in enumerate(lines):
            if is_ph:
                GAPS.append(line)
            run = paragraph.add_run(line if not is_ph else MARK)
            if rpr is not None:
                run._r.insert(0, copy.deepcopy(rpr))
            if bold is not None:
                run.bold = bold
            if is_ph:
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW
            if j < len(lines) - 1:
                run.add_break()
    return paragraph


def fill_cell(cell, text, bold=None, append=False, align=WD_ALIGN_PARAGRAPH.LEFT):
    """Пишет текст в ячейку (в первый абзац, остальные пустые абзацы удаляются).
    append=True — дописать после существующего текста ячейки."""
    ps = cell.paragraphs
    if append:
        base = cell.text.strip()
        text = f"{base} {text}" if base else text
    for extra in ps[1:]:
        extra._p.getparent().remove(extra._p)
    set_par(ps[0], text, bold=bold)
    if align is not None:
        ps[0].alignment = align
    return cell


def replace_in_par(paragraph, pattern, repl, count=1):
    """Regex-замена по полному тексту абзаца (абзац пересобирается с форматированием первого run)."""
    new = re.sub(pattern, repl, paragraph.text, count=count)
    if new != paragraph.text:
        set_par(paragraph, new)
        return True
    return False


def body_elements(doc):
    return list(doc.element.body.iterchildren())


def para_at(doc, idx):
    return Paragraph(body_elements(doc)[idx], doc)


def table_at(doc, idx):
    return Table(body_elements(doc)[idx], doc)


def extract_range(src, first, last):
    """Открывает src и оставляет только элементы тела с индексами first..last
    (по нумерации --dump) + итоговые свойства раздела. Возвращает Document."""
    d = docx.Document(src)
    body = d.element.body
    els = list(body.iterchildren())
    for i, el in enumerate(els):
        if el.tag == qn("w:sectPr"):
            continue
        if i < first or i > last:
            body.remove(el)
    # свойства раздела (ориентация/поля) берём у последнего разрыва раздела внутри куска
    inner = [ppr.find(qn("w:sectPr")) for ppr in body.iter(qn("w:pPr")) if ppr.find(qn("w:sectPr")) is not None]
    final = body.find(qn("w:sectPr"))
    if inner and final is not None:
        new = copy.deepcopy(inner[-1])
        for ch in list(new):
            if ch.tag == qn("w:type"):
                new.remove(ch)
        body.replace(final, new)
    # разрывы разделов внутри оставленного куска убираем, чтобы не было пустых страниц
    for ppr in body.iter(qn("w:pPr")):
        s = ppr.find(qn("w:sectPr"))
        if s is not None:
            ppr.remove(s)
    # первый абзац не должен начинаться с разрыва страницы
    first_p = body.find(qn("w:p"))
    if first_p is not None:
        for br in first_p.iter(qn("w:br")):
            if br.get(qn("w:type")) == "page":
                br.getparent().remove(br)
        ppr = first_p.find(qn("w:pPr"))
        if ppr is not None and ppr.find(qn("w:pageBreakBefore")) is not None:
            ppr.remove(ppr.find(qn("w:pageBreakBefore")))
    return d


def drop_elements(doc, indices):
    """Удаляет элементы тела по индексам (индексы — по текущему состоянию документа)."""
    els = body_elements(doc)
    for i in sorted(indices, reverse=True):
        els[i].getparent().remove(els[i])


def add_par_after(paragraph_or_table, text="", bold=False, align=None, size=None):
    """Вставляет новый абзац после указанного абзаца/таблицы."""
    el = paragraph_or_table._p if hasattr(paragraph_or_table, "_p") else paragraph_or_table._tbl
    new_p = copy.deepcopy(el) if hasattr(paragraph_or_table, "_p") else None
    from docx.oxml import OxmlElement
    if new_p is None:
        new_p = OxmlElement("w:p")
    else:
        _clear_runs(new_p)
    el.addnext(new_p)
    p = Paragraph(new_p, paragraph_or_table._parent)
    if text:
        set_par(p, text, bold=bold, keep_format=False)
        for r in p.runs:
            if size:
                r.font.size = Pt(size)
    if align is not None:
        p.alignment = align
    return p


def signature_block(R, date_text=None):
    """Строки подписи участника."""
    d = date_text or "«___» ____________ 2026 г."
    return [f"{R['position']} _______________ / {R['fio_short']} /",
            "(подпись)" + ("" if R["is_ip"] else "                                        М.П."),
            d]


# ---------- генератор собственной формы (когда шаблона в документации нет) ----------

def _style_doc(d):
    st = d.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    for s in d.sections:
        s.left_margin, s.right_margin, s.top_margin, s.bottom_margin = Cm(2.5), Cm(1.5), Cm(2), Cm(2)


def _p(d, text="", bold=False, align=None, size=None, space_after=6, indent_first=False):
    p = d.add_paragraph()
    if text:
        set_par(p, text, bold=bold, keep_format=False)
    if size:
        for r in p.runs:
            r.font.size = Pt(size)
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    if indent_first:
        p.paragraph_format.first_line_indent = Cm(1.25)
    return p


def participant_line(R):
    return (f"{R['full_name']}, ИНН {R['inn']}, {'ОГРНИП' if R['is_ip'] else 'ОГРН'} {R['ogrn']}, "
            f"адрес{' места жительства (регистрации)' if R['is_ip'] else ' юридического лица'}: {R['legal_address']}")


def build_letter(path, title, body_paragraphs, R, addressee=None, header_note=None, numbered=None,
                 subtitle=None, labels=None, tail_paragraphs=None):
    """Письмо/декларация в свободной форме на «бланке» участника.
    numbered — список пунктов (нумеруются 1), 2), …)."""
    d = docx.Document()
    _style_doc(d)
    _p(d, f"{R['full_name']}", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
    _p(d, f"ИНН {R['inn']}, {'ОГРНИП' if R['is_ip'] else 'ОГРН'} {R['ogrn']}", align=WD_ALIGN_PARAGRAPH.CENTER,
       size=10, space_after=0)
    _p(d, f"{R['legal_address']}; тел. {R['phone']}", align=WD_ALIGN_PARAGRAPH.CENTER, size=10, space_after=12)
    _p(d, "Исх. № _____ от «___» ____________ 2026 г.", space_after=12)
    if addressee:
        _p(d, addressee, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=12)
    if header_note:
        _p(d, header_note, size=10, align=WD_ALIGN_PARAGRAPH.RIGHT)
    _p(d, title, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=4 if subtitle else 12)
    if subtitle:
        _p(d, subtitle, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=12)
    for t in body_paragraphs:
        _p(d, t, align=WD_ALIGN_PARAGRAPH.JUSTIFY, indent_first=True)
    for i, t in enumerate(numbered or [], 1):
        lab = labels[i - 1] if labels else f"{i})"
        p = _p(d, f"{lab} {t}", align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=4)
        p.paragraph_format.left_indent = Cm(0.5)
    for t in tail_paragraphs or []:
        _p(d, t, align=WD_ALIGN_PARAGRAPH.JUSTIFY, indent_first=True)
    _p(d, "", space_after=12)
    for line in signature_block(R):
        _p(d, line, space_after=2)
    d.save(path)
    return path


def dump(path):
    d = docx.Document(path)
    for i, el in enumerate(d.element.body.iterchildren()):
        tag = el.tag.split("}")[1]
        if tag == "p":
            s = Paragraph(el, d).text.strip()
            if s:
                print(f"[{i}] {s[:200]}")
        elif tag == "tbl":
            t = Table(el, d)
            print(f"[{i}] TABLE {len(t.rows)}x{len(t.columns)}")
            for ri, r in enumerate(t.rows):
                seen, cells = None, []
                for c in r.cells:
                    if c._tc is seen:
                        continue
                    seen = c._tc
                    cells.append(c.text.strip().replace("\n", " / ")[:80])
                print(f"   r{ri}: " + " | ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", help="вывести элементы .docx с индексами")
    ap.add_argument("--requisites", action="store_true", help="вывести распознанные реквизиты (JSON)")
    ap.add_argument("--requisites-file", help="путь к файлу реквизитов (по умолчанию из tools/requisites_path.txt)")
    ap.add_argument("--find-marks", help="папка: найти пометки «<нет данных>» и старые варианты пометок в .docx/.xlsx")
    a = ap.parse_args()
    if a.find_marks:
        res = find_marks(a.find_marks)
        for r in res:
            flag = " | СТАРЫЕ ПОМЕТКИ: " + ", ".join(r["old"]) if r["old"] else ""
            print(f"{r['file']} | {MARK}: {r['marks']}{flag}")
        print(f"ИТОГО {MARK}: {sum(r['marks'] or 0 for r in res)}; файлов со старыми пометками: {sum(1 for r in res if r['old'])}")
    if a.dump:
        dump(a.dump)
    if a.requisites:
        R = load_requisites(a.requisites_file)
        R.pop("raw", None)
        print(json.dumps(R, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
