"""
Вход на сайт и права сотрудников (0.7.0): кто вошёл, к каким клиентам у него доступ, что разрешает его роль.

Включается LOGIN_REQUIRED=1 в .env (решение пользователя 03.11.2026 «делай вход для админа и для пользователей»;
с 0.16.0 это и значение по умолчанию: настройки нет совсем — вход всё равно обязателен). Тогда без входа сайт не
открывается: страницы ведут на /login, адреса /api/... отвечают 401. Админ-панель (/admin), страница сотрудника (/user)
и страница входа (/login) при этом включены всегда, независимо от ADMIN_PANEL / USER_PAGE / LOGIN_PAGE.
LOGIN_REQUIRED=0 — сайт как раньше, без входа (только для своего компьютера, не для сервера).

0.16.0 — сайт открывается в интернет (сервер, Caddy с HTTPS): без входа открыты ТОЛЬКО страница входа (/login, её
оформление и скрипт), её адреса /api/auth/... и /health (0.17.0 — и страница-приветствие /start: гость на «/» попадает
туда, ссылки на страницы с параметрами — сразу на вход). Админ-панель (/admin, /api/admin/...) тоже только после
входа (первого администратора через сайт больше не создать — только командой setup-accounts), остальные файлы
/static/ и описание адресов (/docs, /openapi.json) закрыты.

Проверка стоит одной «проходной» (LoginMiddleware) перед всеми адресами, а не в каждом адресе отдельно:
- без сеанса (или с временным паролем, который ещё не сменили) — на /login, для /api — 401;
- /api/clients/<клиент>/... — только клиенты из доступа сотрудника (403);
- чтение (GET) — право «view»; изменяющие запросы — по таблице RULES (загрузка, анализ, цены, задачи, чат);
  неизвестный изменяющий запрос — только администратору;
- «/» (страница администратора) — только администратору, остальных ведёт на /user;
- изменяющий запрос с чужого сайта (заголовок Origin не наш) — 403;
- кто что запустил — в журнал учётных записей (вкладка «Журнал» админ-панели), а у задач обработчика — в колонку
  jobs.started_by (db.ACTOR, 0.16.0);
- путь с «.» / «..» — 404; ответы — с заголовками безопасности (SecurityHeaders).
Модель анализа / чата и поисковик цен выбирает только администратор: для остальных адреса берут настройки сайта
(is_admin() в main.py).

Учётные записи — app/auth.py (те же, что в админ-панели); сеанс — cookie sb_session.
"""

from __future__ import annotations

import contextvars
import os
import re
from urllib.parse import parse_qs, quote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from . import auth, db


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _off(name: str) -> bool:
    """Выключено только явно: 0 / false / no / off (опечатка или пустое значение — не выключает)."""
    return os.environ.get(name, "").strip().lower() in ("0", "false", "no", "off")


# 0.16.0: по умолчанию — обязателен (сайт на сервере открыт в интернет): выключает только явное LOGIN_REQUIRED=0,
# а забытая строка, пустое значение или опечатка вход не выключают.
LOGIN_REQUIRED = not _off("LOGIN_REQUIRED")
# При обязательном входе админ-панель, страница сотрудника и страница входа нужны всегда.
ADMIN_PANEL = LOGIN_REQUIRED or _flag("ADMIN_PANEL")
USER_PAGE = LOGIN_REQUIRED or _flag("USER_PAGE")
LOGIN_PAGE = LOGIN_REQUIRED or _flag("LOGIN_PAGE")

COOKIE = "sb_session"
CURRENT_USER: contextvars.ContextVar[dict | None] = contextvars.ContextVar("supplybro_user", default=None)

# Открыто без входа (0.16.0): только страница входа — сама, её оформление и скрипт, её адреса /api/auth/... — и
# проверка «сайт жив». Админ-панель, страница сайта, остальные файлы /static/ — только после входа.
OPEN_EXACT = {"/login", "/start", "/health", "/favicon.ico", "/robots.txt"}   # /start — страница-приветствие (0.17.0)
OPEN_STATIC = {"/static/app.css", "/static/admin.css", "/static/login.css", "/static/login.js"}
OPEN_PREFIXES = ("/api/auth/", "/static/fonts/")    # шрифты страницы-приветствия (0.17.1)
# Админ-панель: нужен вход, а кому что можно — проверяет сама панель (app/admin.py: администратор сайта / клиента).
ADMIN_PAGE = "/admin"
ADMIN_API = "/api/admin/"

