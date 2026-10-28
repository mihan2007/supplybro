"""
Папки конкурсов в Dropbox: поиск папки по внутреннему номеру, создание новой, безопасные пути.

Устройство (как сейчас ведёт пользователь):
    Dropbox\\Агент\\<папка клиента>\\<К-номер Заказчик Предмет>\\
        Конкурсная документация\\   Разъяснения заказчика\\   Прайсы, счета, КП\\   …   AIExtract\\
В контейнере папка «Агент» подключена как /tenders (TENDERS_DIR в .env).
Новая папка конкурса создаётся копированием шаблона «К---» из папки клиента (если он есть).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .config import Client

TENDERS_DIR = Path(os.environ.get("TENDERS_DIR_IN_CONTAINER", "/tenders"))

TEMPLATE_FOLDER = "К---"
# Подпапки, документы из которых обрабатываются (тексты, поиск, чат, анализ).
DOC_SUBFOLDERS = ["Конкурсная документация", "Разъяснения заказчика"]
# Стандартные подпапки нового конкурса (если в папке клиента нет шаблона «К---»).
DEFAULT_SUBFOLDERS = ["Конкурсная документация", "Разъяснения заказчика", "Прайсы, счета, КП",
                      "Письма поставщикам", "Платежки", "Сертификаты и отказные письма на товар"]
# Папки внутри конкурса, которые никогда не обрабатываются как документы.
NEVER_PROCESS = {"AIExtract", "Платежки", "Письма поставщикам"}

_NUM_RE = re.compile(r"^\s*(?:[КK]\s*-?\s*)?(\d{3,4})(?=\D|$)", re.IGNORECASE)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FolderError(RuntimeError):
    """Ошибка работы с папкой конкурса — текст показывается пользователю."""


def norm_number(text: str | None) -> str | None:
    """«К939», «K-990», «915 …» → «К939», «К990», «К915»; иначе None."""
    m = _NUM_RE.match(str(text or ""))
    return f"К{int(m.group(1))}" if m else None


def client_root(client: Client) -> Path:
    if not client.tenders_folder:
        raise FolderError(f"у клиента «{client.name}» не указана папка конкурсов (tenders_folder в config/clients.json)")
    root = TENDERS_DIR / client.tenders_folder
    if not root.is_dir():
        raise FolderError(f"папка конкурсов клиента не найдена: {client.tenders_folder} "
                          "(проверьте TENDERS_DIR в .env и tenders_folder в config/clients.json)")
    return root


def list_folders(client: Client) -> list[dict]:
    """Папки конкурсов клиента: [{name, number}] (служебные «#…» и шаблон «К---» пропускаются)."""
    try:
        root = client_root(client)
    except FolderError:
        return []
    result = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if p.is_dir() and not p.name.startswith(("#", ".", "~")) and p.name != TEMPLATE_FOLDER:
            result.append({"name": p.name, "number": norm_number(p.name)})
    return result


def folder_path(client: Client, folder: str) -> Path:
    """Путь к папке конкурса; имя — только непосредственная подпапка папки клиента."""
    if not folder or folder != Path(folder).name or folder in (".", "..") or folder.startswith("#"):
        raise FolderError("недопустимое имя папки конкурса")
    path = client_root(client) / folder
    if not path.is_dir():
        raise FolderError(f"папка конкурса не найдена: {folder}")
    return path


def inside(base: Path, rel: str) -> Path:
    """Путь внутри base; выход за её пределы (../) запрещён."""
    target = (base / rel).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        raise FolderError("путь выходит за пределы папки конкурса")
    return target


def doc_roots(folder: Path) -> list[Path]:
    """Где лежат документы конкурса: стандартные подпапки, а если их нет — вся папка."""
    roots = [folder / s for s in DOC_SUBFOLDERS if (folder / s).is_dir()]
    return roots or [folder]


def safe_name(text: str, limit: int = 120) -> str:
    name = _BAD_CHARS.sub(" ", text)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:limit].rstrip(". ")


def _template_ignore(skipped: list[str]):
    """Что не копировать из шаблона «К---»: результаты ИИ (AIExtract), временные файлы и папки, внутри которых есть
    AIExtract, — это материалы какого-то конкурса, попавшие в шаблон по ошибке (так 28.10.2026 в каждый новый конкурс
    копировалась папка Documents_32616424721 чужого конкурса и анализировалась вместо пустой документации)."""
    def ignore(dirpath, names):
        out = set()
        for n in names:
            p = Path(dirpath) / n
            if n == "AIExtract" or n.startswith("~$") or n.lower() in ("thumbs.db", "desktop.ini"):
                out.add(n)
            elif p.is_dir() and (p / "AIExtract").is_dir():
                out.add(n)
                skipped.append(n)
        return out
    return ignore


def create_folder(client: Client, number: str, title: str, skipped: list[str] | None = None) -> str:
    """Создаёт папку «<номер> <название>» по шаблону «К---». Возвращает имя папки. skipped — сюда дописываются
    папки шаблона, которые не скопированы (материалы чужого конкурса)."""
    skipped = skipped if skipped is not None else []
    num = norm_number(number)
    if not num:
        raise FolderError("номер конкурса должен быть вида К940")
    root = client_root(client)
    for f in list_folders(client):
        if f["number"] == num:
            raise FolderError(f"папка конкурса {num} уже есть: {f['name']}")
    name = safe_name(f"{num} {title}".strip())
    dest = root / name
    if dest.exists():
        raise FolderError(f"папка уже существует: {name}")
    template = root / TEMPLATE_FOLDER
    if template.is_dir():
        shutil.copytree(template, dest, ignore=_template_ignore(skipped))
    # Подпапки документов — всегда: обрабатываются только они. Без них обработка берёт всю папку конкурса
    # (так устроены старые папки) — и в новом конкурсе без документов взяла бы всё, что лежало в шаблоне.
    # Шаблона нет или в нём нет ни одной папки — стандартный набор подпапок.
    has_dirs = dest.is_dir() and any(x.is_dir() for x in dest.iterdir())
    for sub in (DOC_SUBFOLDERS if has_dirs else DEFAULT_SUBFOLDERS):
        (dest / sub).mkdir(parents=True, exist_ok=True)
    return name


def next_number(numbers) -> str:
    nums = [int(n[1:]) for n in numbers if n and n[1:].isdigit()]
    return f"К{max(nums) + 1}" if nums else "К1"
