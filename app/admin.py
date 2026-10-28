"""
Админ-панель учётных записей: страница /admin и адреса /api/admin/... (app/auth.py — сами учётные записи).

Подключается, только если в .env ADMIN_PANEL=1 (по умолчанию выключена: решение пользователя 28.10.2026 —
«сделай, но пока не подключай»). Сам сайт вход пока не требует.

Безопасность: сеанс — cookie sb_session (HttpOnly, SameSite=Strict); изменяющие запросы — только с заголовком
X-SupplyBro: 1 (чужая страница в браузере не может его поставить) и JSON-телом; всё, кроме входа, первого
администратора и смены своего пароля, — только администратору, сменившему временный пароль.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import FileResponse

from . import auth
from .config import load_clients

ROOT = Path(__file__).resolve().parent
COOKIE = "sb_session"

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
    if change:
        _guard(request)
    me = _me(request)
    if not me:
        raise HTTPException(401, "войдите в админ-панель")
    if me["role"] != "admin":
        raise HTTPException(403, "у этой учётной записи нет прав администратора")
    if me["must_change"]:
        raise HTTPException(403, "сначала смените временный пароль")
    return me


def _clients() -> list[dict]:
    try:
        return [{"id": c.id, "name": c.name} for c in load_clients().values()]
    except Exception:
        return []


def _known() -> set[str]:
    return {c["id"] for c in _clients()}


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


def _wrap(fn):
    try:
        return fn()
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc))


# ---------- страница ----------

@router.get("/admin")
def admin_page():
    return FileResponse(ROOT / "static" / "admin.html")


# ---------- вход ----------

@router.get("/api/admin/state")
def state(request: Request):
    me = _me(request)
    return {"has_users": auth.count_users() > 0, "me": auth.public(me),
            "roles": auth.ROLES, "permissions": auth.PERMISSIONS,
            "role_permissions": {k: sorted(v) for k, v in auth.ROLE_PERMISSIONS.items()},
            "clients": _clients(), "min_password": auth.MIN_PASSWORD}


@router.post("/api/admin/setup")
def setup(request: Request, response: Response, data: dict = Body(...)):
    """Первый администратор — только пока учётных записей нет совсем."""
    _guard(request)
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
    _admin(request)
    sessions = auth.sessions_count()
    return {"users": [{**auth.public(u), "sessions": sessions.get(u["id"], 0)} for u in auth.list_users()]}


@router.post("/api/admin/users")
def create(request: Request, data: dict = Body(...)):
    me = _admin(request, change=True)
    user, pw = _wrap(lambda: auth.create_user(data, me, _known(), _ip(request)))
    return {"user": auth.public(user), "temp_password": pw}


@router.patch("/api/admin/users/{user_id}")
def update(request: Request, user_id: int, data: dict = Body(...)):
    me = _admin(request, change=True)
    data = {k: v for k, v in data.items() if k in ("login", "name", "email", "phone", "role", "clients", "active", "note")}
    return {"user": auth.public(_wrap(lambda: auth.update_user(user_id, data, me, _known(), _ip(request))))}


@router.post("/api/admin/users/{user_id}/reset-password")
def reset(request: Request, user_id: int):
    me = _admin(request, change=True)
    return {"temp_password": _wrap(lambda: auth.reset_password(user_id, me, _ip(request))),
            "user": auth.public(auth.get_user(user_id))}


@router.post("/api/admin/users/{user_id}/end-sessions")
def end_sessions(request: Request, user_id: int):
    me = _admin(request, change=True)
    keep = _token(request) if user_id == me["id"] else None
    return {"closed": auth.end_sessions(user_id, me, _ip(request), keep_token=keep)}


@router.post("/api/admin/users/{user_id}/unlock")
def unlock(request: Request, user_id: int):
    me = _admin(request, change=True)
    auth.unlock(user_id, me, _ip(request))
    return {"user": auth.public(auth.get_user(user_id))}


@router.delete("/api/admin/users/{user_id}")
def delete(request: Request, user_id: int):
    me = _admin(request, change=True)
    _wrap(lambda: auth.delete_user(user_id, me, _ip(request)))
    return {"ok": True}


@router.get("/api/admin/events")
def events(request: Request, user_id: int | None = None, limit: int = 200):
    _admin(request)
    return {"events": auth.events(min(max(limit, 1), 1000), user_id)}
