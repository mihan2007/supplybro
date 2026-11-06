"""
Вход на сайт и права сотрудников (0.7.0): кто вошёл, к каким клиентам у него доступ, что разрешает его роль.

Включается LOGIN_REQUIRED=1 в .env (решение пользователя 03.11.2026 «делай вход для админа и для пользователей»).
Тогда без входа сайт не открывается: страницы ведут на /login, адреса /api/... отвечают 401. Админ-панель (/admin),
страница сотрудника (/user) и страница входа (/login) при этом включены всегда, независимо от ADMIN_PANEL / USER_PAGE /
LOGIN_PAGE. LOGIN_REQUIRED=0 — сайт как раньше, без входа.

Проверка стоит одной «проходной» (LoginMiddleware) перед всеми адресами, а не в каждом адресе отдельно:
- без сеанса (или с временным паролем, который ещё не сменили) — на /login, для /api — 401;
- /api/clients/<клиент>/... — только клиенты из доступа сотрудника (403);
- чтение (GET) — право «view»; изменяющие запросы — по таблице RULES (загрузка, анализ, цены, задачи, чат);
  неизвестный изменяющий запрос — только администратору;
- «/» (страница администратора) — только администратору, остальных ведёт на /user;
- изменяющий запрос с чужого сайта (заголовок Origin не наш) — 403;
- кто что запустил — в журнал учётных записей (вкладка «Журнал» админ-панели).
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

from . import auth


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


LOGIN_REQUIRED = _flag("LOGIN_REQUIRED")
# При обязательном входе админ-панель, страница сотрудника и страница входа нужны всегда.
ADMIN_PANEL = LOGIN_REQUIRED or _flag("ADMIN_PANEL")
USER_PAGE = LOGIN_REQUIRED or _flag("USER_PAGE")
LOGIN_PAGE = LOGIN_REQUIRED or _flag("LOGIN_PAGE")

COOKIE = "sb_session"
CURRENT_USER: contextvars.ContextVar[dict | None] = contextvars.ContextVar("supplybro_user", default=None)

# Открыто без входа: страница входа и её адреса, оформление, проверка «сайт жив», админ-панель (у неё свой вход).
OPEN_EXACT = {"/login", "/health", "/admin", "/favicon.ico"}
OPEN_PREFIXES = ("/static/", "/api/auth/", "/api/admin/")

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
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": text}, status_code=status)
    if status == 401:
        target = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
    return JSONResponse({"detail": text}, status_code=status)


class LoginMiddleware:
    """Проверка входа и прав перед каждым адресом сайта (только при LOGIN_REQUIRED=1)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not LOGIN_REQUIRED:
            return await self.app(scope, receive, send)
        request = Request(scope)
        path, method = request.url.path, request.method
        if path in OPEN_EXACT or path.startswith(OPEN_PREFIXES):
            return await self.app(scope, receive, send)

        user = user_from_request(request)
        if not user or user["must_change"]:
            return await _deny(request, 401, "войдите на сайт")(scope, receive, send)
        if method in MUTATING and _foreign_origin(request):
            return await _deny(request, 403, "запрос отклонён")(scope, receive, send)

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

        status = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        token = CURRENT_USER.set(user)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            CURRENT_USER.reset(token)
        if journal and status.get("code", 500) < 400:
            try:
                auth.event(user, None, journal[0], journal[1], request.client.host if request.client else "")
            except Exception:
                pass                                         # журнал не должен ломать работу
