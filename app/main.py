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

from . import chat, db
from .config import Client, load_clients
from .analysis.corpus import TEXT_STATUSES
from .analysis.run import ANALYSIS_MODELS, CLAUDE_MODEL, load_result
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
NOT_SPECIFIED = "Не указано в документах"

# id таблицы NocoDB в ссылке вида https://app.nocodb.com/<ws>/<base>/<таблица>/<вид>
TABLE_ID_RE = re.compile(r"/(m[a-z0-9]{8,})(?:/|$|\?)")

app = FastAPI(title="SupplyBro", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# Админ-панель учётных записей (/admin) — только при ADMIN_PANEL=1 в .env; по умолчанию выключена,
# вход на сам сайт пока не требуется (app/admin.py, app/auth.py).
ADMIN_PANEL = os.environ.get("ADMIN_PANEL", "0").strip().lower() in ("1", "true", "yes", "on")
if ADMIN_PANEL:
    from . import admin
    app.include_router(admin.router)

# Страница сотрудника (/user) — та же страница, что «/» (страница администратора), но без выбора модели анализа,
# модели чата и поисковика цен. Только при USER_PAGE=1 в .env; по умолчанию выключена (решение пользователя
# 29.10.2026 «сгенерируй, но пока не подключай»). Вход по логину ещё не требуется — при подключении входа сотрудникам
# (не admin) открывать /user, а сервер должен сам не принимать от них model / engine.
USER_PAGE = os.environ.get("USER_PAGE", "0").strip().lower() in ("1", "true", "yes", "on")

# Страница входа (/login) — проверка логина и пароля сотрудников (те же учётные записи, что в админ-панели).
# Только при LOGIN_PAGE=1 в .env; по умолчанию выключена (решение пользователя 31.10.2026 «пока не подключай, нужно
# просто проверить»). Сам сайт вход пока не требует (app/login.py).
LOGIN_PAGE = os.environ.get("LOGIN_PAGE", "0").strip().lower() in ("1", "true", "yes", "on")
if LOGIN_PAGE:
    from . import login
    app.include_router(login.router)


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


def job_view(job: dict | None, lanes: dict | None = None, active: list[dict] | None = None) -> dict | None:
    """Задача для страницы: сколько ждёт / идёт, когда было последнее действие, в какой очереди и кого ждёт,
    жив ли обработчик."""
    if not job:
        return None
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
            j["waiting_for"] = {"folder": running["folder"], "kind_title": db.KIND_TITLES.get(running["kind"], running["kind"]),
                                "step": running.get("step") or "", "elapsed": round(now - (running.get("started_at") or now))}
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

@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


if USER_PAGE:
    @app.get("/user")
    def user_page():
        """Страница сотрудника: та же index.html с пометкой в <body> (app.js прячет выбор моделей и их не отправляет)."""
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
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
        wanted = [F_INTERNAL, F_NUMBER, F_TITLE, F_STAGE, F_DEADLINE, F_SPEC_LINK, F_DOCS]
        if has_participant_col:
            wanted.append(client.nocodb.participant_column)
        fields = ["Id"] + [f for f in wanted if f in titles]
        rows = await noco.records(reg_id, fields=fields, sort="-Id")
    except NocoError as exc:
        noco_error = str(exc)  # папки всё равно показываем

    folders = list_folders(client)
    by_number = {}
    for f in folders:
        if f["number"] and f["number"] not in by_number:
            by_number[f["number"]] = f["name"]
    try:
        client_root(client)
        folders_error = ""
    except FolderError as exc:
        folders_error = str(exc)

    tenders, used_folders, numbers = [], set(), set()
    for r in rows:
        internal, number = clean(r.get(F_INTERNAL)), clean(r.get(F_NUMBER))
        if not internal and not number:
            continue  # пустые строки реестра
        norm = norm_number(internal)
        if norm:
            numbers.add(norm)
        folder = by_number.get(norm) if norm else None
        if folder:
            used_folders.add(folder)
        parts = participants_of(r, client)
        if entity and parts and entity not in parts:
            continue  # конкурс другого юр. лица
        tenders.append({
            "id": str(r["Id"]),
            "source": "nocodb",
            "internal": internal,
            "number": number,
            "title": first_line(r.get(F_TITLE)),
            "stage": first_line(r.get(F_STAGE)),
            "deadline": first_line(r.get(F_DEADLINE)),
            "participants": parts,
            "has_spec": bool(spec_table_id(r.get(F_SPEC_LINK))),
            "folder": folder,
        })
    for f in folders:
        if f["number"]:
            numbers.add(f["number"])
        if f["name"] in used_folders:
            continue
        title = f["name"][len(f["number"] or ""):].strip() if f["number"] else f["name"]
        tenders.append({
            "id": "f:" + f["name"], "source": "folder", "internal": f["number"] or "", "number": "",
            "title": title, "stage": "", "deadline": "", "participants": [], "has_spec": False,
            "folder": f["name"],
        })
    return {"participant_column": client.nocodb.participant_column,
            "participant_column_exists": has_participant_col,
            "noco_error": noco_error,
            "folders_error": folders_error,
            "next_number": next_number(numbers),
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
    return {
        "folder": folder,
        "display_path": f"{TENDERS_DISPLAY}\\{client.tenders_folder}\\{folder}",
        "targets": DOC_SUBFOLDERS,
        "files": files,
        "job": job,
    }


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FolderError("слишком много файлов с одинаковым именем")


@app.post("/api/clients/{client_id}/docs/upload")
async def api_upload(client_id: str, folder: str, target: str = DOC_SUBFOLDERS[0],
                     files: list[UploadFile] = File(...)):
    """Загрузка файлов в подпапку конкурса и постановка обработки в очередь."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    if target not in DOC_SUBFOLDERS:
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
    # Загрузка через сайт — полный автомат: подготовка документов, затем анализ и запись в NocoDB.
    job_id = db.enqueue(client.id, folder, "prepare", {"analyze": True} if AUTO_ANALYZE else None)
    return {"saved": saved, "job_id": job_id, "auto_analyze": AUTO_ANALYZE}


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


@app.get("/api/clients/{client_id}/docs/file")
def api_doc_file(client_id: str, folder: str, path: str):
    """Сам файл (скачать / открыть)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
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
    job = job_view(db.latest_job(client.id, folder, ("analyze", "noco")))
    if result and result.get("usage"):
        result["usage"].pop("calls", None)  # подробный журнал вызовов — только в файле
    return {
        "result": result,
        "job": job,
        "docs_with_text": docs_with_text,
        "stale": bool(result) and result.get("corpus_hash") != fingerprint,
        "prepare_active": bool(db.active_job(client.id, folder, "prepare")),
        "auto_analyze": AUTO_ANALYZE,
        "auto_prices": AUTO_PRICES,
        "nocodb_write": NOCODB_WRITE,
        "models": ANALYSIS_MODELS,
        "default_model": CLAUDE_MODEL,
    }


@app.post("/api/clients/{client_id}/analysis/run")
def api_analysis_run(client_id: str, folder: str, model: str | None = None):
    """Запустить анализ документов (ИИ) — после него результат записывается в NocoDB."""
    client = get_client(client_id)
    folder_path(client, folder)
    if db.active_job(client.id, folder, "prices"):
        raise HTTPException(409, "идёт поиск цен по этому конкурсу — дождитесь его окончания")
    running = db.active_job(client.id, folder, "analyze")
    if running:       # уже идёт (запущен из другой вкладки или раньше) — второй анализ не ставим
        return {"job_id": running["id"], "already": True}
    params = {"model": model} if model in ANALYSIS_MODELS else {}
    if db.active_job(client.id, folder, "prepare"):
        # Документы ещё готовятся — анализ встанет в очередь сразу после подготовки.
        return {"job_id": db.enqueue(client.id, folder, "prepare", {"analyze": True, **params}), "after_prepare": True}
    return {"job_id": db.enqueue(client.id, folder, "analyze", params)}


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
    for lane in ("docs", "nocodb", "prices"):
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
    return {
        "report": report,
        "job": job,
        "has_analysis": bool(result and result.get("positions")),
        "positions": [{k: r.get(k) for k in ("№", "Наименование позиции", "Марка/модель", "Кол-во", "Ед. изм.",
                                              "Стоимость НМЦ")} for r in (result or {}).get("positions") or []],
        "stale": bool(report and result and (result.get("analyzed_at") or 0) > (report.get("at") or 0)),
        "analysis_active": bool(db.active_job(client.id, folder, "analyze") or db.active_job(client.id, folder, "prepare")),
        "noco_active": bool(db.active_job(client.id, folder, "noco") or db.active_job(client.id, folder, "noco_prices")),
        "estimate": prices.estimate(result) if result else None,
        "auto_prices": AUTO_PRICES,
        "settings": prices.settings(),
    }


@app.post("/api/clients/{client_id}/prices/run")
def api_prices_run(client_id: str, folder: str, urgent: bool = False, abroad: bool = True, engine: str = ""):
    """Запустить поиск цен по позициям анализа. engine — deepseek / yandex / both (чем искать по России)."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    result = load_result(base)
    if not (result and result.get("positions")):
        raise HTTPException(400, "сначала нужен анализ документов — цены ищутся по его позициям")
    if db.active_job(client.id, folder, "analyze") or db.active_job(client.id, folder, "prepare"):
        raise HTTPException(409, "идёт подготовка или анализ документов — поиск цен можно запустить после них")
    running = db.active_job(client.id, folder, "prices")
    if running:       # уже идёт (из другой вкладки или сам после анализа) — второй поиск не ставим
        return {"job_id": running["id"], "already": True}
    from .prices import run as prices
    params = {"urgent": bool(urgent), "abroad": bool(abroad)}
    if engine in prices.ENGINES:
        params["engine"] = engine
    return {"job_id": db.enqueue(client.id, folder, "prices", params)}


# ---------- чат по документации ----------

@app.get("/api/clients/{client_id}/chat")
def api_chat_history(client_id: str, folder: str):
    """История чата конкурса и настройки (модели на выбор)."""
    client = get_client(client_id)
    folder_path(client, folder)
    return {"messages": db.chat_history(client.id, folder), "models": chat.CHAT_MODELS, "default_model": chat.CHAT_MODEL,
            "cache_ttl": chat.CHAT_CACHE_TTL}


@app.get("/api/clients/{client_id}/chat/status")
def api_chat_status(client_id: str, folder: str, model: str | None = None):
    """Готов ли чат и сколько стоит следующий вопрос: документы в кэше модели (до какого времени) или нет."""
    client = get_client(client_id)
    folder_path(client, folder)
    return chat.cache_status(client.id, folder, model)


@app.post("/api/clients/{client_id}/chat")
def api_chat_ask(client_id: str, folder: str, data: dict = Body(...)):
    """Вопрос по документации. Ответ идёт по мере готовности: строки JSON ({"t":"text"…}, в конце {"t":"done"…})."""
    client = get_client(client_id)
    base = folder_path(client, folder)
    gen = chat.ask(client.id, folder, base, str(data.get("question", "")), data.get("model"))
    return StreamingResponse(gen, media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@app.post("/api/clients/{client_id}/chat/clear")
def api_chat_clear(client_id: str, folder: str):
    client = get_client(client_id)
    folder_path(client, folder)
    return {"deleted": db.chat_clear(client.id, folder)}
