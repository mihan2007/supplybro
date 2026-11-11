"""
База NocoDB клиента — выбор в админ-панели (0.21.0).

Пользователь 11.11.2026: «можем ли мы в админ-панели сделать возможность переключаться к разным базам данных NocoDB…
нужно, чтобы переключение происходило без проблем». Выбор хранится в app.db (таблица client_bases, создаёт db.init),
действует сразу на сайте и в обработчике (config.load_clients читает её на каждый запрос) и главнее NOCODB_URL_<КЛИЕНТ>
в .env и config/clients.json. У ноутбука и сервера свои app.db — переключение действует там, где его сделали.

Перед переключением — проверка (check): ключ NocoDB видит базу, таблица реестра — из этой базы и в ней есть
«Внутренний номер» (по нему сайт находит запись конкурса); колонки и представления реестра сравниваются с реестром,
с которым сайт работает сейчас, — чего нет, показывается предупреждением (нужные колонки сайт досоздаёт сам при
записи анализа, как агент). Без ошибок и с подтверждёнными предупреждениями — сохранение (save).

Что ещё сделано, чтобы переключение было безопасным: ссылки сайта на записи NocoDB (db.noco_links) помнят базу —
номер записи из другой базы не используется; своя база цен (prices/history) и строки спецификаций (price_summary) —
отдельно для каждой базы; таблицы спецификаций — только из базы клиента (noco_tables.own_table, 0.16.1).
Идущие задачи доделываются со старой базой (клиент берётся в начале задачи).
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, replace

import httpx

from . import db
from .config import (NOCO_KEYS, Client, NocoSettings, forget_admin_bases, load_clients, noco_from_url, noco_layers,
                     raw_clients)

INTERNAL_COLUMN = "Внутренний номер"
REGISTRY_TITLES = ("Реестр конкурсов",)          # как искать реестр, если выбрана только база
SPEC_TITLE_RE = re.compile(r"^К\d+$")
SYSTEM_UIDT = {"ID", "CreatedTime", "LastModifiedTime", "CreatedBy", "LastModifiedBy", "Order", "Deleted", "Meta"}


class BaseError(RuntimeError):
    """Понятная пользователю ошибка проверки или сохранения."""


# ---------- NocoDB ----------

def _get(s: NocoSettings, path: str) -> httpx.Response:
    try:
        return httpx.get(s.url.rstrip("/") + "/api/v2" + path, headers={"xc-token": s.token()}, timeout=30,
                         trust_env=True)
    except httpx.HTTPError as exc:
        raise BaseError(f"нет связи с NocoDB ({type(exc).__name__})") from exc


def _json(s: NocoSettings, path: str, what: str) -> dict:
    r = _get(s, path)
    if r.status_code in (401, 403, 404, 422):
        raise BaseError(f"{what}: NocoDB не даёт доступа (ответ {r.status_code}) — нет такой базы / таблицы "
                        "или ключ NocoDB сайта её не видит")
    if r.status_code >= 400:
        raise BaseError(f"{what}: ответ NocoDB {r.status_code}: {r.text[:200]}")
    return r.json()


def workspace_bases(s: NocoSettings) -> list[dict]:
    """Базы рабочего пространства клиента: [{id, title}] (для выбора в админ-панели)."""
    data = _json(s, f"/meta/workspaces/{s.workspace_id}/bases", "список баз")
    return [{"id": b["id"], "title": str(b.get("title") or "")} for b in data.get("list", [])]


def _tables(s: NocoSettings, base_id: str) -> dict[str, str]:
    data = _json(s, f"/meta/bases/{base_id}/tables", "таблицы базы")
    return {t["id"]: str(t.get("title") or "") for t in data.get("list", [])}


def _registry_meta(s: NocoSettings, table_id: str) -> tuple[list[str], list[str]]:
    meta = _json(s, f"/meta/tables/{table_id}", "реестр")
    cols = [str(c.get("title") or "") for c in meta.get("columns", []) if c.get("uidt") not in SYSTEM_UIDT
            and not c.get("system")]
    views = [str(v.get("title") or "") for v in meta.get("views", [])]
    if not views:
        views = [str(v.get("title") or "") for v in _json(s, f"/meta/tables/{table_id}/views", "представления")
                 .get("list", [])]
    return cols, views


def _records_count(s: NocoSettings, table_id: str) -> int | None:
    try:
        data = _json(s, f"/tables/{table_id}/records?limit=1", "записи реестра")
    except BaseError:
        return None
    info = data.get("pageInfo") or {}
    total = info.get("totalRows")
    return int(total) if isinstance(total, int) else len(data.get("list", []))


# ---------- проверка ----------

def _target(client: Client, url: str = "", base_id: str = "") -> NocoSettings:
    """Настройки NocoDB, которые получатся: из адреса реестра или (base_id) — реестр с тем же названием в базе."""
    s = client.nocodb
    if url.strip():
        try:
            parsed = noco_from_url(url)
        except ValueError as exc:
            raise BaseError(str(exc)) from None
        return replace(s, **parsed)
    base_id = base_id.strip()
    if not re.fullmatch(r"p[a-z0-9]+", base_id or ""):
        raise BaseError("выберите базу из списка или вставьте адрес «Реестра конкурсов» из NocoDB")
    tables = _tables(s, base_id)
    # Реестр в выбранной базе: таблица «Реестр конкурсов…» (в базе ИП Саргсян их две — пустой «Реестр конкурсов» и
    # рабочий «Реестр конкурсов copy»). Одна — она; несколько — с тем же названием, что у реестра сейчас, если она
    # не пустая, иначе с наибольшим числом записей (какая выбрана — видно в проверке; другую — адресом).
    cands = [tid for tid, t in tables.items() if t.strip().lower().startswith(REGISTRY_TITLES[0].lower())]
    if not cands:
        raise BaseError(f"в этой базе нет таблицы «{REGISTRY_TITLES[0]}» — откройте нужный реестр в NocoDB и "
                        "вставьте его адрес из строки браузера")
    if len(cands) == 1:
        return replace(s, base_id=base_id, registry_table_id=cands[0])
    counts = {tid: _records_count(s, tid) or 0 for tid in cands}
    current_title = (_title_of(s, s.registry_table_id) or "").strip()
    same = [tid for tid in cands if tables[tid].strip() == current_title and counts[tid]]
    reg = same[0] if same else max(cands, key=lambda tid: counts[tid])
    return replace(s, base_id=base_id, registry_table_id=reg)


def _title_of(s: NocoSettings, table_id: str) -> str:
    try:
        return str(_json(s, f"/meta/tables/{table_id}", "реестр").get("title") or "")
    except BaseError:
        return ""


def check(client: Client, url: str = "", base_id: str = "") -> dict:
    """Проверка базы перед переключением. Возвращает {ok, errors, warnings, target, base_title, registry_title,
    records, specs, missing_columns, missing_views, same}; ok — можно переключать (предупреждения — подтвердить)."""
    errors: list[str] = []
    warnings: list[str] = []
    cur = client.nocodb
    try:
        new = _target(client, url, base_id)
    except BaseError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": []}
    out: dict = {"target": {k: getattr(new, k) for k in NOCO_KEYS},
                 "same": (new.base_id, new.registry_table_id) == (cur.base_id, cur.registry_table_id)}
    if new.url.rstrip("/") != cur.url.rstrip("/"):
        errors.append(f"другой адрес NocoDB ({new.url}) — ключ сайта подходит только к {cur.url}")
        return {**out, "ok": False, "errors": errors, "warnings": warnings}
    try:
        bases = {b["id"]: b["title"] for b in workspace_bases(new)}
    except BaseError:
        bases = {}
    out["base_title"] = bases.get(new.base_id, "")
    try:
        tables = _tables(new, new.base_id)
    except BaseError as exc:
        return {**out, "ok": False, "errors": [str(exc)], "warnings": []}
    if new.registry_table_id not in tables:
        errors.append("таблица реестра не из этой базы — скопируйте адрес реестра заново, открыв его в нужной базе")
        return {**out, "ok": False, "errors": errors, "warnings": warnings}
    out["registry_title"] = tables[new.registry_table_id]
    others = [t for tid, t in tables.items() if tid != new.registry_table_id
              and t.strip().lower().startswith(REGISTRY_TITLES[0].lower())]
    if others:
        warnings.append(f"в базе есть и другие реестры: {', '.join('«' + t + '»' for t in others)} — выбран "
                        f"«{out['registry_title']}». Нужен другой — вставьте адрес именно его из строки браузера.")
    out["specs"] = sum(1 for t in tables.values() if SPEC_TITLE_RE.match(t.strip()))
    try:
        cols, views = _registry_meta(new, new.registry_table_id)
    except BaseError as exc:
        return {**out, "ok": False, "errors": [str(exc)], "warnings": []}
    out["records"] = _records_count(new, new.registry_table_id)
    if INTERNAL_COLUMN not in cols:
        errors.append(f"в реестре нет колонки «{INTERNAL_COLUMN}» — по ней сайт находит запись конкурса; "
                      "похоже, выбрана не та таблица")
    missing_cols: list[str] = []
    missing_views: list[str] = []
    if not out["same"]:
        try:
            ref_cols, ref_views = _registry_meta(cur, cur.registry_table_id)
        except BaseError:
            ref_cols, ref_views = [], []
            warnings.append("реестр, с которым сайт работает сейчас, не прочитался — сравнить колонки не с чем")
        missing_cols = [c for c in ref_cols if c not in cols]
        missing_views = [v for v in ref_views if v not in views]
        if missing_cols:
            warnings.append(f"в новом реестре нет колонок ({len(missing_cols)}), которые есть сейчас: "
                            + ", ".join(f"«{c}»" for c in missing_cols[:12]) + (" …" if len(missing_cols) > 12 else "")
                            + ". Нужные для записи анализа сайт создаст сам; остальное (карточка, представления) "
                              "покажет пустым.")
        if missing_views:
            warnings.append("нет представлений: " + ", ".join(f"«{v}»" for v in missing_views)
                            + " — у кого такое представление выбрано по роли, откроется «Вся таблица».")
        if cur.default_registry_view and cur.default_registry_view not in views:
            warnings.append(f"нет представления «{cur.default_registry_view}» — карточка конкурса покажет поля "
                            "реестра без него")
    others_here = [c.name for cid, c in load_clients().items() if cid != client.id and c.nocodb.base_id == new.base_id]
    if others_here and not out["same"]:
        warnings.append(f"с этой базой уже работает {', '.join('«' + n + '»' for n in others_here)} — конкурсы с "
                        "одинаковыми номерами станут общими: анализ, цены и заявки обоих клиентов пойдут в одни "
                        "записи (записи, заполненные агентом, сайт без подтверждения не меняет)")
    if out["specs"] == 0 and out.get("records"):
        warnings.append("в базе нет таблиц спецификаций («К…») — позиции и цены у старых конкурсов будут пустыми")
    out.update(ok=not errors, errors=errors, warnings=warnings, missing_columns=missing_cols,
               missing_views=missing_views)
    return out


# ---------- выбор ----------

def _client(client_id: str) -> Client:
    client = load_clients().get(client_id)
    if not client:
        raise BaseError("клиент не найден")
    return client


def state(client_ids: list[str] | None = None) -> list[dict]:
    """Клиенты и их базы: что действует сейчас и откуда (админ-панель / .env / настройки проекта), названия баз."""
    raw = raw_clients()
    clients = load_clients()
    admin_rows = {r["client_id"]: r for r in _rows()}
    out = []
    titles_by_ws: dict[str, dict[str, str]] = {}
    for cid, c in clients.items():
        if client_ids is not None and cid not in client_ids:
            continue
        s = c.nocodb
        if s.workspace_id not in titles_by_ws:
            try:
                titles_by_ws[s.workspace_id] = {b["id"]: b["title"] for b in workspace_bases(s)}
            except Exception:
                titles_by_ws[s.workspace_id] = {}
        titles = titles_by_ws[s.workspace_id]
        layers = noco_layers(cid, raw.get(cid, {}).get("nocodb", {}))
        row = admin_rows.get(cid)
        out.append({
            "id": cid, "name": c.name, "folder": c.tenders_folder,
            "current": {**{k: getattr(s, k) for k in NOCO_KEYS}, "source": s.source,
                        "base_title": titles.get(s.base_id, "") or (row or {}).get("base_title", "")},
            "settings": {**layers["settings"], "base_title": titles.get(layers["settings"]["base_id"], "")},
            "env": {**layers["env"], "base_title": titles.get(layers["env"]["base_id"], "")} if layers["env"] else None,
            "admin": {**{k: row[k] for k in NOCO_KEYS}, "base_title": row["base_title"],
                      "registry_title": row["registry_title"], "updated_by": row["updated_by"],
                      "updated_at": row["updated_at"]} if row else None,
            "bases": [{"id": i, "title": t, "used_by": [o.name for o in clients.values() if o.nocodb.base_id == i]}
                      for i, t in sorted(titles.items(), key=lambda x: x[1].lower())],
        })
    return out


def _rows() -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM client_bases")]


def save(client_id: str, url: str, base_id: str, by: str, confirm: bool = False) -> dict:
    """Переключить базу клиента. Ошибки проверки — BaseError; предупреждения без confirm — {"need_confirm": True}."""
    client = _client(client_id)
    res = check(client, url, base_id)
    if not res.get("ok"):
        raise BaseError("; ".join(res.get("errors") or ["база не прошла проверку"]))
    if res["warnings"] and not confirm:
        return {"saved": False, "need_confirm": True, "check": res}
    t = res["target"]
    before = asdict(client.nocodb)
    with db.connect() as con:
        con.execute("""INSERT OR REPLACE INTO client_bases(client_id, url, workspace_id, base_id, registry_table_id,
                       base_title, registry_title, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (client_id, t["url"], t["workspace_id"], t["base_id"], t["registry_table_id"],
                     res.get("base_title", ""), res.get("registry_title", ""), by, time.time()))
    forget_admin_bases()
    return {"saved": True, "check": res, "before": before, "after": t}


def reset(client_id: str) -> dict:
    """Убрать выбор админ-панели: снова база из .env или config/clients.json."""
    client = _client(client_id)
    before = asdict(client.nocodb)
    with db.connect() as con:
        n = con.execute("DELETE FROM client_bases WHERE client_id=?", (client_id,)).rowcount
    forget_admin_bases()
    return {"changed": bool(n), "before": before, "after": asdict(_client(client_id).nocodb)}
