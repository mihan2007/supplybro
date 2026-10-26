"""
SupplyBro — локальный сайт.

Страница «/» — выбор клиента → юр. лица → конкурса; по конкурсу показываются
карточка из «Реестра конкурсов» NocoDB и его спецификация.

Данные страница берёт из JSON-адресов /api/... (ниже). Сайт только читает NocoDB.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Client, load_clients
from .nocodb import NocoClient, NocoError

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT.parent / "VERSION"
APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "dev"
DATA_DIR = Path(os.environ.get("DATA_DIR_IN_CONTAINER", "/data"))

# Поля «Реестра конкурсов», которые нужны для списка конкурсов и шапки карточки.
F_INTERNAL = "Внутренний номер"
F_NUMBER = "Номер конкурса"
F_TITLE = "Наименование предмета закупки"
F_STAGE = "Стадия"
F_DEADLINE = "Срок подачи заявок"
F_PRICE = "Начальная цена"
F_CURRENCY = "Валюта"
F_PROCEDURE = "Ссылка на процедуру"
F_SPEC_LINK = "Ссылка на спецификацию"
F_DOCS = "Перечень документов заявки"   # первая строка может быть «Участник: … (ИНН …)»
NOT_SPECIFIED = "Не указано в документах"

# id таблицы NocoDB в ссылке вида https://app.nocodb.com/<ws>/<base>/<таблица>/<вид>
TABLE_ID_RE = re.compile(r"/(m[a-z0-9]{8,})(?:/|$|\?)")

app = FastAPI(title="SupplyBro", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.exception_handler(NocoError)
async def noco_error_handler(_, exc: NocoError):
    # Ошибку NocoDB показываем на странице понятным текстом.
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ---------- вспомогательное ----------

def get_client(client_id: str) -> Client:
    try:
        clients = load_clients()
    except Exception as exc:
        raise HTTPException(500, f"не удалось прочитать config/clients.json: {exc}")
    client = clients.get(client_id)
    if not client:
        raise HTTPException(404, f"клиент не найден: {client_id}")
    return client


def clean(value) -> str:
    """Текст поля без «Не указано в документах» и пустых значений."""
    text = str(value or "").strip()
    return "" if text in ("", NOT_SPECIFIED, "—", "None") else text


def first_line(value) -> str:
    text = clean(value)
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def participants_of(record: dict, client: Client) -> list[str]:
    """Какие юр. лица клиента указаны участниками конкурса.

    Источники: колонка участника (если она есть в реестре) и строка «Участник: …»
    в «Перечне документов заявки» (её пишет агент, Шаг 7.1d).
    """
    texts = [str(record.get(client.nocodb.participant_column) or "")]
    docs = str(record.get(F_DOCS) or "")
    texts += [ln for ln in docs.splitlines()[:5] if ln.strip().lower().startswith("участник:")]
    found = []
    for entity in client.legal_entities:
        if any(entity.matches(t) for t in texts):
            found.append(entity.id)
    return found


def spec_table_id(link: str | None) -> str | None:
    m = TABLE_ID_RE.search(str(link or ""))
    return m.group(1) if m else None


# ---------- страницы ----------

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
def health() -> dict:
    """Проверка работоспособности: сайт жив, папка данных доступна для записи."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        data_ok = True
    except Exception:
        data_ok = False
    return {"status": "ok" if data_ok else "degraded", "version": APP_VERSION}


# ---------- данные для страницы ----------

@app.get("/api/clients")
def api_clients():
    """Клиенты и их юр. лица — для первых двух выпадающих списков."""
    try:
        clients = load_clients()
    except Exception as exc:
        raise HTTPException(500, f"не удалось прочитать config/clients.json: {exc}")
    return {
        "version": APP_VERSION,
        "clients": [
            {"id": c.id, "name": c.name,
             "entities": [{"id": e.id, "name": e.name, "inn": e.inn} for e in c.legal_entities]}
            for c in clients.values()
        ],
    }


