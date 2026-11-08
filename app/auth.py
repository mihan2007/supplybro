"""
Учётные записи сотрудников: логины, пароли, роли, доступ к клиентам, сеансы входа и журнал действий.

Используется админ-панелью (/admin), страницей входа (/login) и проверкой входа на сайт (app/access.py, при
LOGIN_REQUIRED=1 — с 0.7.0, с 0.16.0 по умолчанию): сайт спрашивает логин, показывает только клиентов из доступа
сотрудника и разрешает действия по его роли (ROLES ниже). Защита от подбора (0.16.0): 5 неверных паролей подряд
к учётной записи с одного адреса — с этого адреса вход в неё закрыт на 10 минут; 50 подряд со всех адресов — закрыта
вся учётная запись на 10 минут; 20 неудачных входов с одного адреса за 15 минут — закрыт этот адрес.

Пароли хранятся только как хеш scrypt со своей солью для каждого; сеансы — как хеш случайного ключа из cookie
(украденная база не даёт ни паролей, ни действующих сеансов). Данные — в app.db (папка данных сайта).

Забыли пароль администратора — в Ubuntu, в папке проекта:
    docker compose exec web python -m app.auth reset-password <логин>
(напечатает новый временный пароль).

Учётные записи для начала работы — администратор и по сотруднику на клиента (уже существующие не трогает):
    docker compose exec web python -m app.auth setup-accounts

Тестовые учётные записи для проверки входа (по одной на роль; настоящие не трогает):
    docker compose exec web python -m app.auth demo-users            (ещё раз — новые пароли)
    docker compose exec web python -m app.auth demo-users --delete   (удалить только тестовые)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import time

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    login           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name            TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL,
    clients         TEXT NOT NULL DEFAULT '["*"]',   -- id клиентов из clients.json; ["*"] — все
    active          INTEGER NOT NULL DEFAULT 1,
    note            TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL,
    must_change     INTEGER NOT NULL DEFAULT 1,       -- сменить временный пароль при первом входе
    failed          INTEGER NOT NULL DEFAULT 0,       -- неудачных попыток подряд
    locked_until    REAL NOT NULL DEFAULT 0,
    created_at      REAL, updated_at REAL, password_changed_at REAL, last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  REAL, seen_at REAL, expires_at REAL,
    ip          TEXT NOT NULL DEFAULT '',
    agent       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS user_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL,
    actor_id    INTEGER,                -- кто сделал (NULL — система / командная строка)
    actor       TEXT NOT NULL DEFAULT '',
    user_id     INTEGER,                -- с чьей учётной записью
    user        TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS user_events_at ON user_events(at);
CREATE INDEX IF NOT EXISTS user_events_ip ON user_events(ip, at);
"""

# Роли и их права. По правам сайт (app/access.py) решает, какие действия разрешены сотруднику, а админ-панель — кого пускать.
# view — представление «Реестра конкурсов» NocoDB по умолчанию для роли.
# 0.12.0 (пользователь 04.11.2026: «в админ-панели мы видим наших клиентов, у клиента могут быть сотрудники, у которых
# и должны быть эти роли»): сотрудник принадлежит одному клиенту (clients = [id]); со всеми клиентами работает только
# администратор сайта (clients = ["*"]). Администратор клиента ведёт сотрудников своего клиента в админ-панели.
ROLES = {
    "admin": {"title": "Администратор сайта", "view": "Вся таблица",
              "text": "Все клиенты и вся админ-панель: клиенты, их сотрудники и администраторы сайта; выбор моделей ИИ."},
    "client_admin": {"title": "Администратор клиента", "view": "Вся таблица",
                     "text": "Всё, что тендерный специалист, плюс сотрудники своего клиента в админ-панели: добавить, "
                             "роль, разделы, пароль, блокировка."},
    "tender": {"title": "Тендерный специалист", "view": "Тендерный специалист",
               "text": "Конкурсы, документы, анализ, запись в NocoDB, поиск цен, чат, отмена и пауза задач."},
    "buyer": {"title": "Закупщик", "view": "Закупщик",
              "text": "Просмотр конкурсов и документов, поиск цен, спецификации, чат по документации."},
    "logist": {"title": "Логист", "view": "Логист",
               "text": "Просмотр конкурсов, документов и спецификаций, чат по документации."},
    "viewer": {"title": "Наблюдатель", "view": "Вся таблица",
               "text": "Только просмотр: конкурсы, документы, анализ, цены — без запуска задач и изменений."},
}
PERMISSIONS = {
    "view": "Смотреть конкурсы, документы, анализ, цены",
    "chat": "Вопросы по документации",
    "upload": "Загружать документы, создавать конкурсы",
    "analyze": "Запускать анализ и запись в NocoDB",
    "prices": "Искать цены",
    "jobs": "Отменять и приостанавливать задачи",
    "staff": "Сотрудники своего клиента (админ-панель)",
    "users": "Все клиенты и администраторы сайта (админ-панель)",
}
MANAGER_ROLES = ("admin", "client_admin")        # кому открыта админ-панель
# Разделы страницы, которые админ может выключить сотруднику (0.11.0, пользователь 04.11.2026: «у каждого пользователя
# в админ-панели опция отключать тот или иной блок»). Выключенный раздел не показывается, его адреса отвечают 403
# (app/access.py), карточка NocoDB — без полей (шапка конкурса остаётся), чат не получает цены / анализ.
BLOCKS = {
    "docs": "Документы",
    "analysis": "Анализ документов (и изменения)",
    "prices": "Цены (поиск, все цены, прайсы поставщиков)",
    "bid": "Подготовка заявки",
    "card": "NocoDB — карточка конкурса",
    "spec": "Спецификация",
    "chat": "Чат по документации",
}

