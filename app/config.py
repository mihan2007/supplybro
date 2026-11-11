"""
Настройки сайта: клиенты, их юр. лица и подключение к NocoDB.

Список клиентов лежит в config/clients.json (подключается в контейнер только для чтения).
Ключи API в этом файле НЕ хранятся — только путь к файлу с ключом внутри контейнера
(сам файл подключается в compose.yml из папки secrets).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

CLIENTS_FILE = Path(os.environ.get("CLIENTS_FILE", "/app/config/clients.json"))

# База NocoDB клиента одной строкой в .env (0.16.1, пользователь 08.11.2026: «сделай, чтобы я мог просто поменять данные
# базы данных»): NOCODB_URL_<КЛИЕНТ> — адрес «Реестра конкурсов» из браузера, например
# NOCODB_URL_MAIN=https://app.nocodb.com/wzs963xn/py03vcjrohur7u7/m9w2qkxcvt02nkb/vwrh37vtzc3h9r0r/...
# Из него берутся рабочее пространство, база и таблица реестра; пусто — как в config/clients.json.
NOCO_URL_RE = re.compile(r"^(https?://[^/#?]+)/(?:#/)?(?:nc/)?([a-z0-9]+)/(p[a-z0-9]+)/(m[a-z0-9]+)(?:[/?#]|$)", re.I)


def noco_env_name(client_id: str) -> str:
    return "NOCODB_URL_" + re.sub(r"[^A-Z0-9]", "_", client_id.upper())


def noco_from_url(url: str) -> dict:
    """Адрес таблицы NocoDB → {url, workspace_id, base_id, registry_table_id}. Не адрес таблицы — ValueError."""
    m = NOCO_URL_RE.match((url or "").strip().strip('"').strip("'"))
    if not m:
        raise ValueError("это не адрес таблицы NocoDB — откройте «Реестр конкурсов» в NocoDB и скопируйте адрес "
                         "из строки браузера целиком (вида https://app.nocodb.com/w…/p…/m…/…)")
    return {"url": m.group(1), "workspace_id": m.group(2), "base_id": m.group(3), "registry_table_id": m.group(4)}


def _noco_override(client_id: str) -> dict:
    name = noco_env_name(client_id)
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        return noco_from_url(raw)
    except ValueError as exc:
        raise ValueError(f"{name} в .env: {exc}") from None


# База NocoDB клиента из админ-панели (0.21.0, пользователь 11.11.2026: «в админ-панели — возможность переключаться
# к разным базам данных NocoDB… нужно, чтобы переключение происходило без проблем»). Таблица client_bases в базе
# сайта (app.db, её создаёт db.init; пишет app/noco_bases.py). Главнее .env и clients.json: что выбрано в
# админ-панели, то и действует — сразу и на сайте, и в обработчике (оба читают эту таблицу). У каждого сайта
# (ноутбук, сервер) своя app.db — переключение действует там, где его сделали.
APP_DB = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data")) / "app.db"
ADMIN_BASES_TTL = 2.0                     # секунды: load_clients зовётся на каждый запрос — база сайта не чаще
_admin_cache: tuple[float, dict] = (0.0, {})


def admin_bases(fresh: bool = False) -> dict[str, dict]:
    """{client_id: {url, workspace_id, base_id, registry_table_id, base_title, registry_title, updated_by,
    updated_at}} — базы, выбранные в админ-панели. Нет базы сайта или таблицы — {}."""
    global _admin_cache
    now = time.monotonic()
    if not fresh and now - _admin_cache[0] < ADMIN_BASES_TTL:
        return _admin_cache[1]
    out: dict[str, dict] = {}
    if APP_DB.exists():
        try:
            con = sqlite3.connect(APP_DB, timeout=10)
            try:
                con.row_factory = sqlite3.Row
                for r in con.execute("SELECT * FROM client_bases"):
                    out[r["client_id"]] = dict(r)
            finally:
                con.close()
        except sqlite3.Error:
            out = {}                       # таблицы ещё нет (сайт до 0.21.0) — как без выбора в админ-панели
    _admin_cache = (now, out)
    return out


def forget_admin_bases() -> None:
    """Сбросить кэш выбора в админ-панели (после сохранения — сразу новая база в этом процессе)."""
    global _admin_cache
    _admin_cache = (0.0, {})


NOCO_KEYS = ("url", "workspace_id", "base_id", "registry_table_id")


def noco_layers(client_id: str, raw_nocodb: dict) -> dict[str, dict | None]:
    """Откуда берётся база клиента: settings (clients.json), env (.env), admin (админ-панель) — каждое или None."""
    try:
        env = _noco_override(client_id) or None
    except ValueError:
        env = None
    adm = admin_bases().get(client_id)
    return {"settings": {k: raw_nocodb.get(k, "") for k in NOCO_KEYS}, "env": env,
            "admin": {k: adm.get(k, "") for k in NOCO_KEYS} if adm else None}


@dataclass
class LegalEntity:
    id: str
    name: str
    inn: str = ""
    # Другие написания, по которым юр. лицо узнаётся в данных NocoDB («Простор», «ИП Строк»).
    aliases: list[str] = field(default_factory=list)
    # Материалы участника для подготовки заявки (0.15.0, app/bid/participant.py): папка, сведения, реквизиты, опись.
    materials: dict = field(default_factory=dict)

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
    # Откуда база (0.21.0): settings — config/clients.json, env — NOCODB_URL_<КЛИЕНТ> в .env, admin — админ-панель.
    source: str = "settings"

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
    """Читает config/clients.json. Читается при каждом запросе — правки применяются без перезапуска. База NocoDB
    клиента — выбранная в админ-панели (0.21.0), иначе из NOCODB_URL_<КЛИЕНТ> в .env (0.16.1; .env — после
    `docker compose up -d`), иначе из clients.json."""
    raw = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    admin = admin_bases()
    clients: dict[str, Client] = {}
    for c in raw.get("clients", []):
        env = _noco_override(c["id"])
        adm = {k: admin[c["id"]][k] for k in NOCO_KEYS if admin.get(c["id"], {}).get(k)}
        source = "admin" if adm else "env" if env else "settings"
        noco = NocoSettings(**{**c["nocodb"], **env, **adm, "source": source})
        entities = [LegalEntity(**e) for e in c.get("legal_entities", [])]
        clients[c["id"]] = Client(id=c["id"], name=c["name"], nocodb=noco, legal_entities=entities,
                                  tenders_folder=c.get("tenders_folder", ""))
    return clients


def raw_clients() -> dict[str, dict]:
    """config/clients.json как есть (без .env и админ-панели): {id клиента: описание}."""
    raw = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    return {c["id"]: c for c in raw.get("clients", [])}
