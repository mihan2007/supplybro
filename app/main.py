"""
SupplyBro — локальный сайт.

Страница «/» — выбор клиента → юр. лица → конкурса; по конкурсу показываются
документы конкурса (папка в Dropbox), анализ документов, карточка из «Реестра конкурсов» NocoDB и его спецификация.

Данные страница берёт из JSON-адресов /api/... (ниже). Сам сайт NocoDB только читает; записывает в NocoDB
обработчик (app/worker.py) после анализа. Документы и анализ — задачи в очереди обработчика.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import access, chat, db, price_summary, pricelists, tender_list
from .noco_tables import own_table_async
from .config import Client, load_clients
from .analysis.corpus import TEXT_STATUSES
from .analysis import changes
from .analysis.run import ANALYSIS_MODELS, CLAUDE_MODEL, MSK, load_result
from .docs.names import fix_path
from .docs.pipeline import ARCHIVE_EXT, walk_docs
from .nocodb import NocoClient, NocoError
from .tenders import (DOC_SUBFOLDERS, FolderError, client_root, create_folder, folder_path, inside,
                      list_folders, next_number, norm_number, safe_name)

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
F_CUSTOMER = "Заказчик"
F_RESULT = "Статус конкурса (выиграли - проиграли)"   # есть только в «Реестре конкурсов» клиента «Наша»
NOT_SPECIFIED = "Не указано в документах"

# id таблицы NocoDB в ссылке вида https://app.nocodb.com/<ws>/<base>/<таблица>/<вид>
TABLE_ID_RE = re.compile(r"/(m[a-z0-9]{8,})(?:/|$|\?)")

# Описание адресов (/docs, /redoc, /openapi.json) выключено (0.16.0): сайт открыт в интернет, список адресов никому не нужен.
app = FastAPI(title="SupplyBro", version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# Вход на сайт (0.7.0, app/access.py): при LOGIN_REQUIRED=1 без входа сайт не открывается, сотрудник видит только своих
# клиентов и делает только то, что разрешает его роль. Тогда админ-панель, страница сотрудника и страница входа включены
# всегда; при LOGIN_REQUIRED=0 — по своим настройкам, как раньше:
# - админ-панель учётных записей (/admin) — ADMIN_PANEL (app/admin.py, app/auth.py);
# - страница сотрудника (/user) — та же страница, что «/» (страница администратора), но без выбора модели анализа,
#   модели чата и поисковика цен — USER_PAGE (решение пользователя 29.10.2026);
# - страница входа (/login) — LOGIN_PAGE (app/login.py).
ADMIN_PANEL, USER_PAGE, LOGIN_PAGE = access.ADMIN_PANEL, access.USER_PAGE, access.LOGIN_PAGE
if ADMIN_PANEL:
    from . import admin
    app.include_router(admin.router)
if LOGIN_PAGE:
    from . import login
    app.include_router(login.router)
if access.LOGIN_REQUIRED:
    from . import auth
    auth.init()
    app.add_middleware(access.LoginMiddleware)
# Заголовки безопасности — у всех ответов, и у отказов проходной (добавлена последней — значит, снаружи), 0.16.0.
app.add_middleware(access.SecurityHeaders)


db.init()


@app.exception_handler(NocoError)
async def noco_error_handler(_, exc: NocoError):
    # Ошибку NocoDB показываем на странице понятным текстом.
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(FolderError)
async def folder_error_handler(_, exc: FolderError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------- вспомогательное ----------

ALIVE_SECONDS = 60          # очередь обработчика молчит дольше — считаем, что обработчик не запущен
QUIET_WARN_SECONDS = 180    # задача столько не меняет шаг — показываем предупреждение (но она может быть жива)


OTHER_CLIENT = "конкурс другого клиента"
# Что видно о задаче чужого клиента (очередь обработчика общая): что очередь занята и сколько, без папки, шага и логина.
FOREIGN_KEEP = ("id", "kind", "status", "progress", "control", "created_at", "started_at", "updated_at", "finished_at")


def _foreign(job: dict) -> bool:
    """Задача клиента, к которому у вошедшего нет доступа (0.16.0). Вход выключен / администратор сайта — своё всё."""
    return not access.can_client(access.current_user(), str(job.get("client_id") or ""))


def job_view(job: dict | None, lanes: dict | None = None, active: list[dict] | None = None) -> dict | None:
    """Задача для страницы: сколько ждёт / идёт, когда было последнее действие, в какой очереди и кого ждёт,
    жив ли обработчик, кто запустил (started_by, 0.16.0). Задача чужого клиента — без папки, шага и логина."""
    if not job:
        return None
    if _foreign(dict(job)):
        job = {**{k: dict(job).get(k) for k in FOREIGN_KEEP}, "client_id": "", "folder": OTHER_CLIENT, "step": "",
               "message": "", "started_by": "", "other_client": True}
    now = time.time()
    j = {k: v for k, v in dict(job).items() if k != "params"}
    lanes = db.lanes_state() if lanes is None else lanes
    lane = db.LANES.get(j["kind"], "docs")
    beat = lanes.get(lane) or {}
    j.update(kind_title=db.KIND_TITLES.get(j["kind"], j["kind"]), lane=lane, lane_title=db.LANE_TITLES.get(lane, lane),
             age=round(now - (j.get("created_at") or now)),
             worker_alive=(now - beat["at"] < ALIVE_SECONDS) if beat.get("at") else None,   # None — неизвестно
             worker_seen=round(now - beat["at"]) if beat.get("at") else None)
    if j["status"] == "running":
        last = j.get("updated_at") or j.get("started_at") or now
        j.update(elapsed=round(now - (j.get("started_at") or now)), quiet=round(now - last),
                 quiet_warn=now - last > QUIET_WARN_SECONDS)
    elif j["status"] == "queued":
        active = db.active_jobs() if active is None else active
        same = [a for a in active if db.LANES.get(a["kind"], "docs") == lane]
        running = next((a for a in same if a["status"] == "running"), None)
        ahead = [a for a in same if a["status"] == "queued" and a["id"] < j["id"]]
        j["ahead"] = len(ahead)
        if running and running["id"] != j["id"]:
            other = _foreign(running)
            j["waiting_for"] = {"folder": OTHER_CLIENT if other else running["folder"],
                                "kind_title": db.KIND_TITLES.get(running["kind"], running["kind"]),
                                "step": "" if other else (running.get("step") or ""),
                                "elapsed": round(now - (running.get("started_at") or now))}
    return j


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

def _page_html() -> str:
    """index.html со ссылками на app.js / app.css с номером версии (0.13.0): после обновления сайта браузер берёт
    новые файлы сам, в том числе во вкладках, открытых из панели «Конкурсы», — без Ctrl+F5."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={APP_VERSION}"')
    return html