# Изменяющие запросы: (адрес после /api/clients/<клиент>, право, что записать в журнал).
RULES = [
    (re.compile(r"^/docs/upload$"), "upload", "загрузил документы"),
    (re.compile(r"^/docs/process$"), "upload", "запустил обработку документов"),
    (re.compile(r"^/folders$"), "upload", "создал конкурс"),
    (re.compile(r"^/analysis/run$"), "analyze", "запустил анализ"),
    (re.compile(r"^/analysis/save$"), "analyze", "запустил запись в NocoDB"),
    (re.compile(r"^/analysis/changes/run$"), "analyze", "запустил разбор изменений документации"),
    (re.compile(r"^/analysis/changes/write$"), "analyze", "записал изменения документации в NocoDB"),
    (re.compile(r"^/bid/run$"), "analyze", "запустил подготовку документов заявки"),
    (re.compile(r"^/bid/price$"), "analyze", "задал цену заявки"),
    (re.compile(r"^/bid/noco$"), "analyze", "записал заявку в NocoDB"),
    (re.compile(r"^/prices/run$"), "prices", "запустил поиск цен"),
    (re.compile(r"^/jobs/\d+/cancel$"), "jobs", "отменил задачу"),
    (re.compile(r"^/jobs/\d+/pause$"), "jobs", "приостановил задачу"),
    (re.compile(r"^/jobs/\d+/resume$"), "jobs", "продолжил задачу"),
    (re.compile(r"^/pricelists/upload$"), "prices", "загрузил прайс поставщика"),
    (re.compile(r"^/pricelists/write$"), "prices", "записал цены поставщика в NocoDB"),
    (re.compile(r"^/pricelists/remove$"), "prices", "убрал прайс из списка"),
    (re.compile(r"^/chat$"), "chat", "задал вопрос в чате"),
    (re.compile(r"^/chat/clear$"), "chat", "очистил историю чата"),
]
CLIENT_PATH = re.compile(r"^/api/clients/([^/]+)(/.*)?$")
# Адреса разделов страницы (0.11.0): выключенный сотруднику раздел (auth.BLOCKS, админ-панель) отвечает 403 — и чтение,
# и действия. Карточка NocoDB (/tenders/<id>) не закрывается: из неё и шапка конкурса — поля убирает main.py.
BLOCK_PATHS = [
    (re.compile(r"^/docs(/|$)"), "docs"),
    (re.compile(r"^/analysis(/|$)"), "analysis"),
    (re.compile(r"^/bid(/|$)"), "bid"),
    (re.compile(r"^/(prices|pricelists)(/|$)"), "prices"),
    (re.compile(r"^/tenders/[^/]+/spec$"), "spec"),
    (re.compile(r"^/chat(/|$)"), "chat"),
]


def block_of(rest: str) -> str | None:
    return next((b for rx, b in BLOCK_PATHS if rx.match(rest)), None)


def block_on(name: str) -> bool:
    """Включён ли раздел у того, кто сейчас вошёл (вход выключен — всё включено)."""
    return name not in auth.blocks_off(current_user())
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# ---------- кто вошёл и что ему можно ----------

def user_from_request(request: Request) -> dict | None:
    return auth.session_user(request.cookies.get(COOKIE))


def current_user() -> dict | None:
    """Кто вошёл (в адресах main.py). None — вход не требуется или не выполнен."""
    return CURRENT_USER.get()


def is_admin() -> bool:
    """Можно ли выбирать модели и поисковик: вход выключен (как раньше) или вошёл администратор."""
    if not LOGIN_REQUIRED:
        return True
    user = current_user()
    return bool(user and user["role"] == "admin")


def can(user: dict | None, permission: str) -> bool:
    return bool(user) and permission in auth.ROLE_PERMISSIONS.get(user["role"], set())


def can_client(user: dict | None, client_id: str) -> bool:
    if not LOGIN_REQUIRED:
        return True
    return bool(user) and (user["clients"] == ["*"] or client_id in user["clients"])


def home(user: dict) -> str:
    """Куда вести после входа: администратора — на «/», остальных — на страницу сотрудника."""
    return "/" if user["role"] == "admin" else "/user"


def safe_next(value: str | None) -> str:
    """Адрес «вернуться после входа» — только свой сайт (не //чужой.сайт) и не сама страница входа."""
    value = (value or "").strip()
    # Пробелы, табуляция и служебные символы браузер выбрасывает из адреса: «/<Tab>/чужой.сайт» стал бы «//чужой.сайт»
    # (0.16.0) — такие адреса не принимаем совсем.
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        return ""
    if not value.startswith("/") or value.startswith("//") or "\\" in value or value.startswith("/login"):
        return ""
    return value


# ---------- проходная ----------

def _lc1(text: str) -> str:
    return text[:1].lower() + text[1:]


def _foreign_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin == "null"
    return urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower()


def _deny(request: Request, status: int, text: str):
    if request.url.path.startswith(("/api/", "/static/")):     # адрес и файл — ответ, а не переход на вход
        return JSONResponse({"detail": text}, status_code=status)
    if status == 401:
        target = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
    return JSONResponse({"detail": text}, status_code=status)


def _bad_path(path: str) -> bool:
    """Путь с «.» / «..» или обратной чертой — такие страница сайта не строит; проходную ими не обойти."""
    return "\\" in path or "\x00" in path or any(seg in (".", "..") for seg in path.split("/"))


