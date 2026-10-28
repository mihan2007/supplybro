// Админ-панель учётных записей (/admin): первый администратор, вход, сотрудники, роли, журнал.
"use strict";

const $ = (id) => document.getElementById(id);
let st = null;          // /api/admin/state: me, roles, permissions, clients
let users = [];
let editing = null;     // учётная запись в карточке (null — новая)
let tab = "users";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

async function api(path, method = "GET", body) {
  const opt = { method, headers: { "X-SupplyBro": "1" } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (_) { /* не JSON */ }
  if (!r.ok) {
    const err = new Error(data.detail || `ошибка ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

function setStatus(id, text, bad = false) {
  const s = $(id);
  s.textContent = text || "";
  s.classList.toggle("error", Boolean(bad));
}

let toastTimer = null;
function toast(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 8000 : 4000);
}

function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const days = (Date.now() - d) / 86400000;
  return days < 1 && d.getDate() === new Date().getDate()
    ? "сегодня " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function clientsText(list) {
  if (!list || list[0] === "*") return "все клиенты";
  const known = (st.clients || []).filter((c) => list.includes(c.id)).map((c) => c.name);
  const unknown = list.filter((id) => !(st.clients || []).some((c) => c.id === id));
  return [...known, ...unknown].join(", ");
}

// ---------- какой экран показать ----------

function show(view) {
  for (const v of ["v-setup", "v-login", "v-change", "v-main"]) $(v).hidden = v !== view;
  const me = st && st.me;
  $("me-name").textContent = me && view === "v-main" ? `${me.name} · ${me.role_title}` : "";
  $("me-pass").hidden = $("logout").hidden = !(me && view === "v-main");
}

async function boot() {
  try {
    st = await api("/api/admin/state");
  } catch (e) {
    document.querySelector("main").replaceChildren(el("div", { class: "notice bad", text: "Админ-панель недоступна: " + e.message }));
    return;
  }
  if (!st.has_users) return showSetup();
  if (!st.me) return showLogin();
  if (st.me.must_change) return showChange(true);
  if (st.me.role !== "admin") {
    showLogin();
    setStatus("l-status", "У этой учётной записи нет прав администратора — войдите администратором.", true);
    return;
  }
  show("v-main");
  showTab(tab);
}

function showSetup() { show("v-setup"); $("s-name").focus(); }
function showLogin() { show("v-login"); $("l-login").focus(); }
function showChange(forced) {
  show("v-change");
  $("c-title").textContent = forced ? "Смените временный пароль" : "Сменить пароль";
  $("c-note").textContent = forced ? "Вы вошли временным паролем — придумайте свой. Не короче " + st.min_password + " символов." :
    "Не короче " + st.min_password + " символов. Другие ваши входы (на других компьютерах) будут закрыты.";
  $("c-cancel").hidden = forced;
  for (const id of ["c-old", "c-new", "c-new2"]) $(id).value = "";
  setStatus("c-status", "");
  $("c-old").focus();
}

$("v-setup").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("s-pass").value !== $("s-pass2").value) return setStatus("s-status", "пароли не совпадают", true);
  try {
    await api("/api/admin/setup", "POST", { name: $("s-name").value, login: $("s-login").value, password: $("s-pass").value });
    boot();
  } catch (err) { setStatus("s-status", err.message, true); }
});

$("v-login").addEventListener("submit", async (e) => {
  e.preventDefault();
  setStatus("l-status", "Проверяю…");
  try {
    await api("/api/admin/login", "POST", { login: $("l-login").value, password: $("l-pass").value });
    $("l-pass").value = "";
    setStatus("l-status", "");
    boot();
  } catch (err) { setStatus("l-status", err.message, true); }
});

$("v-change").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("c-new").value !== $("c-new2").value) return setStatus("c-status", "новые пароли не совпадают", true);
  try {
    await api("/api/admin/password", "POST", { old: $("c-old").value, new: $("c-new").value });
    toast("Пароль изменён");
    boot();
  } catch (err) { setStatus("c-status", err.message, true); }
});
$("c-cancel").addEventListener("click", () => boot());
$("me-pass").addEventListener("click", () => showChange(false));
$("logout").addEventListener("click", async () => {
  try { await api("/api/admin/logout", "POST"); } catch (_) { /* всё равно на вход */ }
  boot();
});

// ---------- вкладки ----------

function showTab(name) {
  tab = name;
  for (const b of $("tabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.tab === name);
  $("t-users").hidden = name !== "users";
  $("t-roles").hidden = name !== "roles";
  $("t-events").hidden = name !== "events";
  if (name === "users") loadUsers();
  if (name === "roles") renderRoles();
  if (name === "events") loadEvents();
}
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});

async function guarded(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err.status === 401 || err.status === 403) { boot(); return null; }
    throw err;
  }
}

// ---------- сотрудники ----------

async function loadUsers() {
  try {
    const d = await guarded(() => api("/api/admin/users"));
    if (!d) return;
    users = d.users;
    renderUsers();
  } catch (err) { setStatus("u-status", "Не удалось загрузить: " + err.message, true); }
}

function statePills(u) {
  const out = [];
  out.push(u.active ? el("span", { class: "pill ok", text: "активна" }) : el("span", { class: "pill bad", text: "заблокирована" }));
  if (u.locked) out.push(el("span", { class: "pill bad", text: "вход закрыт (неверные пароли)" }));
  if (u.must_change) out.push(el("span", { class: "pill warn", text: "временный пароль" }));
  if (u.sessions) out.push(el("span", { class: "pill", text: `входов: ${u.sessions}` }));
  return out;
}

function renderUsers() {
  const q = $("u-search").value.trim().toLowerCase();
  const f = $("u-filter").value;
  const list = users.filter((u) => (f === "all" || (f === "active" ? u.active : !u.active)) &&
    (!q || [u.name, u.login, u.email, u.phone, u.note].join(" ").toLowerCase().includes(q)));
  $("users-count").textContent = users.length ? `(${users.length})` : "";
  const head = el("tr", {}, ...["Сотрудник", "Роль", "Клиенты", "Состояние", "Последний вход", "Контакты"].map((t) => el("th", { text: t })));
  const body = el("tbody");
  for (const u of list) {
    const tr = el("tr", { title: "Открыть карточку" },
      el("td", {}, el("div", { class: "u-name", text: u.name }), el("div", { class: "u-login", text: u.login + (st.me && u.id === st.me.id ? " · это вы" : "") })),
      el("td", {}, el("span", { class: "pill" + (u.role === "admin" ? " role-admin" : ""), text: u.role_title })),
      el("td", { text: clientsText(u.clients) }),
      el("td", {}, ...statePills(u)),
      el("td", { text: when(u.last_login_at) }),
      el("td", { text: [u.email, u.phone].filter(Boolean).join("\n") || "—" }));
    tr.addEventListener("click", () => openEditor(u));
    body.append(tr);
  }
  if (!list.length) body.append(el("tr", {}, el("td", { colspan: "6", class: "muted", text: users.length ? "Никого не нашлось." : "Сотрудников пока нет." })));
  $("u-table").replaceChildren(el("thead", {}, head), body);
}
$("u-search").addEventListener("input", renderUsers);
$("u-filter").addEventListener("change", renderUsers);

// ---------- роли ----------

function renderRoles() {
  const perms = Object.entries(st.permissions);
  const head = el("tr", {}, el("th", { text: "Роль" }), el("th", { text: "Представление NocoDB" }),
    ...perms.map(([, t]) => el("th", { class: "perm", text: t })));
  const body = el("tbody");
  for (const [id, r] of Object.entries(st.roles)) {
    const have = new Set(st.role_permissions[id] || []);
    body.append(el("tr", {},
      el("td", {}, el("div", { class: "u-name", text: r.title }), el("div", { class: "u-login", text: r.text })),
      el("td", { text: r.view }),
      ...perms.map(([p]) => el("td", { class: have.has(p) ? "yes" : "no", text: have.has(p) ? "✓" : "—" }))));
  }
  $("r-table").replaceChildren(el("thead", {}, head), body);
}

// ---------- журнал ----------

async function loadEvents() {
  try {
    const d = await guarded(() => api("/api/admin/events"));
    if (!d) return;
    const head = el("tr", {}, ...["Когда", "Кто", "С учётной записью", "Действие", "Подробности", "Адрес"].map((t) => el("th", { text: t })));
    const body = el("tbody");
    for (const e of d.events) {
      body.append(el("tr", {}, el("td", { text: when(e.at) }), el("td", { text: e.actor || "—" }), el("td", { text: e.user || "—" }),
        el("td", { text: e.action }), el("td", { text: e.details || "" }), el("td", { text: e.ip || "" })));
    }
    if (!d.events.length) body.append(el("tr", {}, el("td", { colspan: "6", class: "muted", text: "Записей нет." })));
    $("e-table").replaceChildren(el("thead", {}, head), body);
  } catch (err) { toast("Журнал: " + err.message, true); }
}

// ---------- карточка сотрудника ----------

function fillRoles(value) {
  $("ed-role").replaceChildren(...Object.entries(st.roles).map(([id, r]) => el("option", { value: id, text: r.title })));
  $("ed-role").value = value || "tender";
  roleText();
}
function roleText() { $("ed-role-text").textContent = (st.roles[$("ed-role").value] || {}).text || ""; }
$("ed-role").addEventListener("change", roleText);

function fillClients(list) {
  const all = !list || list[0] === "*";
  $("ed-all").checked = all;
  $("ed-clients").replaceChildren(...st.clients.map((c) => el("label", { class: "check" },
    el("input", { type: "checkbox", value: c.id, checked: all || list.includes(c.id) ? "" : null, disabled: all ? "" : null }), c.name)));
}
$("ed-all").addEventListener("change", () => {
  // «Все» сняли — отметки тоже снимаются: дальше отмечаются только нужные клиенты.
  for (const cb of $("ed-clients").querySelectorAll("input")) { cb.disabled = $("ed-all").checked; cb.checked = $("ed-all").checked; }
});
for (const r of document.querySelectorAll("input[name=pw]")) {
  r.addEventListener("change", () => { $("ed-pass").hidden = document.querySelector("input[name=pw]:checked").value !== "set"; });
}

function openEditor(u) {
  editing = u || null;
  $("ed-title").textContent = u ? u.name : "Новый сотрудник";
  $("ed-name").value = u ? u.name : "";
  $("ed-login").value = u ? u.login : "";
  $("ed-email").value = u ? u.email : "";
  $("ed-phone").value = u ? u.phone : "";
  $("ed-note").value = u ? u.note : "";
  fillRoles(u ? u.role : "tender");
  fillClients(u ? u.clients : ["*"]);
  $("ed-pass-box").hidden = Boolean(u);
  document.querySelector("input[name=pw][value=temp]").checked = true;
  $("ed-pass").hidden = true;
  $("ed-pass").value = "";
  $("ed-save").textContent = u ? "Сохранить" : "Создать";
  $("ed-state").replaceChildren(...(u ? statePills(u) : []));
  setStatus("ed-status", "");
  $("ed-more").hidden = !u;
  $("ed-ask").hidden = true;
  if (u) { renderActions(u); loadHistory(u); }
  if (!$("editor").open) $("editor").showModal();
  $("ed-name").focus();
}

function formData() {
  const all = $("ed-all").checked;
  const clients = all ? ["*"] : [...$("ed-clients").querySelectorAll("input:checked")].map((cb) => cb.value);
  return { name: $("ed-name").value, login: $("ed-login").value, email: $("ed-email").value, phone: $("ed-phone").value,
    role: $("ed-role").value, clients, note: $("ed-note").value };
}

$("ed-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = formData();
  try {
    if (editing) {
      const r = await guarded(() => api(`/api/admin/users/${editing.id}`, "PATCH", data));
      if (!r) return;
      toast("Сохранено");
      $("editor").close();
      if (r.user.id === st.me.id) { boot(); return; }
    } else {
      if (document.querySelector("input[name=pw]:checked").value === "set") data.password = $("ed-pass").value;
      const r = await guarded(() => api("/api/admin/users", "POST", data));
      if (!r) return;
      $("editor").close();
      if (r.temp_password) showSecret(r.user.login, r.temp_password);
      else toast(`Учётная запись ${r.user.login} создана`);
    }
    loadUsers();
  } catch (err) { setStatus("ed-status", err.message, true); }
});
$("ed-cancel").addEventListener("click", () => $("editor").close());
$("ed-close").addEventListener("click", () => $("editor").close());

function renderActions(u) {
  const me = st.me && u.id === st.me.id;
  const btn = (text, cls, fn, title) => {
    const b = el("button", { class: "btn small " + (cls || ""), type: "button", text, title });
    b.addEventListener("click", fn);
    return b;
  };
  const out = [btn("Сбросить пароль", "", () => ask(u, "reset"), "Выдать новый временный пароль; все входы сотрудника закроются")];
  if (u.sessions) out.push(btn(me ? "Закрыть мои другие входы" : "Закрыть все входы", "", () => act(u, "end-sessions")));
  if (u.locked) out.push(btn("Открыть вход", "", () => act(u, "unlock"), "Вход закрыт после неверных паролей — открыть сейчас"));
  if (!me) {
    out.push(u.active ? btn("Заблокировать", "danger", () => ask(u, "block")) : btn("Разблокировать", "", () => act(u, "activate")));
    out.push(btn("Удалить", "danger", () => ask(u, "delete")));
  }
  $("ed-actions").replaceChildren(...out);
}

const ASK = {
  reset: ["Выдать новый временный пароль?", "Старый пароль перестанет работать, все входы сотрудника закроются. Новый пароль покажется один раз.", "Сбросить пароль"],
  block: ["Заблокировать учётную запись?", "Сотрудник не сможет войти, его входы закроются сразу. Разблокировать можно в любой момент.", "Заблокировать"],
  delete: ["Удалить учётную запись совсем?", "Это не отменить. Если сотрудник просто ушёл в отпуск или уволился — лучше заблокировать: история останется.", "Удалить"],
};

function ask(u, kind) {
  const [title, text, yes] = ASK[kind];
  const box = $("ed-ask");
  const ok = el("button", { class: "btn small " + (kind === "reset" ? "primary" : "danger"), type: "button", text: yes });
  const no = el("button", { class: "btn small", type: "button", text: "Нет" });
  ok.addEventListener("click", () => { box.hidden = true; act(u, kind); });
  no.addEventListener("click", () => { box.hidden = true; });
  box.replaceChildren(el("b", { text: title }), el("div", { text }), el("div", { class: "row-actions" }, ok, no));
  box.hidden = false;
}

async function act(u, kind) {
  try {
    let r;
    if (kind === "reset") {
      r = await guarded(() => api(`/api/admin/users/${u.id}/reset-password`, "POST"));
      if (!r) return;
      $("editor").close();
      showSecret(u.login, r.temp_password);
    } else if (kind === "end-sessions") {
      r = await guarded(() => api(`/api/admin/users/${u.id}/end-sessions`, "POST"));
      toast(`Закрыто входов: ${r ? r.closed : 0}`);
    } else if (kind === "unlock") {
      await guarded(() => api(`/api/admin/users/${u.id}/unlock`, "POST"));
      toast("Вход открыт");
    } else if (kind === "block" || kind === "activate") {
      await guarded(() => api(`/api/admin/users/${u.id}`, "PATCH", { active: kind === "activate" }));
      toast(kind === "block" ? "Учётная запись заблокирована" : "Учётная запись разблокирована");
    } else if (kind === "delete") {
      await guarded(() => api(`/api/admin/users/${u.id}`, "DELETE"));
      $("editor").close();
      toast(`Учётная запись ${u.login} удалена`);
    }
    await loadUsers();
    const fresh = users.find((x) => x.id === u.id);
    if (fresh && $("editor").open) openEditor(fresh);
  } catch (err) {
    setStatus("ed-status", err.message, true);
  }
}

async function loadHistory(u) {
  $("ed-history").replaceChildren(el("div", { text: "…" }));
  try {
    const d = await api(`/api/admin/events?user_id=${u.id}&limit=30`);
    $("ed-history").replaceChildren(...(d.events.length ? d.events.map((e) =>
      el("div", { text: `${when(e.at)} — ${e.action}${e.details ? ": " + e.details : ""}${e.actor && e.actor !== u.login ? " (" + e.actor + ")" : ""}` }))
      : [el("div", { text: "пока ничего" })]));
  } catch (_) { $("ed-history").replaceChildren(); }
}

// ---------- временный пароль ----------

function showSecret(login, pw) {
  $("sec-login").textContent = login;
  $("sec-pass").textContent = pw;
  setStatus("sec-status", "");
  $("secret").showModal();
}
$("sec-copy").addEventListener("click", async () => {
  const text = `Адрес: ${location.origin}\nЛогин: ${$("sec-login").textContent}\nВременный пароль: ${$("sec-pass").textContent}`;
  try {
    await navigator.clipboard.writeText(text);
    setStatus("sec-status", "Скопировано");
  } catch (_) {
    setStatus("sec-status", "Не удалось скопировать — выделите пароль мышью и скопируйте", true);
  }
});
$("sec-close").addEventListener("click", () => { $("secret").close(); $("sec-pass").textContent = ""; });

$("u-new").addEventListener("click", () => openEditor(null));

boot();