@app.get("/start")
def start_page():
    """Страница-приветствие (0.17.0): что умеет сайт и кнопка «Войти». Открыта без входа, данных не показывает."""
    return FileResponse(ROOT / "static" / "start.html", headers={"Cache-Control": "no-cache"})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    """Поисковикам — не индексировать сайт (ТЗ стартовой страницы: внутренний сервис; продвигать — убрать)."""
    return "User-agent: *\nDisallow: /\n"


@app.get("/")
def index():
    return HTMLResponse(_page_html(), headers={"Cache-Control": "no-cache"})


if USER_PAGE:
    @app.get("/user")
    def user_page():
        """Страница сотрудника: та же index.html с пометкой в <body> (app.js прячет выбор моделей и их не отправляет)."""
        html = _page_html()
        html = html.replace("<body>", '<body class="user-mode" data-mode="user">', 1)
        html = html.replace("<title>SupplyBro — конкурсы</title>", "<title>SupplyBro — конкурсы (сотрудник)</title>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


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
    user = access.current_user()
    return {
        "version": APP_VERSION,
        "clients": [
            {"id": c.id, "name": c.name,
             "entities": [{"id": e.id, "name": e.name, "inn": e.inn} for e in c.legal_entities]}
            for c in clients.values() if access.can_client(user, c.id)
        ],
    }


@app.get("/api/clients/{client_id}/tenders")
async def api_tenders(client_id: str, entity: str | None = None, fresh: bool = False):
    """Конкурсы клиента: записи «Реестра конкурсов» NocoDB + папки конкурсов в Dropbox.

    У записи NocoDB указывается её папка (по внутреннему номеру в начале имени папки).
    Папки, для которых записи в NocoDB ещё нет, идут отдельными строками (id «f:<папка>»).
    """
    client = get_client(client_id)
    noco = NocoClient(client.nocodb)
    reg_id = client.nocodb.registry_table_id
    has_participant_col, rows, noco_error = False, [], ""
    try:
        meta = await noco.table_meta(reg_id, fresh)
        titles = {c["title"] for c in meta["columns"]}
        has_participant_col = client.nocodb.participant_column in titles
        wanted = [F_INTERNAL, F_NUMBER, F_TITLE, F_STAGE, F_DEADLINE, F_SPEC_LINK, F_DOCS,
                  F_CUSTOMER, F_PRICE, F_CURRENCY, F_RESULT]          # заказчик, НМЦ, итог — для панели (0.13.0)
        if has_participant_col:
            wanted.append(client.nocodb.participant_column)
        fields = ["Id"] + [f for f in wanted if f in titles]
        # Список хранится 2 мин (кнопка «Обновить из NocoDB» и окончание анализа — fresh): вкладки, открытые из
        # панели «Конкурсы», не читают реестр заново (0.13.0).
        rows = await noco.records(reg_id, fields=fields, sort="-Id", cache=True, fresh=fresh)
    except NocoError as exc:
        noco_error = str(exc)  # папки всё равно показываем

    folders = list_folders(client)
    by_number = _folders_by_number(folders)
    try:
        client_root(client)
        folders_error = ""
    except FolderError as exc:
        folders_error = str(exc)

    tenders, used_folders, numbers = [], set(), set()
    for r in rows:
        item = _noco_item(r, client, by_number)
        if not item:
            continue  # пустые строки реестра
        norm = norm_number(item["internal"])
        if norm:
            numbers.add(norm)
        if item["folder"]:
            used_folders.add(item["folder"])
        if entity and item["participants"] and entity not in item["participants"]:
            continue  # конкурс другого юр. лица
        tenders.append(item)
    for f in folders:
        if f["number"]:
            numbers.add(f["number"])
        if f["name"] in used_folders:
            continue
        tenders.append(_folder_item(f))
    return {"participant_column": client.nocodb.participant_column,
            "participant_column_exists": has_participant_col,
            "noco_error": noco_error,
            "folders_error": folders_error,
            "next_number": next_number(numbers),
            "tenders": tenders}


def _folders_by_number(folders: list[dict]) -> dict[str, str]:
    """Внутренний номер → имя папки (первая, если папок с одним номером несколько)."""
    by_number: dict[str, str] = {}
    for f in folders:
        if f["number"] and f["number"] not in by_number:
            by_number[f["number"]] = f["name"]
    return by_number


def _noco_item(r: dict, client: Client, by_number: dict[str, str]) -> dict | None:
    """Строка списка конкурсов из записи «Реестра конкурсов» (None — пустая строка реестра)."""
    internal, number = clean(r.get(F_INTERNAL)), clean(r.get(F_NUMBER))
    if not internal and not number:
        return None
    norm = norm_number(internal)
    return {
        "id": str(r["Id"]),
        "source": "nocodb",
        "internal": internal,
        "number": number,
        "title": first_line(r.get(F_TITLE)),
        "stage": first_line(r.get(F_STAGE)),
        "deadline": first_line(r.get(F_DEADLINE)),
        "participants": participants_of(r, client),
        "has_spec": bool(spec_table_id(r.get(F_SPEC_LINK))),
        "folder": by_number.get(norm) if norm else None,
        **tender_list.brief(r.get(F_CUSTOMER), r.get(F_TITLE), r.get(F_STAGE), r.get(F_DEADLINE),
                            r.get(F_PRICE), r.get(F_CURRENCY), r.get(F_RESULT)),
    }


def _folder_item(f: dict) -> dict:
    """Строка списка конкурсов для папки в Dropbox без записи в NocoDB (id «f:<папка>»)."""
    title = f["name"][len(f["number"] or ""):].strip() if f["number"] else f["name"]
    return {
        "id": "f:" + f["name"], "source": "folder", "internal": f["number"] or "", "number": "",
        "title": title, "stage": "", "deadline": "", "participants": [], "has_spec": False,
        "folder": f["name"], **tender_list.brief(None, title, None, None, None, None, None),
    }


@app.get("/api/clients/{client_id}/tender-brief")
async def api_tender_brief(client_id: str, id: str):
    """Один конкурс в том же виде, что строка списка (0.13.0): вкладка, открытая из панели «Конкурсы», показывает
    конкурс сразу — одна запись NocoDB вместо всего реестра; полный список (для выпадающего) догружается следом."""
    client = get_client(client_id)
    folders = list_folders(client)
    if id.startswith("f:"):
        f = next((x for x in folders if x["name"] == id[2:]), None)
        if not f:
            raise HTTPException(404, "папка конкурса не найдена")
        return {"tender": _folder_item(f)}
    if not id.isdigit():
        raise HTTPException(400, "неверный номер записи")
    try:
        record = await NocoClient(client.nocodb).record(client.nocodb.registry_table_id, int(id))
    except NocoError as exc:
        raise HTTPException(502, str(exc))
    item = _noco_item(record, client, _folders_by_number(folders))
    if not item:
        raise HTTPException(404, "запись реестра пустая")
    return {"tender": item}


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
    card_on = access.block_on("card")                # раздел выключен сотруднику — только шапка конкурса (0.11.0)
    spec_on = access.block_on("spec")

    # Ссылка на спецификацию — только на таблицу своей базы (0.16.1: в копии базы ссылки ведут в старую базу —
    # тогда ссылка на таблицу с тем же названием в своей базе).
    spec_id = spec_table_id(record.get(F_SPEC_LINK))
    own_spec = await own_table_async(client.nocodb, spec_id) if spec_id and spec_on else None
    spec_link = (clean(record.get(F_SPEC_LINK)) if own_spec == spec_id else noco.table_link(own_spec)) if own_spec else ""
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
            "nocodb": noco.table_link(reg_id, (meta["views"][0]["id"] if meta["views"] else None), record_id) if card_on else "",
            "spec": spec_link,
        },
        "views": meta["views"] if card_on else [],
        "view": chosen if card_on else None,
        "fields": fields if card_on else [],
        "card_off": not card_on,
    }


