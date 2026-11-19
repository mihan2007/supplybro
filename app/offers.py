"""
Предложения поставщиков — сравнение цен перед записью в NocoDB (0.29.0).

Пользователь 18.11.2026: «промежуточный интерфейс с комментариями; если будет ответ от нескольких — табличка отдельная,
перед тем как в базу данных заносить; данные эти нужно тоже где-то хранить, чтобы они не стирались». Решения:
папка документов поставщиков — как есть («Прайсы, счета, КП»); записывать в NocoDB — закупщик и администратор
(право «offers»); до 10 цен на позицию (места «Отдел закупок: поставщик 1…10»), лучшая — подсвечена.

Данные: найденные цены — из разобранных прайсов, КП, счетов и писем (`AIExtract/pricelists.json`, app/pricelists.py,
не меняются); поверх — `AIExtract/offers.json` в папке конкурса (Dropbox — не стирается при повторном анализе и новых
письмах): выбор к записи, исправленная цена (исходная остаётся), комментарии (к цене, к поставщику, к позиции) с
автором и временем, журнал действий. Перед каждым изменением прежний файл — в `AIExtract/history/offers_<время>.json`
(последние HISTORY_KEEP).

Таблица (`build`): строки — позиции конкурса, колонки — поставщики (документы одного поставщика — одна колонка; в
ячейке — его предложение: отмеченное к записи, иначе последнее по времени, остальные — «ещё»). Лучшая цена позиции —
самая низкая без замечаний (⚠ — неполное совпадение, нет в документе дословно, цена вне 0,15–10× НМЦ); если у всех
есть замечания — самая низкая из них, с пометкой.

Запись (`write`): отмеченные цены → по документам `pricelists.request_write` (с исправленными ценами — `overrides`)
→ задача «noco_purch»: в строку спецификации на место этого поставщика или первое свободное из 10, комментарий
в «Отдел закупок: комментарий». Только эта кнопка пишет цены поставщиков в NocoDB (из карточки в чате — больше нет).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import db, pricelists

STORE = Path("AIExtract") / "offers.json"
HISTORY_KEEP = 30
MAX_SLOTS = 10
MAX_COMMENT = 2000


class OfferError(ValueError):
    pass


# ---------- хранение ----------

@contextmanager
def _locked(folder: Path):
    lock_dir = db.DATA_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(str(folder).encode("utf-8")).hexdigest()[:16]
    with open(lock_dir / f"offers-{name}.lock", "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load(folder: Path) -> dict:
    try:
        data = json.loads((folder / STORE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("cells", {})
            data.setdefault("comments", [])
            data.setdefault("log", [])
            return data
    except (OSError, ValueError):
        pass
    return {"cells": {}, "comments": [], "log": []}


def _save(folder: Path, data: dict) -> None:
    path = folder / STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():                              # прежняя версия — в историю (данные не теряются)
        hist = path.parent / "history"
        hist.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"   # несколько правок в секунду
        (hist / f"offers_{stamp}.json").write_bytes(path.read_bytes())
        old = sorted(hist.glob("offers_*.json"))
        for p in old[:-HISTORY_KEEP]:
            p.unlink(missing_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update(folder: Path, fn, by: str = "", action: str = ""):
    with _locked(folder):
        data = load(folder)
        out = fn(data)
        if action:
            data["log"] = (data["log"] + [{"at": time.time(), "by": by, "action": action}])[-500:]
        _save(folder, data)
        return out


def cell_key(file_id: str, pos: str) -> str:
    return f"{file_id}|{pos}"


# ---------- таблица ----------

def _supplier_of(e: dict) -> str:
    mail = e.get("mail") or {}
    return pricelists.supplier_name(e)


def _pos_sort(n: str):
    try:
        return (0, float(str(n).replace(",", ".")), "")
    except ValueError:
        return (1, 0.0, str(n))


def build(folder: Path, positions: list[dict]) -> dict:
    """Таблица сравнения: {positions, suppliers, cells, best, totals, comments, pending}."""
    files = [e for e in pricelists.load(folder)["files"]]
    state = load(folder)
    over = state["cells"]
    # Строки — позиции конкурса (анализ или спецификация NocoDB); позиции из документов, которых нет в списке, — тоже.
    rows: dict[str, dict] = {}
    for p in positions:
        n = str(p.get("№") or "").strip().rstrip(".")
        if n:
            rows[n] = {"n": n, "name": " ".join(str(p.get("Наименование позиции") or "").split())[:300],
                       "brand": str(p.get("Марка/модель") or "").strip()[:200], "qty": p.get("Кол-во"),
                       "unit": p.get("Ед. изм."), "nmc": pricelists.parse_price(p.get("Стоимость НМЦ"))}
    suppliers: dict[str, dict] = {}
    cells: dict[str, dict[str, list[dict]]] = {}
    pending = []
    for e in files:
        if e.get("status") in ("queued", "running"):
            pending.append({"id": e["id"], "name": e.get("name"), "status": e["status"]})
            continue
        if e.get("status") != "done":
            continue
        name = _supplier_of(e)
        key = pricelists._supplier_key(name) or e["id"]
        s = suppliers.setdefault(key, {"key": key, "name": name, "files": [], "vat": e.get("vat") or "", "inn": ""})
        s["inn"] = s["inn"] or e.get("supplier_inn") or ""
        s["files"].append({"id": e["id"], "name": e.get("name"), "file": e.get("file"), "at": e.get("uploaded_at"),
                           "vat": e.get("vat"), "doc_date": e.get("doc_date"), "mail": e.get("mail"),
                           "by": e.get("by")})
        w = e.get("write") or {}
        written = {x["pos"] for ww in [*(e.get("write_history") or []), w] if ww.get("status") == "saved"
                   for x in ww.get("written") or []}
        for m in e.get("matches") or []:
            n = m["pos"]
            rows.setdefault(n, {"n": n, "name": m.get("name") or "", "brand": "", "qty": m.get("qty"),
                                "unit": m.get("unit"), "nmc": m.get("nmc")})
            o = over.get(cell_key(e["id"], n)) or {}
            price = o.get("price_rub") if o.get("price_rub") is not None else m.get("price_rub")
            warn = list(m.get("problems") or [])
            if m.get("quote") in ("loose", "none"):
                warn.append("фрагмент не найден в документе дословно")
            if m.get("match") != "exact":
                warn.append("неполное совпадение позиции")
            if m.get("price") and not m.get("price_in_line"):
                warn.append("цены нет в строке документа — пересчитана моделью")
            cells.setdefault(n, {}).setdefault(key, []).append({
                "file_id": e["id"], "file": e.get("name"), "at": e.get("uploaded_at") or 0,
                "price_rub": price, "orig_price_rub": m.get("price_rub"), "edited": o.get("price_rub") is not None,
                "edited_by": o.get("edited_by"), "price": m.get("price"), "currency": m.get("currency"),
                "match": m.get("match"), "note": m.get("note"), "text": m.get("text") or m.get("line"),
                "line_no": m.get("line_no"), "warn": warn,
                "selected": o["selected"] if "selected" in o else bool(m.get("selected")),
                "written": n in written, "write_status": w.get("status") if n in (w.get("positions") or []) else "",
            })
    best: dict[str, dict] = {}
    totals = {k: {"positions": 0, "sum": 0.0, "best": 0} for k in suppliers}
    for n, by_sup in cells.items():
        for key, offers in by_sup.items():
            offers.sort(key=lambda o: (not o["selected"], -(o["at"] or 0)))
        cand = [(o["price_rub"], key, bool(o["warn"])) for key, offers in by_sup.items() for o in offers[:1]
                if o["price_rub"]]
        clean_ = [c for c in cand if not c[2]]
        pick = min(clean_ or cand, default=None)
        if pick:
            best[n] = {"supplier": pick[1], "price_rub": pick[0], "with_warn": pick[2]}
        qty = pricelists.parse_price((rows.get(n) or {}).get("qty")) or 1
        for key, offers in by_sup.items():
            o = offers[0]
            if o["price_rub"]:
                t = totals[key]
                t["positions"] += 1
                t["sum"] += o["price_rub"] * qty
                if pick and pick[1] == key:
                    t["best"] += 1
    for t in totals.values():
        t["sum"] = round(t["sum"], 2)
    selected = sum(1 for by_sup in cells.values() for offers in by_sup.values() for o in offers if o["selected"]
                   and not o["written"])
    return {
        "positions": sorted(rows.values(), key=lambda r: _pos_sort(r["n"])),
        "suppliers": sorted(suppliers.values(), key=lambda s: (-totals[s["key"]]["positions"], s["name"].lower())),
        "cells": cells, "best": best, "totals": totals, "comments": state["comments"], "pending": pending,
        "selected": selected, "log": state["log"][-30:], "max_slots": MAX_SLOTS,
    }


# ---------- действия ----------

def _match(folder: Path, file_id: str, pos: str) -> dict:
    e = next((f for f in pricelists.load(folder)["files"] if f.get("id") == file_id), None)
    m = next((x for x in (e or {}).get("matches") or [] if x["pos"] == pos), None)
    if not e or not m:
        raise OfferError("предложение не найдено (документ убрали или разобрали заново)")
    return m


def select(folder: Path, cells: list[dict], by: str) -> int:
    """Отметить / снять: [{file, pos, selected}]."""
    for c in cells:
        _match(folder, str(c.get("file")), str(c.get("pos")))

    def fn(data):
        for c in cells:
            k = cell_key(str(c["file"]), str(c["pos"]))
            data["cells"].setdefault(k, {})["selected"] = bool(c.get("selected"))
        return len(cells)
    return update(folder, fn, by, f"выбор к записи: {len(cells)}")


def edit_price(folder: Path, file_id: str, pos: str, price_rub, by: str) -> None:
    """Исправить цену (руб. за ед.); None — вернуть найденную в документе."""
    m = _match(folder, file_id, pos)
    if price_rub not in (None, ""):
        price_rub = pricelists.parse_price(price_rub)
        if not price_rub or price_rub <= 0:
            raise OfferError("цена — положительное число, руб. за единицу")

    def fn(data):
        c = data["cells"].setdefault(cell_key(file_id, pos), {})
        if price_rub in (None, ""):
            for k in ("price_rub", "edited_by", "edited_at"):
                c.pop(k, None)
        else:
            c.update(price_rub=round(float(price_rub), 2), edited_by=by, edited_at=time.time(),
                     orig_price_rub=m.get("price_rub"))
    update(folder, fn, by, f"цена № {pos}: {m.get('price_rub')} → {price_rub or 'как в документе'}")


def comment(folder: Path, target: str, text: str, by: str) -> dict:
    """Комментарий: target — cell:<файл>|<№>, supplier:<ключ>, pos:<№>."""
    text = str(text or "").strip()
    if not text:
        raise OfferError("пустой комментарий")
    if not str(target).startswith(("cell:", "supplier:", "pos:")):
        raise OfferError("неизвестно, к чему комментарий")
    c = {"id": uuid.uuid4().hex[:10], "target": str(target)[:300], "text": text[:MAX_COMMENT], "by": by,
         "at": time.time()}
    update(folder, lambda d: d["comments"].append(c), by, "комментарий")
    return c


def delete_comment(folder: Path, comment_id: str, by: str, admin: bool = False) -> None:
    def fn(data):
        c = next((x for x in data["comments"] if x["id"] == comment_id), None)
        if not c:
            raise OfferError("комментарий не найден")
        if c["by"] != by and not admin:
            raise OfferError("удалить можно только свой комментарий")
        data["comments"].remove(c)
    update(folder, fn, by, "комментарий удалён")


def select_best(folder: Path, positions: list[dict], by: str) -> int:
    """«Отметить лучшие»: по каждой позиции — только лучшая цена (остальные цены позиции снимаются)."""
    t = build(folder, positions)
    cells = []
    for n, by_sup in t["cells"].items():
        b = t["best"].get(n)
        for key, offers in by_sup.items():
            for o in offers:
                if o["written"]:
                    continue
                want = bool(b) and key == b["supplier"] and o is offers[0]
                if o["selected"] != want:
                    cells.append({"file": o["file_id"], "pos": n, "selected": want})
    return select(folder, cells, by) if cells else 0


def write(folder: Path, positions: list[dict], by: str) -> dict:
    """«Записать отмеченные в NocoDB»: по документам — request_write (исправленные цены — overrides). Возвращает
    {files, positions, over_limit}; задачу «noco_purch» ставит адрес сайта."""
    t = build(folder, positions)
    per_file: dict[str, dict[str, float | None]] = {}
    over_limit = []
    for n, by_sup in t["cells"].items():
        chosen = [o for offers in by_sup.values() for o in offers if o["selected"] and not o["written"] and o["price_rub"]]
        already = sum(1 for offers in by_sup.values() for o in offers if o["written"])
        if already + len(chosen) > MAX_SLOTS:
            over_limit.append(n)
            chosen.sort(key=lambda o: o["price_rub"])
            chosen = chosen[:max(0, MAX_SLOTS - already)]
        for o in chosen:
            per_file.setdefault(o["file_id"], {})[n] = o["price_rub"] if o["edited"] else None
    if not per_file:
        raise OfferError("не отмечено ни одной цены, которой ещё нет в NocoDB")
    for file_id, pos in per_file.items():
        overrides = {n: p for n, p in pos.items() if p is not None}
        pricelists.request_write(folder, file_id, list(pos), by, overrides=overrides)
    update(folder, lambda d: None, by, f"запись в NocoDB: цен {sum(len(p) for p in per_file.values())}")
    return {"files": len(per_file), "positions": sum(len(p) for p in per_file.values()), "over_limit": over_limit}