def is_open(path: str) -> bool:
    """Открыто ли без входа (страница входа и то, что ей нужно, /health)."""
    return path in OPEN_EXACT or path in OPEN_STATIC or path.startswith(OPEN_PREFIXES)


class LoginMiddleware:
    """Проверка входа и прав перед каждым адресом сайта (только при LOGIN_REQUIRED=1)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and LOGIN_REQUIRED:
            await receive()                                  # websocket сайт не использует — закрываем
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http" or not LOGIN_REQUIRED:
            return await self.app(scope, receive, send)
        request = Request(scope)
        path, method = request.url.path, request.method
        if _bad_path(path):
            return await JSONResponse({"detail": "не найдено"}, status_code=404)(scope, receive, send)
        if is_open(path):
            return await self.app(scope, receive, send)

        user = user_from_request(request)
        if not user and path == "/" and not request.url.query:
            # Гость на главной (0.17.0) — страница-приветствие с кнопкой «Войти»; ссылки на конкурсы — сразу на вход.
            return await RedirectResponse("/start", status_code=303)(scope, receive, send)
        if not user or user["must_change"]:
            return await _deny(request, 401, "войдите на сайт")(scope, receive, send)
        if method in MUTATING and _foreign_origin(request):
            return await _deny(request, 403, "запрос отклонён")(scope, receive, send)

        if path.startswith(ADMIN_API):                     # админ-панель: права проверяет она сама
            await self._run(user, scope, receive, send)
            return
        if path == ADMIN_PAGE and user["role"] not in auth.MANAGER_ROLES:
            return await RedirectResponse(home(user), status_code=303)(scope, receive, send)

        if path == "/" and user["role"] != "admin":
            q = ("?" + request.url.query) if request.url.query else ""
            return await RedirectResponse("/user" + q, status_code=303)(scope, receive, send)

        journal = None                                       # (действие, подробности) — для журнала
        m = CLIENT_PATH.match(path)
        if m:
            client_id, rest = m.group(1), m.group(2) or ""
            if not can_client(user, client_id):
                return await _deny(request, 403, "у вашей учётной записи нет доступа к этому клиенту")(scope, receive, send)
            blk = block_of(rest)
            if blk and blk in auth.blocks_off(user):
                return await _deny(request, 403, f"раздел «{auth.BLOCKS[blk].split(' (')[0]}» для вашей учётной записи "
                                                 "выключен (админ-панель)")(scope, receive, send)
            if method in MUTATING:
                rule = next((r for r in RULES if r[0].match(rest)), None)
                need = rule[1] if rule else "users"          # неизвестное изменение — только администратору
                if not can(user, need):
                    return await _deny(request, 403, f"вашей роли это недоступно: {_lc1(auth.PERMISSIONS.get(need, need))}")(
                        scope, receive, send)
                if rule:
                    folder = (parse_qs(request.url.query).get("folder") or [""])[0]
                    journal = (rule[2], f"{client_id}{' · ' + folder if folder else ''}")
            elif not can(user, "view"):
                return await _deny(request, 403, "вашей роли это недоступно")(scope, receive, send)
        elif path.startswith("/api/") and method in MUTATING and user["role"] != "admin":
            return await _deny(request, 403, "вашей роли это недоступно")(scope, receive, send)

        code = await self._run(user, scope, receive, send)
        if journal and code < 400:
            try:
                auth.event(user, None, journal[0], journal[1], request.client.host if request.client else "")
            except Exception:
                pass                                         # журнал не должен ломать работу

    async def _run(self, user: dict, scope, receive, send) -> int:
        """Выполнить адрес от имени вошедшего: current_user() и «кто запустил» у задач (db.ACTOR). Код ответа."""
        status = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        token = CURRENT_USER.set(user)
        actor = db.ACTOR.set(user["login"])
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            db.ACTOR.reset(actor)
            CURRENT_USER.reset(token)
        return status.get("code", 500)


# ---------- заголовки безопасности (0.16.0) ----------

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),          # браузер не угадывает тип файла (скачанный .html не запустится)
    (b"x-frame-options", b"SAMEORIGIN"),              # чужой сайт не вставит наш в рамку (подмена кнопок)
    (b"referrer-policy", b"same-origin"),             # адреса наших страниц не уходят на чужие сайты (NocoDB и др.)
]
HSTS = (b"strict-transport-security", b"max-age=31536000")   # по HTTPS — дальше браузер ходит только по HTTPS


class SecurityHeaders:
    """Заголовки безопасности у каждого ответа (и при LOGIN_REQUIRED=0). HSTS — только если запрос пришёл по HTTPS
    (за Caddy: X-Forwarded-Proto, uvicorn доверяет ему при FORWARDED_ALLOW_IPS)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        extra = SECURITY_HEADERS + ([HSTS] if scope.get("scheme") == "https" else [])

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra if k not in have]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