@app.get("/api/clients/{client_id}/tenders/{record_id}/spec")
async def api_spec(client_id: str, record_id: int, view: str | None = None, fresh: bool = False):
    """Спецификация конкурса: строки выбранного представления его таблицы."""
    client = get_client(client_id)
    noco = NocoClient(client.nocodb)
    record = await noco.record(client.nocodb.registry_table_id, record_id)

    # Только таблица своей базы (0.16.1): ссылка в чужую базу — таблица с тем же названием в своей или запасной путь.
    table_id = await own_table_async(client.nocodb, spec_table_id(record.get(F_SPEC_LINK)))
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


# ---------- документы конкурса (папка в Dropbox) ----------

TENDERS_DISPLAY = os.environ.get("TENDERS_DISPLAY", "Dropbox\\Агент")
# После загрузки документов через сайт сразу запускать анализ (ИИ, платно) и запись в NocoDB.
AUTO_ANALYZE = os.environ.get("AUTO_ANALYZE", "1").strip() not in ("0", "false", "no", "")
AUTO_PRICES = os.environ.get("AUTO_PRICES", "1").strip() not in ("0", "false", "no", "")
NOCODB_WRITE = os.environ.get("NOCODB_WRITE", "1").strip() not in ("0", "false", "no", "")
STATUS_LABELS = {
    "ok": "текст извлечён", "ocr": "распознан", "empty": "текста нет", "skipped": "пропущен",
    "error": "ошибка", "archive": "архив", "pending": "ждёт обработки",
}


@app.get("/api/clients/{client_id}/docs")
def api_docs(client_id: str, folder: str):
    """Файлы конкурса со статусами обработки и последняя задача обработки."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    rows = db.doc_rows(client.id, folder)
    files = []
    for path in walk_docs(base):
        rel = path.relative_to(base).as_posix()
        r = rows.get(rel)
        is_archive = path.name.lower().endswith(ARCHIVE_EXT)
        status = r["status"] if r else "pending"
        files.append({
            "path": rel,
            "name": fix_path(rel),
            "size": path.stat().st_size,
            "kind": (r or {}).get("kind") or ("archive" if is_archive else ""),
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "pages": (r or {}).get("pages") or 0,
            "ocr_pages": (r or {}).get("ocr_pages") or 0,
            "chars": (r or {}).get("chars") or 0,
            "flags": (r or {}).get("flags") or "",
            "error": (r or {}).get("error") or "",
            "has_text": bool((r or {}).get("text_path")),
        })
    job = job_view(db.latest_job(client.id, folder))
    result = load_result(base)
    return {
        "folder": folder,
        "display_path": f"{TENDERS_DISPLAY}\\{client.tenders_folder}\\{folder}",
        "targets": DOC_SUBFOLDERS,
        # Конкурс уже разобран (0.9.0): новые файлы — изменения / разъяснения, по умолчанию в «Изменения <сегодня>».
        "has_analysis": bool(result and result.get("fields")),
        "changes_target": {"value": changes.UPLOAD_TARGET, "folder": changes.folder_name_for(_today())},
        "files": files,
        "job": job,
    }


def _today() -> str:
    from datetime import datetime
    return datetime.now(MSK).strftime("%d.%m.%Y")


def _changes_flow(client: Client, folder: str, base: Path) -> bool:
    """Новые документы — изменения уже разобранного конкурса (или анализ ещё идёт: разбор сравнит с его итогом)."""
    result = load_result(base)
    return bool(result and result.get("fields")) or bool(db.active_job(client.id, folder, "analyze"))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FolderError("слишком много файлов с одинаковым именем")


def _entity(client: Client, entity: str | None) -> str:
    """Юр. лицо, выбранное на странице (0.19.0): с его промтами (админ-панель) пойдут задача и её цепочка.
    Пустое или чужое клиенту — промты клиента."""
    return entity if entity and client.entity(entity) else ""


def _with_entity(params: dict | None, client: Client, entity: str | None) -> dict | None:
    e = _entity(client, entity)
    return {**(params or {}), "entity": e} if e else params


@app.post("/api/clients/{client_id}/docs/upload")
async def api_upload(client_id: str, folder: str, target: str = DOC_SUBFOLDERS[0],
                     files: list[UploadFile] = File(...), entity: str = ""):
    """Загрузка файлов в подпапку конкурса и постановка обработки в очередь."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    if target == changes.UPLOAD_TARGET:
        target = changes.folder_name_for(_today())         # «Изменения 04.11.2026» — дозагрузка в разобранный конкурс
    elif target not in DOC_SUBFOLDERS:
        raise FolderError(f"нельзя загружать в «{target}»")
    dest_dir = base / target
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for upload in files:
        name = safe_name(Path(upload.filename or "").name) or "файл"
        dest = _unique(dest_dir / name)
        with open(dest, "wb") as out:
            while chunk := await upload.read(1 << 20):
                out.write(chunk)
        saved.append(fix_path(dest.relative_to(base).as_posix()))
    # Загрузка через сайт — полный автомат: подготовка документов, затем анализ и запись в NocoDB. Конкурс уже
    # разобран — вместо повторного анализа разбор изменений: что поменялось, обновить данные, комментарии (0.9.0).
    is_changes = _changes_flow(client, folder, base)
    params = None
    if AUTO_ANALYZE:
        user = access.current_user() or {}
        params = {"changes": True, "by": user.get("login") or ""} if is_changes else {"analyze": True}
    job_id = db.enqueue(client.id, folder, "prepare", _with_entity(params, client, entity))
    return {"saved": saved, "job_id": job_id, "auto_analyze": AUTO_ANALYZE, "changes": is_changes,
            "target": target}


