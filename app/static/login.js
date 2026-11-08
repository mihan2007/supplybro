// Страница входа (/login): вход, смена временного пароля, «что доступно сотруднику», выход.
// Если вход на сайт обязателен (LOGIN_REQUIRED=1, app/access.py), после входа сразу ведёт туда, откуда пришли
// (?next=…), или на свою страницу; иначе только проверяет логин и пароль (app/login.py).
"use strict";

const $ = (id) => document.getElementById(id);
let st = null;          // /api/auth/state: has_users, me, access, min_password, required
let justEntered = false; // только что вошли / сменили пароль — пора на сайт

// Куда вернуться после входа: только свой сайт (не //чужой.сайт) и не сама страница входа. Пробелы, табуляция и
// служебные символы браузер из адреса выбрасывает («/<Tab>/чужой.сайт» → «//чужой.сайт») — такие не принимаем (0.16.0),
// и в конце сверяем, что адрес действительно на этом сайте.
function safeNext() {
  const v = new URLSearchParams(location.search).get("next") || "";
  if (/[\u0000-\u0020\u007f]/.test(v)) return "";
  if (!v.startsWith("/") || v.startsWith("//") || v.includes("\\") || v.startsWith("/login")) return "";
  try {
    const u = new URL(v, location.origin);
    if (u.origin !== location.origin || u.pathname.startsWith("//") || u.pathname.startsWith("/login")) return "";
    return u.pathname + u.search + u.hash;
  } catch (_) { return ""; }
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
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit" });
}

function show(view) {
  for (const v of ["v-empty", "v-login", "v-change", "v-me"]) $(v).hidden = v !== view;
}

async function boot() {
  try {
    st = await api("/api/auth/state");
  } catch (e) {
    document.querySelector("main").replaceChildren(Object.assign(document.createElement("div"),
      { className: "notice bad", textContent: "Страница входа недоступна: " + e.message }));
    return;
  }
  // Вход обязателен (сайт может быть в интернете) — первого администратора через сайт не создать, только командой.
  if (!st.me && !st.has_users) { $("e-admin").hidden = Boolean(st.required); return show("v-empty"); }
  if (!st.me) return showLogin();
  if (st.me.must_change) return showChange(true);
  // Вход обязателен: после входа — сразу на сайт (туда, откуда пришли, или на свою страницу).
  if (st.required && (justEntered || safeNext())) {
    location.replace(safeNext() || st.access.home.url);
    return;
  }
  showMe();
}

// ---------- вход ----------

function showLogin() {
  show("v-login");
  document.title = "SupplyBro — вход";
  $("l-pass").value = "";
  ($("l-login").value ? $("l-pass") : $("l-login")).focus();
}

$("l-show").addEventListener("change", () => { $("l-pass").type = $("l-show").checked ? "text" : "password"; });

$("v-login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("l-submit");
  btn.disabled = true;
  setStatus("l-status", "Проверяю…");
  try {
    await api("/api/auth/login", "POST", { login: $("l-login").value, password: $("l-pass").value });
    justEntered = true;
    setStatus("l-status", "");
    $("toast").hidden = true;
    $("l-pass").value = "";
    $("l-show").checked = false;
    $("l-pass").type = "password";
    await boot();
  } catch (err) {
    setStatus("l-status", err.message, true);
    $("l-pass").select();
  } finally {
    btn.disabled = false;
  }
});

// ---------- смена пароля ----------

function showChange(forced) {
  show("v-change");
  $("c-title").textContent = forced ? "Смените временный пароль" : "Сменить пароль";
  $("c-note").textContent = forced
    ? `Вы вошли временным паролем — придумайте свой. Не короче ${st.min_password} символов, без логина внутри.`
    : `Не короче ${st.min_password} символов. Другие ваши входы (в других браузерах и на других компьютерах) будут закрыты.`;
  $("c-cancel").hidden = forced;
  for (const id of ["c-old", "c-new", "c-new2"]) $(id).value = "";
  setStatus("c-status", "");
  $("c-old").focus();
}

$("v-change").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("c-new").value !== $("c-new2").value) return setStatus("c-status", "новые пароли не совпадают", true);
  try {
    await api("/api/auth/password", "POST", { old: $("c-old").value, new: $("c-new").value });
    justEntered = true;
    toast("Пароль изменён");
    boot();
  } catch (err) {
    if (err.status === 401) return boot();
    setStatus("c-status", err.message, true);
  }
});
$("c-cancel").addEventListener("click", () => boot());

// ---------- вошли ----------

function showMe() {
  const me = st.me, a = st.access;
  show("v-me");
  document.title = `SupplyBro — ${me.name}`;
  $("m-name").textContent = me.name;
  $("m-login").textContent = `логин ${me.login}` + (me.last_login_at ? ` · вход ${when(me.last_login_at)}` : "");
  $("m-role").textContent = a.role_title;
  $("m-role-text").textContent = a.role_text;

  const cl = $("m-clients");
  cl.textContent = a.clients.length ? a.clients.join(", ") : "нет клиентов";
  if (a.all_clients) cl.append(" ", Object.assign(document.createElement("span"),
    { className: "muted", textContent: "(все клиенты, и новые тоже)" }));

  $("m-view").textContent = a.view ? `представление «${a.view}»` : "—";

  const home = $("m-home");
  home.textContent = `${a.home.title} (${a.home.url === "/" && !a.home.on ? "/user" : a.home.url})`;
  if (!a.home.on) home.append(" ", Object.assign(document.createElement("span"),
    { className: "muted", textContent: "— сейчас выключена (USER_PAGE=0), пока откроется обычная страница" }));

  $("m-perms").replaceChildren(...a.permissions.map((p) =>
    Object.assign(document.createElement("li"), { className: p.allowed ? "" : "no", textContent: p.title })));

  const open = $("m-open");
  open.href = a.home.url;
  open.textContent = a.home.open;
  $("m-admin").hidden = !a.admin_panel;
  // Вход обязателен — это уже не «проверка»: подсказку убираем.
  $("m-notice").hidden = Boolean(st.required);
}

$("m-pass").addEventListener("click", () => showChange(false));

async function logout() {
  try { await api("/api/auth/logout", "POST"); } catch (_) { /* всё равно на вход */ }
  toast("Вы вышли");
  boot();
}
$("m-logout").addEventListener("click", logout);
$("c-logout").addEventListener("click", logout);
$("e-retry").addEventListener("click", () => boot());

boot();
