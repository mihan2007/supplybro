"""
Настройки сайта: клиенты, их юр. лица и подключение к NocoDB.

Список клиентов лежит в config/clients.json (подключается в контейнер только для чтения).
Ключи API в этом файле НЕ хранятся — только путь к файлу с ключом внутри контейнера
(сам файл подключается в compose.yml из папки secrets).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CLIENTS_FILE = Path(os.environ.get("CLIENTS_FILE", "/app/config/clients.json"))


@dataclass
class LegalEntity:
    id: str
    name: str
    inn: str = ""
    # Другие написания, по которым юр. лицо узнаётся в данных NocoDB («Простор», «ИП Строк»).
    aliases: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        """Упоминается ли это юр. лицо в тексте (по ИНН, названию или другому написанию)."""
        if not text:
            return False
        low = text.lower()
        keys = [self.inn, self.name, *self.aliases]
        return any(k and k.lower() in low for k in keys)


@dataclass
class NocoSettings:
    url: str
    workspace_id: str
    base_id: str
    registry_table_id: str
    token_file: str
    # Колонка «Реестр конкурсов», где указано юр. лицо-участник (если её ещё нет — фильтра нет).
    participant_column: str = "Участник"
    # Представления по умолчанию (если такие есть в NocoDB).
    default_registry_view: str = "Вся таблица"
    default_spec_view: str = "Сбор цен"

    def token(self) -> str:
        path = Path(self.token_file)
        if not path.exists():
            raise RuntimeError(f"нет файла с ключом NocoDB: {self.token_file} (см. NOCODB_TOKEN_FILE в .env)")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"файл с ключом NocoDB пустой: {self.token_file}")
        return token


@dataclass
class Client:
    id: str
    name: str
    nocodb: NocoSettings
    legal_entities: list[LegalEntity]
    # Папка клиента внутри папки конкурсов (Dropbox\Агент): «Наша», «ИП Саргсян» …
    tenders_folder: str = ""

    def entity(self, entity_id: str | None) -> LegalEntity | None:
        return next((e for e in self.legal_entities if e.id == entity_id), None)


def load_clients() -> dict[str, Client]:
    """Читает config/clients.json. Читается при каждом запросе — правки применяются без перезапуска."""
    raw = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    clients: dict[str, Client] = {}
    for c in raw.get("clients", []):
        noco = NocoSettings(**c["nocodb"])
        entities = [LegalEntity(**e) for e in c.get("legal_entities", [])]
        clients[c["id"]] = Client(id=c["id"], name=c["name"], nocodb=noco, legal_entities=entities,
                                  tenders_folder=c.get("tenders_folder", ""))
    return clients