@app.post("/api/clients/{client_id}/docs/process")
def api_process(client_id: str, folder: str):
    """Запустить подготовку документов заново (обработаются только новые и изменённые файлы)."""
    client = get_client(client_id)
    folder_path(client, folder)
    return {"job_id": db.enqueue(client.id, folder, "prepare")}


@app.get("/api/clients/{client_id}/docs/text", response_class=PlainTextResponse)
def api_doc_text(client_id: str, folder: str, path: str):
    """Извлечённый текст файла — для просмотра в браузере."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    row = db.doc_rows(client.id, folder).get(path)
    if not row or not row.get("text_path"):
        raise HTTPException(404, "текста для этого файла нет")
    return PlainTextResponse(inside(base, row["text_path"]).read_text(encoding="utf-8"))


def _file_block(rel: str) -> str | None:
    """Чей раздел этот файл папки конкурса (0.16.0): результаты в AIExtract — анализа, цен или заявки; прайсы
    поставщиков — цен. Выключенный сотруднику раздел не отдаёт свои файлы и через «скачать файл»."""
    parts = [x for x in fix_path(rel).replace("\\", "/").split("/") if x]
    if not parts:
        return None
    top = parts[0].lower()
    if re.search(r"прайс", top):
        return "prices"
    if top != "aiextract":
        return None
    rest = "/".join(parts[1:]).lower()
    if rest.startswith(("заявка", "bid", "history/заявка", "поставщикам")):
        return "bid"
    if rest.startswith(("prices", "цены_поиск", "pricelists", "_converted/pricelists")):
        return "prices"
    return "analysis"


@app.get("/api/clients/{client_id}/docs/file")
def api_doc_file(client_id: str, folder: str, path: str):
    """Сам файл (скачать / открыть)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    blk = _file_block(path)
    if blk and not access.block_on(blk):
        raise HTTPException(403, "этот файл — из раздела, который для вашей учётной записи выключен (админ-панель)")
    target = inside(base, path)
    if not target.is_file():
        raise HTTPException(404, "файл не найден")
    return FileResponse(target, filename=fix_path(path).split("/")[-1])


@app.post("/api/clients/{client_id}/folders")
def api_create_folder(client_id: str, data: dict = Body(...)):
    """Новый конкурс: папка «<номер> <заказчик и предмет>» по шаблону «К---»."""
    client = get_client(client_id)
    skipped: list[str] = []
    name = create_folder(client, str(data.get("number", "")), str(data.get("title", "")), skipped)
    return {"folder": name, "template_skipped": skipped}


# ---------- анализ документов (ИИ) ----------

def _docs_fingerprint(client_id: str, folder: str) -> tuple[str, int]:
    """Отпечаток набора документов с текстом (как corpus_hash анализа, но без чтения файлов)."""
    rows = [r for r in db.doc_rows(client_id, folder).values()
            if r.get("status") in TEXT_STATUSES and r.get("text_path")]
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: x["rel_path"]):
        h.update(f"{r['rel_path']}\0{r.get('sha256') or ''}\n".encode("utf-8"))
    return h.hexdigest(), len(rows)