@app.get("/api/clients/{client_id}/tenders")
async def api_tenders(client_id: str, entity: str | None = None, fresh: bool = False):
    """Конкурсы клиента из «Реестра конкурсов» с отметкой, к какому юр. лицу они относятся."""
    client = get_client(client_id)
    noco = NocoClient(client.nocodb)
    reg_id = client.nocodb.registry_table_id
    meta = await noco.table_meta(reg_id, fresh)
    titles = {c["title"] for c in meta["columns"]}
    has_participant_col = client.nocodb.participant_column in titles

    wanted = [F_INTERNAL, F_NUMBER, F_TITLE, F_STAGE, F_DEADLINE, F_SPEC_LINK, F_DOCS]
    if has_participant_col:
        wanted.append(client.nocodb.participant_column)
    fields = ["Id"] + [f for f in wanted if f in titles]
    rows = await noco.records(reg_id, fields=fields, sort="-Id")

    tenders = []
    for r in rows:
        internal, number = clean(r.get(F_INTERNAL)), clean(r.get(F_NUMBER))
        if not internal and not number:
            continue  # пустые строки реестра
        parts = participants_of(r, client)
        if entity and parts and entity not in parts:
            continue  # конкурс другого юр. лица
        tenders.append({
            "id": r["Id"],
            "internal": internal,
            "number": number,
            "title": first_line(r.get(F_TITLE)),
            "stage": first_line(r.get(F_STAGE)),
            "deadline": first_line(r.get(F_DEADLINE)),
            "participants": parts,
            "has_spec": bool(spec_table_id(r.get(F_SPEC_LINK))),
        })
    return {"participant_column": client.nocodb.participant_column,
            "participant_column_exists": has_participant_col,
            "tenders": tenders}


@app.get("/api/clients/{client_id}/tenders/{record_id}")
async def api_tender(client_id: str, record_id: int, view: str | None = None, fresh: bool = False):
    """Карточка конкурса: поля выбранного представления «Реестра конкурсов»."""
    client = get_client(client_id)
    noco = NocoClient(client.nocodb)
    reg_id = client.nocodb.registry_table_id
    meta = await noco.table_meta(reg_id, fresh)
    chosen = noco.pick_view(meta, view, client.nocodb.default_registry_view)
    record = await noco.record(reg_id, record_id)

    if chosen:
        columns = await noco.view_columns(reg_id, chosen["id"], fresh)
    else:
        columns = [{"title": c["title"], "uidt": c["uidt"]} for c in meta["columns"]]
    fields = [{"title": c["title"], "uidt": c["uidt"], "value": record.get(c["title"])} for c in columns]

    spec_id = spec_table_id(record.get(F_SPEC_LINK))
    return {
        "id": record_id,
        "internal": clean(record.get(F_INTERNAL)),
        "number": clean(record.get(F_NUMBER)),
        "title": first_line(record.get(F_TITLE)),
        "stage": first_line(record.get(F_STAGE)),
        "deadline": first_line(record.get(F_DEADLINE)),
        "price": clean(record.get(F_PRICE)),
        "currency": clean(record.get(F_CURRENCY)),
        "participants": participants_of(record, client),
        "links": {
            "procedure": clean(record.get(F_PROCEDURE)),
            "nocodb": noco.table_link(reg_id, (meta["views"][0]["id"] if meta["views"] else None), record_id),
            "spec": clean(record.get(F_SPEC_LINK)) if spec_id else "",
        },
        "views": meta["views"],
        "view": chosen,
        "fields": fields,
    }


@app.get("/api/clients/{client_id}/tenders/{record_id}/spec")
async def api_spec(client_id: str, record_id: int, view: str | None = None, fresh: bool = False):
    """Спецификация конкурса: строки выбранного представления его таблицы."""
    client = get_client(client_id)
    noco = NocoClient(client.nocodb)
    record = await noco.record(client.nocodb.registry_table_id, record_id)

    table_id = spec_table_id(record.get(F_SPEC_LINK))
    if not table_id:
        # Запасной путь: таблица названа внутренним номером конкурса (К939) или «Спецификация — <номер>».
        for title in (clean(record.get(F_INTERNAL)), f"Спецификация — {clean(record.get(F_NUMBER))}"):
            if title and (table_id := await noco.find_table_by_title(title, fresh)):
                break
    if not table_id:
        return {"found": False}

    meta = await noco.table_meta(table_id, fresh)
    chosen = noco.pick_view(meta, view, client.nocodb.default_spec_view)
    if chosen:
        columns = await noco.view_columns(table_id, chosen["id"], fresh)
    else:
        columns = [{"title": c["title"], "uidt": c["uidt"]} for c in meta["columns"]]
    rows = await noco.records(table_id, view_id=chosen["id"] if chosen else None)
    return {
        "found": True,
        "table_id": table_id,
        "title": meta["title"],
        "link": noco.table_link(table_id, chosen["id"] if chosen else None),
        "views": meta["views"],
        "view": chosen,
        "columns": columns,
        "rows": [{c["title"]: r.get(c["title"]) for c in columns} for r in rows],
    }
