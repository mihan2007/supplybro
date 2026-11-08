"""
Страница входа на сайт: /login и адреса /api/auth/... (сами учётные записи — app/auth.py, те же, что в админ-панели).

Включена при LOGIN_REQUIRED=1 (вход на сайт обязателен, 0.7.0 — app/access.py) или LOGIN_PAGE=1. Сеанс — cookie
sb_session, та же, что у админ-панели: вошли здесь — вошли и в /admin. При обязательном входе после входа (и смены
временного пароля) страница сразу ведёт туда, откуда пришли (?next=…), или на свою страницу: администратора — «/»,
остальных — /user. Без обязательного входа (LOGIN_PAGE=1, 0.6.4) — только проверка логина и пароля и карточка «что
будет доступно сотруднику».

Таблицы — те же users / sessions / user_events (структура не меняется). Защита — как у админ-панели: изменяющие
запросы только с заголовком X-SupplyBro: 1; подбор паролей — ограничения в app/auth.py (0.16.0: 5 неверных подряд с
одного адреса — вход в эту учётную запись с него закрыт на 10 минут, 50 подряд со всех — вся учётная запись, 20
неудачных с адреса за 15 минут — закрыт адрес).

"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request, Response

from . import access, auth
from .admin import COOKIE, _guard, _ip, _me, _set_cookie, _token, _wrap, page
from .config import load_clients

ROOT = Path(__file__).resolve().parent

router = APIRouter()
auth.init()


def _home(user: dict) -> dict:
    """Какая страница откроется сотруднику после входа."""
    if user["role"] == "admin":
        return {"url": "/", "title": "страница администратора", "open": "Открыть страницу администратора", "on": True}
    on = access.USER_PAGE
    return {"url": "/user" if on else "/", "title": "страница сотрудника",
            "open": "Открыть страницу сотрудника" if on else "Открыть сайт", "on": on}


def _access(user: dict | None) -> dict | None:
    """Что будет доступно сотруднику: роль, права, клиенты, представление NocoDB, страница после входа."""
    if not user:
        return None
    role = auth.ROLES.get(user["role"], {})
    allowed = auth.ROLE_PERMISSIONS.get(user["role"], set())
    try:
        clients = [{"id": c.id, "name": c.name} for c in load_clients().values()]
    except Exception:
        clients = []
    if user["clients"] == ["*"]:
        names = [c["name"] for c in clients]
        all_clients = True
    else:
        names = [c["name"] for c in clients if c["id"] in user["clients"]]
        names += [c for c in user["clients"] if c not in {x["id"] for x in clients}]   # клиент удалён из clients.json
        all_clients = False
    return {
        "role_title": role.get("title", user["role"]), "view": role.get("view", ""),
        "role_text": role.get("text", "").replace("(эта панель)", "(админ-панель)"),
        "permissions": [{"key": k, "title": t, "allowed": k in allowed} for k, t in auth.PERMISSIONS.items()],
        "clients": names, "all_clients": all_clients,
        "home": _home(user),
        "admin_panel": user["role"] in auth.MANAGER_ROLES and access.ADMIN_PANEL,   # админ клиента — свои сотрудники
        "blocks": [{"key": k, "title": t, "on": k not in auth.blocks_off(user)} for k, t in auth.BLOCKS.items()],
    }


# ---------- страница ----------

@router.get("/login")
def login_page():
    return page("login.html")


# ---------- вход ----------

@router.get("/api/auth/state")
def state(request: Request):
    me = _me(request)
    return {"has_users": auth.count_users() > 0, "me": auth.public(me), "access": _access(me),
            "min_password": auth.MIN_PASSWORD, "connected": access.LOGIN_REQUIRED, "required": access.LOGIN_REQUIRED,
            "admin_panel": access.ADMIN_PANEL}


@router.post("/api/auth/login")
def do_login(request: Request, response: Response, data: dict = Body(...)):
    _guard(request)
    remember = data.get("remember", True) is not False        # «Не выходить на этом компьютере» (0.17.0)
    user, token = _wrap(lambda: auth.login(str(data.get("login") or ""), str(data.get("password") or ""),
                                           _ip(request), request.headers.get("user-agent", ""), remember))
    _set_cookie(request, response, token, remember)
    return {"me": auth.public(user), "access": _access(user)}


@router.post("/api/auth/logout")
def do_logout(request: Request, response: Response):
    _guard(request)
    me = _me(request)
    auth.logout(_token(request))
    if me:
        auth.event(me, me, "выход", "", _ip(request))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/api/auth/password")
def my_password(request: Request, data: dict = Body(...)):
    """Смена своего пароля (временного — при первом входе, или по кнопке). Другие входы сотрудника закрываются."""
    _guard(request)
    me = _me(request)
    if not me:
        raise HTTPException(401, "войдите заново")
    _wrap(lambda: auth.change_password(me["id"], str(data.get("old") or ""), str(data.get("new") or ""), _ip(request)))
    auth.end_sessions(me["id"], me, _ip(request), keep_token=_token(request))
    user = auth.get_user(me["id"])
    return {"me": auth.public(user), "access": _access(user)}
