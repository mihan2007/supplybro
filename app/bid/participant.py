"""Участник закупки (юр. лицо клиента) и его материалы: сведения, реквизиты, библиотека документов.

Где лежат материалы — `config/clients.json`, у юр. лица ключ `materials` (пути — внутри папки клиента в Dropbox\\Агент,
имена файлов — с * вместо даты, чтобы обновлённый файл находился сам):
    "materials": {"dir": "#Материалы агента/Документы ООО/ООО Простор — все документы",
                  "profile": "001 Выжимка*.md", "requisites": "401 Реквизиты*.docx", "inventory": "000 Опись*.xlsx",
                  "vat_rate": 22, "wrong_values": ["191036", "09804728", "1027800000140"]}
Сведения (выжимка) — главный источник для форм: в ней исправлены известные ошибки карточки реквизитов. Если выжимки
нет — сведения собираются из карточки реквизитов без паспортных данных. Паспортные данные модели не передаются —
их подставляет программа меткой {{ПАСПОРТ_РУКОВОДИТЕЛЯ}}.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Client
from ..tenders import client_root

PD_RE = re.compile(r"паспорт|снилс|дата рождения|место рождения", re.I)


@dataclass
class Participant:
    id: str
    name: str
    inn: str
    is_ip: bool = False
    profile: str = ""                          # сведения для модели (без паспортных данных)
    library: list[dict] = field(default_factory=list)   # [{no, file, path, section, date, actual, purpose, note}]
    library_dir: Path | None = None
    requisites: dict = field(default_factory=dict)
    vat_rate: float | None = None              # 22 — плательщик НДС 22 %; 0 — без НДС; None — неизвестно
    wrong_values: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def passport(self) -> str:
        for label, value in (self.requisites.get("raw") or {}).items():
            if "паспорт" in label.lower() and value.strip():
                return value.strip()
        return ""

    def library_text(self) -> str:
        if not self.library:
            return "(библиотеки документов нет)"
        lines = []
        for it in self.library:
            parts = [it["no"], it["file"]]
            for k in ("date", "actual", "purpose", "note"):
                if it.get(k):
                    parts.append(str(it[k]))
            lines.append(" — ".join(parts))
        return "\n".join(lines)


def _glob(base: Path, pattern: str) -> Path | None:
    if not pattern:
        return None
    hits = sorted(base.glob(pattern))
    return hits[-1] if hits else None


def _library(base: Path, inventory: Path | None) -> list[dict]:
    files = {p.name: p for p in base.iterdir() if p.is_file() and not p.name.startswith(("~$", "."))}
    by_no = {}
    for name, p in files.items():
        m = re.match(r"^(\d{3})\s", name)
        if m:
            by_no.setdefault(m.group(1), p)
    items: dict[str, dict] = {}
    if inventory and inventory.is_file():
        try:
            import openpyxl
            wb = openpyxl.load_workbook(inventory, read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = list(ws.iter_rows(values_only=True))
            head = [str(x or "").strip().lower() for x in rows[0]] if rows else []

            def col(*names):
                for n in names:
                    for i, h in enumerate(head):
                        if h.startswith(n):
                            return i
                return None
            c_file, c_date, c_act, c_for, c_note = (col("файл"), col("дата"), col("актуальн"), col("для тендер"),
                                                    col("примечан"))
            for r in rows[1:]:
                if c_file is None or not r[c_file]:
                    continue
                fname = str(r[c_file]).strip()
                m = re.match(r"^(\d{3})\s", fname)
                no = m.group(1) if m else ""
                path = files.get(fname) or (by_no.get(no) if no else None)
                if not path:
                    continue                            # файла в папке нет (удалён) — в библиотеку не берём
                cell = lambda c: str(r[c]).strip() if c is not None and r[c] not in (None, "") else ""
                items[path.name] = {"no": no, "file": path.name, "path": str(path), "date": cell(c_date),
                                    "actual": cell(c_act), "purpose": cell(c_for), "note": cell(c_note)}
        except Exception:
            items = {}
    for name, p in sorted(files.items()):                # файлы, которых нет в описи
        if name not in items and re.match(r"^\d{3}\s", name) and not name.startswith(("000 ", "001 ")):
            items[name] = {"no": name[:3], "file": name, "path": str(p)}
    return sorted(items.values(), key=lambda x: (x["no"] or "999", x["file"]))


def _profile_from_requisites(req: dict) -> str:
    lines = ["Сведения из карточки реквизитов (выжимки нет):"]
    for label, value in (req.get("raw") or {}).items():
        if value and not PD_RE.search(label):
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def load(client: Client, entity_id: str | None) -> Participant:
    """Участник: названный (entity_id) или первое юр. лицо клиента (участник по умолчанию)."""
    from tools import declarative_forms as df
    entity = client.entity(entity_id) if entity_id else None
    entity = entity or (client.legal_entities[0] if client.legal_entities else None)
    if not entity:
        raise RuntimeError("у клиента не заведено ни одного юр. лица — участника закупки")
    p = Participant(id=entity.id, name=entity.name, inn=entity.inn)
    mat = dict(getattr(entity, "materials", None) or {})
    if not mat:
        p.problems.append(f"для {entity.name} не указаны материалы (реквизиты, документы) — формы заполнятся "
                          "пометками «<нет данных>»; добавить — в config/clients.json")
        p.profile = f"Участник: {entity.name}, ИНН {entity.inn or '<нет данных>'}. Других сведений нет."
        return p
    try:
        base = client_root(client) / mat.get("dir", "")
    except Exception as exc:
        raise RuntimeError(f"папка клиента недоступна: {exc}") from exc
    if not base.is_dir():
        p.problems.append(f"папки материалов «{mat.get('dir')}» нет")
        p.profile = f"Участник: {entity.name}, ИНН {entity.inn}. Других сведений нет."
        return p
    req_path = _glob(base, mat.get("requisites", ""))
    if req_path:
        try:
            p.requisites = df.load_requisites(str(req_path))
            p.is_ip = bool(p.requisites.get("is_ip"))
        except Exception as exc:
            p.problems.append(f"карточка реквизитов не прочиталась: {exc}")
    else:
        p.problems.append("карточки реквизитов нет")
    prof_path = _glob(base, mat.get("profile", ""))
    if prof_path:
        p.profile = prof_path.read_text(encoding="utf-8", errors="replace")
    elif p.requisites:
        p.profile = _profile_from_requisites(p.requisites)
    else:
        p.profile = f"Участник: {entity.name}, ИНН {entity.inn}. Других сведений нет."
    p.profile = "\n".join(ln for ln in p.profile.splitlines() if not PD_RE.search(ln) or "намеренно" in ln)
    p.library_dir = base
    p.library = _library(base, _glob(base, mat.get("inventory", "")))
    vr = mat.get("vat_rate")
    p.vat_rate = float(vr) if isinstance(vr, (int, float)) else None
    p.wrong_values = [str(x) for x in mat.get("wrong_values") or []]
    if p.requisites and p.inn and p.requisites.get("inn") and p.requisites["inn"] != p.inn:
        p.problems.append(f"ИНН в карточке реквизитов ({p.requisites['inn']}) не совпадает с ИНН юр. лица ({p.inn})")
    return p