ROLE_PERMISSIONS = {
    "admin": set(PERMISSIONS),
    "client_admin": {"view", "chat", "upload", "analyze", "prices", "jobs", "staff"},
    "tender": {"view", "chat", "upload", "analyze", "prices", "jobs"},
    "buyer": {"view", "chat", "prices"},
    "logist": {"view", "chat"},
    "viewer": {"view"},
}

# Сеанс (0.17.0, пользователь 08.11.2026: «чтобы не надо было постоянно [входить] при посещении сайта»):
# «Не выходить на этом компьютере» (по умолчанию) — сеанс продлевается при каждом посещении: выход только после
# SESSION_DAYS дней без посещений или через SESSION_MAX_DAYS после входа (украденный ключ сеанса не вечен);
# без галочки — до закрытия браузера и не дольше SHORT_SESSION_HOURS.
SESSION_DAYS = 30
SESSION_MAX_DAYS = 365
SHORT_SESSION_HOURS = 12
# Защита от подбора паролей (0.16.0, сайт в интернете). Адрес — настоящий адрес посетителя: за Caddy его передаёт
# X-Forwarded-For (uvicorn доверяет ему при FORWARDED_ALLOW_IPS).
# - MAX_FAILED неверных паролей подряд к учётной записи С ОДНОГО АДРЕСА — с этого адреса вход в неё закрыт на
#   LOCK_MINUTES. С других адресов сотрудник входит как обычно: чужой человек не может держать его вход закрытым,
#   просто вводя неверный пароль (до 0.16.0 закрывалась вся учётная запись — с любого адреса);
# - MAX_FAILED_TOTAL подряд со всех адресов — закрыта вся учётная запись на LOCK_MINUTES (подбор с многих адресов;
#   отметка «вход закрыт» и кнопка «Открыть вход» в админ-панели);
# - MAX_FAILED_IP неудачных входов с адреса за IP_WINDOW_MINUTES (к любым логинам) — закрыт этот адрес.
# Сброс (вход, «Открыть вход», сброс пароля) обнуляет счёт по учётной записи.
MAX_FAILED = 5
MAX_FAILED_TOTAL = 50
LOCK_MINUTES = 10
MAX_FAILED_IP = 20
IP_WINDOW_MINUTES = 15
RESET_ACTIONS = ("вход", "вход открыт", "пароль сброшен")
# Проверка и запись попыток — по одной (иначе одновременные запросы обходят счёт: прочитали «4», записали «5» каждый).
_LOGIN_LOCK = threading.Lock()
MIN_PASSWORD = 10
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AuthError(Exception):
    """Понятная пользователю ошибка (показывается на странице)."""


def init() -> None:
    db.init()
    with db.connect() as con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        if "blocks_off" not in cols:                 # 0.11.0: выключенные разделы страницы
            try:
                con.execute("ALTER TABLE users ADD COLUMN blocks_off TEXT NOT NULL DEFAULT '[]'")
            except Exception:                        # сайт и админ-панель добавили одновременно
                pass
        if "remember" not in {r[1] for r in con.execute("PRAGMA table_info(sessions)")}:   # 0.17.0
            try:
                con.execute("ALTER TABLE sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 1")
            except Exception:
                pass


# ---------- пароли ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(dk)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split("$")
        if kind != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), dklen=len(want),
                             n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


def password_problem(password: str, login: str = "") -> str:
    """Почему пароль не годится ('' — годится)."""
    if len(password or "") < MIN_PASSWORD:
        return f"пароль — не короче {MIN_PASSWORD} символов"
    if login and login.lower() in password.lower():
        return "пароль не должен содержать логин"
    if len(set(password)) < 5:
        return "слишком простой пароль"
    return ""