@app.get("/api/clients/{client_id}/analysis")
def api_analysis(client_id: str, folder: str):
    """Результат анализа (AIExtract/analysis.json) и последняя задача анализа / записи в NocoDB."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    result = load_result(base)
    fingerprint, docs_with_text = _docs_fingerprint(client.id, folder)
    job = job_view(db.latest_job(client.id, folder, ("analyze", "noco", "changes", "noco_changes")))
    if result and result.get("usage"):
        result["usage"].pop("calls", None)  # подробный журнал вызовов — только в файле
    return {
        "result": result,
        "job": job,
        "docs_with_text": docs_with_text,
        "stale": bool(result) and result.get("corpus_hash") != fingerprint,
        # Изменения документации (0.9.0): журнал разборов и документы, появившиеся после анализа.
        "changes": list(reversed(changes.load_log(base)["sets"])) if result else [],
        "new_docs": changes.new_doc_names(client.id, folder, result),
        "changes_active": bool(db.active_job(client.id, folder, "changes")),
        "changes_write_active": bool(db.active_job(client.id, folder, "noco_changes")),
        "prepare_active": bool(db.active_job(client.id, folder, "prepare")),
        "auto_analyze": AUTO_ANALYZE,
        "auto_prices": AUTO_PRICES,
        "nocodb_write": NOCODB_WRITE,
        "models": ANALYSIS_MODELS,
        "default_model": CLAUDE_MODEL,
    }


@app.post("/api/clients/{client_id}/analysis/run")
def api_analysis_run(client_id: str, folder: str, model: str | None = None, entity: str = ""):
    """Запустить анализ документов (ИИ) — после него результат записывается в NocoDB."""
    client = get_client(client_id)
    folder_path(client, folder)
    if db.active_job(client.id, folder, "prices"):
        raise HTTPException(409, "идёт поиск цен по этому конкурсу — дождитесь его окончания")
    running = db.active_job(client.id, folder, "analyze")
    if running:       # уже идёт (запущен из другой вкладки или раньше) — второй анализ не ставим
        return {"job_id": running["id"], "already": True}
    if not access.is_admin():
        model = None          # сотрудник модель не выбирает — настройка сайта
    params = {"model": model} if model in ANALYSIS_MODELS else {}
    params = _with_entity(params, client, entity) or {}
    if db.active_job(client.id, folder, "prepare"):
        # Документы ещё готовятся — анализ встанет в очередь сразу после подготовки.
        return {"job_id": db.enqueue(client.id, folder, "prepare", {"analyze": True, **params}), "after_prepare": True}
    return {"job_id": db.enqueue(client.id, folder, "analyze", params)}


@app.post("/api/clients/{client_id}/analysis/changes/run")
def api_changes_run(client_id: str, folder: str, entity: str = ""):
    """Разобрать документы, появившиеся после анализа: что изменилось, обновить данные, комментарии (0.9.0)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    result = load_result(base)
    if not (result and result.get("fields")):
        raise HTTPException(400, "анализа ещё нет — запустите анализ, новые документы войдут в него")
    running = db.active_job(client.id, folder, "changes")
    if running:
        return {"job_id": running["id"], "already": True}
    user = access.current_user() or {}
    # Сначала подготовка (обрабатывает только новые и изменённые файлы — секунды): файлы могли положить в папку
    # вручную, мимо сайта; разбор изменений встанет следом.
    params = _with_entity({"changes": True, "by": user.get("login") or ""}, client, entity)
    return {"job_id": db.enqueue(client.id, folder, "prepare", params), "after_prepare": True}


@app.post("/api/clients/{client_id}/analysis/changes/write")
def api_changes_write(client_id: str, folder: str, change: str):
    """Записать изменения (один разбор) в NocoDB: только изменённые поля и строки спецификации."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    user = access.current_user() or {}
    try:
        changes.request_write(base, change, user.get("login") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": db.enqueue(client.id, folder, "noco_changes")}


@app.post("/api/clients/{client_id}/analysis/save")
def api_analysis_save(client_id: str, folder: str, force: bool = False):
    """Записать готовый анализ в NocoDB (force — несмотря на найденную запись-двойник / чужую запись)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    if not load_result(base):
        raise HTTPException(400, "анализа этого конкурса ещё нет")
    running = db.active_job(client.id, folder, "noco")
    if running and running["status"] == "running":
        return {"job_id": running["id"], "already": True}
    return {"job_id": db.enqueue(client.id, folder, "noco", {"force": bool(force)})}


# ---------- обработчик: что выполняется и что ждёт ----------

@app.get("/api/clients/{client_id}/jobs")
def api_jobs(client_id: str):
    """Состояние обработчика: жива ли каждая очередь, что она выполняет, что ждёт; недавно законченные задачи."""
    client = get_client(client_id)
    now = time.time()
    lanes = db.lanes_state()
    active = db.active_jobs()
    out_lanes = []
    for lane in ("docs", "nocodb", "prices", "pricelists", "bid"):
        beat = lanes.get(lane) or {}
        running = next((a for a in active if a["status"] == "running" and db.LANES.get(a["kind"], "docs") == lane), None)
        out_lanes.append({"lane": lane, "title": db.LANE_TITLES[lane],
                          "alive": bool(beat.get("at") and now - beat["at"] < ALIVE_SECONDS),
                          "seen": round(now - beat["at"]) if beat.get("at") else None,
                          "job": job_view(running, lanes, active)})
    mine = [a for a in active if a["client_id"] == client.id]
    return {"now": now, "lanes": out_lanes,
            "active": [job_view(a, lanes, active) for a in mine],
            "recent": [job_view(r, lanes, active) for r in db.recent_jobs(client.id)]}


def _own_job(client: Client, job_id: int) -> dict:
    job = db.get_job(job_id)
    if not job or job["client_id"] != client.id:
        raise HTTPException(404, "задача не найдена")
    return job


STOP_TEXT = {"canceled": "задача отменена", "paused": "задача приостановлена",
             "stopping": "задача остановится на ближайшем шаге (обычно за несколько секунд)",
             "finished": "задача уже закончилась", "missing": "задача не найдена"}


@app.post("/api/clients/{client_id}/jobs/{job_id}/cancel")
def api_job_cancel(client_id: str, job_id: int):
    """Отменить задачу: ждущую — сразу, выполняющуюся — на ближайшем шаге."""
    client = get_client(client_id)
    _own_job(client, job_id)
    res = db.request_stop(job_id, "cancel")
    return {"result": res, "text": STOP_TEXT[res], "job": job_view(db.get_job(job_id))}


@app.post("/api/clients/{client_id}/jobs/{job_id}/pause")
def api_job_pause(client_id: str, job_id: int):
    """Приостановить задачу (её очередь берёт следующую). «Продолжить» — снова в очередь, на прежнее место."""
    client = get_client(client_id)
    _own_job(client, job_id)
    res = db.request_stop(job_id, "pause")
    return {"result": res, "text": STOP_TEXT[res], "job": job_view(db.get_job(job_id))}


@app.post("/api/clients/{client_id}/jobs/{job_id}/resume")
def api_job_resume(client_id: str, job_id: int):
    """Продолжить приостановленную задачу или передумать останавливать выполняющуюся."""
    client = get_client(client_id)
    job = _own_job(client, job_id)
    if job["status"] == "paused":
        # Пока задача стояла на паузе, такую же могли запустить заново кнопкой — тогда вторую не ставим.
        twin = db.active_job(client.id, job["folder"], job["kind"])
        if twin and twin["id"] != job_id:
            db.request_stop(job_id, "cancel")
            return {"result": "replaced", "text": "такая задача уже стоит в очереди или идёт — приостановленная отменена",
                    "job": job_view(db.get_job(twin["id"]))}
    res = db.resume(job_id)
    text = {"queued": "задача снова в очереди", "running": "задача продолжает работу",
            "finished": "задача уже закончилась", "missing": "задача не найдена"}[res]
    return {"result": res, "text": text, "job": job_view(db.get_job(job_id))}


