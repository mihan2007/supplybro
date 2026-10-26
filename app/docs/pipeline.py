"""
Подготовка документов конкурса — без ИИ. Выполняется обработчиком (worker) как задача «prepare».

Шаги:
  1. Архивы в папках документов распаковываются рядом с архивом в «<имя>_extracted»
     (как делал агент; скрипт tools/extract_archive.py из старого проекта). Уже распакованные не трогаются.
  2. Каждый файл: .doc/.xls/.rtf/.odt → .docx/.xlsx (копия в AIExtract/_converted, исходник не меняется),
     извлечение текста (app/docs/extract.py), запись текста в AIExtract/text/*.md.
  3. Сведения о файле и фрагменты текста для поиска — в базу сайта.
  4. Список файлов со статусами — AIExtract/text/_manifest.json (его может читать и агент в приложении).

Повторно обрабатываются только новые и изменённые файлы (сравнение размера, даты и хеша содержимого).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .. import db
from ..tenders import NEVER_PROCESS, doc_roots, safe_name
from .convert import TARGET_FORMAT, convert
from .extract import EXTRACTOR_VERSION, KINDS, SKIP_KINDS, chunks, extract, injection_snippets, render
from .names import fix_name, fix_path

from tools import extract_archive  # копия скрипта распаковки из старого проекта

TEXT_DIR = Path("AIExtract") / "text"
CONV_DIR = Path("AIExtract") / "_converted"
ARCHIVE_EXT = extract_archive.ARCHIVE_EXTENSIONS
IGNORED = {"thumbs.db", "desktop.ini", ".ds_store"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def walk_docs(folder: Path) -> list[Path]:
    """Все файлы в папках документов конкурса (без AIExtract, служебных и временных файлов)."""
    found = []
    for root in doc_roots(folder):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in NEVER_PROCESS and not d.startswith("."))
            for name in sorted(filenames):
                if name.lower() in IGNORED or name.startswith(("~$", ".")):
                    continue
                found.append(Path(dirpath) / name)
    return found


def _archive_dest(archive: Path) -> Path:
    """Куда распаковывает extract_archive.py: «<имя без расширения>_extracted» рядом с архивом."""
    name = archive.name
    for ext in (".tar.gz", ".tar.bz2"):
        if name.lower().endswith(ext):
            return archive.parent / (name[: -len(ext)] + "_extracted")
    return archive.parent / (archive.stem + "_extracted")


def _unpack_archives(folder: Path, progress) -> dict[str, dict]:
    info = {}
    stats = {"files": 0, "bytes": 0, "max_files": extract_archive.DEFAULT_MAX_FILES,
             "max_bytes": extract_archive.DEFAULT_MAX_TOTAL_BYTES}
    # Распаковываем, пока появляются новые архивы (архив внутри архива распакуется на следующем круге).
    for _round in range(3):
        new = False
        for a in walk_docs(folder):
            if not a.name.lower().endswith(ARCHIVE_EXT):
                continue
            rel = a.relative_to(folder).as_posix()
            if rel in info:
                continue
            dest = _archive_dest(a)
            if dest.is_dir() and any(dest.iterdir()):
                info[rel] = {"status": "archive", "flags": f"распакован в «{fix_name(dest.name)}»", "error": ""}
                continue
            progress(f"Распаковка: {fix_name(a.name)}", 0.03)
            try:
                _d, extracted, nested = extract_archive.extract_recursive(
                    str(a), str(a.parent), None, extract_archive.DEFAULT_MAX_DEPTH, stats)
                errors = [n.get("error", "") for n in nested if n.get("status") == "error"]
                info[rel] = {"status": "archive", "flags": f"распакован: файлов {len(extracted)}",
                             "error": "; ".join(errors)[:300]}
                new = True
            except Exception as exc:
                info[rel] = {"status": "error", "flags": "архив не распакован", "error": str(exc)[:300]}
        if not new:
            break
    return info


def _process_file(folder: Path, path: Path, rel: str, sha: str) -> tuple[dict, list[tuple[str, str]]]:
    """Обрабатывает один файл. Возвращает (сведения для базы, фрагменты для поиска)."""
    ext = path.suffix.lower()
    row = {"kind": "other", "status": "skipped", "pages": 0, "chars": 0, "ocr_pages": 0,
           "hidden_chars": 0, "flags": "", "text_path": "", "error": ""}
    if ext in SKIP_KINDS:
        row["flags"] = SKIP_KINDS[ext]
        return row, []
    source, kind = path, KINDS.get(ext)
    if ext in TARGET_FORMAT:
        out_name = f"{sha[:8]}_{safe_name(fix_name(path.stem), 60)}"
        source = convert(path, folder / CONV_DIR, out_name)
        kind = "docx" if TARGET_FORMAT[ext] == "docx" else "xlsx"
    if not kind:
        row["flags"] = "формат не поддерживается"
        return row, []

    doc = extract(source, kind)
    flags = []
    if doc.hidden and kind in ("docx", "pdf"):
        flags.append("⚠ скрытый текст")
    flags.extend(doc.notes)
    visible = "\n".join(t for _, t in doc.parts)
    suspicious = injection_snippets(visible + "\n" + "\n".join(doc.hidden))
    if suspicious:
        flags.append("⚠ текст, обращённый к ИИ: «" + suspicious[0][:100] + "»")

    text_rel = TEXT_DIR / f"{sha[:8]}_{safe_name(fix_name(path.name), 80)}.md"
    (folder / TEXT_DIR).mkdir(parents=True, exist_ok=True)
    (folder / text_rel).write_text(render(doc, fix_path(rel), flags), encoding="utf-8")

    row.update(kind=kind, pages=doc.pages, chars=doc.chars, ocr_pages=doc.ocr_pages,
               hidden_chars=doc.hidden_chars, flags="; ".join(flags), text_path=text_rel.as_posix(),
               status="empty" if doc.chars == 0 else ("ocr" if doc.ocr_pages else "ok"))
    return row, chunks(doc)


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def prepare(client_id: str, folder_name: str, folder: Path, progress) -> str:
    """Подготовка всех документов конкурса. progress(шаг, доля 0..1). Возвращает итог одной строкой."""
    progress("Поиск архивов", 0.01)
    archives = _unpack_archives(folder, progress)

    files = [p for p in walk_docs(folder) if not p.name.lower().endswith(ARCHIVE_EXT)]
    known = db.doc_rows(client_id, folder_name)
    present, processed, failed = set(archives), 0, 0
    con = db.connect()
    try:
        for i, path in enumerate(files):
            rel = path.relative_to(folder).as_posix()
            present.add(rel)
            progress(f"Файл {i + 1} из {len(files)}: {fix_name(path.name)}", 0.05 + 0.9 * i / max(len(files), 1))
            st = path.stat()
            old = known.get(rel)
            fresh = old and old["extractor"] == EXTRACTOR_VERSION and old["status"] != "error"
            if fresh and old["size"] == st.st_size and abs((old["mtime"] or 0) - st.st_mtime) < 2:
                continue  # файл не менялся
            sha = _sha256(path)
            if fresh and old["sha256"] == sha:
                con.execute("UPDATE doc_files SET mtime=? WHERE client_id=? AND folder=? AND rel_path=?",
                            (st.st_mtime, client_id, folder_name, rel))
                con.commit()
                continue
            try:
                row, parts = _process_file(folder, path, rel, sha)
            except Exception as exc:
                row, parts = {"kind": KINDS.get(path.suffix.lower(), "other"), "status": "error", "pages": 0,
                              "chars": 0, "ocr_pages": 0, "hidden_chars": 0, "flags": "", "text_path": "",
                              "error": f"{type(exc).__name__}: {exc}"[:300]}, []
                failed += 1
            if old and old.get("text_path") and old["text_path"] != row["text_path"]:
                _remove_quietly(folder / old["text_path"])  # текст прежней версии файла
            con.execute("""INSERT OR REPLACE INTO doc_files(client_id, folder, rel_path, sha256, size, mtime, kind,
                           status, pages, chars, ocr_pages, hidden_chars, flags, text_path, error, extractor, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (client_id, folder_name, rel, sha, st.st_size, st.st_mtime, row["kind"], row["status"],
                         row["pages"], row["chars"], row["ocr_pages"], row["hidden_chars"], row["flags"],
                         row["text_path"], row["error"], EXTRACTOR_VERSION, time.time()))
            con.execute("DELETE FROM doc_chunks WHERE client_id=? AND folder=? AND rel_path=?",
                        (client_id, folder_name, rel))
            con.executemany("INSERT INTO doc_chunks(content, client_id, folder, rel_path, label) VALUES (?,?,?,?,?)",
                            [(text, client_id, folder_name, rel, label) for label, text in parts])
            con.commit()
            processed += 1

        # Архивы — отдельными строками (без текста).
        for rel, a in archives.items():
            p = folder / rel
            st = p.stat()
            con.execute("""INSERT OR REPLACE INTO doc_files(client_id, folder, rel_path, sha256, size, mtime, kind,
                           status, flags, error, extractor, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (client_id, folder_name, rel, "", st.st_size, st.st_mtime, "archive", a["status"],
                         a["flags"], a["error"], EXTRACTOR_VERSION, time.time()))
        # Файлы, которых больше нет на диске, — из базы и их тексты.
        for rel, old in known.items():
            if rel not in present:
                if old.get("text_path"):
                    _remove_quietly(folder / old["text_path"])
                con.execute("DELETE FROM doc_files WHERE client_id=? AND folder=? AND rel_path=?",
                            (client_id, folder_name, rel))
                con.execute("DELETE FROM doc_chunks WHERE client_id=? AND folder=? AND rel_path=?",
                            (client_id, folder_name, rel))
        con.commit()
    finally:
        con.close()

    progress("Список файлов", 0.97)
    rows = sorted(db.doc_rows(client_id, folder_name).values(), key=lambda r: r["rel_path"])
    manifest = [{"file": fix_path(r["rel_path"]), "path_on_disk": r["rel_path"], "kind": r["kind"],
                 "status": r["status"], "pages": r["pages"], "ocr_pages": r["ocr_pages"], "chars": r["chars"],
                 "flags": r["flags"], "error": r["error"], "text": r["text_path"]} for r in rows]
    (folder / TEXT_DIR).mkdir(parents=True, exist_ok=True)
    (folder / TEXT_DIR / "_manifest.json").write_text(
        json.dumps({"updated": time.strftime("%d.%m.%Y %H:%M"), "files": manifest}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    docs = [r for r in rows if r["kind"] != "archive"]
    ok = sum(1 for r in docs if r["status"] in ("ok", "ocr"))
    ocr = sum(r["ocr_pages"] or 0 for r in docs)
    errors = sum(1 for r in rows if r["status"] == "error")
    return (f"Файлов: {len(docs)}, текст извлечён: {ok}, распознано страниц: {ocr}, ошибок: {errors}"
            + (f"; обработано сейчас: {processed}" if processed else "; изменений нет"))