def temp_password() -> str:
    """Временный пароль для выдачи сотруднику: 12 символов без похожих букв (l/1, O/0)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


# ---------- учётные записи ----------

def _row(r) -> dict | None:
    if not r:
        return None
    u = dict(r)
    try:
        u["clients"] = json.loads(u.get("clients") or '["*"]')
    except ValueError:
        u["clients"] = ["*"]
    try:
        u["blocks_off"] = [b for b in json.loads(u.get("blocks_off") or "[]") if b in BLOCKS]
    except ValueError:
        u["blocks_off"] = []
    return u


def blocks_off(user: dict | None) -> set[str]:
    """Выключенные разделы страницы у сотрудника (нет входа — ничего не выключено)."""
    return set((user or {}).get("blocks_off") or [])


def public(u: dict | None) -> dict | None:
    """Учётная запись для страницы — без хеша пароля."""
    if not u:
        return None
    out = {k: v for k, v in u.items() if k not in ("password_hash",)}
    out["role_title"] = ROLES.get(u["role"], {}).get("title", u["role"])
    out["locked"] = bool(u.get("locked_until") and u["locked_until"] > time.time())
    return out


def count_users() -> int:
    with db.connect() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(user_id: int) -> dict | None:
    with db.connect() as con:
        return _row(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def find_user(login: str) -> dict | None:
    with db.connect() as con:
        return _row(con.execute("SELECT * FROM users WHERE login=?", (login.strip().lower(),)).fetchone())


def list_users() -> list[dict]:
    with db.connect() as con:
        return [_row(r) for r in con.execute("SELECT * FROM users ORDER BY active DESC, role='admin' DESC, name, login")]


def _active_admins(exclude: int | None = None) -> int:
    with db.connect() as con:
        return con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id!=?",
                           (exclude or -1,)).fetchone()[0]


def _clean(data: dict, known_clients: set[str], creating: bool) -> dict:
    out = {}
    if creating or "login" in data:
        login = str(data.get("login") or "").strip().lower()
        if not LOGIN_RE.match(login):
            raise AuthError("логин — 2–32 символа: латинские буквы, цифры, точка, дефис, подчёркивание")
        out["login"] = login
    if creating or "name" in data:
        name = " ".join(str(data.get("name") or "").split())[:120]
        if not name:
            raise AuthError("укажите ФИО")
        out["name"] = name
    for k, limit in (("email", 120), ("phone", 40), ("note", 500)):
        if k in data:
            out[k] = str(data.get(k) or "").strip()[:limit]
    if out.get("email") and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", out["email"]):
        raise AuthError("e-mail указан неверно")
    if creating or "role" in data:
        role = str(data.get("role") or "")
        if role not in ROLES:
            raise AuthError("выберите роль")
        out["role"] = role
    if creating or "clients" in data:
        cl = data.get("clients") or []
        if cl == ["*"] or cl == "*":
            out["clients"] = ["*"]
        else:
            cl = [str(c) for c in cl if str(c) in known_clients]
            if not cl:
                raise AuthError("выберите клиента сотрудника")
            out["clients"] = sorted(set(cl))
    if "active" in data:
        out["active"] = 1 if data.get("active") else 0
    if "blocks_off" in data:
        bo = data.get("blocks_off") or []
        if not isinstance(bo, list):
            raise AuthError("разделы указаны неверно")
        out["blocks_off"] = [b for b in BLOCKS if b in {str(x) for x in bo}]
    return out


def _one_client(role: str, clients: list[str]) -> list[str]:
    """Администратор сайта — все клиенты; остальные — ровно один клиент (0.12.0)."""
    if role == "admin":
        return ["*"]
    if clients == ["*"] or len(clients) != 1:
        raise AuthError("выберите клиента сотрудника: сотрудник работает с одним клиентом "
                        "(со всеми — только администратор сайта)")
    return clients


def client_of(user: dict | None) -> str | None:
    """Клиент сотрудника (None — администратор сайта или учётная запись без одного клиента, как до 0.12.0)."""
    cl = (user or {}).get("clients") or []
    return cl[0] if len(cl) == 1 and cl[0] != "*" else None


def create_user(data: dict, actor: dict | None, known_clients: set[str], ip: str = "") -> tuple[dict, str]:
    """Новая учётная запись. Пароль: data['password'] или временный (возвращается один раз, чтобы передать
    сотруднику). Возвращает (учётная запись, временный пароль или '')."""
    fields = _clean(data, known_clients, creating=True)
    fields["clients"] = _one_client(fields["role"], fields["clients"])
    if find_user(fields["login"]):
        raise AuthError(f"логин «{fields['login']}» уже занят")
    given = str(data.get("password") or "")
    if given:
        problem = password_problem(given, fields["login"])
        if problem:
            raise AuthError(problem)
    pw = given or temp_password()
    now = time.time()
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO users(login, name, email, phone, role, clients, active, note, password_hash, must_change, "
            "created_at, updated_at, password_changed_at, blocks_off) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fields["login"], fields["name"], fields.get("email", ""), fields.get("phone", ""), fields["role"],
             json.dumps(fields["clients"]), 1, fields.get("note", ""), hash_password(pw),
             0 if given and data.get("must_change") is False else 1, now, now, now,
             json.dumps(fields.get("blocks_off") or [])))
        user_id = cur.lastrowid
    user = get_user(user_id)
    event(actor, user, "создана", f"роль: {ROLES[user['role']]['title']}; клиенты: {_clients_text(user['clients'])}", ip)
    return user, ("" if given else pw)


def update_user(user_id: int, data: dict, actor: dict | None, known_clients: set[str], ip: str = "") -> dict:
    user = get_user(user_id)
    if not user:
        raise AuthError("учётная запись не найдена")
    fields = _clean(data, known_clients, creating=False)
    if "login" in fields and fields["login"] != user["login"]:
        other = find_user(fields["login"])
        if other and other["id"] != user_id:
            raise AuthError(f"логин «{fields['login']}» уже занят")
    if "role" in fields or "clients" in fields:     # роль и клиент — вместе: один клиент или все (админ сайта)
        fields["clients"] = _one_client(fields.get("role", user["role"]), fields.get("clients", user["clients"]))
    losing_admin = user["role"] == "admin" and user["active"] and (
        fields.get("role", "admin") != "admin" or fields.get("active", 1) == 0)
    if losing_admin and not _active_admins(exclude=user_id):
        raise AuthError("это единственный администратор — сначала назначьте другого")
    if actor and actor["id"] == user_id and fields.get("active", 1) == 0:
        raise AuthError("себя заблокировать нельзя")
    js = ("clients", "blocks_off")                   # хранятся списком в JSON
    changes = {k: v for k, v in fields.items() if (json.dumps(v) if k in js else v) !=
               (json.dumps(user.get(k)) if k in js else user[k])}
    if not changes:
        return user
    sets = ", ".join(f"{k}=?" for k in changes) + ", updated_at=?"
    vals = [json.dumps(v) if k in js else v for k, v in changes.items()]
    with db.connect() as con:
        con.execute(f"UPDATE users SET {sets} WHERE id=?", (*vals, time.time(), user_id))
        if changes.get("active") == 0:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))   # заблокирован — сразу выход
    new = get_user(user_id)
    diff = _diff_text(user, changes)
    if diff:
        event(actor, new, "изменена", diff, ip)
    if changes.get("active") == 0:
        event(actor, new, "заблокирована", "", ip)
    elif changes.get("active") == 1:
        event(actor, new, "разблокирована", "", ip)
    return new


def reset_password(user_id: int, actor: dict | None, ip: str = "") -> str:
    """Новый временный пароль (сотрудник сменит его при входе); все сеансы сотрудника закрываются."""
    user = get_user(user_id)
    if not user:
        raise AuthError("учётная запись не найдена")
    pw = temp_password()
    with db.connect() as con:
        con.execute("UPDATE users SET password_hash=?, must_change=1, failed=0, locked_until=0, password_changed_at=?, "
                    "updated_at=? WHERE id=?", (hash_password(pw), time.time(), time.time(), user_id))
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    event(actor, user, "пароль сброшен", "выдан временный пароль, все входы закрыты", ip)
    return pw


def change_password(user_id: int, old: str, new: str, ip: str = "") -> None:
    user = get_user(user_id)
    if not user or not check_password(old, user["password_hash"]):
        raise AuthError("текущий пароль указан неверно")
    problem = password_problem(new, user["login"])
    if problem:
        raise AuthError(problem)
    if check_password(new, user["password_hash"]):
        raise AuthError("новый пароль совпадает с текущим")
    with db.connect() as con:
        con.execute("UPDATE users SET password_hash=?, must_change=0, password_changed_at=?, updated_at=? WHERE id=?",
                    (hash_password(new), time.time(), time.time(), user_id))
    event(user, user, "пароль изменён", "сотрудником", ip)


def delete_user(user_id: int, actor: dict | None, ip: str = "") -> None:
    user = get_user(user_id)
    if not user:
        raise AuthError("учётная запись не найдена")
    if actor and actor["id"] == user_id:
        raise AuthError("себя удалить нельзя")
    if user["role"] == "admin" and user["active"] and not _active_admins(exclude=user_id):
        raise AuthError("это единственный администратор — его удалить нельзя")
    with db.connect() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        con.execute("DELETE FROM users WHERE id=?", (user_id,))
    event(actor, user, "удалена", f"{user['name']}, {ROLES.get(user['role'], {}).get('title', user['role'])}", ip)


def unlock(user_id: int, actor: dict | None, ip: str = "") -> None:
    with db.connect() as con:
        con.execute("UPDATE users SET failed=0, locked_until=0 WHERE id=?", (user_id,))
    event(actor, get_user(user_id), "вход открыт", "после неверных паролей", ip)


# ---------- вход и сеансы ----------

def _wait(rows, window: float, limit: int, now: float) -> float:
    """Сколько ещё ждать: limit неудач — пока самая ранняя из последних limit не выйдет из окна."""
    if limit <= 0 or len(rows) < limit:
        return 0.0
    return max(0.0, rows[limit - 1]["at"] + window - now)


def ip_wait(ip: str) -> float:
    """Сколько секунд с этого адреса вход закрыт (0 — открыт): MAX_FAILED_IP неудачных входов за IP_WINDOW_MINUTES."""
    if not ip:
        return 0.0
    now = time.time()
    window = IP_WINDOW_MINUTES * 60
    with db.connect() as con:
        rows = con.execute("SELECT at FROM user_events WHERE ip=? AND at>? AND action='неудачный вход' "
                           "ORDER BY at DESC LIMIT ?", (ip, now - window, max(MAX_FAILED_IP, 1))).fetchall()
    return _wait(rows, window, MAX_FAILED_IP, now)


def account_ip_wait(user_id: int, ip: str) -> float:
    """Сколько секунд вход в эту учётную запись с этого адреса закрыт: MAX_FAILED неверных паролей подряд с него
    (после последнего входа / «Открыть вход» / сброса пароля) за LOCK_MINUTES."""
    if not ip:
        return 0.0
    now = time.time()
    window = LOCK_MINUTES * 60
    with db.connect() as con:
        reset = con.execute(f"SELECT MAX(at) FROM user_events WHERE user_id=? AND action IN "
                            f"({','.join('?' * len(RESET_ACTIONS))})", (user_id, *RESET_ACTIONS)).fetchone()[0] or 0
        rows = con.execute("SELECT at FROM user_events WHERE user_id=? AND ip=? AND action='неудачный вход' AND at>? "
                           "ORDER BY at DESC LIMIT ?", (user_id, ip, max(now - window, reset), MAX_FAILED)).fetchall()
    return _wait(rows, window, MAX_FAILED, now)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))      # для одинакового времени ответа на чужие логины


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def session_window(remember: bool) -> float:
    """На сколько секунд вперёд продлевается сеанс при посещении."""
    return SESSION_DAYS * 86400 if remember else SHORT_SESSION_HOURS * 3600


def login(login_name: str, password: str, ip: str = "", agent: str = "", remember: bool = True) -> tuple[dict, str]:
    """Проверка логина и пароля. Возвращает (учётная запись, ключ сеанса для cookie). Попытки — по одной.
    remember — «Не выходить на этом компьютере» (SESSION_DAYS с продлением), иначе — SHORT_SESSION_HOURS."""
    with _LOGIN_LOCK:
        return _login(login_name, password, ip, agent, remember)


def _login(login_name: str, password: str, ip: str, agent: str, remember: bool = True) -> tuple[dict, str]:
    wait = ip_wait(ip)
    if wait:                                                    # пароль даже не проверяем (и не пишем в журнал)
        raise AuthError(f"слишком много неудачных попыток входа с вашего адреса — попробуйте через {_minutes(wait)} мин")
    user = find_user(login_name or "")
    now = time.time()
    if not user:
        check_password(password or "", _DUMMY_HASH)              # одинаковое время ответа для чужих логинов
        event(None, None, "неудачный вход", f"логин «{(login_name or '')[:40]}» не найден", ip)
        raise AuthError("неверный логин или пароль")
    if user["locked_until"] > now:
        raise AuthError(f"слишком много неверных паролей — вход закрыт ещё на {_minutes(user['locked_until'] - now)} мин")
    wait = account_ip_wait(user["id"], ip)
    if wait:
        raise AuthError(f"слишком много неверных паролей — вход закрыт ещё на {_minutes(wait)} мин")
    if not check_password(password or "", user["password_hash"]):
        failed = user["failed"] + 1
        locked = now + LOCK_MINUTES * 60 if failed >= MAX_FAILED_TOTAL else 0
        with db.connect() as con:
            con.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (0 if locked else failed, locked, user["id"]))
        event(None, user, "неудачный вход", f"учётная запись закрыта на {LOCK_MINUTES} мин (неверных паролей подряд — "
                                            f"{failed}, с разных адресов)" if locked else f"неверный пароль ({failed})", ip)
        raise AuthError("неверный логин или пароль")
    if not user["active"]:
        event(None, user, "неудачный вход", "учётная запись заблокирована", ip)
        raise AuthError("учётная запись заблокирована — обратитесь к администратору")
    token = secrets.token_urlsafe(32)
    with db.connect() as con:
        con.execute("UPDATE users SET failed=0, locked_until=0, last_login_at=? WHERE id=?", (now, user["id"]))
        con.execute("INSERT INTO sessions(token_hash, user_id, created_at, seen_at, expires_at, ip, agent, remember) "
                    "VALUES (?,?,?,?,?,?,?,?)", (_token_hash(token), user["id"], now, now,
                                                 now + session_window(remember), ip, (agent or "")[:200],
                                                 1 if remember else 0))
        con.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
    event(user, user, "вход", "", ip)
    return get_user(user["id"]), token


def session_user(token: str | None) -> dict | None:
    """Учётная запись по ключу сеанса из cookie (None — не вошёл, сеанс истёк или сотрудник заблокирован)."""
    if not token:
        return None
    now = time.time()
    with db.connect() as con:
        s = con.execute("SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)).fetchone()
        if not s or s["expires_at"] < now:
            return None
        hard = (s["created_at"] or now) + SESSION_MAX_DAYS * 86400        # не дольше года после входа
        if hard < now:
            return None
        if now - (s["seen_at"] or 0) > 300:
            # Посещение продлевает сеанс (0.17.0): выход — только после долгого перерыва.
            remember = bool(s["remember"]) if "remember" in s.keys() else True
            con.execute("UPDATE sessions SET seen_at=?, expires_at=? WHERE token_hash=?",
                        (now, min(hard, max(s["expires_at"], now + session_window(remember))), s["token_hash"]))
    user = get_user(s["user_id"])
    return user if user and user["active"] else None


def logout(token: str | None) -> None:
    if token:
        with db.connect() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def end_sessions(user_id: int, actor: dict | None, ip: str = "", keep_token: str | None = None) -> int:
    with db.connect() as con:
        n = con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?",
                        (user_id, _token_hash(keep_token) if keep_token else "")).rowcount
    if n:
        event(actor, get_user(user_id), "входы закрыты", f"закрыто сеансов: {n}", ip)
    return n


def sessions_count() -> dict[int, int]:
    with db.connect() as con:
        return {r[0]: r[1] for r in con.execute(
            "SELECT user_id, COUNT(*) FROM sessions WHERE expires_at>? GROUP BY user_id", (time.time(),))}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- журнал ----------

def event(actor: dict | None, user: dict | None, action: str, details: str = "", ip: str = "") -> None:
    with db.connect() as con:
        con.execute("INSERT INTO user_events(at, actor_id, actor, user_id, user, action, details, ip) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), actor["id"] if actor else None, (actor or {}).get("login", ""),
                     user["id"] if user else None, (user or {}).get("login", ""), action, details[:500], ip))


def events(limit: int = 200, user_id: int | None = None, among: set[int] | None = None) -> list[dict]:
    """Журнал; among — только о сотрудниках из этого набора или сделанное ими (админ клиента видит своих)."""
    q, args, where = "SELECT * FROM user_events", [], []
    if user_id:
        where.append("user_id=?")
        args.append(user_id)
    if among is not None:
        ids = sorted(among) or [-1]
        marks = ",".join("?" * len(ids))
        where.append(f"(user_id IN ({marks}) OR actor_id IN ({marks}))")
        args += ids + ids
    if where:
        q += " WHERE " + " AND ".join(where)
    with db.connect() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]


def _clients_text(clients: list[str]) -> str:
    if clients == ["*"]:
        return "все"
    try:
        from .config import load_clients
        names = {c.id: c.name for c in load_clients().values()}
    except Exception:
        names = {}
    return ", ".join(names.get(c, c) for c in clients)


_FIELD_TITLES = {"login": "логин", "name": "ФИО", "email": "e-mail", "phone": "телефон", "role": "роль",
                 "clients": "клиенты", "active": "активна", "note": "заметка"}


def _diff_text(before: dict, changes: dict) -> str:
    parts = []
    for k, v in changes.items():
        if k == "role":
            parts.append(f"роль: {ROLES.get(before['role'], {}).get('title')} → {ROLES[v]['title']}")
        elif k == "clients":
            parts.append(f"клиент: {_clients_text(before['clients'])} → {_clients_text(v)}")
        elif k == "blocks_off":
            names = lambda keys: ", ".join(BLOCKS[b].split(" (")[0] for b in keys) or "нет"
            parts.append(f"выключенные разделы: {names(before.get('blocks_off') or [])} → {names(v)}")
        elif k == "active":
            continue
        elif k == "note":
            parts.append("заметка изменена")
        else:
            parts.append(f"{_FIELD_TITLES.get(k, k)}: «{before.get(k) or ''}» → «{v}»")
    return "; ".join(parts)


# ---------- тестовые учётные записи (проверка входа, 0.6.4) ----------

# По одной на каждую роль + сотрудник только одного клиента с временным паролем (проверить смену пароля при первом
# входе и доступ к клиентам). Узнаются по заметке DEMO_NOTE — настоящие учётные записи команда не трогает никогда.
DEMO_NOTE = "тестовая учётная запись (demo-users); удалить: python -m app.auth demo-users --delete"
DEMO_USERS = [
    # логин, ФИО, роль, клиенты, временный пароль (сменить при первом входе)
    ("test.admin", "Тест Администратор сайта", "admin", ["*"], False),
    ("test.cladmin", "Тест Администратор клиента", "client_admin", ["main"], False),
    ("test.tender", "Тест Тендерный специалист", "tender", ["main"], False),
    ("test.buyer", "Тест Закупщик", "buyer", ["main"], False),
    ("test.logist", "Тест Логист", "logist", ["main"], False),
    ("test.viewer", "Тест Наблюдатель", "viewer", ["main"], False),
    ("test.sargsyan", "Тест Сотрудник ИП Саргсян", "tender", ["sargsyan"], True),
]


def _is_demo(user: dict | None) -> bool:
    return bool(user) and user["login"] in {d[0] for d in DEMO_USERS} and user.get("note") == DEMO_NOTE


def demo_users(known_clients: set[str], ip: str = "командная строка") -> list[dict]:
    """Создать тестовые учётные записи (или выдать им новые пароли, если уже есть). Возвращает строки для печати:
    login, name, role, clients, password, must_change, state ('создана' / 'новый пароль' / 'логин занят')."""
    out = []
    for login_name, name, role, clients, temp in DEMO_USERS:
        clients = clients if clients == ["*"] else ([c for c in clients if c in known_clients]
                                                    or sorted(known_clients)[:1] or ["*"])
        row = {"login": login_name, "name": name, "role": role, "clients": clients, "must_change": temp, "password": ""}
        user = find_user(login_name)
        if user and not _is_demo(user):
            out.append({**row, "state": "логин занят настоящей учётной записью — не трогаю"})
            continue
        pw = temp_password()
        if user:
            with db.connect() as con:
                con.execute("UPDATE users SET password_hash=?, must_change=?, failed=0, locked_until=0, active=1, "
                            "role=?, clients=?, name=?, password_changed_at=?, updated_at=? WHERE id=?",
                            (hash_password(pw), 1 if temp else 0, role, json.dumps(clients), name,
                             time.time(), time.time(), user["id"]))
                con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            event(None, user, "пароль сброшен", "тестовые учётные записи (demo-users), все входы закрыты", ip)
            out.append({**row, "password": pw, "state": "новый пароль"})
        else:
            create_user({"login": login_name, "name": name, "role": role, "clients": clients, "note": DEMO_NOTE,
                         "password": pw, "must_change": True if temp else False}, None, known_clients, ip)
            out.append({**row, "password": pw, "state": "создана"})
    return out


def delete_demo_users(ip: str = "командная строка") -> list[tuple[str, str]]:
    """Удалить только тестовые учётные записи (по заметке DEMO_NOTE). Возвращает [(логин, что сделано)]."""
    out = []
    for login_name, *_ in reversed(DEMO_USERS):          # администратор — последним
        user = find_user(login_name)
        if not user:
            continue
        if not _is_demo(user):
            out.append((login_name, "настоящая учётная запись — не трогаю"))
            continue
        try:
            if count_users() == 1:
                # Последняя учётная запись вообще — удаляем и её: админ-панель снова предложит «Первого администратора».
                with db.connect() as con:
                    con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
                    con.execute("DELETE FROM users WHERE id=?", (user["id"],))
                event(None, user, "удалена", "тестовые учётные записи (demo-users)", ip)
            else:
                delete_user(user["id"], None, ip)
            out.append((login_name, "удалена"))
        except AuthError as exc:
            out.append((login_name, f"не удалена: {exc}"))
    return out[::-1]


def _cli_demo(delete: bool) -> int:
    if delete:
        rows = delete_demo_users()
        for login_name, what in rows:
            print(f"{login_name:<16} {what}")
        if not rows:
            print("Тестовых учётных записей нет.")
        return 0
    try:
        from .config import load_clients
        known = {c.id for c in load_clients().values()}
    except Exception:
        known = set()
    rows = demo_users(known)
    print("Тестовые учётные записи (пароли показываются только сейчас; запустить команду ещё раз — новые пароли):\n")
    print(f"{'Логин':<16} {'Пароль':<14} {'Роль':<22} Клиенты")
    for r in rows:
        if not r["password"]:
            print(f"{r['login']:<16} {'—':<14} {r['state']}")
            continue
        role = ROLES[r["role"]]["title"]
        note = "  (временный — сменить при первом входе)" if r["must_change"] else ""
        print(f"{r['login']:<16} {r['password']:<14} {role:<22} {_clients_text(r['clients'])}{note}")
    others = [u for u in list_users() if not _is_demo(u)]
    if others:
        print("\nДругие учётные записи (команда их не меняла):")
        for u in others:
            print(f"{u['login']:<16} {ROLES.get(u['role'], {}).get('title', u['role']):<22} "
                  f"{'активна' if u['active'] else 'заблокирована'}  {u['name']}")
    print("\nВход: http://127.0.0.1:8000/login (нужно LOGIN_PAGE=1 в .env). "
          "Удалить тестовые: python -m app.auth demo-users --delete")
    return 0


# ---------- учётные записи для начала работы (0.7.0) ----------

# Решение пользователя 03.11.2026: администратор и по сотруднику на каждого клиента. ФИО, логины, роли и доступ
# потом меняются в админ-панели.
STANDARD_ACCOUNTS = [
    # логин, ФИО, роль, клиенты
    ("admin", "Администратор", "admin", ["*"]),
    ("nasha", "Сотрудник — Наша компания", "tender", ["main"]),
    ("sargsyan", "Сотрудник — ИП Саргсян", "tender", ["sargsyan"]),
]


def _cli_setup(known_clients: set[str]) -> int:
    print("Учётные записи (временные пароли показываются только сейчас; при первом входе их нужно сменить):\n")
    print(f"{'Логин':<12} {'Пароль':<14} {'Роль':<22} Клиенты")
    for login_name, name, role, clients in STANDARD_ACCOUNTS:
        if find_user(login_name):
            print(f"{login_name:<12} {'—':<14} уже есть — не трогаю (новый пароль: python -m app.auth reset-password {login_name})")
            continue
        missing = [c for c in clients if c != "*" and c not in known_clients]
        if missing:
            print(f"{login_name:<12} {'—':<14} не создана: в config/clients.json нет клиента {', '.join(missing)}")
            continue
        user, pw = create_user({"login": login_name, "name": name, "role": role, "clients": clients}, None,
                               known_clients, "командная строка")
        print(f"{login_name:<12} {pw:<14} {ROLES[role]['title']:<22} {_clients_text(user['clients'])}")
    demo = [u for u in list_users() if _is_demo(u)]
    if demo:
        print("\n⚠ Есть тестовые учётные записи (" + ", ".join(u["login"] for u in demo) + ") — с включённым входом "
              "ими тоже можно войти. Удалить: python -m app.auth demo-users --delete")
    print("\nВход: http://127.0.0.1:8000 (откроется страница входа).")
    return 0


# ---------- командная строка (если забыли пароль администратора) ----------

def _cli(argv: list[str]) -> int:
    init()
    if argv and argv[0] == "demo-users" and argv[1:] in ([], ["--delete"]):
        return _cli_demo(argv[1:] == ["--delete"])
    if argv == ["setup-accounts"]:
        try:
            from .config import load_clients
            known = {c.id for c in load_clients().values()}
        except Exception:
            known = set()
        return _cli_setup(known)
    if len(argv) == 2 and argv[0] == "reset-password":
        user = find_user(argv[1])
        if not user:
            print(f"учётной записи «{argv[1]}» нет")
            return 1
        pw = reset_password(user["id"], None, "командная строка")
        with db.connect() as con:
            con.execute("UPDATE users SET active=1 WHERE id=?", (user["id"],))
        print(f"Новый временный пароль для «{user['login']}»: {pw}\nПри входе его нужно будет сменить.")
        return 0
    if argv == ["list"]:
        for u in list_users():
            print(f"{u['login']:<20} {ROLES.get(u['role'], {}).get('title', u['role']):<22} "
                  f"{'активна' if u['active'] else 'заблокирована'}  {u['name']}")
        return 0
    print("использование: python -m app.auth setup-accounts | reset-password <логин> | list | demo-users [--delete]")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
