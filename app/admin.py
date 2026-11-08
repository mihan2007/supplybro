"""
Админ-панель учётных записей: страница /admin и адреса /api/admin/... (app/auth.py — сами учётные записи).

Подключается при LOGIN_REQUIRED=1 (вход на сайт обязателен — тогда всегда) или ADMIN_PANEL=1. При обязательном входе
(0.16.0) панель и её адреса открываются только после входа на /login (app/access.py), первого администратора создаёт
только команда setup-accounts.

Безопасность: сеанс — cookie sb_session (HttpOnly, SameSite=Strict); изменяющие запросы — только с заголовком
X-SupplyBro: 1 (чужая страница в браузере не может его поставить) и JSON-телом; всё, кроме входа, первого
администратора и смены своего пароля, — только администратору, сменившему временный пароль.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from . import auth
from .config import load_clients

ROOT = Path(__file__).resolve().parent
COOKIE = "sb_session"
_VERSION_FILE = ROOT.parent / "VERSION"
VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip() if _VERSION_FILE.exists() else "dev"


def page(name: str) -> HTMLResponse:
    """Страница из static/ со ссылками на свои .js / .css с номером версии (0.16.0): после обновления сайта браузер
    берёт новые файлы сам, без Ctrl+F5."""
    html = (ROOT / "static" / name).read_text(encoding="utf-8")
    for asset in ("app.css", "admin.css", "login.css", "admin.js", "login.js"):
        html = html.replace(f'"/static/{asset}"', f'"/static/{asset}?v={VERSION}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

router = APIRouter()
auth.init()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _token(request: Request) -> str | None:
    return request.cookies.get(COOKIE)


def _me(request: Request) -> dict | None:
    return auth.session_user(_token(request))


def _guard(request: Request) -> None:
    """Изменяющий запрос — только со страницы панели (заголовок, который чужой сайт поставить не может)."""
    if request.headers.get("x-supplybro") != "1":
        raise HTTPException(403, "запрос отклонён")


def _admin(request: Request, change: bool = False) -> dict:
    """Администратор сайта или администратор клиента (0.12.0) — у второго всё только в пределах своего клиента."""
    if change:
        _guard(request)
    me = _me(request)
    if not me:
        raise HTTPException(401, "войдите в админ-панель")
    if me["role"] not in auth.MANAGER_ROLES:
        raise HTTPException(403, "у этой учётной записи нет прав администратора")
    if me["must_change"]:
        raise HTTPException(403, "сначала смените временный пароль")
    if me["role"] == "client_admin" and not auth.client_of(me):
        raise HTTPException(403, "администратору клиента не назначен клиент — обратитесь к администратору сайта")
    return me


def _scope(me: dict) -> str | None:
    """Чей клиент: None — администратор сайта (все), иначе id клиента администратора клиента."""
    return None if me["role"] == "admin" else auth.client_of(me)


def _in_scope(me: dict, user: dict | None) -> bool:
    sc = _scope(me)
    return bool(user) and (sc is None or (user["role"] != "admin" and user["clients"] == [sc]))


def _target(me: dict, user_id: int) -> dict:
    user = auth.get_user(user_id)
    if not _in_scope(me, user):
        raise HTTPException(404, "сотрудник не найден")
    return user


def _scoped_data(me: dict, data: dict, target: dict | None = None) -> dict:
    """Администратор клиента: клиент — всегда свой, роль «администратор сайта» назначить нельзя, свою роль — менять нельзя."""
    sc = _scope(me)
    if target and target["id"] == me["id"] and "role" in data and data["role"] != me["role"]:
        raise HTTPException(400, "свою роль поменять нельзя — попросите другого администратора")
    if sc is None:
        return data
    if data.get("role") == "admin":
        raise HTTPException(403, "назначить администратора сайта может только администратор сайта")
    if "clients" in data or target is None:
        data["clients"] = [sc]
    return data


def _clients() -> list[dict]:
    try:
        return [{"id": c.id, "name": c.name, "folder": c.tenders_folder} for c in load_clients().values()]
    except Exception:
        return []


def _known() -> set[str]:
    return {c["id"] for c in _clients()}


def _set_cookie(request: Request, response: Response, token: str, remember: bool = True) -> None:
    """Cookie сеанса. remember — живёт до года (сколько продлится сеанс, решает сервер: auth.session_user), иначе —
    до закрытия браузера. SameSite=Lax (0.17.0, было Strict): по ссылке на сайт из почты / мессенджера / NocoDB
    сотрудник приходит уже вошедшим; изменяющие запросы с чужих сайтов по-прежнему отклоняются (Origin, X-SupplyBro)."""
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_MAX_DAYS * 86400 if remember else None, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")


def _wrap(fn):
    try:
        return fn()
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc))


# ---------- страница ----------

@router.get("/admin")
def admin_page():
    return page("admin.html")


# ---------- вход ----------

@router.get("/api/admin/state")
def state(request: Request):
    me = _me(request)
    site = bool(me) and me["role"] == "admin"
    sc = auth.client_of(me) if me and me["role"] == "client_admin" else None
    clients = _clients()
    if me and not site:
        clients = [c for c in clients if c["id"] == sc]
    return {"has_users": auth.count_users() > 0, "me": auth.public(me),
            "roles": auth.ROLES, "permissions": auth.PERMISSIONS,
            "role_permissions": {k: sorted(v) for k, v in auth.ROLE_PERMISSIONS.items()}, "blocks": auth.BLOCKS,
            "clients": clients, "min_password": auth.MIN_PASSWORD, "login_required": _login_required(),
            # 0.12.0: администратор сайта — все клиенты; администратор клиента — только свой, без админов сайта.
            "site_admin": site, "scope": sc,
            "assignable_roles": [r for r in auth.ROLES if site or r != "admin"]}


def _login_required() -> bool:
    from . import access                         # вход на сайт включён — сотрудники уже входят (0.7.0)
    return access.LOGIN_REQUIRED


@router.post("/api/admin/setup")
def setup(request: Request, response: Response, data: dict = Body(...)):
    """Первый администратор — только пока учётных записей нет совсем и только при выключенном входе на сайт.
    При LOGIN_REQUIRED=1 (сайт может быть открыт в интернет, 0.16.0) — только командой setup-accounts: иначе первым
    администратором стал бы любой, кто откроет новый сервер раньше вас."""
    _guard(request)
    if _login_required():
        raise HTTPException(403, "первого администратора создают командой в папке проекта: "
                                 "docker compose exec web python -m app.auth setup-accounts")
    if auth.count_users() > 0:
        raise HTTPException(409, "администратор уже есть — войдите")
    def run():
        problem = auth.password_problem(str(data.get("password") or ""), str(data.get("login") or ""))
        if problem:
            raise auth.AuthError(problem)
        user, _ = auth.create_user({**data, "role": "admin", "clients": ["*"], "must_change": False}, None, _known(),
                                   _ip(request))
        return auth.login(user["login"], data["password"], _ip(request), request.headers.get("user-agent", ""))
    user, token = _wrap(run)
    _set_cookie(request, response, token)
    return {"me": auth.public(user)}


@router.post("/api/admin/login")
def do_login(request: Request, response: Response, data: dict = Body(...)):
    _guard(request)
    user, token = _wrap(lambda: auth.login(str(data.get("login") or ""), str(data.get("password") or ""),
                                           _ip(request), request.headers.get("user-agent", "")))
    _set_cookie(request, response, token)
    return {"me": auth.public(user)}


@router.post("/api/admin/logout")
def do_logout(request: Request, response: Response):
    _guard(request)
    auth.logout(_token(request))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/api/admin/password")
def my_password(request: Request, data: dict = Body(...)):
    """Смена своего пароля (в том числе временного — при первом входе). Другие входы этого сотрудника закрываются."""
    _guard(request)
    me = _me(request)
    if not me:
        raise HTTPException(401, "войдите в админ-панель")
    _wrap(lambda: auth.change_password(me["id"], str(data.get("old") or ""), str(data.get("new") or ""), _ip(request)))
    auth.end_sessions(me["id"], me, _ip(request), keep_token=_token(request))
    return {"me": auth.public(auth.get_user(me["id"]))}


# ---------- учётные записи ----------

@router.get("/api/admin/users")
def users(request: Request):
    me = _admin(request)
    sessions = auth.sessions_count()
    return {"users": [{**auth.public(u), "sessions": sessions.get(u["id"], 0)} for u in auth.list_users()
                      if _in_scope(me, u)]}


@router.post("/api/admin/users")
def create(request: Request, data: dict = Body(...)):
    me = _admin(request, change=True)
    data = _scoped_data(me, dict(data))
    user, pw = _wrap(lambda: auth.create_user(data, me, _known(), _ip(request)))
    return {"user": auth.public(user), "temp_password": pw}


@router.patch("/api/admin/users/{user_id}")
def update(request: Request, user_id: int, data: dict = Body(...)):
    me = _admin(request, change=True)
    target = _target(me, user_id)
    data = {k: v for k, v in data.items()
            if k in ("login", "name", "email", "phone", "role", "clients", "active", "note", "blocks_off")}
    data = _scoped_data(me, data, target)
    return {"user": auth.public(_wrap(lambda: auth.update_user(user_id, data, me, _known(), _ip(request))))}


@router.post("/api/admin/users/{user_id}/reset-password")
def reset(request: Request, user_id: int):
    me = _admin(request, change=True)
    _target(me, user_id)
    return {"temp_password": _wrap(lambda: auth.reset_password(user_id, me, _ip(request))),
            "user": auth.public(auth.get_user(user_id))}


@router.post("/api/admin/users/{user_id}/end-sessions")
def end_sessions(request: Request, user_id: int):
    me = _admin(request, change=True)
    _target(me, user_id)
    keep = _token(request) if user_id == me["id"] else None
    return {"closed": auth.end_sessions(user_id, me, _ip(request), keep_token=keep)}


@router.post("/api/admin/users/{user_id}/unlock")
def unlock(request: Request, user_id: int):
    me = _admin(request, change=True)
    _target(me, user_id)
    auth.unlock(user_id, me, _ip(request))
    return {"user": auth.public(auth.get_user(user_id))}


@router.delete("/api/admin/users/{user_id}")
def delete(request: Request, user_id: int):
    me = _admin(request, change=True)
    _target(me, user_id)
    _wrap(lambda: auth.delete_user(user_id, me, _ip(request)))
    return {"ok": True}


@router.get("/api/admin/events")
def events(request: Request, user_id: int | None = None, limit: int = 200):
    me = _admin(request)
    if user_id:
        _target(me, user_id)
    among = None if _scope(me) is None else {u["id"] for u in auth.list_users() if _in_scope(me, u)}
    return {"events": auth.events(min(max(limit, 1), 1000), user_id, among)}
