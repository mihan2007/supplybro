"""
Конвертация старых и «открытых» форматов в современные через LibreOffice без окна:
.doc / .rtf / .odt → .docx,  .xls / .ods → .xlsx.

Исходный файл не трогается: результат кладётся в отдельную папку (AIExtract/_converted).
У каждого запуска — свой временный профиль LibreOffice, чтобы параллельные запуски не мешали
друг другу и зависший файл не блокировал следующие (по образцу convert_odf.py старого проекта).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

TARGET_FORMAT = {
    ".doc": "docx", ".rtf": "docx", ".odt": "docx", ".ott": "docx", ".fodt": "docx", ".wps": "docx", ".dot": "docx",
    ".xls": "xlsx", ".ods": "xlsx", ".ots": "xlsx", ".fods": "xlsx", ".xlt": "xlsx",
}


def find_soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert(src: Path, out_dir: Path, out_name: str, timeout: int = 180) -> Path:
    """Конвертирует src и кладёт результат в out_dir/out_name.<формат>. Возвращает путь к результату."""
    fmt = TARGET_FORMAT[src.suffix.lower()]
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice не установлен — конвертация недоступна")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile, tempfile.TemporaryDirectory() as tmp_out:
        cmd = [soffice, "--headless", "--norestore", "--nolockcheck",
               f"-env:UserInstallation=file://{profile}",
               "--convert-to", fmt, "--outdir", tmp_out, str(src)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"LibreOffice не справился за {timeout} с")
        produced = sorted(Path(tmp_out).glob(f"*.{fmt}"))
        if not produced:
            raise RuntimeError("LibreOffice не создал файл: " + (proc.stderr or proc.stdout or "").strip()[:300])
        target = out_dir / f"{out_name}.{fmt}"
        shutil.move(str(produced[0]), target)
    return target