# ---------- поиск цен ----------

@app.get("/api/clients/{client_id}/prices")
def api_prices(client_id: str, folder: str):
    """Отчёт поиска цен (AIExtract/prices.json), последняя задача поиска, оценка стоимости следующего поиска."""
    from .prices import run as prices
    client = get_client(client_id)
    base = folder_path(client, folder)
    result = load_result(base)
    report = prices.load_report(base)
    job = job_view(db.latest_job(client.id, folder, ("prices", "noco_prices")))
    if report:
        for g in report.get("groups") or []:
            cands = g.pop("candidates", []) or []
            g["checked"] = sum(1 for c in cands if c.get("opened"))
            g["found"] = len(cands)
    has_analysis = bool(result and result.get("positions"))
    # Без анализа на сайте позиции — из спецификации NocoDB (0.14.0): сколько их — по прочитанной «Всеми ценами»
    # спецификации (None — ещё не прочитана; кнопка всё равно доступна, сайт проверит при запуске).
    spec = None if has_analysis else price_summary.cached_spec(client.id, folder)
    spec_positions = None if spec is None else prices.positions_from_spec(spec)
    if has_analysis:
        positions = result["positions"]
    else:
        positions = (report or {}).get("positions") or spec_positions or []
    return {
        "report": report,
        "job": job,
        "has_analysis": has_analysis,
        "noco_positions": None if spec_positions is None else len(spec_positions),
        "positions": [{k: r.get(k) for k in ("№", "Наименование позиции", "Марка/модель", "Кол-во", "Ед. изм.",
                                              "Стоимость НМЦ")} for r in positions],
        "stale": bool(report and result and (result.get("analyzed_at") or 0) > (report.get("at") or 0)),
        "analysis_active": bool(db.active_job(client.id, folder, "analyze") or db.active_job(client.id, folder, "prepare")),
        "analysis_pending": _analysis_pending(client.id, folder),
        "noco_active": bool(db.active_job(client.id, folder, "noco") or db.active_job(client.id, folder, "noco_prices")),
        "estimate": prices.estimate({"positions": positions}) if positions else None,
        "auto_prices": AUTO_PRICES,
        "settings": prices.settings(),
    }


def _analysis_pending(client_id: str, folder: str) -> bool:
    """Позиции вот-вот изменятся: идёт анализ или подготовка, после которой будет анализ."""
    if db.active_job(client_id, folder, "analyze"):
        return True
    prep = db.active_job(client_id, folder, "prepare")
    return bool(prep and db.job_params(prep).get("analyze"))


@app.post("/api/clients/{client_id}/prices/run")
async def api_prices_run(client_id: str, folder: str, urgent: bool = False, abroad: bool = True, engine: str = "",
                         entity: str = ""):
    """Запустить поиск цен — в любой момент (0.14.0, пользователь 04.11.2026: «искать цены всегда, даже если они были
    извлечены ранее»). Позиции — из анализа на сайте, а без него — из спецификации NocoDB. Идёт анализ — поиск
    запустится сам сразу после него (AUTO_PRICES). engine — deepseek / yandex / both (чем искать по России)."""
    from .prices import run as prices
    client = get_client(client_id)
    base = folder_path(client, folder)
    running = db.active_job(client.id, folder, "prices")
    if running:       # уже идёт (из другой вкладки или сам после анализа) — второй поиск не ставим
        return {"job_id": running["id"], "already": True}
    if _analysis_pending(client.id, folder):
        if AUTO_PRICES:
            return {"after_analysis": True}
        raise HTTPException(409, "идёт анализ документов — позиции ещё меняются; поиск цен можно запустить после него")
    result = load_result(base)
    if not (result and result.get("positions")):
        spec, err = await price_summary.read_spec(client, folder)
        if not prices.positions_from_spec(spec):
            raise HTTPException(400, "позиций нет: анализа документов на сайте нет, а в спецификации NocoDB этого "
                                     "конкурса нет строк с наименованием" + (f" ({err})" if err else ""))
    params = {"urgent": bool(urgent), "abroad": bool(abroad)}
    if not (result and result.get("positions")):
        params["source"] = "nocodb"                           # для журнала и страницы; позиции обработчик прочитает сам
    if engine in prices.ENGINES and access.is_admin():      # сотрудник поисковик не выбирает
        params["engine"] = engine
    return {"job_id": db.enqueue(client.id, folder, "prices", _with_entity(params, client, entity))}


# ---------- прайсы, КП и счета поставщиков (0.8.0, app/pricelists.py) ----------

def _pricelist_job_views(client: Client, folder: str) -> dict:
    return {"job": job_view(db.latest_job(client.id, folder, ("pricelist",))),
            "write_job": job_view(db.latest_job(client.id, folder, ("noco_purch",)))}


@app.get("/api/clients/{client_id}/pricelists")
def api_pricelists(client_id: str, folder: str):
    """Загруженные прайсы конкурса: разбор, найденные позиции и цены, запись в NocoDB."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    result = load_result(base) or {}
    return {"files": pricelists.load(base)["files"], "has_analysis": bool(result.get("positions")),
            "positions": len(result.get("positions") or []), "extensions": sorted(pricelists.EXTENSIONS),
            "model": pricelists.MODEL, **_pricelist_job_views(client, folder)}


@app.post("/api/clients/{client_id}/pricelists/upload")
async def api_pricelists_upload(client_id: str, folder: str, files: list[UploadFile] = File(...), entity: str = ""):
    """Прайс / КП / счёт поставщика из чата: в «Прайсы, счета, КП» конкурса и в очередь на разбор."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    dest_dir = pricelists.price_dir(base)
    saved = []
    for upload in files:
        name = safe_name(Path(upload.filename or "").name) or "прайс"
        if Path(name).suffix.lower() not in pricelists.EXTENSIONS:
            raise HTTPException(400, f"«{name}»: такие файлы не читаются — нужен Excel, Word, PDF или картинка")
        dest = _unique(dest_dir / name)
        size = 0
        with open(dest, "wb") as out:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > pricelists.MAX_FILE_MB * 1024 * 1024:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(400, f"«{name}»: файл больше {pricelists.MAX_FILE_MB} МБ")
                out.write(chunk)
        saved.append(dest.relative_to(base).as_posix())
    user = access.current_user()
    added = pricelists.add_files(base, saved, (user or {}).get("login", ""))
    job_id = db.enqueue(client.id, folder, "pricelist", _with_entity(None, client, entity))
    return {"files": added, "job_id": job_id}


