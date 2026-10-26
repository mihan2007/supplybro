"""
Имена файлов: исправление «кракозябр» для показа.

Архивы из Windows часто хранят русские имена в кодировке CP866. Если такой архив распаковать
программой, которая считает имена «маковской» кириллицей, получается «Па®ЂЃ¶•≠®• 1_ТЗ» вместо
«Приложение 1_ТЗ». Сами файлы на диске сайт НЕ переименовывает — исправленное имя только показывается.
"""

from __future__ import annotations

# Символы, которые почти не встречаются в нормальных именах, но характерны для такой порчи.
_SUSPECT = set("ЃЂ≠™®•¶§ђѓ†Ґ£ІЉЊЋЌЎўЏ∞±≤≥µ∂∑∏πƒ¬√≈∆…ќћ")
# Псевдографика — признак того, что «исправление» само дало мусор.
_BOX = set("░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀■")


def _cyrillic_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / len(letters)


def fix_name(name: str) -> str:
    """Исправленное имя (если имя испорчено указанным образом), иначе — как есть."""
    if not any(c in _SUSPECT for c in name):
        return name
    try:
        fixed = name.encode("mac_cyrillic").decode("cp866")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    if any(c in _BOX for c in fixed) or any(c in _SUSPECT for c in fixed):
        return name
    return fixed if _cyrillic_share(fixed) >= _cyrillic_share(name) else name


def fix_path(rel_path: str) -> str:
    """Исправленный относительный путь (каждая часть по отдельности), с «/» в качестве разделителя."""
    return "/".join(fix_name(part) for part in rel_path.replace("\\", "/").split("/"))