@app.post("/api/clients/{client_id}/pricelists/write")
def api_pricelists_write(client_id: str, folder: str, data: dict = Body(...)):
    """«Записать в спецификацию»: отмеченные позиции прайса — в колонки «Отдел закупок» спецификации NocoDB."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    user = access.current_user()
    positions = [str(x) for x in (data.get("positions") or [])]
    try:
        entry = pricelists.request_write(base, str(data.get("file") or ""), positions, (user or {}).get("login", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    job_id = db.enqueue(client.id, folder, "noco_purch")
    return {"file": entry, "job_id": job_id}


@app.post("/api/clients/{client_id}/pricelists/remove")
def api_pricelists_remove(client_id: str, folder: str, data: dict = Body(...)):
    """Убрать прайс из списка (файл остаётся в папке конкурса)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    try:
        pricelists.remove(base, str(data.get("file") or ""), (access.current_user() or {}).get("login", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.get("/api/clients/{client_id}/prices/summary")
async def api_prices_summary(client_id: str, folder: str, record: int | None = None):
    """Общая таблица цен по позициям: НМЦ, интернет (проверенные и из выдачи — «не проверено»), поставщики (прайсы
    и «Отдел закупок»), цены, уже записанные в спецификацию NocoDB (агент, рынки, рыночная цена, доставка), прошлые
    конкурсы с тем же артикулом (своя база цен), лучшая цена. Анализа на сайте нет — позиции из спецификации NocoDB."""
    import asyncio
    client = get_client(client_id)
    base = folder_path(client, folder)
    spec, noco_error = await price_summary.read_spec(client, folder, record)
    hist, hist_info = await asyncio.to_thread(price_summary.history_index, client)
    data = await asyncio.to_thread(price_summary.build, base, spec, hist)
    return {**data, "noco_error": noco_error, "noco_read": bool(spec), "history": hist_info}


# ---------- подготовка заявки (0.15.0, app/bid) ----------
# Пользователь 05.11.2026: «кнопка подготовка документов должна быть на сайте далее идет полная подготовка, с минимум
# вопросов». Одна кнопка — весь пакет; от пользователя — только цена (по желанию) и недостающие сведения.

BID_FILE_DIRS = ("AIExtract/Заявка/", "AIExtract/Поставщикам")


def _bid_view(report: dict | None, base: Path) -> dict | None:
    """Отчёт подготовки заявки для страницы — без правок модели и служебных подробностей."""
    if not report:
        return None
    from .bid import forms as bid_forms
    from .bid.run import BID_DIR
    plan = report.get("plan") or {}
    files = []
    root = base / BID_DIR
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.startswith("~$"):
                rel = p.relative_to(base).as_posix()
                files.append({"path": rel, "name": p.relative_to(root).as_posix(), "size": p.stat().st_size,
                              "marks": bid_forms.count_marks(p) if p.suffix.lower() == ".docx" else 0})
    keys = ("structure", "documents", "checks", "errors", "gaps", "summary", "participant", "price", "price_values",
            "supplier_files", "noco", "at", "at_text", "model", "usd", "seconds", "backup", "reapplied", "positions_source",
            "skipped_docs")
    view = {k: report.get(k) for k in keys}
    view["requirements"] = plan.get("requirements") or []
    view["notes"] = plan.get("notes") or []
    view["forms"] = [{k: f.get(k) for k in ("doc_n", "title", "source", "file", "marks", "error", "problems")}
                     for f in (report.get("forms") or []) + (report.get("inventory_forms") or [])]
    view["files"] = files
    view["usage_totals"] = (report.get("usage") or {}).get("totals") if isinstance(report.get("usage"), dict) else None
    return view


@app.get("/api/clients/{client_id}/bid")
def api_bid(client_id: str, folder: str):
    """Подготовленная заявка (AIExtract/bid.json, папка AIExtract/Заявка), задача подготовки, участники на выбор."""
    from .bid.run import BID_MODEL, BID_MODELS, load_report
    client = get_client(client_id)
    base = folder_path(client, folder)
    result = load_result(base)
    _fp, docs_with_text = _docs_fingerprint(client.id, folder)
    report = load_report(base)
    latest = db.latest_job(client.id, folder, ("bid",))
    job = job_view(latest)
    if job:
        job["reapply"] = bool(db.job_params(latest).get("reapply"))     # пересчёт цены — без модели
    return {
        "report": _bid_view(report, base),
        "job": job,
        "noco_job": job_view(db.latest_job(client.id, folder, ("noco_bid",))),
        "entities": [{"id": e.id, "name": e.name, "inn": e.inn, "materials": bool(e.materials)}
                     for e in client.legal_entities],
        "entity": (report or {}).get("entity") or (client.legal_entities[0].id if client.legal_entities else None),
        "docs_with_text": docs_with_text,
        "has_analysis": bool(result),
        "positions": len((result or {}).get("positions") or []),
        "analysis_pending": _analysis_pending(client.id, folder),
        "stale": bool(report and result and float(result.get("analyzed_at") or 0) > float(report.get("at") or 0)),
        "models": BID_MODELS if access.is_admin() else [],
        "default_model": BID_MODEL,
    }


@app.post("/api/clients/{client_id}/bid/run")
def api_bid_run(client_id: str, folder: str, entity: str | None = None, model: str | None = None):
    """Подготовить документы заявки: модель читает документацию и сведения участника, сайт собирает пакет."""
    client = get_client(client_id)
    folder_path(client, folder)
    running = db.active_job(client.id, folder, "bid")
    if running:
        return {"job_id": running["id"], "already": True}
    if _analysis_pending(client.id, folder) or db.active_job(client.id, folder, "prepare"):
        raise HTTPException(409, "документы конкурса ещё обрабатываются (идёт подготовка или анализ) — "
                                 "запустите подготовку заявки после этого")
    _fp, docs_with_text = _docs_fingerprint(client.id, folder)
    if not docs_with_text:
        raise HTTPException(400, "нет документов с извлечённым текстом — сначала загрузите и обработайте документацию")
    if entity and not client.entity(entity):
        raise HTTPException(400, "такого участника у клиента нет")
    params = {"entity": entity} if entity else {}
    from .bid.run import BID_MODELS
    if model in BID_MODELS and access.is_admin():      # сотрудник модель не выбирает — настройка сайта
        params["model"] = model
    return {"job_id": db.enqueue(client.id, folder, "bid", params)}


@app.post("/api/clients/{client_id}/bid/price")
def api_bid_price(client_id: str, folder: str, body: dict = Body(...)):
    """Цена заявки: {"mode": "percent", "value": 5} — снижение от НМЦ, %; {"mode": "total", "value": 1000000,
    "vat": "with"|"without"} — общая сумма; {"mode": "none"} — убрать. Формы пересчитываются без модели."""
    from .bid.run import load_report, set_price
    client = get_client(client_id)
    base = folder_path(client, folder)
    report = load_report(base)
    if not report or not report.get("documents"):
        raise HTTPException(400, "документы заявки ещё не готовились — нажмите «Подготовить документы заявки»")
    if db.active_job(client.id, folder, "bid"):
        raise HTTPException(409, "идёт подготовка заявки — цену можно задать после неё")
    mode = str(body.get("mode") or "")
    if mode == "none":
        price = None
    elif mode in ("percent", "total"):
        try:
            value = float(str(body.get("value")).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            raise HTTPException(400, "цена: нужно число")
        if mode == "percent" and not (0 <= value < 100):
            raise HTTPException(400, "снижение — от 0 до 100 %")
        if mode == "total" and value <= 0:
            raise HTTPException(400, "сумма должна быть больше нуля")
        price = {"mode": mode, "value": value}
        if mode == "total":
            price["vat"] = "without" if body.get("vat") == "without" else "with"
    else:
        raise HTTPException(400, "неизвестный способ задать цену")
    set_price(base, price)
    return {"job_id": db.enqueue(client.id, folder, "bid", {"reapply": True})}


@app.post("/api/clients/{client_id}/bid/noco")
def api_bid_noco(client_id: str, folder: str):
    """Записать перечень документов и формы заявки в NocoDB — только по этой кнопке."""
    from .bid.run import load_report
    client = get_client(client_id)
    base = folder_path(client, folder)
    if not (load_report(base) or {}).get("documents"):
        raise HTTPException(400, "документы заявки ещё не готовились")
    running = db.active_job(client.id, folder, "noco_bid")
    if running:
        return {"job_id": running["id"], "already": True}
    return {"job_id": db.enqueue(client.id, folder, "noco_bid")}


@app.get("/api/clients/{client_id}/bid/file")
def api_bid_file(client_id: str, folder: str, path: str):
    """Файл пакета заявки или файл поставщику (скачать)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    if not path.startswith(BID_FILE_DIRS):
        raise HTTPException(404, "файл не найден")
    target = inside(base, path)
    if not target.is_file():
        raise HTTPException(404, "файл не найден")
    return FileResponse(target, filename=target.name)


@app.get("/api/clients/{client_id}/bid/zip")
def api_bid_zip(client_id: str, folder: str):
    """Весь пакет заявки одним архивом (папки частей сохраняются)."""
    import io
    import zipfile
    from urllib.parse import quote
    from .bid.run import BID_DIR
    client = get_client(client_id)
    base = folder_path(client, folder)
    root = base / BID_DIR
    if not root.is_dir():
        raise HTTPException(404, "пакета заявки ещё нет")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.startswith("~$"):
                z.write(p, p.relative_to(root).as_posix())
    from .tenders import norm_number
    name = f"Заявка {norm_number(folder) or folder}.zip"
    return StreamingResponse(iter([buf.getvalue()]), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------- чат по документации ----------
# Личный чат (0.20.0): сотрудник видит только свои вопросы и ответы; модель получает только его историю.

def _chat_owner() -> tuple[str, bool]:
    """(логин вошедшего, видит ли он сообщения без автора — до 0.20.0: администратор сайта; вход выключен — все)."""
    user = access.current_user()
    if not user:
        return "", True
    return user["login"], user["role"] == "admin"


@app.get("/api/clients/{client_id}/chat")
def api_chat_history(client_id: str, folder: str):
    """История чата конкурса этого сотрудника и настройки (модели на выбор)."""
    client = get_client(client_id)
    folder_path(client, folder)
    owner, unowned = _chat_owner()
    return {"messages": db.chat_history(client.id, folder, owner=owner, with_unowned=unowned), "personal": bool(owner),
            "models": chat.CHAT_MODELS, "default_model": chat.CHAT_MODEL, "cache_ttl": chat.CHAT_CACHE_TTL}


@app.get("/api/clients/{client_id}/chat/status")
def api_chat_status(client_id: str, folder: str, model: str | None = None, entity: str = ""):
    """Готов ли чат и сколько стоит следующий вопрос: документы в кэше модели (до какого времени) или нет."""
    client = get_client(client_id)
    folder_path(client, folder)
    return chat.cache_status(client.id, folder, model if access.is_admin() else None, _entity(client, entity))


@app.post("/api/clients/{client_id}/chat")
def api_chat_ask(client_id: str, folder: str, data: dict = Body(...), entity: str = ""):
    """Вопрос по документации. Ответ идёт по мере готовности: строки JSON ({"t":"text"…}, в конце {"t":"done"…})."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    model = data.get("model") if access.is_admin() else None   # сотрудник модель чата не выбирает
    owner, unowned = _chat_owner()
    gen = chat.ask(client.id, folder, base, str(data.get("question", "")), model, _entity(client, entity),
                   owner=owner, with_unowned=unowned)
    return StreamingResponse(gen, media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@app.post("/api/clients/{client_id}/chat/clear")
def api_chat_clear(client_id: str, folder: str):
    client = get_client(client_id)
    folder_path(client, folder)
    owner, unowned = _chat_owner()
    return {"deleted": db.chat_clear(client.id, folder, owner=owner, with_unowned=unowned)}
