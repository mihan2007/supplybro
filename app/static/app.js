// SupplyBro — логика страницы: «клиент → юр. лицо», панель «Конкурсы» (0.13.0; конкурс открывается в новой
// вкладке браузера), документы конкурса (папка в Dropbox), карточка из NocoDB и спецификация.
// Выбор запоминается в адресе страницы (?client=…&entity=…&tender=…), поэтому
// страницу можно обновить или сохранить в закладки.
"use strict";

const $ = (id) => document.getElementById(id);
const DOCS_TARGET = "Конкурсная документация"; // куда кладутся документы из формы нового конкурса
const NUMERIC = new Set(["Decimal", "Number", "Currency", "Percent", "Rating"]);
const UNKNOWN_GROUP = "Участник не указан";
const FOLDER_GROUP = "Есть папка, в NocoDB ещё нет";

const url = new URLSearchParams(location.search);
const state = {
  clients: [],
  client: url.get("client") || "",
  entity: url.get("entity") || "",
  tender: url.get("tender") || "",
  regView: url.get("rv") || "",
  specView: url.get("sv") || "",
  fresh: false,
  tenders: new Map(),   // id → конкурс из последнего списка
  nextNumber: "",
  opened: "",           // какой конкурс показан на странице (его разделы загружены)
};
// Счётчики запросов: ответ на устаревший выбор не перерисовывает страницу.
const seq = { tenders: 0, card: 0, spec: 0, docs: 0, an: 0, chat: 0, chatStatus: 0, pr: 0, ps: 0, pl: 0 };
let docsTimer = null;
let anTimer = null;
let anLastJob = null;   // последняя увиденная задача анализа — чтобы заметить её окончание
let anTab = "fields";
let prTimer = null;
let prLastJob = null;   // последняя увиденная задача поиска цен

// ---------- общее ----------

function saveUrl() {
  const p = new URLSearchParams();
  for (const [k, v] of [["client", state.client], ["entity", state.entity], ["tender", state.tender],
                        ["rv", state.regView], ["sv", state.specView]]) {
    if (v) p.set(k, v);
  }
  history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
}

// Вход закончился (сеанс истёк, учётную запись заблокировали) — на страницу входа, потом обратно сюда.
function toLogin() {
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
}

async function api(path, query = {}, options = {}) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(query)) if (v) u.searchParams.set(k, v);
  if (state.fresh) u.searchParams.set("fresh", "1");
  const r = await fetch(u, options);
  if (r.status === 401) toLogin();
  let body = {};
  try { body = await r.json(); } catch (_) { /* ответ не JSON */ }
  if (!r.ok) throw new Error(body.detail || `ошибка ${r.status}`);
  return body;
}

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

function setStatus(id, text, isError = false) {
  const s = $(id);
  s.textContent = text || "";
  s.classList.toggle("error", Boolean(isError));
}

function fillSelect(select, options, value) {
  select.replaceChildren(...options.map((o) => el("option", { value: o.value, text: o.label })));
  if (value !== undefined && options.some((o) => o.value === value)) select.value = value;
}

function entityName(id) {
  const client = state.clients.find((c) => c.id === state.client);
  const e = client && client.entities.find((x) => x.id === id);
  return e ? e.name : id;
}

function clientPath(suffix) {
  return `/api/clients/${encodeURIComponent(state.client)}${suffix}`;
}

// ---------- значения полей ----------

// Целые — без дробной части (2 шт.), дробные — с копейками (212 820,60), количество — до 3 знаков.
const fmtInt = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const fmtDec = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 3 });
const fmtNumber = { format: (v) => (Number.isInteger(v) ? fmtInt : fmtDec).format(v) };

// Текст с кликабельными ссылками (в комментариях агента ссылки идут прямо в тексте).
function linkify(text) {
  const frag = document.createDocumentFragment();
  const re = /https?:\/\/[^\s<>"«»]+/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    frag.append(text.slice(last, m.index));
    frag.append(el("a", { href: m[0], target: "_blank", rel: "noopener", text: m[0] }));
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}

function shortUrl(href) {
  try {
    const u = new URL(href);
    const path = u.pathname.length > 1 ? u.pathname : "";
    const s = u.hostname.replace(/^www\./, "") + path;
    return s.length > 40 ? s.slice(0, 38) + "…" : s;
  } catch (_) {
    return href;
  }
}

// Длинный текст сворачивается до нескольких строк; щелчок — развернуть/свернуть.
function longText(text) {
  const box = el("div", { class: "clamp", title: "Щёлкните, чтобы развернуть" });
  box.append(linkify(text));
  box.addEventListener("click", (ev) => {
    if (ev.target.tagName === "A") return;
    box.classList.toggle("open");
  });
  return box;
}

function isEmpty(value) {
  return value === null || value === undefined || value === "" ||
    (Array.isArray(value) && value.length === 0);
}

// Значение поля NocoDB → элемент страницы (по типу колонки uidt).
function renderValue(value, uidt) {
  if (isEmpty(value)) return null;
  if (Array.isArray(value)) {
    // Вложения: список файлов со ссылками на скачивание.
    if (value.length && typeof value[0] === "object") {
      return el("div", { class: "files" }, ...value.map((f) => {
        const href = f.signedUrl || f.url || "";
        const name = f.title || f.name || "файл";
        return href ? el("a", { class: "file", href, target: "_blank", rel: "noopener", text: name })
                    : el("span", { class: "file", text: name });
      }));
    }
    return document.createTextNode(value.join(", "));
  }
  if (typeof value === "boolean") return document.createTextNode(value ? "Да" : "Нет");
  if (typeof value === "number") return document.createTextNode(fmtNumber.format(value));
  if (typeof value === "object") return document.createTextNode(JSON.stringify(value));

  const text = String(value);
  if (uidt === "URL") {
    return el("a", { href: text, target: "_blank", rel: "noopener", title: text, text: shortUrl(text) + " ↗" });
  }
  if (uidt === "SingleSelect" || uidt === "MultiSelect") {
    const box = el("span");
    for (const part of text.split(",").map((s) => s.trim()).filter(Boolean)) {
      box.append(el("span", { class: "sel", text: part }));
    }
    return box;
  }
  if (text.length > 220 || text.split("\n").length > 4) return longText(text);
  const span = el("span");
  span.append(linkify(text));
  return span;
}

// ---------- списки ----------

async function loadClients() {
  try {
    const data = await api("/api/clients");
    $("version").textContent = "версия " + data.version + (USER_MODE ? " · страница сотрудника" : "");
    state.clients = data.clients;
  } catch (err) {
    setPickersHint("Не удалось загрузить список клиентов: " + err.message);
    return;
  }
  if (!state.clients.some((c) => c.id === state.client)) state.client = state.clients[0] ? state.clients[0].id : "";
  fillSelect($("client"), state.clients.map((c) => ({ value: c.id, label: c.name })), state.client);
  fillEntities();
  if (state.tender) {
    // Конкурс по ссылке (вкладка, открытая из панели «Конкурсы»): показать его сразу по одной записи, а весь
    // список — для выпадающего — догрузить одновременно (0.13.0; реестр «Наша» читается несколько секунд).
    $("pickers").classList.replace("mode-list", "mode-tender");
    $("opening").hidden = false;
    openFast();
  }
  await loadTenders();
}

async function openFast() {
  const id = state.tender;
  let d;
  try {
    d = await api(clientPath("/tender-brief"), { id });
  } catch (_) {
    return;                         // не вышло — конкурс откроет полный список
  }
  if (state.tender !== id || state.opened === id || state.list) return;   // список успел раньше
  state.tenders = new Map([[d.tender.id, d.tender]]);
  fillSelect($("tender"), [{ value: d.tender.id, label: tenderLabel(d.tender) }], d.tender.id);
  await loadTender();
}

function fillEntities() {
  const client = state.clients.find((c) => c.id === state.client);
  const entities = client ? client.entities : [];
  if (!entities.some((e) => e.id === state.entity)) state.entity = "";
  fillSelect($("entity"), [{ value: "", label: "Все юр. лица" },
    ...entities.map((e) => ({ value: e.id, label: e.inn ? `${e.name} · ИНН ${e.inn}` : e.name }))], state.entity);
}

function setPickersHint(text) {
  const h = $("pickers-hint");
  h.textContent = text || "";
  h.hidden = !text;
}

function tenderLabel(t) {
  const parts = [t.internal || "без внутр. номера"];
  if (t.number) parts.push("№ " + t.number);
  if (t.title) parts.push(t.title);
  const s = parts.join(" · ");
  return s.length > 120 ? s.slice(0, 118) + "…" : s;
}

async function loadTenders() {
  const my = ++seq.tenders;
  const select = $("tender");
  select.disabled = true;
  fillSelect(select, [{ value: "", label: "Загрузка конкурсов…" }]);
  if (!state.tender) { $("tlist").hidden = false; tlMessage("Загрузка конкурсов…"); }
  if (!state.client) { tlMessage("Нет доступных клиентов."); return; }
  let data;
  try {
    data = await api(clientPath("/tenders"), { entity: state.entity });
  } catch (err) {
    if (my !== seq.tenders) return;
    fillSelect(select, [{ value: "", label: "Не удалось загрузить конкурсы" }]);
    setPickersHint(err.message);
    tlMessage("Не удалось загрузить конкурсы: " + err.message);
    if (state.tender && state.opened !== state.tender) {
      $("opening").textContent = "Не удалось открыть конкурс: " + err.message + ". Нажмите «Обновить из NocoDB».";
    } else if (state.opened && state.tenders.has(state.opened)) {
      // конкурс уже открыт по ссылке — оставить в выпадающем списке хотя бы его
      fillSelect(select, [{ value: state.opened, label: tenderLabel(state.tenders.get(state.opened)) }], state.opened);
      select.disabled = false;
    }
    return;
  }
  if (my !== seq.tenders) return;

  const hints = [];
  if (data.noco_error) hints.push("NocoDB: " + data.noco_error);
  if (data.folders_error) hints.push("Папки конкурсов: " + data.folders_error);
  const curClient = state.clients.find((c) => c.id === state.client);
  // Подсказка про колонку «Участник» нужна, только если у клиента несколько юр. лиц (у ИП Саргсян — одно).
  if (!data.participant_column_exists && !data.noco_error && curClient && curClient.entities.length > 1) {
    hints.push(`В «Реестре конкурсов» нет колонки «${data.participant_column}», поэтому юр. лицо известно только там, ` +
      `где агент записал «Участник: …» в перечне документов заявки. Остальные конкурсы — в группе «${UNKNOWN_GROUP}».`);
  }
  setPickersHint(hints.join(" "));
  state.nextNumber = data.next_number || "";
  state.tenders = new Map(data.tenders.map((t) => [t.id, t]));
  state.list = data.tenders;

  // Группы: по юр. лицу-участнику, затем «Участник не указан», затем папки без записи в NocoDB.
  const groups = new Map();
  for (const t of data.tenders) {
    const key = t.source === "folder" ? FOLDER_GROUP
      : (t.participants.length ? t.participants.map(entityName).join(" + ") : UNKNOWN_GROUP);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  }
  const rank = (k) => (k === FOLDER_GROUP ? 2 : k === UNKNOWN_GROUP ? 1 : 0);
  const keys = [...groups.keys()].sort((a, b) => rank(a) - rank(b));
  select.replaceChildren(
    el("option", { value: "", text: data.tenders.length ? "— выберите конкурс —" : "Конкурсов нет" }));
  for (const key of keys) {
    const og = el("optgroup", { label: `${key} (${groups.get(key).length})` });
    for (const t of groups.get(key)) og.append(el("option", { value: t.id, title: t.title, text: tenderLabel(t) }));
    select.append(og);
  }
  select.disabled = false;
  if (state.tenders.has(state.tender)) {
    select.value = state.tender;
    // Уже открыт (по ссылке, раньше списка) — не загружать разделы второй раз; «Обновить из NocoDB» и окончание
    // анализа (fresh) — загрузить заново.
    if (state.opened !== state.tender || state.fresh) await loadTender();
  } else {
    state.tender = "";
    state.opened = "";
    stopDocsPolling();
    stopAnalysisPolling();
    stopPricesPolling();
    showTender(null);
    state.fresh = false;
  }
  saveUrl();
}

// ---------- панель «Конкурсы» (0.13.0) ----------
// Пользователь 04.11.2026: вместо выпадающего списка при входе — панель с конкурсами выбранного юр. лица (номер,
// заказчик, оборудование, минимум сведений); конкурс открывается в отдельной вкладке браузера. Данные — тот же
// список /tenders (сервер добавляет короткие поля из app/tender_list.py). Фильтр и сортировка запоминаются в браузере.

const TL_FILTER_KEY = "sb.tl.filter";
const TL_SORT_KEY = "sb.tl.sort";
const TL_SORTS = { no: "№", price: "НМЦ", deadline: "Подача заявок до" };
let tlSort = { key: "no", dir: -1 };
try { const s = JSON.parse(storageGet(TL_SORT_KEY) || "null"); if (s && TL_SORTS[s.key]) tlSort = s; } catch (_) { /* как по умолчанию */ }

function tlHref(t) {
  const p = new URLSearchParams({ client: state.client });
  if (state.entity) p.set("entity", state.entity);
  if (t) p.set("tender", t.id);
  return location.pathname + "?" + p;
}
function tlNo(t) { const m = /(\d+)/.exec(t.internal || ""); return m ? Number(m[1]) : -1; }
function tlDeadline(t) {
  const d = t.deadline_at;
  return d ? new Date(`${d.date}T${d.time || "23:59"}:00`) : null;
}
function tlHasData(t) { return t.source === "folder" || Boolean(t.customer || t.subject || t.number); }
function tlNorm(s) { return String(s || "").toLowerCase().replace(/k/g, "к").replace(/ё/g, "е"); }

function tlMessage(text) {
  $("tl-count").textContent = "";
  $("tl-more").hidden = true;
  $("tl-table").replaceChildren(el("tbody", {}, el("tr", {}, el("td", { class: "tl-empty-val", text }))));
}

const CUR_SIGN = { RUB: "₽", EUR: "€", USD: "$", CNY: "¥" };
function tlPrice(p) {
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(p.value)} ${CUR_SIGN[p.currency] || p.currency}`;
}

function tlLeft(dt) {
  const ms = dt - Date.now();
  if (ms < 0) return el("span", { class: "badge muted tl-left", text: "подача прошла" });
  const h = ms / 3600000;
  if (h < 24) return el("span", { class: "badge bad tl-left", text: h < 1 ? "меньше часа" : `осталось ${Math.floor(h)} ч` });
  const d = Math.floor(h / 24);
  return el("span", { class: "badge tl-left " + (d <= 3 ? "warn" : "ok"), text: `ещё ${d} дн.` });
}

const RESULT_CLASS = { "выиграли": "ok", "проиграли": "bad" };

function tlRow(t) {
  const href = tlHref(t);
  const none = () => el("span", { class: "tl-empty-val", text: "—" });
  const dt = tlDeadline(t);
  const d = t.deadline_at;
  const status = t.source === "folder"
    ? el("span", { class: "badge muted", text: "нет в NocoDB", title: "Есть папка в Dropbox, записи в «Реестре конкурсов» ещё нет" })
    : t.result ? el("span", { class: "badge " + (RESULT_CLASS[t.result.toLowerCase()] || "muted"), text: t.result })
    : t.stage_short ? el("span", { text: t.stage_short }) : none();
  const tr = el("tr", { "data-id": t.id, title: "Открыть в новой вкладке" },
    el("td", { class: "no" },
      el("a", { class: "tl-no", href, target: "_blank", rel: "noopener", text: t.internal || "без номера" }),
      t.number ? el("div", { class: "tl-sub", text: "№ " + t.number }) : null),
    el("td", { class: "tl-cust", title: t.customer_full || null },
      t.customer ? el("div", { class: "tl-clamp", text: t.customer }) : none()),
    el("td", { class: "tl-subj", title: t.subject_full || t.title || null },
      t.subject ? el("div", { class: "tl-clamp", text: t.subject }) : none()),
    el("td", { class: "price", title: t.price_full || null },
      ...(t.price ? [el("div", { text: tlPrice(t.price) }), t.price.vat ? el("div", { class: "tl-sub", text: t.price.vat }) : null] : [none()])),
    el("td", { class: "dl", title: t.deadline_full || null },
      ...(d ? [el("div", { text: d.date.split("-").reverse().join(".") + (d.time ? " " + d.time : "") + (d.local ? " (местн.)" : "") }), tlLeft(dt)] : [none()])),
    el("td", { class: "st", title: t.stage_full || null }, status));
  const open = (e) => {
    if (e.target.closest("a")) return;                        // по номеру — ссылка сама откроет вкладку
    if (e.type === "auxclick" && e.button !== 1) return;
    window.open(href, "_blank", "noopener");
  };
  tr.addEventListener("click", open);
  tr.addEventListener("auxclick", open);
  return tr;
}

function renderTenderList() {
  if (state.tender || !state.list) return;
  const all = state.list;
  const filter = $("tl-filter").value;
  const q = tlNorm($("tl-search").value.trim());
  const now = Date.now();
  let list = all.filter((t) => filter === "all" || (filter === "open" ? (tlDeadline(t) || 0) >= now : tlHasData(t)));
  const before = list.length;
  if (q) {
    list = list.filter((t) => tlNorm([t.internal, t.number, t.customer_full, t.subject_full, t.title, t.folder].join(" ")).includes(q));
  }
  const { key, dir } = tlSort;
  const val = (t) => (key === "no" ? tlNo(t) : key === "price" ? (t.price ? t.price.value : null) : (tlDeadline(t) || null));
  list = [...list].sort((a, b) => {
    const x = val(a), y = val(b);
    const nx = x === null || x === -1, ny = y === null || y === -1;
    if (nx !== ny) return nx ? 1 : -1;                         // без значения — всегда внизу
    if (!nx && x - y) return (x - y) * dir;
    return (Number(b.id) || 0) - (Number(a.id) || 0);
  });

  const head = el("tr", {}, ...[["no", "№"], ["", "Заказчик"], ["", "Что поставляем"], ["price", "НМЦ"],
    ["deadline", "Подача заявок до"], ["", "Итог / стадия"]].map(([k, text]) => {
    if (!k) return el("th", { text });
    const th = el("th", { class: "sort", "data-sort": k, title: "Сортировать" }, text,
      key === k ? el("span", { class: "arrow", text: dir < 0 ? "↓" : "↑" }) : null);
    th.addEventListener("click", () => {
      tlSort = { key: k, dir: key === k ? -dir : (k === "deadline" ? 1 : -1) };
      storageSet(TL_SORT_KEY, JSON.stringify(tlSort));
      renderTenderList();
    });
    return th;
  }));
  const body = el("tbody", {}, ...list.map(tlRow));
  if (!list.length) {
    body.append(el("tr", {}, el("td", { colspan: "6", class: "tl-empty-val",
      text: !all.length ? "Конкурсов пока нет — нажмите «+ Новый конкурс»." : q ? "Ничего не нашлось." :
        filter === "open" ? "Конкурсов, где подача заявок ещё идёт, нет (по сроку подачи в NocoDB)." : "Конкурсов нет." })));
  }
  $("tl-table").replaceChildren(el("thead", {}, head), body);
  $("tl-count").textContent = list.length === all.length ? `(${all.length})` : `(${list.length} из ${all.length})`;
  const hidden = filter === "data" ? all.length - before : 0;
  $("tl-more").hidden = !hidden || Boolean(q);
  if (hidden && !q) {
    const a = el("a", { class: "link", href: "#", text: "показать все записи реестра" });
    a.addEventListener("click", (e) => { e.preventDefault(); $("tl-filter").value = "all"; storageSet(TL_FILTER_KEY, "all"); renderTenderList(); });
    $("tl-more").replaceChildren(`Ещё ${hidden} записей «Реестра конкурсов» — только с внутренним номером, без заказчика и `
      + "предмета (старые или зарезервированные номера) — ", a, ".");
  }
}

// ---------- новый конкурс ----------

// Файлы документации, выбранные в форме нового конкурса (загрузятся сразу после создания папки).
let newTenderFiles = [];
let ntEmptyOk = false;     // «Создать без документов» подтверждено

function showNewTenderForm(on) {
  $("new-tender").hidden = !on;
  $("new-tender-btn").hidden = on;
  if (on) {
    $("nt-number").value = state.nextNumber;
    $("nt-title").value = "";
    setNewTenderFiles([]);
    ntEmptyOk = false;
    $("nt-nofiles").hidden = true;
    setStatus("nt-status", "");
    $("nt-title").focus();
  }
}

function setNewTenderFiles(files) {
  newTenderFiles = [...files];
  if (newTenderFiles.length) $("nt-nofiles").hidden = true;
  const box = $("nt-file-list");
  box.replaceChildren();
  if (newTenderFiles.length) {
    const total = newTenderFiles.reduce((s, f) => s + f.size, 0);
    box.append(el("div", { text: `Выбрано файлов: ${newTenderFiles.length} (${fmtSize(total)})` }));
    for (const f of newTenderFiles.slice(0, 8)) box.append(el("div", { class: "muted", text: "• " + f.name }));
    if (newTenderFiles.length > 8) box.append(el("div", { class: "muted", text: `… и ещё ${newTenderFiles.length - 8}` }));
  }
}

async function createFolder(number, title) {
  const data = await api(clientPath("/folders"), {}, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ number, title }),
  });
  if (data.template_skipped && data.template_skipped.length) {
    toast(`Из шаблона «К---» не скопировано: ${data.template_skipped.join(", ")} — там результаты анализа, похоже, ` +
      "это материалы другого конкурса. Уберите их из шаблона.", true);
  }
  return data.folder;
}

// ---------- конкурс ----------

function currentTender() {
  return state.tenders.get(state.tender) || null;
}

// Разделы, выключенные сотруднику в админ-панели (0.11.0): на <body> класс off-<раздел> (loadMe). Раздел не
// показывается и не загружается (сайт всё равно ответил бы 403); вкладка-переход прячется вместе с разделом.
function blockOff(name) { return document.body.classList.contains("off-" + name); }

function showTender(t) {
  // Конкурс не выбран — панель «Конкурсы» (0.13.0); выбран — его разделы, вверху список для смены и «Все конкурсы».
  $("tlist").hidden = Boolean(t);
  $("opening").hidden = true;
  $("pickers").classList.toggle("mode-list", !t);
  $("pickers").classList.toggle("mode-tender", Boolean(t));
  $("to-list").href = tlHref(null);
  document.title = t ? `${[t.internal || (t.number ? "№ " + t.number : ""), t.subject || t.customer || t.title]
    .filter(Boolean).join(" · ").slice(0, 80)} — SupplyBro` : "Конкурсы — SupplyBro";
  if (!t) renderTenderList();
  $("head").hidden = !t;
  $("docs").hidden = !t || blockOff("docs");
  $("analysis").hidden = !(t && t.folder) || blockOff("analysis");
  $("prices").hidden = !(t && t.folder) || blockOff("prices");
  $("chat").hidden = !(t && t.folder) || blockOff("chat");
  $("card").hidden = !(t && t.source === "nocodb") || blockOff("card");
  $("spec").hidden = !(t && t.source === "nocodb") || blockOff("spec");
}

async function loadTender() {
  const t = currentTender();
  state.opened = t ? state.tender : "";
  showTender(t);
  saveUrl();
  stopDocsPolling();
  stopAnalysisPolling();
  stopPricesPolling();
  if (!t) return;
  anLastJob = null;
  prLastJob = null;
  if (t.source === "nocodb") {
    await Promise.all([loadCard(), loadSpec(), loadDocs(), loadAnalysis(), loadPrices(), loadChat()]);
  } else {
    renderFolderHead(t);
    await Promise.all([loadDocs(), loadAnalysis(), loadPrices(), loadChat()]);
  }
  state.fresh = false;
}

function renderFolderHead(t) {
  $("head-no").textContent = t.internal || "без внутр. номера";
  $("head-title").textContent = t.title || t.folder;
  $("head-links").replaceChildren();
  const c = el("span", { class: "chip" });
  c.append(el("b", { text: "NocoDB: " }), "записи пока нет — появится после анализа документов");
  $("head-chips").replaceChildren(c);
}

async function loadCard() {
  const my = ++seq.card;
  setStatus("card-status", "Загрузка…");
  $("fields").replaceChildren();
  let d;
  try {
    d = await api(clientPath(`/tenders/${state.tender}`), { view: state.regView });
  } catch (err) {
    if (my === seq.card) setStatus("card-status", "Не удалось загрузить данные: " + err.message, true);
    return;
  }
  if (my !== seq.card) return;
  setStatus("card-status", "");

  // Шапка
  $("head-no").textContent = [d.internal || "без внутр. номера", d.number ? "№ " + d.number : ""].filter(Boolean).join(" · ");
  $("head-title").textContent = d.title || "Без названия";
  const links = [];
  if (d.links.procedure && /^https?:/.test(d.links.procedure)) links.push(["Процедура ↗", d.links.procedure]);
  links.push(["Запись в NocoDB ↗", d.links.nocodb]);
  if (d.links.spec) links.push(["Спецификация в NocoDB ↗", d.links.spec]);
  $("head-links").replaceChildren(...links.map(([t, h]) => el("a", { class: "btn ghost small", href: h, target: "_blank", rel: "noopener", text: t })));
  const chips = [];
  if (d.stage) chips.push(["Стадия", d.stage]);
  if (d.deadline) chips.push(["Подача заявок", d.deadline]);
  if (d.price) chips.push(["НМЦ", d.price + (d.currency ? " " + d.currency : "")]);
  chips.push(["Участник", d.participants.length ? d.participants.map(entityName).join(", ") : "не указан"]);
  $("head-chips").replaceChildren(...chips.map(([k, v]) => {
    const c = el("span", { class: "chip" });
    c.append(el("b", { text: k + ": " }), v.length > 90 ? v.slice(0, 88) + "…" : v);
    return c;
  }));

  // Представления реестра (роли) и поля выбранного
  fillSelect($("reg-view"), d.views.map((v) => ({ value: v.id, label: v.title })), d.view ? d.view.id : "");
  state.regView = d.view ? d.view.id : "";
  renderFields(d.fields);
  saveUrl();
}

function renderFields(fields) {
  const hideEmpty = $("hide-empty").checked;
  const dl = $("fields");
  dl.replaceChildren();
  let shown = 0;
  for (const f of fields) {
    const node = renderValue(f.value, f.uidt);
    if (!node && hideEmpty) continue;
    dl.append(el("dt", { text: f.title }));
    dl.append(node ? el("dd", {}, node) : el("dd", { class: "empty-val", text: "—" }));
    shown++;
  }
  if (!shown) dl.append(el("dd", { class: "empty-val", text: "В этом представлении все поля пустые." }));
  dl._fields = fields; // для переключателя «скрыть пустые»
}

// ---------- документы ----------

const STATUS_CLASS = { ok: "ok", ocr: "ocr", pending: "wait", skipped: "muted", archive: "muted", empty: "warn", error: "bad" };

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " Б";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " КБ";
  return (bytes / 1024 / 1024).toFixed(1).replace(".", ",") + " МБ";
}

function stopDocsPolling() {
  if (docsTimer) { clearTimeout(docsTimer); docsTimer = null; }
}

async function loadDocs() {
  const t = currentTender();
  const my = ++seq.docs;
  stopDocsPolling();
  if (!t || blockOff("docs")) return;
  if (!t.folder) {
    $("docs-nofolder").hidden = false;
    $("docs-body").hidden = true;
    $("docs-process").hidden = true;
    $("docs-path").textContent = "";
    $("docs-count").textContent = "";
    setStatus("docs-status", "");
    return;
  }
  $("docs-nofolder").hidden = true;
  let d;
  try {
    d = await api(clientPath("/docs"), { folder: t.folder });
  } catch (err) {
    if (my === seq.docs) {
      setStatus("docs-status", "Не удалось прочитать папку конкурса: " + err.message, true);
      $("docs-body").hidden = true;
    }
    return;
  }
  if (my !== seq.docs) return;
  setStatus("docs-status", "");
  $("docs-body").hidden = false;
  $("docs-process").hidden = false;
  $("docs-path").textContent = "Папка: " + d.display_path;
  const target = $("upload-target");
  const targetKey = d.folder + "|" + (d.has_analysis ? d.changes_target.folder : "");
  if (!target.options.length || target._key !== targetKey) {
    // Конкурс уже разобран (0.9.0): новые файлы — изменения / разъяснения, по умолчанию в «Изменения <сегодня>».
    const opts = d.targets.map((x) => ({ value: x, label: x }));
    if (d.has_analysis) opts.unshift({ value: d.changes_target.value, label: d.changes_target.folder + " (изменения)" });
    fillSelect(target, opts, opts[0].value);
    target._key = targetKey;
  }
  $("drop").querySelector(".drop-main").textContent = d.has_analysis
    ? "Перетащите сюда изменения, разъяснения или недостающие документы — сайт сравнит их с анализом и покажет, что изменилось"
    : "Перетащите сюда файлы или архив";
  renderDocs(d.files);
  const active = renderJob(d.job);
  // Файлы положили в папку вручную (не через сайт) — запускаем обработку сами, один раз за показ.
  const pending = d.files.some((f) => f.status === "pending");
  // Подготовку приостановили или недавно отменили — сами не запускаем (решение пользователя).
  const userStopped = d.job && (d.job.status === "paused" ||
    (d.job.status === "canceled" && Date.now() / 1000 - (d.job.finished_at || 0) < 3600));
  if (pending && !active && !userStopped && t._autoQueued !== d.folder) {
    t._autoQueued = d.folder;
    try {
      await api(clientPath("/docs/process"), { folder: d.folder }, { method: "POST" });
    } catch (_) { /* покажем на следующем обновлении */ }
    docsTimer = setTimeout(loadDocs, 1000);
    return;
  }
  if (active) docsTimer = setTimeout(loadDocs, 2000);
}

function renderDocs(files) {
  const docs = files.filter((f) => f.kind !== "archive");
  const ocr = docs.reduce((s, f) => s + (f.ocr_pages || 0), 0);
  const warn = files.filter((f) => f.flags.includes("⚠")).length;
  const parts = [`${docs.length} файл(ов)`];
  if (ocr) parts.push(`распознано страниц: ${ocr}`);
  if (warn) parts.push(`⚠ ${warn}`);
  $("docs-count").textContent = files.length ? "· " + parts.join(" · ") : "";

  const table = $("docs-table");
  if (!files.length) {
    table.replaceChildren(el("tbody", {}, el("tr", {}, el("td", { class: "empty-val", text: "Документов пока нет — загрузите их выше." }))));
    return;
  }
  const head = el("tr", {}, ...["Файл", "Страниц", "Статус", "Пометки", "Размер", ""].map((h) => el("th", { text: h })));
  const body = el("tbody");
  for (const f of files) {
    const segs = f.name.split("/");
    const nameCell = el("td", { class: "doc-name" });
    if (segs.length > 1) nameCell.append(el("div", { class: "doc-dir", text: segs.slice(0, -1).join(" / ") }));
    const base = segs[segs.length - 1];
    if (f.has_text) {
      const q = new URLSearchParams({ folder: currentTender().folder, path: f.path });
      nameCell.append(el("a", { href: clientPath("/docs/text") + "?" + q, target: "_blank", rel: "noopener",
        title: "Открыть извлечённый текст", text: base }));
    } else {
      nameCell.append(el("span", { text: base }));
    }
    const pages = f.pages ? (f.ocr_pages ? `${f.pages} (распозн. ${f.ocr_pages})` : String(f.pages)) : "";
    const note = [f.flags, f.error].filter(Boolean).join("; ");
    const q = new URLSearchParams({ folder: currentTender().folder, path: f.path });
    body.append(el("tr", {},
      nameCell,
      el("td", { class: "num", text: f.kind === "xlsx" && f.pages ? `${f.pages} л.` : pages }),
      el("td", {}, el("span", { class: "badge " + (STATUS_CLASS[f.status] || ""), text: f.status_label })),
      el("td", { class: "doc-note" + (note.includes("⚠") || f.error ? " warn-text" : ""), text: note }),
      el("td", { class: "num", text: fmtSize(f.size) }),
      el("td", {}, el("a", { href: clientPath("/docs/file") + "?" + q, title: "Скачать исходный файл", text: "⤓" })),
    ));
  }
  table.replaceChildren(el("thead", {}, head), body);
}

// Возвращает true, если задача ещё идёт (нужно обновлять).
// ---------- ход задач: ждёт / идёт / жив ли обработчик ----------

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return `${sec} с`;
  if (sec < 3600) return `${Math.round(sec / 60)} мин`;
  return `${Math.floor(sec / 3600)} ч ${Math.round((sec % 3600) / 60)} мин`;
}

// «В очереди» — за кем стоит задача или что обработчик не запущен.
function queuedText(job) {
  if (job.worker_alive === false && job.age > 20) {
    return "в очереди — обработчик не отвечает" + (job.worker_seen ? ` (последний отклик ${fmtDur(job.worker_seen)} назад)` : "") +
      ". Запустите сайт заново: «Запустить SupplyBro.bat».";
  }
  if (job.waiting_for) {
    const w = job.waiting_for;
    return `в очереди «${job.lane_title}» — ждёт, пока закончится: ${w.folder} — ${w.kind_title} (идёт ${fmtDur(w.elapsed)})` +
      (job.ahead ? `; перед ней ещё ${job.ahead}` : "");
  }
  return "в очереди…" + (job.ahead ? ` (перед ней ${job.ahead})` : "");
}

// «Идёт» — сколько идёт, когда было последнее действие; предупреждение, если давно нет движения.
function runningTail(job) {
  if (job.elapsed === undefined) return "";
  let t = ` · идёт ${fmtDur(job.elapsed)} · последнее действие ${fmtDur(job.quiet)} назад`;
  if (job.worker_alive === false) {
    t += ". ⚠ Обработчик не отвечает — запустите сайт заново («Запустить SupplyBro.bat»), задача продолжится";
  } else if (job.quiet_warn) {
    t += ". ⚠ Давно нет движения, но обработчик жив — скорее всего, сервис (NocoDB, поиск) просит подождать;" +
      " если так больше 15 минут — перезапустите сайт, задача продолжится";
  }
  return t;
}

// ---------- отмена и пауза задач (меню «В работе» и блоки задач конкурса) ----------

let ctlConfirm = null;          // у какой задачи открыт вопрос «Отменить?»
const ctlBusy = new Set();      // по каким задачам запрос уже отправлен
const lastJobs = {};            // последняя задача каждого блока — перерисовать кнопки без запроса к сайту

// Что будет, если отменить задачу, которая ещё не начиналась / уже идёт.
const CANCEL_NOT_STARTED = {
  prepare: "Если после подготовки должен был идти анализ — он тоже не запустится.",
  analyze: "Запись в NocoDB и поиск цен по этому анализу тоже не начнутся.",
  noco: "Записать можно потом кнопкой «Записать в NocoDB» в блоке «Анализ».",
  noco_prices: "Цены не попадут в NocoDB (на сайте и в xlsx они есть).",
  prices: "Запустить поиск можно потом кнопкой «Искать цены».",
};
const CANCEL_STARTED = {
  prepare: "Уже обработанные файлы останутся; анализ после подготовки не запустится.",
  analyze: "Запись в NocoDB и поиск цен по нему не начнутся. Ответы модели, полученные до отмены, сохранены — " +
    "при новом запуске анализа они второй раз не оплачиваются.",
  noco: "Запись конкурса в реестре могла уже появиться, а спецификация — остаться недописанной: следующая " +
    "«Записать в NocoDB» доделает её в ту же таблицу (лишних таблиц не будет).",
  noco_prices: "Цены не попадут в NocoDB (на сайте и в xlsx они есть).",
  prices: "Найденное до отмены не сохранится. Новый поиск в течение суток почти не платит: результаты поиска, " +
    "открытые страницы и проверки моделью берутся из кэша.",
};
const PAUSE_TITLE_RUNNING = "Остановить на ближайшем шаге и отложить — очередь возьмёт следующую задачу. " +
  "«Продолжить» вернёт её на прежнее место в очереди: она начнётся заново, но готовое не повторяется и второй раз не оплачивается.";
const PAUSE_TITLE_QUEUED = "Отложить — очередь будет пропускать эту задачу, пока не нажмёте «Продолжить».";

function lc1(s) { return s ? s[0].toLowerCase() + s.slice(1) : s; }

function cancelEffect(j) {
  if (j.status === "queued") return "Она ещё не начиналась. " + (CANCEL_NOT_STARTED[j.kind] || "");
  return (j.status === "running" ? "Остановится на ближайшем шаге (обычно за несколько секунд). " : "") +
    (CANCEL_STARTED[j.kind] || "");
}

let toastTimer = null;
function toast(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 9000 : 5000);
}

// Блоки перерисовываются каждые несколько секунд: пока кнопка нажата (между нажатием и отпусканием),
// содержимое не заменяем — иначе нажатие «проваливается».
let ptrTarget = null;
const held = new Map();
document.addEventListener("pointerdown", (e) => { ptrTarget = e.target; }, true);
document.addEventListener("pointerup", () => {
  setTimeout(() => {
    ptrTarget = null;
    for (const [box, nodes] of held) box.replaceChildren(...nodes);
    held.clear();
  }, 0);
}, true);
function replaceSafe(box, ...nodes) {
  if (ptrTarget && box.contains(ptrTarget)) { held.set(box, nodes); return; }
  held.delete(box);
  box.replaceChildren(...nodes);
}

function rerenderControls() {
  if (actLast) renderActivity(actLast);
  for (const [id, j] of Object.entries(lastJobs)) if (j) renderJobControls($(id), j);
}

function renderJobControls(box, j) {
  replaceSafe(box, ...jobControls(j));
}

// Кнопки задачи: «Пауза» / «Продолжить» / «Отменить» (с вопросом), «Не останавливать».
function jobControls(j) {
  if (!j || !["running", "queued", "paused"].includes(j.status)) return [];
  const busy = ctlBusy.has(j.id);
  const btn = (text, title, fn, cls = "") => {
    const b = el("button", { class: "btn small " + cls, type: "button", title, text });
    b.disabled = busy;
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  };
  if (ctlConfirm === j.id) {
    return [
      el("span", { class: "ctl-ask", text: `Отменить «${j.folder} — ${j.kind_title}»? ${cancelEffect(j)}` }),
      btn("Да, отменить", "", () => jobAction(j, "cancel"), "danger"),
      btn("Нет", "", () => { ctlConfirm = null; rerenderControls(); }),
    ];
  }
  if (j.status === "running" && j.control) {
    return [btn("Не останавливать", "Передумали — пусть задача работает дальше", () => jobAction(j, "resume"))];
  }
  const out = [];
  if (j.status === "paused") {
    out.push(btn("Продолжить", "Снова в очередь, на прежнее место. Готовое второй раз не делается и не оплачивается",
      () => jobAction(j, "resume"), "primary"));
  } else {
    out.push(btn("Пауза", j.status === "running" ? PAUSE_TITLE_RUNNING : PAUSE_TITLE_QUEUED, () => jobAction(j, "pause")));
  }
  out.push(btn("Отменить", "Отменить задачу (спросит подтверждение)", () => { ctlConfirm = j.id; rerenderControls(); }, "danger"));
  return out;
}

async function jobAction(j, action) {
  ctlBusy.add(j.id);
  ctlConfirm = null;
  rerenderControls();
  try {
    const r = await api(clientPath(`/jobs/${j.id}/${action}`), {}, { method: "POST" });
    toast(`${j.folder} — ${j.kind_title}: ${r.text}`);
  } catch (e) {
    toast(`Не получилось: ${e.message}`, true);
  } finally {
    ctlBusy.delete(j.id);
  }
  // Обновить меню и блоки конкурса (кнопки «Запустить», ход задачи).
  pollActivity();
  const t = currentTender();
  if (t && t.folder === j.folder) {
    clearTimeout(docsTimer); docsTimer = null; loadDocs();
    clearTimeout(anTimer); anTimer = null; loadAnalysis();
    clearTimeout(prTimer); prTimer = null; loadPrices();
  }
}

// Задача на паузе / отменена / останавливается — текст для блоков конкурса.
function stoppedText(job) {
  if (job.status === "paused") {
    return lc1(job.message || "Приостановлена") + ". «Продолжить» — снова в очередь; готовое второй раз не делается.";
  }
  const when = job.finished_at ? new Date(job.finished_at * 1000).toLocaleString("ru-RU") : "";
  return `${lc1(job.message || "Отменена")} (${when}).`;
}
function stoppingText(job) {
  return job.control ? ` — ${job.control === "pause" ? "приостанавливается" : "отменяется"} на ближайшем шаге…` : runningTail(job);
}

function renderJob(job) {
  const box = $("job");
  lastJobs["job-ctl"] = job;
  renderJobControls($("job-ctl"), job);
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done", "paused", "canceled");
  const bar = $("job-bar");
  if (job.status === "queued") {
    bar.style.width = "0%";
    $("job-text").textContent = "Обработка документов: " + queuedText(job);
    return true;
  }
  if (job.status === "running") {
    bar.style.width = Math.round((job.progress || 0) * 100) + "%";
    $("job-text").textContent = "Обработка: " + (job.step || "…") + stoppingText(job);
    return true;
  }
  if (job.status === "paused" || job.status === "canceled") {
    box.classList.add(job.status);
    bar.style.width = Math.round((job.progress || 0) * 100) + "%";
    $("job-text").textContent = "Обработка документов: " + stoppedText(job);
    return false;
  }
  bar.style.width = "100%";
  const when = job.finished_at ? new Date(job.finished_at * 1000).toLocaleString("ru-RU") : "";
  if (job.status === "error") {
    box.classList.add("error");
    $("job-text").textContent = `Ошибка обработки (${when}): ${job.message}`;
  } else {
    box.classList.add("done");
    $("job-text").textContent = `Обработано ${when}. ${job.message}`;
  }
  return false;
}

function uploadFiles(fileList, target, statusId = "docs-status") {
  const t = currentTender();
  if (!t || !t.folder || !fileList.length) return;
  const form = new FormData();
  for (const f of fileList) form.append("files", f, f.name);
  const q = new URLSearchParams({ folder: t.folder, target: target || $("upload-target").value || DOCS_TARGET });
  const xhr = new XMLHttpRequest();
  xhr.open("POST", clientPath("/docs/upload") + "?" + q);
  $("upload-progress").hidden = false;
  $("upload-bar").style.width = "0%";
  setStatus(statusId, `Загрузка ${fileList.length} файл(ов)…`);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) $("upload-bar").style.width = Math.round((e.loaded / e.total) * 100) + "%";
  };
  xhr.onload = () => {
    if (xhr.status === 401) return toLogin();
    $("upload-progress").hidden = true;
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch (_) { /* не JSON */ }
    if (xhr.status >= 200 && xhr.status < 300) {
      setStatus(statusId, `Загружено: ${body.saved.length}` + (body.target ? ` (папка «${body.target}»)` : "") +
        ". Документы поставлены в обработку" + (!body.auto_analyze ? "." : body.changes
          ? ", затем сайт прочитает их и покажет в разделе «Анализ», что изменилось (было → стало), обновит данные и даст комментарии."
          : ", затем — анализ и запись в NocoDB."));
      loadAnalysis();
    } else {
      setStatus(statusId, "Не удалось загрузить: " + (body.detail || xhr.status), true);
    }
    loadDocs();
  };
  xhr.onerror = () => {
    $("upload-progress").hidden = true;
    setStatus(statusId, "Не удалось загрузить: нет связи с сайтом", true);
  };
  xhr.send(form);
}

// ---------- анализ документов (ИИ) ----------

const VERDICT_TEXT = {
  match: "✅ совпало с DeepSeek",
  ours_confirmed: "🔺 расхождение с DeepSeek, перепроверено — оставлено",
  theirs_correct: "исправлено по перепроверке",
  not_found_by_them: "DeepSeek не нашёл",
  not_found_both: "не найдено обеими моделями",
};
const QUOTE_TEXT = {
  ok: ["цитата найдена", "ok"], other_file: ["цитата найдена (в другом документе)", "ok"],
  partial: ["цитата найдена частями", "warn"], not_found: ["цитата не найдена", "bad"],
  empty: ["без цитаты", "warn"], none: ["", ""], computed: ["собрано из позиций", "muted"],
};
const ANALOG_CLASS = { "Да": "ok", "Нет": "bad", "Уточни": "warn" };

function stopAnalysisPolling() {
  if (anTimer) { clearTimeout(anTimer); anTimer = null; }
}

async function loadAnalysis() {
  const t = currentTender();
  const my = ++seq.an;
  stopAnalysisPolling();
  if (!t || !t.folder || blockOff("analysis")) { $("analysis").hidden = true; return; }
  $("analysis").hidden = false;
  let d;
  try {
    d = await api(clientPath("/analysis"), { folder: t.folder });
  } catch (err) {
    if (my === seq.an) setStatus("an-status", "Не удалось загрузить анализ: " + err.message, true);
    return;
  }
  if (my !== seq.an) return;
  setStatus("an-status", "");
  const jobActive = renderAnalysisJob(d.job);
  const busy = jobActive || d.prepare_active;
  renderAnalysis(d, busy);

  // Задача закончилась — обновить список конкурсов и данные NocoDB (могла появиться или обновиться запись).
  const finished = anLastJob && d.job && anLastJob.id === d.job.id &&
    ["queued", "running"].includes(anLastJob.status) && !["queued", "running"].includes(d.job.status);
  anLastJob = d.job ? { id: d.job.id, status: d.job.status } : null;
  if (finished) loadPrices();
  if (finished && d.job.kind === "noco_changes" && d.job.status === "done") {
    state.fresh = true;
    await loadTenders();
    return;
  }
  if (finished && ["analyze", "noco"].includes(d.job.kind)) {
    const noco = d.result && d.result.noco;
    if (noco && noco.status === "saved") {
      state.fresh = true;
      if (t.source === "folder" && noco.record_id) state.tender = String(noco.record_id);
      await loadTenders();
      return;
    }
  }
  if (busy) anTimer = setTimeout(loadAnalysis, 2500);
}

function renderAnalysisJob(job) {
  const box = $("an-job");
  lastJobs["an-job-ctl"] = job;
  renderJobControls($("an-job-ctl"), job);
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done", "paused", "canceled");
  const what = { noco: "Запись в NocoDB", changes: "Разбор изменений", noco_changes: "Запись изменений в NocoDB" }[job.kind] || "Анализ";
  if (job.status === "queued") {
    $("an-bar").style.width = "0%";
    $("an-job-text").textContent = `${what}: ` + queuedText(job);
    return true;
  }
  if (job.status === "running") {
    $("an-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("an-job-text").textContent = `${what}: ${job.step || "…"}` + stoppingText(job) + (job.kind === "noco" && !job.control
      ? ". У нового конкурса запись идёт 10–20 минут (настраиваются представления спецификации); подготовка и анализ других конкурсов её не ждут" : "");
    return true;
  }
  if (job.status === "paused" || job.status === "canceled") {
    box.classList.add(job.status);
    $("an-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("an-job-text").textContent = `${what}: ` + stoppedText(job);
    return false;
  }
  $("an-bar").style.width = "100%";
  const when = job.finished_at ? new Date(job.finished_at * 1000).toLocaleString("ru-RU") : "";
  if (job.status === "error") {
    box.classList.add("error");
    $("an-job-text").textContent = `${what} — ошибка (${when}): ${job.message}`;
  } else {
    box.classList.add("done");
    $("an-job-text").textContent = `${what} — готово (${when}). ${job.message}`;
  }
  return false;
}

const AN_MODEL_KEY = "supplybro.analysisModel";
// Страница сотрудника (/user, та же страница с пометкой в <body>): выбора модели анализа, модели чата и поисковика
// цен нет — сайт берёт свои настройки по умолчанию (CLAUDE_MODEL, CHAT_MODEL, PRICES_SEARCH).
const USER_MODE = document.body.dataset.mode === "user";

function usd(x) {
  if (x === null || x === undefined) return "";
  return x < 0.01 ? "$" + x.toFixed(4) : "$" + x.toFixed(2);
}

function renderAnalysis(d, busy) {
  const r = d.result;
  if (d.models) {
    const saved = storageGet(AN_MODEL_KEY);
    fillSelect($("an-model"), d.models.map((x) => ({ value: x, label: x })), d.models.includes(saved) ? saved : d.default_model);
  }
  const run = $("an-run");
  run.textContent = r ? "Повторить анализ" : "Запустить анализ";
  run.disabled = busy || !d.docs_with_text;
  run.title = d.docs_with_text ? "Анализ документов ИИ (Claude + перепроверка DeepSeek) и запись в NocoDB"
    : "Сначала загрузите документы — анализировать пока нечего";
  $("an-changes-upload").hidden = !r;
  renderNewDocs(d, busy);
  renderChanges(d.changes || [], busy || d.changes_active || d.changes_write_active, r);
  $("an-info").textContent = r
    ? `· ${r.analyzed_at_text} · ${r.models.claude}` + (r.models.fallback ? ` → ${r.models.fallback}` : "") +
      (r.seconds ? ` · ${Math.max(1, Math.round(r.seconds / 60))} мин` : "") +
      (r.usage && r.usage.usd !== undefined ? ` · ≈ ${usd(r.usage.usd)}` : "") +
      (r.changes_at_text ? ` · изменения ${r.changes_at_text}` : "")
    : "";
  if (!r) {
    $("an-body").hidden = true;
    $("an-noco").replaceChildren();
    $("an-checks").replaceChildren();
    if (!busy) {
      setStatus("an-status", d.docs_with_text
        ? "Анализа ещё нет. «Запустить анализ» — ИИ извлечёт поля конкурса и позиции, перепроверит важные поля и запишет результат в NocoDB" +
          (d.auto_prices ? ", затем сам найдёт цены и допишет их в спецификацию." : ".")
        : "Анализ появится после загрузки документов" + (d.auto_analyze ? " — дальше всё само: анализ, запись в NocoDB" +
          (d.auto_prices ? ", поиск цен." : ".") : "."));
    }
    return;
  }
  renderNoco(r, busy);
  $("an-checks").replaceChildren(...(r.checks || []).map((c) =>
    el("li", { class: c.startsWith("✅") ? "" : "warn", text: c })));
  $("an-body").hidden = false;
  renderAnalysisFields(r.fields || []);
  renderAnalysisPositions(r.positions || []);
  $("an-pos-count").textContent = (r.positions || []).length ? `(${r.positions.length})` : "";
  showAnalysisTab(anTab);
  $("an-usage").textContent = usageText(r.usage && r.usage.totals);
}

// ---------- изменения и разъяснения к разобранному конкурсу (0.9.0) ----------

const CS_ACTION = { changed: "изменена", added: "🆕 добавлена", removed: "❌ исключена" };
const CS_KIND_ICON = { "изменение": "🔄", "разъяснение": "💬", "новая редакция": "📑", "протокол": "📋" };

function renderNewDocs(d, busy) {
  const box = $("an-stale");
  const names = d.new_docs || [];
  if (busy || !d.result || (!names.length && !d.stale)) { box.hidden = true; return; }
  box.hidden = false;
  if (!names.length) {
    box.replaceChildren("Документы изменились после анализа — запустите анализ заново.");
    return;
  }
  const list = el("ul", {}, ...names.slice(0, 8).map((n) => el("li", { text: n })));
  if (names.length > 8) list.append(el("li", { text: `и ещё ${names.length - 8}` }));
  const run = el("button", { class: "btn small primary need-analyze", type: "button", text: "Разобрать изменения" });
  run.title = "Прочитать только новые документы: что изменилось (было → стало), обновить данные анализа, комментарии. Дёшево — несколько центов.";
  run.addEventListener("click", () => runChanges(run));
  const full = el("button", { class: "btn small need-analyze", type: "button", text: "Повторить анализ целиком" });
  full.addEventListener("click", () => $("an-run").click());
  box.replaceChildren(el("div", { text: `Появились документы после анализа (${names.length}):` }), list, run, full);
}

async function runChanges(btn) {
  const t = currentTender();
  if (!t || !t.folder) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api(clientPath("/analysis/changes/run"), { folder: t.folder }, { method: "POST" });
    if (r.already) setStatus("an-status", "Разбор изменений уже идёт — показываю его ход.");
  } catch (err) {
    setStatus("an-status", "Не удалось запустить разбор изменений: " + err.message, true);
  }
  loadAnalysis();
}

function quoteCell(item) {
  const td = el("td");
  if (item.file) td.append(el("div", { class: "src-file", text: item.file.split("/").pop() }));
  if (item.quote) {
    const q = el("div", { class: "quote clamp", title: "Щёлкните, чтобы развернуть", text: "«" + item.quote + "»" });
    q.addEventListener("click", () => q.classList.toggle("open"));
    td.append(q);
  }
  return td;
}

function appliedCell(item) {
  const td = el("td");
  if (item.computed) td.append(el("span", { class: "badge muted", text: "собрано из позиций" }));
  else if (item.applied) td.append(el("span", { class: "badge ok", text: "применено" }));
  else td.append(el("span", { class: "badge bad", text: "не применено", title: item.problem || "" }),
    el("div", { class: "src-file", text: item.problem || "" }));
  if (item.comment && !item.computed) td.append(el("div", { class: "src-file", text: item.comment }));
  return td;
}

function changeSetCard(s, open, busy, superseded = false, r = null) {
  const files = (s.files || []).map((f) => f.split("/").pop());
  const head = el("div", { class: "cs-head" }, `🔄 Изменения от ${s.at_text}`,
    el("span", { class: "muted", text: " · " + files.join(", ") + (s.by ? ` · ${s.by}` : "") +
      (s.usage && s.usage.usd !== undefined ? ` · ≈ ${usd(s.usage.usd)}` : "") }));
  const box = el(open ? "div" : "details", { class: "change-set" + (open ? "" : " old") });
  box.append(open ? head : el("summary", {}, head));
  if (s.summary) box.append(el("div", { class: "cs-summary", text: s.summary }));
  if ((s.documents || []).length) {
    box.append(el("ul", { class: "cs-docs" }, ...s.documents.map((x) => el("li", {
      text: `${CS_KIND_ICON[x.kind] || "📄"} ${(x.file || "").split("/").pop()} — ${x.kind || "документ"}` +
        (x.date ? `, ${x.date}` : "") + (x.summary ? `: ${x.summary}` : "") }))));
  }
  if (s.full_reanalysis && s.full_reanalysis.needed) {
    const n = el("div", { class: "notice warn", text: "⚠ Документация сильно переделана — точечные изменения могут быть неполными. " +
      (s.full_reanalysis.reason || "") });
    const btn = el("button", { class: "btn small need-analyze", type: "button", text: "Повторить анализ целиком" });
    btn.addEventListener("click", () => $("an-run").click());
    if (!busy) n.append(el("br"), btn);
    box.append(n);
  }
  const fields = s.fields || [];
  if (fields.length) {
    const body = el("tbody");
    for (const c of fields) {
      body.append(el("tr", {},
        el("td", { text: c.label }),
        el("td", { class: "cs-old" }, longText(c.old || "—")),
        el("td", { class: "cs-new" }, longText(c.new || "—")),
        quoteCell(c), appliedCell(c)));
    }
    box.append(el("div", { class: "table-wrap" }, el("table", { class: "cs-table" },
      el("thead", {}, el("tr", {}, ...["Поле", "Было", "Стало", "Источник", ""].map((h) => el("th", { text: h })))), body)));
  }
  const poss = s.positions || [];
  if (poss.length) {
    const body = el("tbody");
    for (const c of poss) {
      const what = el("td");
      if (c.action === "removed") what.append(el("div", { text: "позиция исключена" }));
      for (const ch of c.changes || []) {
        const line = el("div", {}, el("span", { class: "cs-col", text: ch.column + ": " }));
        if (c.action === "changed" && ch.column !== "Техническое задание") {
          line.append(el("span", { class: "cs-old", text: ch.old || "—" }), " → ");
        }
        line.append(el("b", { text: ch.new }));
        what.append(line);
      }
      body.append(el("tr", {},
        el("td", { class: "num", text: c.n }),
        el("td", {}, el("div", { text: c.name || "" }), el("span", { class: "badge " + (c.action === "removed" ? "bad" : c.action === "added" ? "ok" : "ocr"), text: CS_ACTION[c.action] || c.action })),
        what, quoteCell(c), appliedCell(c)));
    }
    box.append(el("div", { class: "table-wrap" }, el("table", { class: "cs-table" },
      el("thead", {}, el("tr", {}, ...["№", "Позиция", "Что изменилось", "Источник", ""].map((h) => el("th", { text: h })))), body)));
  }
  if (!fields.length && !poss.length) {
    box.append(el("div", { class: "muted", text: "Данные конкурса не изменились." }));
  }
  if ((s.comments || []).length) {
    box.append(el("div", { class: "cs-col", text: "Комментарии и разъяснения:" }),
      el("ul", { class: "cs-comments" }, ...s.comments.map((c) => {
        const li = el("li", {}, "💬 " + c.text);
        if (c.file || c.quote) {
          const src = el("div", { class: "src-file", text: (c.file || "").split("/").pop() + (c.quote ? " — «" + c.quote + "»" : "") });
          li.append(src);
        }
        return li;
      })));
  }
  if ((s.checks || []).length) {
    box.append(el("ul", { class: "checks" }, ...s.checks.map((c) => el("li", { class: c.startsWith("✅") ? "" : "warn", text: c }))));
  }
  const pr = s.prices || {};
  if ((pr.positions || []).length) {
    box.append(el("div", { class: "src-file", text: (pr.queued ? "Цены: поиск по позициям № " : "Цены нужно искать по позициям № ") +
      pr.positions.join(", ") + (pr.queued ? " поставлен в очередь (раздел «Цены»)." : " — новые или с другим названием / маркой.") }));
  }
  if (superseded) {
    box.append(el("div", { class: "src-file", text: `После этого разбора анализ повторён целиком${r && r.analyzed_at_text ? " (" + r.analyzed_at_text + ")" : ""} — ` +
      "эти документы в нём уже учтены." + (s.noco && s.noco.status === "saved" ? " В NocoDB изменения были записаны." : "") }));
  } else {
    box.append(changeSetNoco(s, busy));
  }
  return box;
}

function changeSetNoco(s, busy) {
  const n = s.noco;
  const any = (s.fields || []).concat(s.positions || []).some((c) => c.applied);
  if (!any) return el("div", { class: "src-file", text: "В NocoDB записывать нечего — изменений в данных нет." });
  if (n && ["requested", "running"].includes(n.status)) {
    return el("div", { class: "notice", text: "⏳ Изменения записываются в NocoDB…" });
  }
  if (n && n.status === "saved") {
    const b = el("div", { class: "notice ok" }, `✅ ${n.text || "Записано в NocoDB"}` +
      (n.finished_at ? ` (${new Date(n.finished_at * 1000).toLocaleString("ru-RU")})` : "") + ". ");
    if (n.link) b.append(el("a", { href: n.link, target: "_blank", rel: "noopener", text: "Открыть запись ↗" }));
    if (n.spec_note) b.append(el("div", { text: n.spec_note }));
    if ((n.issues || []).length) {
      b.append(el("div", { class: "warn-text", text: "⚠ Проверка записанного нашла расхождения:" }),
        el("ul", {}, ...n.issues.slice(0, 8).map((i) => el("li", { text: `${i.column || ""}: ${i.problem || ""}` }))));
    }
    return b;
  }
  const b = el("div", { class: "notice" + (n && n.status === "error" ? " bad" : "") });
  if (n && n.status === "error") b.append(el("div", { text: "Запись изменений в NocoDB не удалась: " + (n.text || "") }));
  b.append(el("div", { text: "В NocoDB ещё не записано. Запишется только изменённое: поля в реестре и строки спецификации " +
    "(изменённые — обновятся, новые — добавятся, исключённые — пометятся в «Примечании», не удаляются)." }));
  if (!busy) {
    const btn = el("button", { class: "btn small primary need-analyze", type: "button",
      text: n && n.status === "error" ? "Повторить запись" : "Записать изменения в NocoDB" });
    btn.addEventListener("click", async () => {
      const t = currentTender();
      if (!t || !t.folder) return;
      btn.disabled = true;
      try {
        await api(clientPath("/analysis/changes/write"), { folder: t.folder, change: s.id }, { method: "POST" });
      } catch (err) {
        setStatus("an-status", "Не удалось запустить запись изменений: " + err.message, true);
      }
      loadAnalysis();
    });
    b.append(btn);
  }
  return b;
}

function renderChanges(sets, busy, r) {
  const box = $("an-changes");
  if (!sets.length || !r) { box.replaceChildren(); return; }
  // Разборы до последнего повторного анализа целиком: их документы уже вошли в анализ — только история.
  const inResult = new Set((r.changes || []).map((c) => c.id));
  box.replaceChildren(...sets.map((s, i) => changeSetCard(s, i === 0 && inResult.has(s.id), busy, !inResult.has(s.id), r)));
}

function renderNoco(r, busy) {
  const box = $("an-noco");
  const n = r.noco;
  if (!n) {
    const b = el("div", { class: "notice" }, "В NocoDB ещё не записано.");
    if (!busy) b.append(el("br"), saveButton("Записать в NocoDB", false));
    box.replaceChildren(b);
    return;
  }
  if (n.status === "saved") {
    const b = el("div", { class: "notice ok" });
    b.append(`✅ NocoDB: запись ${n.record_id} ${n.action === "created" ? "создана" : "обновлена"}` +
      (n.at ? ` (${new Date(n.at * 1000).toLocaleString("ru-RU")})` : "") + ". ");
    b.append(el("a", { href: n.link, target: "_blank", rel: "noopener", text: "Открыть запись ↗" }));
    if (n.spec && n.spec.url) {
      b.append(" · ", el("a", { href: n.spec.url, target: "_blank", rel: "noopener", text: "Спецификация ↗" }));
    }
    const issues = [...((n.verification && n.verification.issues) || []),
                    ...((n.spec && n.spec.verification && n.spec.verification.issues) || [])];
    if (n.separate && n.conflict) b.append(el("div", { text: n.conflict.text }));
    if (n.spec && n.spec.status === "kept") b.append(el("div", { text: n.spec.text }));
    if (issues.length) {
      const ul = el("ul");
      for (const i of issues.slice(0, 10)) {
        ul.append(el("li", { text: `${i.column || ""}${i.row ? " (строка " + i.row + ")" : ""}: ${i.problem}` }));
      }
      b.append(el("div", { class: "warn-text", text: "⚠ Проверка записанного нашла расхождения:" }), ul);
    }
    box.replaceChildren(b);
    return;
  }
  if (n.status === "conflict") {
    const c = n.conflict || {};
    const b = el("div", { class: "notice warn" });
    b.append(el("div", { text: "⚠ В NocoDB не записано: " + (c.text || "") }));
    if (!busy) {
      b.append(el("br"));
      b.append(saveButton(c.kind === "duplicate"
        ? `Записать отдельной записью ${r.internal_number}` : `Обновить запись ${r.internal_number} данными анализа`, true));
    }
    box.replaceChildren(b);
    return;
  }
  const b = el("div", { class: "notice bad", text: "Запись в NocoDB не удалась: " + (n.text || "") });
  if (!busy) b.append(el("br"), saveButton("Повторить запись", false));
  box.replaceChildren(b);
}

function saveButton(label, force) {
  const btn = el("button", { class: "btn small need-analyze" + (force ? "" : " primary"), type: "button", text: label });
  btn.addEventListener("click", async () => {
    const t = currentTender();
    if (!t || !t.folder) return;
    btn.disabled = true;
    try {
      await api(clientPath("/analysis/save"), { folder: t.folder, force: force ? "1" : "" }, { method: "POST" });
    } catch (err) {
      setStatus("an-status", "Не удалось запустить запись: " + err.message, true);
    }
    loadAnalysis();
  });
  return btn;
}

function renderAnalysisFields(fields) {
  const head = el("tr", {}, ...["Поле", "Значение", "Источник", "Проверка"].map((h) => el("th", { text: h })));
  const body = el("tbody");
  const BLOCKS = { 1: "Блок 1 — Товар и поставка", 2: "Блок 2 — Цена и условия оплаты", 3: "Блок 3 — Обеспечение",
    4: "Блок 4 — Ключевые даты", 5: "Блок 5 — Организатор и заказчик", 6: "Блок 6 — Площадка и процедура",
    7: "Блок 7 — Национальный режим (ПП РФ № 1875)" };
  let block = null;
  for (const f of fields) {
    if (f.block !== block) {
      block = f.block;
      body.append(el("tr", {}, el("td", { class: "block-row", colspan: "4", text: BLOCKS[block] || "" })));
    }
    const src = el("td");
    if (f.file) src.append(el("div", { class: "src-file", text: f.file.split("/").pop() }));
    if (f.quote) {
      const q = el("div", { class: "quote clamp", title: "Щёлкните, чтобы развернуть", text: "«" + f.quote + "»" });
      q.addEventListener("click", () => q.classList.toggle("open"));
      src.append(q);
    }
    const chk = el("td");
    const [qt, qc] = QUOTE_TEXT[f.quote_status] || ["", ""];
    if (qt) chk.append(el("span", { class: "badge " + qc, text: qt }));
    if (f.check) {
      const v = el("div", { class: "src-file", text: VERDICT_TEXT[f.check.verdict] || f.check.verdict });
      if (f.check.comment) v.title = f.check.comment;
      chk.append(v);
    }
    const val = el("td");
    const node = renderValue(f.value, "");
    if (node) val.append(node);
    if (f.changed) {
      val.append(el("div", { class: "changed-badge", text: `🔄 изменено ${f.changed.date || ""}`,
        title: "Было: " + (f.changed.old || "—") + (f.changed.comment ? "\n" + f.changed.comment : "") }));
    }
    body.append(el("tr", { class: f.changed ? "row-changed" : null }, el("td", { text: f.label }), val, src, chk));
  }
  $("an-fields-table").replaceChildren(el("thead", {}, head), body);
}

function renderAnalysisPositions(rows) {
  const cols = ["№", "Наименование позиции", "Кол-во", "Ед. изм.", "Марка/модель", "Производитель",
    "Страна происхождения", "Аналог (статус)", "Аналог допускается", "НМЦК за позицию", "Стоимость НМЦ",
    "Техническое задание", "Ошибки в документации", "Примечание"];
  if (!rows.length) {
    $("an-pos-table").replaceChildren(el("tbody", {}, el("tr", {}, el("td", { class: "empty-val", text: "Позиции не найдены." }))));
    return;
  }
  const head = el("tr", {}, ...cols.map((c, i) => el("th", { class: i === 0 ? "stick0" : (i === 1 ? "stick1" : null), text: c })));
  const body = el("tbody");
  for (const r of rows) {
    const note = String(r["Примечание"] || "");
    const tr = el("tr", { class: /🔄 Изменение|🆕 Добавлена/.test(note) ? "row-changed" : null });
    cols.forEach((c, i) => {
      const v = r[c];
      const cls = [i === 0 ? "stick0" : (i === 1 ? "stick1" : ""), typeof v === "number" ? "num" : ""].filter(Boolean).join(" ") || null;
      const td = el("td", { class: cls });
      if (c === "Аналог (статус)" && v) td.append(el("span", { class: "badge " + (ANALOG_CLASS[v] || ""), text: v }));
      else if (["Техническое задание", "Ошибки в документации", "Примечание", "Аналог допускается"].includes(c) && v) td.append(longText(String(v)));
      else { const node = renderValue(v, ""); if (node) td.append(node); }
      tr.append(td);
    });
    body.append(tr);
  }
  const table = $("an-pos-table");
  table.replaceChildren(el("thead", {}, head), body);
  const first = table.querySelector("th.stick0");
  if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
}

function showAnalysisTab(tab) {
  anTab = tab;
  for (const b of document.querySelectorAll("#analysis .tab")) b.classList.toggle("active", b.dataset.tab === tab);
  $("an-fields").hidden = tab !== "fields";
  $("an-positions").hidden = tab !== "positions";
  if (tab === "positions") {
    const table = $("an-pos-table");
    const first = table.querySelector("th.stick0");
    if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
  }
}

function usageText(totals) {
  if (!totals) return "";
  const n = (x) => fmtInt.format(x || 0);
  const parts = Object.entries(totals).map(([model, u]) =>
    `${model}: вызовов ${u.calls}, вход ${n(u.input)}` + (u.cache_write ? `, в кэш ${n(u.cache_write)}` : "") +
    (u.cache_read ? `, из кэша ${n(u.cache_read)}` : "") + `, ответ ${n(u.output)}` +
    (u.usd !== undefined ? ` ≈ ${usd(u.usd)}` : ""));
  return parts.length ? "Токены — " + parts.join("; ") + ". Стоимость — оценка по ценам API; точная сумма — в консоли Anthropic." : "";
}

// ---------- поиск цен ----------

const PR_ABROAD_KEY = "supplybro.pricesAbroad";
const fmtRub = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: 2 });

function stopPricesPolling() {
  if (prTimer) { clearTimeout(prTimer); prTimer = null; }
}

async function loadPrices() {
  const t = currentTender();
  const my = ++seq.pr;
  stopPricesPolling();
  if (!t || !t.folder || blockOff("prices")) { $("prices").hidden = true; return; }
  $("prices").hidden = false;
  let d;
  try {
    d = await api(clientPath("/prices"), { folder: t.folder });
  } catch (err) {
    if (my === seq.pr) setStatus("pr-status", "Не удалось загрузить поиск цен: " + err.message, true);
    return;
  }
  if (my !== seq.pr) return;
  setStatus("pr-status", "");
  const busy = renderPricesJob(d.job);
  renderPrices(d, busy);
  loadPriceSummary();
  const finished = prLastJob && d.job && prLastJob.id === d.job.id &&
    ["queued", "running"].includes(prLastJob.status) && !["queued", "running"].includes(d.job.status);
  prLastJob = d.job ? { id: d.job.id, status: d.job.status } : null;
  if (finished) {
    loadAnalysis();                         // в позициях анализа появились цены рынков
    if (t.source === "nocodb") loadSpec();  // и, возможно, в спецификации NocoDB
  }
  // Пока идёт поиск — каждые 3 с; пока идёт подготовка / анализ / запись в NocoDB — каждые 5 с
  // (иначе блок так и показывал бы состояние «до анализа» и не заметил бы поставленный сам поиск цен).
  if (busy) prTimer = setTimeout(loadPrices, 3000);
  else if (d.analysis_active || d.noco_active) prTimer = setTimeout(loadPrices, 5000);
}

function renderPricesJob(job) {
  const box = $("pr-job");
  lastJobs["pr-job-ctl"] = job;
  renderJobControls($("pr-job-ctl"), job);
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done", "paused", "canceled");
  const what = job.kind === "noco_prices" ? "Запись цен в NocoDB" : "Поиск цен";
  if (job.status === "queued") {
    $("pr-bar").style.width = "0%";
    $("pr-job-text").textContent = `${what}: ` + queuedText(job);
    return true;
  }
  if (job.status === "running") {
    $("pr-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("pr-job-text").textContent = `${what}: ` + (job.step || "…") + stoppingText(job);
    return true;
  }
  if (job.status === "paused" || job.status === "canceled") {
    box.classList.add(job.status);
    $("pr-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("pr-job-text").textContent = `${what}: ` + stoppedText(job);
    return false;
  }
  $("pr-bar").style.width = "100%";
  const when = job.finished_at ? new Date(job.finished_at * 1000).toLocaleString("ru-RU") : "";
  if (job.status === "error") {
    box.classList.add("error");
    $("pr-job-text").textContent = `${what} — ошибка (${when}): ${job.message}`;
  } else {
    box.classList.add("done");
    $("pr-job-text").textContent = `${what} — готово (${when}). ${job.message}`;
  }
  return false;
}

function rub(x) {
  return x === null || x === undefined || x === "" ? "" : fmtRub.format(x) + " ₽";
}

function toNum(v) {
  if (typeof v === "number") return v;
  const n = parseFloat(String(v || "").replace(/\s/g, "").replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

const PR_ENGINE_KEY = "supplybro.pricesEngine";
const ENGINE_LABEL = { deepseek: "DeepSeek", yandex: "Яндекс (Россия) + DeepSeek (зарубеж)", both: "DeepSeek + Яндекс (сравнить)" };
const SOURCE_LABEL = { deepseek: "DeepSeek", yandex: "Яндекс", claude: "Claude" };
let prLast = null;   // последний ответ /prices — чтобы пересчитать оценку при смене настроек
let prBusy = false;

function priceEstimate(d) {
  const e = d.estimate;
  if (!e) return "";
  const engine = $("pr-engine").value || "deepseek";
  const abroad = $("pr-abroad").checked;
  const ds = (engine !== "yandex" ? e.deepseek_rf_usd : 0) + (abroad ? e.deepseek_abroad_usd : 0) + e.deepseek_pages_usd +
    (e.recheck_usd || 0);
  const usdTotal = ds + e.judge_usd + (abroad ? e.claude_abroad_usd || 0 : 0);
  let text = `${e.groups} артикулов: ≈ ${usd(usdTotal)} (поиск DeepSeek ≈ ${usd(ds)}, проверка моделью ≈ ${usd(e.judge_usd)}` +
    (abroad && e.claude_abroad_usd ? `, Claude за рубежом ≈ ${usd(e.claude_abroad_usd)}` : "") + ")";
  if (engine !== "deepseek") {
    const rubY = $("pr-urgent").checked ? e.yandex_sync_rub : e.yandex_async_rub;
    text += ` + Яндекс ≈ ${fmtRub.format(rubY)} ₽`;
  }
  return text;
}

function renderPrices(d, busy) {
  prLast = d;
  prBusy = busy;
  const r = d.report;
  const s = d.settings || {};
  const engines = s.engines || ["deepseek"];
  const savedEngine = storageGet(PR_ENGINE_KEY);
  fillSelect($("pr-engine"), engines.map((x) => ({ value: x, label: ENGINE_LABEL[x] || x })),
    !USER_MODE && engines.includes(savedEngine) ? savedEngine : s.engine);
  const abroadBox = $("pr-abroad");
  const saved = storageGet(PR_ABROAD_KEY);
  abroadBox.checked = saved === null ? s.abroad !== false : saved === "1";
  abroadBox.disabled = s.abroad === false;
  const engine = $("pr-engine").value || "deepseek";
  $("pr-urgent-box").hidden = engine === "deepseek";
  const needYandex = engine !== "deepseek";
  const needDeepseek = engine !== "yandex" || abroadBox.checked;
  const run = $("pr-run");
  run.textContent = r ? "Искать заново" : "Искать цены";
  const blocker = !d.has_analysis ? "Сначала нужен анализ документов — цены ищутся по его позициям"
    : d.analysis_active ? "Идёт подготовка, анализ или запись в NocoDB — поиск цен после них"
    : needYandex && !s.yandex_key ? "Нет ключа Яндекса (secrets/API_key_Yandex.txt) — выберите поиск DeepSeek"
    : needDeepseek && !s.deepseek_key ? "Нет ключа DeepSeek (secrets/API_key_DeepSeek.txt)" : "";
  run.disabled = busy || Boolean(blocker);
  const est = priceEstimate(d);
  run.title = blocker || (est ? "Оценка: " + est : "");
  $("pr-stale").hidden = !(d.stale && !busy);
  if (r) {
    const c = r.cost || {};
    const total = c.usd !== undefined ? c.usd : (c.claude_usd || 0);
    $("pr-info").textContent = `· ${r.at_text} · ${Math.max(1, Math.round((r.seconds || 0) / 60))} мин · ≈ ${usd(total)}` +
      (c.yandex_rub ? ` + ${fmtRub.format(c.yandex_rub)} ₽` : "");
  } else {
    $("pr-info").textContent = "";
  }
  if (blocker && d.has_analysis && !d.analysis_active) {
    setStatus("pr-status", blocker + ".", true);
  } else if (!busy && d.analysis_active) {
    setStatus("pr-status", (d.has_analysis
      ? "Анализ готов, идёт запись в NocoDB."
      : "Идёт подготовка и анализ документов.") +
      (d.auto_prices ? " Поиск цен запустится сам сразу после этого; найденные цены попадут в спецификацию NocoDB."
        : " После этого можно будет искать цены."));
  } else if (!r && !busy) {
    setStatus("pr-status", d.has_analysis
      ? "Цен ещё нет. «Искать цены» — программа найдёт товар поиском " + (engine === "deepseek" ? "DeepSeek" : engine === "yandex" ? "Яндекса" : "DeepSeek и Яндекса") +
        (abroadBox.checked ? " (Россия и зарубеж)" : " (Россия)") +
        ", откроет страницы магазинов, проверит артикул и цену и выберет по 3 самые низкие цены со ссылками." +
        (est ? " Оценка: " + est + "." : "")
      : "Поиск цен в интернете на сайте — после анализа документов. Цены, которые уже есть в спецификации NocoDB " +
        "(агент, рынки, отдел закупок) и в прошлых конкурсах, — во вкладке «Все цены».");
  } else {
    setStatus("pr-status", "");
  }
  if (!r) {
    $("pr-wrap").hidden = true;
    applyPriceTab();
    $("pr-noco").replaceChildren();
    $("pr-checks").replaceChildren();
    $("pr-usage").textContent = "";
    return;
  }
  const n = r.noco || {};
  $("pr-noco").replaceChildren(el("div", {
    class: "notice " + (n.status === "saved" ? "ok" : n.status === "error" ? "bad" : ""),
    text: n.status === "saved" ? `✅ NocoDB: цены записаны в спецификацию (строк: ${n.rows}).`
      : n.status === "error" ? "Запись цен в NocoDB не удалась: " + (n.text || "") : "NocoDB: " + (n.text || "не записано"),
  }));
  const sm = r.summary || {};
  const checks = [];
  if (r.partial) {
    // Поиск после изменений документации (0.9.0) — только новые позиции и позиции с другим названием / маркой.
    checks.push(`Последний поиск — только по позициям № ${(r.partial.positions || []).join(", ")} (после изменений документации)` +
      (r.partial.previous_at_text ? `; цены остальных позиций — из поиска от ${r.partial.previous_at_text}.` : "."));
  }
  checks.push(`Артикулов: ${sm.groups}; Россия — цены у ${sm.rf_found} (все три — у ${sm.rf_full}); ` +
    (r.abroad === false ? "зарубеж не искали." : `зарубеж — у ${sm.abroad_found} (все три — у ${sm.abroad_full}).`));
  const bs = sm.by_source || {};
  if (Object.keys(bs).length > 1) {
    checks.push("Кто нашёл подтверждённые цены: " + Object.entries(bs).map(([src, v]) =>
      `${SOURCE_LABEL[src] || src} — Россия у ${v.rf_groups} арт. (${v.rf_offers} цен)` +
      (r.abroad === false ? "" : `, зарубеж у ${v.abroad_groups} арт. (${v.abroad_offers} цен)`)).join("; ") + ".");
  }
  checks.push(...(r.checks || []), ...(r.errors || []).slice(0, 8).map((x) => "⚠ " + x));
  $("pr-checks").replaceChildren(...checks.map((c) => el("li", { class: c.startsWith("⚠") || /замечаний — [1-9]/.test(c) ? "warn" : "", text: c })));
  renderPricesTable(r, d.positions || []);
  const c = r.cost || {};
  const parts = [];
  if (c.deepseek_searches || c.deepseek_pages || c.deepseek_cached) {
    parts.push(`DeepSeek: поисков ${c.deepseek_searches || 0}` + (c.deepseek_cached ? ` (+ ${c.deepseek_cached} из сохранённых за сутки)` : "") +
      `, страниц открыто ${c.deepseek_pages || 0} ≈ ${usd(c.deepseek_usd || 0)}.`);
  }
  if (c.yandex_async || c.yandex_sync || c.yandex_cached) {
    parts.push(`Яндекс: отложенных запросов ${c.yandex_async || 0}, мгновенных ${c.yandex_sync || 0}` +
      (c.yandex_cached ? `, из сохранённых ${c.yandex_cached}` : "") + ` ≈ ${fmtRub.format(c.yandex_rub || 0)} ₽.`);
  }
  if (c.claude_searches || c.searches) parts.push(`Поисков Claude за рубежом: ${c.claude_searches || c.searches}.`);
  parts.push(usageText(c.models || c.claude));
  if (r.timing && Object.keys(r.timing).length) {
    parts.push("Время: " + Object.entries(r.timing).filter(([, v]) => v >= 1).map(([k, v]) => `${k} ${fmtDur(v)}`).join(" · ") + ".");
  }
  $("pr-usage").textContent = parts.join(" ");
}

function offerList(items, abroad) {
  const box = el("div", { class: "offers" });
  for (const a of items || []) {
    const line = el("div");
    line.append(el("a", { href: a.url, target: "_blank", rel: "noopener", text: rub(a.price_rub), title: a.note || a.url }));
    const src = (a.sources || []).map((x) => SOURCE_LABEL[x] || x).join(" + ");
    if (!a.url) line.replaceChildren(el("b", { text: rub(a.price_rub) }));
    if (a.from_noco) line.title = "Цена из спецификации NocoDB (записал агент или сайт)";
    line.append(el("span", { class: "muted", text: " " + (a.domain || "") + (a.from_noco ? " · NocoDB" : "") + (abroad && a.currency && a.currency !== "RUB" ? ` · ${a.price} ${a.currency}` : "") +
      (abroad && a.country ? ` · ${a.country}` : "") + (a.original === "no" ? " · аналог" : ""),
      title: src ? "Нашёл: " + src + (a.via === "deepseek" ? "; страницу открыл DeepSeek" : a.via === "browser" ? "; страница открыта встроенным браузером" : "") : null }));
    box.append(line);
  }
  return box;
}

// Цены из выдачи поиска (страницу не открыли или модель не подтвердила) — только показать, с пометкой (0.8.0).
function unverifiedList(items) {
  if (!items || !items.length) return null;
  const box = el("div", { class: "offers unverified" });
  for (const a of items) {
    const line = el("div", { title: "Цена из выдачи поиска: страницу магазина не открывали или модель её не подтвердила. " +
      "В спецификацию NocoDB такие цены не пишутся." + (a.title ? "\n" + a.title : "") });
    line.append(el("span", { class: "badge warn", text: "не проверено" }), " ",
      el("a", { href: a.url, target: "_blank", rel: "noopener", text: rub(a.price_rub) }),
      el("span", { class: "muted", text: " " + a.domain + (a.currency && a.currency !== "RUB" ? ` · ${a.price} ${a.currency}` : "") }));
    box.append(line);
  }
  return box;
}

// ---------- все цены (0.8.0): интернет, поставщики, лучшая цена ----------

let psLast = null;          // /prices/summary
let prTab = "summary";      // вкладка раздела «Цены»: summary — все цены, search — поиск в интернете

function applyPriceTab() {
  const hasSearch = Boolean(prLast && prLast.report);
  const hasSummary = Boolean(psLast && (psLast.has_positions || psLast.has_analysis));
  $("pr-tabs").hidden = !hasSummary;
  const tab = hasSummary ? prTab : "search";
  for (const b of $("pr-tabs").querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.ptab === tab);
  $("ps-wrap").hidden = !(hasSummary && tab === "summary");
  $("ps-status").hidden = !(hasSummary && tab === "summary") || !$("ps-status").textContent;
  $("pr-wrap").hidden = !(hasSearch && tab === "search");
}

async function loadPriceSummary() {
  const t = currentTender();
  if (!t || !t.folder || blockOff("prices")) return;
  const my = ++seq.ps;
  let d;
  try {
    d = await api(clientPath("/prices/summary"), { folder: t.folder, record: t.source === "nocodb" ? t.id : "" });
  } catch (err) {
    if (my === seq.ps) setStatus("ps-status", "Не удалось загрузить общую таблицу цен: " + err.message, true);
    return;
  }
  if (my !== seq.ps) return;
  psLast = d;
  renderPriceSummary(d);
  applyPriceTab();
}

function supplierList(items) {
  const box = el("div", { class: "offers" });
  const t = currentTender();
  for (const s of items) {
    const line = el("div");
    const label = rub(s.price_rub);
    line.append(s.file_path && t ? el("a", { href: clientPath("/docs/file") + "?" + new URLSearchParams({ folder: t.folder, path: s.file_path }),
      target: "_blank", rel: "noopener", text: label, title: "Открыть «" + s.file + "»" }) : el("b", { text: label }));
    line.append(el("span", { text: " " + s.supplier }));
    if (s.match !== "exact") line.append(" ", el("span", { class: "badge warn", text: "🔴 не полностью", title: s.note || "Неполное совпадение с позицией" }));
    if ((s.problems || []).length) line.append(" ", el("span", { class: "badge bad", text: "⚠", title: s.problems.join("; ") }));
    const tail = [];
    if (s.currency && s.currency !== "RUB" && s.price) tail.push(`${s.price} ${s.currency}`);
    if (s.vat && s.vat !== "не указано") tail.push(s.vat);
    tail.push(s.source === "NocoDB" ? "NocoDB" : s.in_noco || s.written ? "в NocoDB" : "не записано");
    line.append(el("span", { class: "muted", text: " · " + tail.join(" · ") }));
    box.append(line);
  }
  return box;
}

function ratioBadge(price, nmc) {
  if (!price || !nmc) return null;
  const pct = Math.round((price / nmc - 1) * 100);
  return el("span", { class: "badge " + (pct <= -15 ? "ok" : pct < 0 ? "warn" : "bad"), text: (pct > 0 ? "+" : "") + pct + " %",
    title: `${rub(price)} к НМЦ за единицу ${rub(nmc)}` });
}

function agentList(r) {
  const box = el("div", { class: "offers" });
  for (const a of r.agent || []) {
    const line = el("div", { title: a.comment || "" });
    line.append(a.url ? el("a", { href: a.url, target: "_blank", rel: "noopener", text: rub(a.price_rub) }) : el("b", { text: rub(a.price_rub) }));
    // Комментарий агента: «Источник: … / Ссылка: … / Пояснение: …» — коротко источник, целиком — во всплывающей подсказке.
    const m = /Источник:\s*([^\n]+)/.exec(a.comment || "");
    let c = (m ? m[1] : (a.comment || "").split("\n")[0]).replace(/\s+/g, " ").trim();
    if (c) line.append(el("span", { class: "muted", text: " " + (c.length > 45 ? c.slice(0, 45) + "…" : c) }));
    box.append(line);
  }
  if (r.market) {
    const m = r.market;
    const line = el("div", { title: [m.text, m.source].filter(Boolean).join("\n") });
    line.append(el("span", { class: "muted", text: "рыночная: " }),
      m.url ? el("a", { href: m.url, target: "_blank", rel: "noopener", text: rub(m.price_rub) }) : el("b", { text: rub(m.price_rub) }));
    box.append(line);
  }
  return box;
}

function historyList(items) {
  const box = el("div", { class: "offers" });
  for (const h of items || []) {
    const line = el("div", { title: (h.name || "") + (h.updated ? "\nОбновлено: " + h.updated : "") });
    line.append(el("span", { class: "muted", text: `${h.tender} № ${h.n}: ` }));
    h.prices.slice(0, 3).forEach((p, i) => {
      if (i) line.append(", ");
      line.append(el("span", { class: "muted", text: p.kind + " " }),
        p.url ? el("a", { href: p.url, target: "_blank", rel: "noopener", text: rub(p.price_rub), title: p.who || p.url })
          : el("b", { text: rub(p.price_rub), title: p.who || "" }));
    });
    box.append(line);
  }
  return box;
}

function renderPriceSummary(d) {
  const rows = d.rows || [];
  const withPrice = rows.filter((r) => r.best).length;
  const withSup = rows.filter((r) => (r.suppliers || []).length).length;
  const showAgent = rows.some((r) => (r.agent || []).length || r.market);
  const showHist = rows.some((r) => (r.history || []).length);
  const parts = [`Позиций: ${rows.length}; с ценой — ${withPrice}; с ценами поставщиков — ${withSup}.`];
  if (d.positions_source === "nocodb") parts.push("Позиции — из спецификации NocoDB (анализа документов на сайте у этого конкурса нет).");
  parts.push(d.has_report ? `Поиск в интернете на сайте — ${d.report_at}.` : "Поиск в интернете на сайте ещё не запускался.");
  if (d.noco_read) parts.push("Цены из спецификации NocoDB учтены (агент, рынки РФ / зарубеж, отдел закупок, доставка).");
  if (d.noco_error) parts.push("⚠ Спецификация NocoDB не прочитана: " + d.noco_error);
  const h = d.history || {};
  if (h.at) parts.push(`«Прошлые конкурсы» — тот же артикул в других спецификациях NocoDB (своя база цен от ${h.at}` + (h.building ? ", обновляется" : "") + ").");
  else if (h.building) parts.push("Своя база цен (прошлые конкурсы) собирается — обновите страницу через минуту.");
  parts.push("Лучшая — самая низкая из проверенных (интернет, агент, поставщики; 🔴 — с пометкой; «не проверено» и прошлые конкурсы — не учитываются).");
  setStatus("ps-status", parts.join(" "), Boolean(d.noco_error));
  const cols = ["№", "Позиция", "Кол-во", "НМЦ за ед.", "Интернет: Россия", "Интернет: зарубеж"];
  if (showAgent) cols.push("Агент (NocoDB)");
  cols.push("Поставщики (прайсы, КП, счета, NocoDB)");
  if (showHist) cols.push("Прошлые конкурсы");
  cols.push("Лучшая цена", "К НМЦ");
  const head = el("tr", {}, ...cols.map((h, i) => el("th", { class: i === 0 ? "stick0" : (i === 1 ? "stick1" : null), text: h })));
  const body = el("tbody");
  for (const r of rows) {
    const pos = el("td", { class: "stick1" }, el("div", { text: r.name }));
    if (r.brand_model) pos.append(el("div", { class: "muted", text: r.brand_model }));
    const none = (text) => el("span", { class: "empty-val", text });
    const rfCell = el("td", {}, r.rf.length ? offerList(r.rf, false) : none(r.searched ? "не найдено" : "—"),
      unverifiedList(r.unverified.filter((a) => a.market === "rf")));
    const abCell = el("td", {}, r.abroad.length ? offerList(r.abroad, true) : none(r.searched && d.abroad !== false ? "не найдено" : "—"),
      unverifiedList(r.unverified.filter((a) => a.market === "abroad")));
    const cells = [el("td", { class: "stick0", text: r.n }), pos,
      el("td", { class: "num", text: [r.qty, r.unit].filter((x) => x !== null && x !== undefined && x !== "").join(" ") }),
      el("td", { class: "num", text: r.nmc ? rub(r.nmc) : "" }), rfCell, abCell];
    if (showAgent) cells.push(el("td", {}, (r.agent || []).length || r.market ? agentList(r) : none("—")));
    cells.push(el("td", {}, r.suppliers.length ? supplierList(r.suppliers) : none("—")));
    if (showHist) cells.push(el("td", {}, (r.history || []).length ? historyList(r.history) : none("—")));
    const best = el("td", { class: "num" });
    if (r.best) {
      best.append(r.best.url ? el("a", { href: r.best.url, target: "_blank", rel: "noopener", text: rub(r.best.price_rub) }) : el("b", { text: rub(r.best.price_rub) }),
        el("div", { class: "muted small", text: r.best.source }));
    }
    if (r.delivery) best.append(el("div", { class: "muted small", text: "доставка СДЭК: " + rub(r.delivery) }));
    cells.push(best, el("td", { class: "num" }, ratioBadge(r.best && r.best.price_rub, r.nmc)));
    body.append(el("tr", {}, ...cells));
  }
  const table = $("ps-table");
  table.replaceChildren(el("thead", {}, head), body);
  const first = table.querySelector("th.stick0");
  if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
}

for (const b of document.querySelectorAll("#pr-tabs .tab")) {
  b.addEventListener("click", () => { prTab = b.dataset.ptab; applyPriceTab(); });
}

function renderPricesTable(r, positions) {
  const byN = new Map(positions.map((p) => [String(p["№"]), p]));
  const cols = ["№", "Позиция", "НМЦ за ед.", "Россия (3 лучшие)", "Зарубеж (3 лучшие, в рублях по курсу ЦБ)", "Мин. к НМЦ", "Своя база", "Как искали"];
  const head = el("tr", {}, ...cols.map((h, i) => el("th", { class: i === 0 ? "stick0" : (i === 1 ? "stick1" : null), text: h })));
  const body = el("tbody");
  for (const g of r.groups || []) {
    const p = byN.get(String((g.positions || [])[0])) || {};
    const nmc = toNum(p["Стоимость НМЦ"]);
    const rf = (g.chosen && g.chosen.rf) || [];
    const ab = (g.chosen && g.chosen.abroad) || [];
    const all = [...rf, ...ab].map((a) => a.price_rub).filter((x) => x);
    const min = all.length ? Math.min(...all) : null;
    const pos = el("td", { class: "stick1" });
    pos.append(el("div", { text: g.name }));
    if (g.brand_model) pos.append(el("div", { class: "muted", text: g.brand_model }));
    const ratio = el("td", { class: "num" });
    if (min && nmc) {
      const pct = Math.round((min / nmc - 1) * 100);
      ratio.append(el("span", { class: "badge " + (pct <= -15 ? "ok" : pct < 0 ? "warn" : "bad"), text: (pct > 0 ? "+" : "") + pct + " %",
        title: `Самая низкая найденная цена ${rub(min)} к НМЦ за единицу ${rub(nmc)}` }));
    }
    const hist = el("td");
    for (const h of (g.history || []).slice(0, 3)) {
      const line = el("div", { class: "src-file" });
      line.append(`${h.tender} № ${h.n}: `);
      (h.prices || []).slice(0, 3).forEach((x, i) => {
        if (i) line.append(", ");
        const label = `${x.kind} ${rub(toNum(x.price))}`;
        line.append(x.url ? el("a", { href: x.url, target: "_blank", rel: "noopener", text: label }) : el("span", { text: label, title: x.who || "" }));
      });
      hist.append(line);
    }
    const how = el("td");
    const qs = [`Россия (${r.engine === "yandex" ? "Яндекс" : r.engine === "both" ? "DeepSeek + Яндекс" : r.engine === "deepseek" ? "DeepSeek" : "Яндекс"}): ` +
      (g.rf_queries || []).map((q) => "«" + q + "»").join(", ")];
    if ((g.abroad_queries || []).length) qs.push(`Зарубеж: ${g.abroad_queries.map((q) => "«" + q + "»").join(", ")}`);
    qs.push(`Найдено страниц: ${g.found || 0}, открыто и проверено: ${g.checked || 0}`);
    if ((g.unopened || []).length) qs.push("Не открылись: " + [...new Set(g.unopened)].join(", "));
    for (const note of g.notes || []) qs.push(note);
    how.append(longText(qs.join("\n")));
    body.append(el("tr", {},
      el("td", { class: "stick0", text: (g.positions || []).join(", ") }), pos,
      el("td", { class: "num", text: nmc ? rub(nmc) : "" }),
      el("td", {}, rf.length ? offerList(rf, false) : el("span", { class: "empty-val", text: "не найдено" }),
        unverifiedList((g.unverified || []).filter((a) => a.market === "rf"))),
      el("td", {}, ab.length ? offerList(ab, true) : el("span", { class: "empty-val", text: r.abroad === false ? "не искали" : "не найдено" }),
        unverifiedList((g.unverified || []).filter((a) => a.market === "abroad"))),
      ratio, hist, how));
  }
  const table = $("pr-table");
  table.replaceChildren(el("thead", {}, head), body);
  applyPriceTab();
  const first = table.querySelector("th.stick0");
  if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
}

// ---------- чат по документации ----------

const CHAT_MODEL_KEY = "supplybro.chatModel";
let chatBusy = false;

function storageGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function storageSet(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* браузер не даёт хранить */ } }

// Простое оформление ответа: абзацы, списки «- », **жирный**. Текст вставляется только как текст.
function inlineMd(text) {
  const frag = document.createDocumentFragment();
  text.split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) frag.append(el("b", { text: part.slice(2, -2) }));
    else if (part) frag.append(linkify(part));
  });
  return frag;
}

function renderMd(text) {
  const box = document.createDocumentFragment();
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*[-•*]\s+(.*)$/) || line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (item) {
      if (!list) { list = el("ul"); box.append(list); }
      list.append(el("li", {}, inlineMd(item[1])));
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const head = line.match(/^#{1,4}\s+(.*)$/);
    box.append(el("p", {}, head ? el("b", { text: head[1] }) : inlineMd(line)));
  }
  return box;
}

// Ответ делится на текст и раздел «Источники:» (его программа проверяет по документам).
function splitSources(text) {
  const m = text.match(/(^|\n)\s*\**\s*Источники\s*:?\s*\**\s*(\n|$)/i);
  return m ? [text.slice(0, m.index), text.slice(m.index + m[0].length)] : [text, ""];
}

const SRC_BADGE = { ok: ["✓", "ok"], other_file: ["✓", "ok"], partial: ["≈", "warn"], not_found: ["✗ цитата не найдена", "bad"] };

function chatMeta(meta) {
  if (!meta) return "";
  const u = meta.usage || {};
  const n = (x) => fmtInt.format(x || 0);
  const parts = [meta.fallback ? `${meta.fallback} (вместо ${meta.model}: на счёте Anthropic нет денег)` : meta.model];
  if (meta.mode === "fragments") parts.push("по найденным фрагментам");
  if (u.input !== undefined) {
    parts.push(`вход ${n(u.input)}` + (u.cache_read ? `, из кэша ${n(u.cache_read)}` : "") +
      (u.cache_write ? `, в кэш ${n(u.cache_write)}` : "") + `, ответ ${n(u.output)} токенов`);
  }
  if (u.usd !== undefined && u.usd !== null) parts.push(`≈ ${usd(u.usd)}`);
  if (meta.seconds) parts.push(`${meta.seconds} с`);
  return parts.filter(Boolean).join(" · ");
}

function renderChatMessage(m) {
  if (m.role === "user") return el("div", { class: "msg user", text: m.content });
  if (m.role === "error") return el("div", { class: "msg error", text: "Ошибка: " + (m.meta && m.meta.error ? m.meta.error : m.content) });
  const box = el("div", { class: "msg assistant" });
  const [body, src] = splitSources(m.content);
  box.append(renderMd(body));
  const checked = (m.meta && m.meta.sources) || [];
  if (src.trim() || checked.length) {
    const ul = el("ul");
    const lines = checked.length ? checked : src.split("\n").filter((l) => l.trim()).map((l) => ({ text: l.replace(/^\s*[-•*]\s*/, "") }));
    for (const s of lines) {
      const li = el("li", {}, linkify(s.text));
      const b = SRC_BADGE[s.quote_status];
      if (b) li.append(" ", el("span", { class: "badge " + b[1], text: b[0] }));
      ul.append(li);
    }
    box.append(el("div", { class: "sources" }, el("b", { text: "Источники:" }), ul));
  }
  const meta = chatMeta(m.meta);
  if (meta) box.append(el("div", { class: "meta", text: meta }));
  return box;
}

async function loadChat() {
  const t = currentTender();
  const my = ++seq.chat;
  if (!t || !t.folder || blockOff("chat")) return;
  let d;
  try {
    d = await api(clientPath("/chat"), { folder: t.folder });
  } catch (err) {
    if (my === seq.chat) $("chat-hint").textContent = "Не удалось загрузить чат: " + err.message;
    return;
  }
  if (my !== seq.chat) return;
  const saved = storageGet(CHAT_MODEL_KEY);
  fillSelect($("chat-model"), d.models.map((x) => ({ value: x, label: x })), d.models.includes(saved) ? saved : d.default_model);
  chatMsgs = d.messages;
  plData = null;
  plSel.clear();
  renderChatLog();
  $("chat-hint").textContent = "Ответы — только по документам этого конкурса и найденным ценам, с источниками; цитаты программа сверяет с текстом. " +
    "Задавать вопросы можно в любое время, как только документы подготовлены, — анализ для этого не нужен. " +
    "Прайс, КП или счёт поставщика — скрепкой 📎 или перетащите файл сюда: сайт найдёт позиции конкурса и цены.";
  scrollChat();
  loadChatStatus();
  loadPricelists();
}

// ---------- прайсы, КП и счета поставщиков в чате (0.8.0) ----------

let chatMsgs = [];          // сообщения чата (для общего порядка с карточками прайсов)
let plData = null;          // /pricelists
const plSel = new Map();    // id файла → отмеченные к записи позиции (Set)
const plCards = new Map();  // id файла → карточка в чате
let plTimer = null;
const PL_BUSY = (f) => ["queued", "running"].includes(f.status) || ["requested", "running"].includes((f.write || {}).status);

function renderChatLog() {
  const items = chatMsgs.map((m) => ({ t: m.created_at || 0, node: renderChatMessage(m) }));
  plCards.clear();
  for (const f of (plData && plData.files) || []) {
    const node = pricelistCard(f);
    plCards.set(f.id, node);
    items.push({ t: f.uploaded_at || 0, node });
  }
  items.sort((a, b) => a.t - b.t);
  $("chat-log").replaceChildren(...items.map((x) => x.node));
}

async function loadPricelists() {
  const t = currentTender();
  if (!t || !t.folder || blockOff("chat") || blockOff("prices")) return;
  const my = ++seq.pl;
  clearTimeout(plTimer);
  let d;
  try {
    d = await api(clientPath("/pricelists"), { folder: t.folder });
  } catch (_) { return; }
  if (my !== seq.pl || currentTender() !== t) return;
  const before = new Map(((plData && plData.files) || []).map((f) => [f.id, f]));
  plData = d;
  let changedPrices = false, added = false;
  for (const f of d.files) {
    const old = before.get(f.id);
    if (!old) { added = true; continue; }
    if (JSON.stringify(old) === JSON.stringify(f)) continue;
    if (old.status !== f.status || (old.write || {}).status !== (f.write || {}).status) changedPrices = true;
    const card = plCards.get(f.id);
    if (card) {
      const fresh = pricelistCard(f);
      card.replaceWith(fresh);
      plCards.set(f.id, fresh);
    }
  }
  if (added && chatBusy) {
    // Идёт ответ чата — ленту не перестраиваем (ответ печатается), новые карточки — в конец.
    for (const f of d.files) {
      if (before.has(f.id)) continue;
      const node = pricelistCard(f);
      plCards.set(f.id, node);
      $("chat-log").append(node);
    }
  } else if (added || d.files.length !== before.size) {
    const log = $("chat-log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    renderChatLog();
    if (atBottom || added) scrollChat();
  }
  if (changedPrices) loadPriceSummary();
  if (d.files.some(PL_BUSY)) plTimer = setTimeout(loadPricelists, 3000);
}

function fmtWhen(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
}

function pricelistCard(f) {
  const t = currentTender();
  const box = el("div", { class: "msg pricelist" });
  const link = t ? el("a", { href: clientPath("/docs/file") + "?" + new URLSearchParams({ folder: t.folder, path: f.file }),
    target: "_blank", rel: "noopener", text: f.name }) : el("b", { text: f.name });
  box.append(el("div", { class: "pl-head" }, el("span", { text: "📎 Документ поставщика: " }), link,
    el("span", { class: "muted", text: " · " + fmtWhen(f.uploaded_at) + (f.by ? " · " + f.by : "") })));
  const status = (cls, text) => el("div", { class: "pl-status " + cls, text });
  if (f.status === "queued") box.append(status("wait", "В очереди на разбор…"));
  if (f.status === "running") box.append(status("wait", "Читаю файл и ищу позиции конкурса… обычно до минуты."));
  if (f.status === "error") box.append(el("div", { class: "notice bad", text: "Не удалось разобрать: " + (f.error || "ошибка") }));
  if (f.status === "done") {
    const ms = f.matches || [];
    const facts = [f.supplier ? "Поставщик: " + f.supplier + (f.supplier_inn ? ` (ИНН ${f.supplier_inn})` : "") : "Поставщик не указан в документе — запишется именем файла",
      f.vat && f.vat !== "не указано" ? "цены " + f.vat : "НДС не указан",
      `найдено позиций: ${ms.length} из ${f.positions_total || (plData && plData.positions) || "?"}`];
    if (f.doc_date) facts.push("от " + f.doc_date);
    if (f.usd !== undefined) facts.push("≈ " + usd(f.usd));
    box.append(el("div", { class: "pl-status ok", text: facts.join(" · ") }));
    for (const n of f.notes || []) box.append(el("div", { class: "muted small", text: n }));
    if (!ms.length) {
      box.append(el("div", { class: "muted", text: "Позиций конкурса в документе не найдено." }));
    } else {
      if (!plSel.has(f.id)) plSel.set(f.id, new Set(ms.filter((m) => m.selected).map((m) => m.pos)));
      const sel = plSel.get(f.id);
      const w = f.write || {};
      const locked = ["requested", "running"].includes(w.status);
      const writeBtn = el("button", { class: "btn small primary need-prices", type: "button" });
      const updBtn = () => {
        writeBtn.textContent = `Записать в спецификацию (${sel.size})`;
        writeBtn.disabled = locked || !sel.size;
      };
      const head = el("tr", {}, ...["", "№", "Позиция конкурса", "Строка документа", "Цена за ед.", "К НМЦ", "Совпадение"].map((h) => el("th", { text: h })));
      const body = el("tbody");
      for (const m of ms) {
        const cb = el("input", { type: "checkbox", class: "need-prices" });
        cb.checked = sel.has(m.pos);
        cb.disabled = locked;
        cb.addEventListener("change", () => { if (cb.checked) sel.add(m.pos); else sel.delete(m.pos); updBtn(); });
        const price = el("td", { class: "num" }, el("b", { text: m.price_rub ? rub(m.price_rub) : "—" }));
        if (m.currency && m.currency !== "RUB" && m.price) price.append(el("div", { class: "muted small", text: `${m.price} ${m.currency}` }));
        if (m.price && !m.price_in_line) price.append(el("div", { class: "muted small", text: "пересчитано", title: "Этой суммы нет в строке документа — модель пересчитала (упаковка, сумма за количество). Проверьте." }));
        const kind = el("td");
        kind.append(m.match === "exact" ? el("span", { class: "badge ok", text: "точно" }) : el("span", { class: "badge warn", text: "🔴 не полностью" }));
        if ((m.problems || []).length) kind.append(" ", el("span", { class: "badge bad", text: "⚠ " + m.problems.join("; ") }));
        if (m.quote === "loose" || m.quote === "none") kind.append(" ", el("span", { class: "badge bad", text: "⚠ фрагмент не найден в документе дословно" }));
        if (m.note) kind.append(el("div", { class: "muted small", text: m.note }));
        body.append(el("tr", {}, el("td", {}, cb), el("td", { class: "num", text: m.pos }),
          el("td", {}, el("div", { text: m.name }), el("div", { class: "muted small", text: [m.qty, m.unit].filter(Boolean).join(" ") + (m.nmc ? " · НМЦ " + rub(m.nmc) : "") })),
          el("td", { class: "pl-line", title: `Строка ${m.line_no}: ${m.line}` }, m.text || m.line),
          price, el("td", { class: "num" }, ratioBadge(m.price_rub, m.nmc)), kind));
      }
      box.append(el("div", { class: "table-wrap pl-wrap" }, el("table", { class: "pl-table" }, el("thead", {}, head), body)));
      updBtn();
      writeBtn.addEventListener("click", async () => {
        writeBtn.disabled = true;
        try {
          await api(clientPath("/pricelists/write"), { folder: currentTender().folder }, { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file: f.id, positions: [...sel] }) });
          toast("Запись в спецификацию NocoDB поставлена в очередь");
        } catch (err) { toast("Не удалось: " + err.message, true); }
        loadPricelists();
      });
      const actions = el("div", { class: "pl-actions" }, writeBtn);
      if (w.status === "requested") actions.append(el("span", { class: "muted", text: "Запись в NocoDB — в очереди…" }));
      if (w.status === "running") actions.append(el("span", { class: "muted", text: "Записываю в спецификацию NocoDB…" }));
      box.append(actions);
      if (w.status === "saved") {
        box.append(el("div", { class: "notice ok", text: `✅ Записано в спецификацию NocoDB (${fmtWhen(w.finished_at)}${w.by ? ", " + w.by : ""}): ` +
          (w.text || "") + ((w.skipped || []).length ? ". Не записано: " + w.skipped.join("; ") : "") +
          ". Неполные совпадения — с 🔴 у имени поставщика." }));
      }
      if (w.status === "error") box.append(el("div", { class: "notice bad", text: "Запись в NocoDB не удалась: " + (w.text || "") }));
    }
  }
  if (!PL_BUSY(f)) {
    const rm = el("button", { class: "btn small ghost need-prices pl-remove", type: "button", text: "Убрать из списка",
      title: "Убрать карточку (сам файл остаётся в папке «Прайсы, счета, КП»)" });
    let armed = false;
    rm.addEventListener("click", async () => {
      if (!armed) { armed = true; rm.textContent = "Точно убрать?"; return; }
      try {
        await api(clientPath("/pricelists/remove"), { folder: currentTender().folder }, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file: f.id }) });
      } catch (err) { toast("Не удалось: " + err.message, true); }
      loadPricelists();
    });
    box.append(rm);
  }
  return box;
}

async function uploadPricelists(fileList) {
  const t = currentTender();
  const files = [...(fileList || [])];
  if (!t || !t.folder || !files.length) return;
  if (document.body.classList.contains("no-prices")) return toast("Загружать прайсы вашей роли недоступно", true);
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  const u = new URL(clientPath("/pricelists/upload"), location.origin);
  u.searchParams.set("folder", t.folder);
  toast(files.length === 1 ? `Загружаю «${files[0].name}»…` : `Загружаю файлов: ${files.length}…`);
  try {
    const r = await fetch(u, { method: "POST", body: form });
    if (r.status === 401) return toLogin();
    let b = {};
    try { b = await r.json(); } catch (_) { /* не JSON */ }
    if (!r.ok) throw new Error(b.detail || `ошибка ${r.status}`);
    toast("Загружено — ищу в документе позиции конкурса и цены");
  } catch (err) {
    toast("Не удалось загрузить: " + err.message, true);
  }
  $("chat-files").value = "";
  await loadPricelists();
  scrollChat();
}

$("chat-files").addEventListener("change", (e) => uploadPricelists(e.target.files));
// Перетащить прайс прямо в чат.
for (const ev of ["dragover", "dragenter"]) {
  $("chat").addEventListener(ev, (e) => {
    if (document.body.classList.contains("no-prices") || !e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    $("chat").classList.add("drag-over");
  });
}
$("chat").addEventListener("dragleave", (e) => { if (!$("chat").contains(e.relatedTarget)) $("chat").classList.remove("drag-over"); });
$("chat").addEventListener("drop", (e) => {
  $("chat").classList.remove("drag-over");
  if (document.body.classList.contains("no-prices") || !e.dataTransfer || !e.dataTransfer.files.length) return;
  e.preventDefault();
  uploadPricelists(e.dataTransfer.files);
});

// ---------- состояние чата: готов ли и сколько стоит следующий вопрос ----------

let chatStatus = null;     // ответ /chat/status; skew — разница часов сайта и браузера

async function loadChatStatus() {
  const t = currentTender();
  if (!t || !t.folder || blockOff("chat")) return;
  const my = ++seq.chatStatus;
  let d = null;
  try {
    d = await api(clientPath("/chat/status"), { folder: t.folder, model: USER_MODE ? "" : $("chat-model").value });
    d.skew = d.now - Date.now() / 1000;
  } catch (_) { /* покажем без состояния */ }
  if (my !== seq.chatStatus) return;
  chatStatus = d;
  renderChatStatus();
}

function renderChatStatus() {
  const box = $("chat-cache");
  const d = chatStatus;
  box.className = "chat-cache";
  if (!d) { box.replaceChildren(); return; }
  const line = (cls, ...parts) => { box.classList.add(cls); box.replaceChildren(el("span", { class: "dot" }), ...parts); };
  const b = (text) => el("b", { text });
  $("chat-send").disabled = chatBusy || !d.ready;
  $("chat-send").title = d.ready ? "" : "Сначала нужны документы с текстом";
  if (!d.ready) {
    if (d.preparing) return line("wait", "Документы готовятся — вопросы можно задавать, как только подготовка закончится (анализ ждать не нужно).");
    return line("cold", "У конкурса пока нет документов с текстом — загрузите их в блоке «Документы».");
  }
  const est = d.tokens_estimated ? "≈ " : "";
  if (d.provider === "deepseek") {
    return line("warm", "Чат готов. DeepSeek кэширует документы сам: первый вопрос ", b(`≈ ${usd(d.cost_cold)}`),
      ", повторные обычно ", b(`≈ ${usd(d.cost_warm)}`), ".");
  }
  if (d.mode === "fragments") {
    return line("warm", "Чат готов. Документы большие — модель получает найденные по вопросу фрагменты (дешевле, но что-то можно упустить).");
  }
  const ttlText = d.ttl === 300 ? "5 минут" : "час";
  const left = d.warm && d.expires_at ? d.expires_at - (Date.now() / 1000 + d.skew) : 0;
  if (left > 0) {
    const mins = Math.ceil(left / 60);
    return line("warm", "Чат готов, документы в кэше ещё ", b(mins <= 1 ? "минуту" : `${mins} мин`), " — вопрос ",
      b(`≈ ${usd(d.cost_warm)}`), `. Каждый вопрос продлевает кэш на ${ttlText}.`);
  }
  line("cold", "Чат готов. Документы сейчас не в кэше — первый вопрос ", b(`${est}${usd(d.cost_cold)}`),
    ` и на несколько секунд дольше, следующие ${ttlText === "час" ? "в течение часа" : "5 минут"} — `, b(`≈ ${usd(d.cost_warm)}`), ".");
}

// Обратный отсчёт и смена «готовятся → готов»: раз в 30 с, пока блок чата виден.
setInterval(() => {
  if (!$("chat").hidden && !document.hidden) loadChatStatus();
}, 30000);

function scrollChat() {
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
}

async function askChat(question) {
  const t = currentTender();
  if (!t || !t.folder || chatBusy || !question.trim()) return;
  chatBusy = true;
  $("chat-send").disabled = true;
  const log = $("chat-log");
  log.append(el("div", { class: "msg user", text: question }));
  chatMsgs.push({ role: "user", content: question, created_at: Date.now() / 1000 });
  const answer = el("div", { class: "msg assistant" }, el("span", { class: "typing", text: "Думаю…" }));
  log.append(answer);
  scrollChat();
  let text = "";
  let finished = false;
  try {
    const u = new URL(clientPath("/chat"), location.origin);
    u.searchParams.set("folder", t.folder);
    const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, model: USER_MODE ? null : $("chat-model").value }) });
    if (r.status === 401) toLogin();
    if (!r.ok || !r.body) {
      let detail = "";
      try { detail = (await r.json()).detail; } catch (_) { /* не JSON */ }
      throw new Error(detail || `ошибка ${r.status}`);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.t === "text") {
          text += ev.text;
          answer.replaceChildren(renderMd(splitSources(text)[0]));
          scrollChat();
        } else if (ev.t === "notice") {
          answer.replaceChildren(el("span", { class: "typing", text: ev.text }));
        } else if (ev.t === "done") {
          const m = { role: "assistant", content: text, meta: ev.meta, created_at: Date.now() / 1000 };
          answer.replaceWith(renderChatMessage(m));
          chatMsgs.push(m);
          finished = true;
        } else if (ev.t === "error") {
          const m = { role: "error", content: ev.text, meta: { error: ev.text }, created_at: Date.now() / 1000 };
          answer.replaceWith(renderChatMessage(m));
          chatMsgs.push(m);
          finished = true;
        }
      }
    }
    if (!finished) throw new Error("ответ оборвался — проверьте, что сайт запущен");
  } catch (err) {
    answer.replaceWith(renderChatMessage({ role: "error", content: err.message, meta: { error: err.message } }));
  } finally {
    chatBusy = false;
    $("chat-send").disabled = false;
    scrollChat();
    loadChatStatus();     // после ответа документы в кэше — показать «ещё час»
  }
}

// ---------- спецификация ----------

async function loadSpec() {
  const my = ++seq.spec;
  if (blockOff("spec")) return;
  setStatus("spec-status", "Загрузка спецификации…");
  $("spec-table").replaceChildren();
  $("spec-count").textContent = "";
  $("spec-link").hidden = true;
  let d;
  try {
    d = await api(clientPath(`/tenders/${state.tender}/spec`), { view: state.specView });
  } catch (err) {
    if (my === seq.spec) setStatus("spec-status", "Не удалось загрузить спецификацию: " + err.message, true);
    return;
  }
  if (my !== seq.spec) return;
  if (!d.found) {
    setStatus("spec-status", "У этого конкурса нет таблицы спецификации в NocoDB.");
    fillSelect($("spec-view"), []);
    return;
  }
  setStatus("spec-status", d.rows.length ? "" : "В спецификации нет строк.");
  $("spec-count").textContent = `· ${d.title} · ${d.rows.length} поз.`;
  $("spec-link").href = d.link;
  $("spec-link").hidden = false;
  fillSelect($("spec-view"), d.views.map((v) => ({ value: v.id, label: v.title })), d.view ? d.view.id : "");
  state.specView = d.view ? d.view.id : "";
  renderSpec(d.columns, d.rows);
  saveUrl();
}

function renderSpec(columns, rows) {
  const table = $("spec-table");
  const sticky = (i) => (i === 0 && columns[0] && columns[0].title === "№") ? "stick0"
    : (i === 1 && columns[0] && columns[0].title === "№" && columns[1].title === "Наименование позиции") ? "stick1" : "";
  const head = el("tr", {}, ...columns.map((c, i) => el("th", { class: sticky(i) || null, text: c.title })));
  const body = el("tbody");
  for (const r of rows) {
    const tr = el("tr");
    columns.forEach((c, i) => {
      const v = r[c.title];
      const numeric = NUMERIC.has(c.uidt) || typeof v === "number";
      const cls = [sticky(i), numeric ? "num" : ""].filter(Boolean).join(" ") || null;
      tr.append(el("td", { class: cls }, renderValue(v, c.uidt)));
    });
    body.append(tr);
  }
  table.replaceChildren(el("thead", {}, head), body);
  // Вторая закреплённая колонка начинается там, где кончается первая.
  const first = table.querySelector("th.stick0");
  if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
}

// ---------- обработчик: что выполняется сейчас (шапка) ----------

let actTimer = null;
let actSignature = null;   // задачи текущего конкурса при прошлой проверке — заметить запуск из другой вкладки
let actLast = null;        // последний ответ /jobs — перерисовать меню без запроса (кнопки отмены)

function jobLine(j, withFolder = true) {
  const head = (withFolder ? j.folder + " — " : "") + j.kind_title;
  if (j.status === "running") return `${head}: ${j.step || "…"}` + stoppingText(j);
  if (j.status === "queued") return `${head}: ` + queuedText(j);
  if (j.status === "paused") return `${head}: ${lc1(j.message || "Приостановлена")}`;
  const ago = j.finished_at ? fmtDur(Date.now() / 1000 - j.finished_at) + " назад" : "";
  if (j.status === "canceled") return `${head}: ${lc1(j.message || "Отменена")} ${ago}`;
  return `${head}: ${j.status === "error" ? "ошибка" : "готово"} ${ago}` + (j.status === "error" ? ` — ${j.message}` : "");
}

async function pollActivity() {
  clearTimeout(actTimer);
  actTimer = setTimeout(pollActivity, 5000);
  if (!state.client) return;
  let d;
  try {
    d = await api(clientPath("/jobs"));
  } catch (_) {
    renderActivity(null);
    return;
  }
  renderActivity(d);
  // Задачу текущего конкурса запустили / закончили в другой вкладке (или сама поставилась) — обновить блоки.
  const t = currentTender();
  if (!t || !t.folder) return;
  const sig = [...d.active, ...d.recent].filter((j) => j.folder === t.folder).map((j) => `${j.id}:${j.status}`).sort().join(",");
  if (actSignature !== null && sig !== actSignature) {
    if (!docsTimer) loadDocs();
    if (!anTimer) loadAnalysis();
    if (!prTimer) loadPrices();
    loadChatStatus();     // документы подготовились — чат готов
  }
  actSignature = sig;
}

function renderActivity(d) {
  const btn = $("activity");
  const panel = $("activity-panel");
  actLast = d;
  btn.hidden = false;
  btn.className = "activity";
  if (!d) {
    btn.textContent = "Сайт не отвечает";
    btn.classList.add("bad");
    panel.replaceChildren(el("div", { text: "Не удалось узнать состояние обработчика — сайт не отвечает. Запустите «Запустить SupplyBro.bat»." }));
    return;
  }
  const known = d.lanes.filter((l) => l.seen !== null);
  const dead = known.length ? d.lanes.filter((l) => !l.alive) : [];
  const running = d.lanes.filter((l) => l.job);
  const queued = d.active.filter((j) => j.status === "queued");
  const paused = d.active.filter((j) => j.status === "paused");
  const pausedTail = paused.length ? ` · на паузе: ${paused.length}` : "";
  if (dead.length) {
    btn.textContent = "⚠ Обработчик не отвечает";
    btn.classList.add("bad");
  } else if (running.length || queued.length) {
    btn.textContent = `● В работе: ${running.length}` + (queued.length ? ` · в очереди: ${queued.length}` : "") + pausedTail;
    btn.classList.add("busy");
  } else if (paused.length) {
    btn.textContent = `● На паузе: ${paused.length}`;
    btn.classList.add("idle");
  } else {
    btn.textContent = "● Обработчик свободен";
    btn.classList.add("idle");
  }
  const rows = [];
  for (const l of d.lanes) {
    const box = el("div", { class: "act-lane" });
    const laneState = l.seen === null ? "ещё не запускалась" : l.alive ? `работает (отклик ${fmtDur(l.seen)} назад)` : `не отвечает ${fmtDur(l.seen)}`;
    box.append(el("div", { class: "act-title", text: `Очередь «${l.title}» — ${laneState}` }));
    // Выполняемая задача другого клиента видна (очередь общая), но кнопок у неё нет.
    if (l.job) box.append(jobItem(l.job, l.job.client_id === state.client));
    for (const j of queued.filter((q) => q.lane === l.lane)) box.append(jobItem(j));
    for (const j of paused.filter((q) => q.lane === l.lane)) box.append(jobItem(j));
    if (!l.job && ![...queued, ...paused].some((q) => q.lane === l.lane)) box.append(el("div", { class: "muted", text: "задач нет" }));
    rows.push(box);
  }
  if (d.recent.length) {
    const box = el("div", { class: "act-lane" }, el("div", { class: "act-title", text: "Закончились за последние 10 минут" }));
    for (const j of d.recent) box.append(jobItem(j));
    rows.push(box);
  }
  if (dead.length) {
    rows.unshift(el("div", { class: "notice bad", text: "Обработчик не отвечает больше минуты. Запустите «Запустить SupplyBro.bat» — " +
      "прерванные задачи продолжатся сами (анализ, который уже был готов, повторно не оплачивается)." }));
  }
  replaceSafe(panel, ...rows);
}

function jobItem(j, controls = true) {
  const item = el("div", { class: "act-job " + j.status, title: "Открыть этот конкурс" },
    el("span", { class: "act-text", text: jobLine(j) }));
  const ctl = controls ? jobControls(j) : [];
  if (ctl.length) {
    const box = el("span", { class: "job-ctl" }, ...ctl);
    box.addEventListener("click", (e) => e.stopPropagation());   // нажатие рядом с кнопкой — не переход к конкурсу
    item.append(box);
  }
  item.addEventListener("click", () => {
    const t = [...state.tenders.values()].find((x) => x.folder === j.folder);
    if (!t) return;
    state.tender = t.id;
    $("tender").value = t.id;
    $("activity-panel").hidden = true;
    loadTender();
  });
  return item;
}

// ---------- события ----------

$("tl-search").addEventListener("input", renderTenderList);
$("tl-filter").addEventListener("change", (e) => { storageSet(TL_FILTER_KEY, e.target.value); renderTenderList(); });
{
  const f = storageGet(TL_FILTER_KEY);
  if (f && [...$("tl-filter").options].some((o) => o.value === f)) $("tl-filter").value = f;
}
$("client").addEventListener("change", (e) => {
  state.client = e.target.value;
  state.entity = ""; state.tender = ""; state.regView = ""; state.specView = "";
  fillEntities();
  loadTenders();
});
$("entity").addEventListener("change", (e) => {
  state.entity = e.target.value;
  loadTenders();
});
$("tender").addEventListener("change", (e) => {
  showNewTenderForm(false);
  state.tender = e.target.value;
  state.specView = ""; // у каждой спецификации свои представления
  loadTender();
});
$("new-tender").addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = newTenderFiles;
  // Документы не выбраны — сначала спросить (28.10.2026 так по ошибке создали конкурс без документации).
  if (!files.length && !ntEmptyOk) {
    $("nt-nofiles").hidden = false;
    return;
  }
  $("nt-nofiles").hidden = true;
  setStatus("nt-status", "Создаю папку…");
  try {
    const folder = await createFolder($("nt-number").value.trim(), $("nt-title").value.trim());
    showNewTenderForm(false);
    state.tender = "f:" + folder;
    await loadTenders();
    if (files.length) uploadFiles(files, DOCS_TARGET);
  } catch (err) {
    setStatus("nt-status", "Не удалось создать папку: " + err.message, true);
  }
});
$("new-tender-btn").addEventListener("click", () => showNewTenderForm(true));
$("nt-files").addEventListener("change", (e) => { setNewTenderFiles(e.target.files); e.target.value = ""; });
$("nt-files2").addEventListener("change", (e) => { setNewTenderFiles(e.target.files); e.target.value = ""; });
$("nt-empty-ok").addEventListener("click", () => { ntEmptyOk = true; $("new-tender").requestSubmit(); });
const ntDrop = $("nt-drop");
for (const ev of ["dragenter", "dragover"]) {
  ntDrop.addEventListener(ev, (e) => { e.preventDefault(); ntDrop.classList.add("over"); });
}
for (const ev of ["dragleave", "drop"]) {
  ntDrop.addEventListener(ev, (e) => { e.preventDefault(); ntDrop.classList.remove("over"); });
}
ntDrop.addEventListener("drop", (e) => setNewTenderFiles([...newTenderFiles, ...e.dataTransfer.files]));
$("nt-cancel").addEventListener("click", () => showNewTenderForm(false));
$("docs-create").addEventListener("click", async () => {
  const t = currentTender();
  if (!t) return;
  setStatus("docs-status", "Создаю папку…");
  try {
    await createFolder(t.internal || state.nextNumber, (t.title || "").slice(0, 60));
    await loadTenders();
  } catch (err) {
    setStatus("docs-status", "Не удалось создать папку: " + err.message, true);
  }
});
$("docs-process").addEventListener("click", async () => {
  const t = currentTender();
  if (!t || !t.folder) return;
  try {
    await api(clientPath("/docs/process"), { folder: t.folder }, { method: "POST" });
  } catch (err) {
    setStatus("docs-status", "Не удалось запустить обработку: " + err.message, true);
  }
  loadDocs();
});
$("file-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
const drop = $("drop");
for (const ev of ["dragenter", "dragover"]) {
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
}
for (const ev of ["dragleave", "drop"]) {
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
}
drop.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));
$("an-changes-files").addEventListener("change", (e) => {
  uploadFiles(e.target.files, "@changes", "an-status");
  e.target.value = "";
});
$("an-run").addEventListener("click", async () => {
  const t = currentTender();
  if (!t || !t.folder) return;
  $("an-run").disabled = true;
  try {
    const r = await api(clientPath("/analysis/run"), { folder: t.folder, model: USER_MODE ? "" : $("an-model").value }, { method: "POST" });
    if (r.already) setStatus("an-status", "Анализ уже идёт (запущен раньше или в другой вкладке) — показываю его ход.");
  } catch (err) {
    setStatus("an-status", "Не удалось запустить анализ: " + err.message, true);
  }
  loadAnalysis();
});
$("pr-run").addEventListener("click", async () => {
  const t = currentTender();
  if (!t || !t.folder) return;
  $("pr-run").disabled = true;
  try {
    const engine = $("pr-engine").value || "deepseek";
    const r = await api(clientPath("/prices/run"), { folder: t.folder, engine: USER_MODE ? "" : engine,
      urgent: !USER_MODE && engine !== "deepseek" && $("pr-urgent").checked ? "1" : "",
      abroad: $("pr-abroad").checked ? "1" : "0" }, { method: "POST" });
    if (r.already) setStatus("pr-status", "Поиск цен уже идёт (запущен сам после анализа или в другой вкладке) — показываю его ход.");
    $("pr-urgent").checked = false;
  } catch (err) {
    setStatus("pr-status", "Не удалось запустить поиск цен: " + err.message, true);
  }
  loadPrices();
});
$("pr-abroad").addEventListener("change", (e) => { storageSet(PR_ABROAD_KEY, e.target.checked ? "1" : "0"); if (prLast) renderPrices(prLast, prBusy); });
$("pr-engine").addEventListener("change", (e) => { storageSet(PR_ENGINE_KEY, e.target.value); if (prLast) renderPrices(prLast, prBusy); });
$("pr-urgent").addEventListener("change", () => { if (prLast) renderPrices(prLast, prBusy); });
$("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("chat-input").value;
  if (!q.trim() || chatBusy) return;
  $("chat-input").value = "";
  askChat(q.trim());
});
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); }
});
$("chat-model").addEventListener("change", (e) => { storageSet(CHAT_MODEL_KEY, e.target.value); loadChatStatus(); });
$("chat-clear").addEventListener("click", async () => {
  const t = currentTender();
  if (!t || !t.folder || chatBusy) return;
  try { await api(clientPath("/chat/clear"), { folder: t.folder }, { method: "POST" }); } catch (_) { /* покажем как есть */ }
  loadChat();
});
$("an-model").addEventListener("change", (e) => storageSet(AN_MODEL_KEY, e.target.value));
for (const b of document.querySelectorAll("#analysis .tab")) b.addEventListener("click", () => showAnalysisTab(b.dataset.tab));
$("reg-view").addEventListener("change", (e) => { state.regView = e.target.value; loadCard(); });
$("spec-view").addEventListener("change", (e) => { state.specView = e.target.value; loadSpec(); });
$("hide-empty").addEventListener("change", () => { const f = $("fields")._fields; if (f) renderFields(f); });
$("refresh").addEventListener("click", () => { state.fresh = true; loadTenders(); });

$("activity").addEventListener("click", () => { $("activity-panel").hidden = !$("activity-panel").hidden; });
document.addEventListener("click", (e) => {
  if (!e.target.closest("#activity, #activity-panel")) $("activity-panel").hidden = true;
});

// ---------- вкладки-переходы (0.6.5; панель — 0.6.6) ----------
// Панель вкладок под выбором конкурса (при прокрутке закреплена под шапкой): нажатие прокручивает к разделу этой же
// страницы, подсвечена вкладка раздела, который сейчас перед глазами. Вкладка раздела, скрытого для этого конкурса
// (например, нет записи в NocoDB), не видна.
(function jumpNav() {
  const wrap = $("jump-wrap");
  const nav = $("jump-nav");
  const header = document.querySelector(".topbar");
  const links = [...nav.querySelectorAll(".jump")];
  const section = (a) => (a.dataset.target === "top" ? null : $(a.dataset.target));
  let pinned = null;          // вкладка, нажатая последней, — пока пользователь сам не прокрутит страницу
  let autoScroll = false, autoTimer = 0, frame = 0;

  const headerHeight = () => header.getBoundingClientRect().height;
  // Сколько сверху занято закреплёнными шапкой и панелью вкладок.
  const stickyHeight = () => headerHeight() + (wrap.hidden ? 0 : wrap.getBoundingClientRect().height);

  function syncVisibility() {
    wrap.hidden = $("head").hidden;         // конкурс не выбран — вкладок нет
    for (const a of links) {
      const s = section(a);
      a.hidden = Boolean(s && s.hidden);
    }
    const root = document.documentElement.style;
    root.setProperty("--topbar-h", headerHeight() + "px");
    root.setProperty("--sticky-h", stickyHeight() + "px");
    spy();
  }

  function setActive(a) {
    for (const l of links) {
      const on = l === a;
      l.classList.toggle("active", on);
      if (on) l.setAttribute("aria-current", "true"); else l.removeAttribute("aria-current");
    }
    if (a && nav.scrollWidth > nav.clientWidth) {           // узкий экран: активная вкладка — в поле зрения
      const r = a.getBoundingClientRect(), n = nav.getBoundingClientRect();
      if (r.left < n.left || r.right > n.right) nav.scrollLeft += r.left - n.left - 24;
    }
  }

  function spy() {
    if (wrap.hidden) return;
    // Панель прилипла к шапке — лёгкая тень, чтобы было видно, что страница уходит под неё.
    wrap.classList.toggle("stuck", window.scrollY > 0 && wrap.getBoundingClientRect().top <= headerHeight() + 0.5);
    if (pinned && !pinned.hidden) return setActive(pinned);
    // Раздел «текущий», когда его верх выше линии на трети видимой под шапкой высоты (что занимает экран).
    const sticky = stickyHeight();
    const line = sticky + Math.max(40, (window.innerHeight - sticky) * 0.3);
    const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
    let current = links[0];
    for (const a of window.scrollY > 0 ? links : []) {      // в самом верху страницы — «Конкурс»
      const s = section(a);
      if (!s || a.hidden) continue;
      if (s.getBoundingClientRect().top <= line) current = a;
      if (atBottom) current = a;                            // долистали до конца — последний раздел
    }
    setActive(current);
  }

  nav.addEventListener("click", (e) => {
    const a = e.target.closest(".jump");
    if (!a) return;
    e.preventDefault();                                     // адрес страницы не меняем (там выбор конкурса)
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const s = section(a);
    if (s) s.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
    else window.scrollTo({ top: 0, behavior: smooth ? "smooth" : "auto" });
    pinned = a;
    autoScroll = true;                                      // прокрутка от нажатия — подсветку не сбивает
    clearTimeout(autoTimer);
    autoTimer = setTimeout(() => { autoScroll = false; }, 1200);
    setActive(a);
  });
  // Пользователь сам крутит страницу (колесо, палец, клавиши, полоса прокрутки) — снова подсвечиваем по положению.
  const unpin = () => { pinned = null; };
  for (const ev of ["wheel", "touchstart", "keydown", "mousedown"]) {
    window.addEventListener(ev, (e) => { if (!nav.contains(e.target)) { autoScroll = false; unpin(); } }, { passive: true });
  }
  window.addEventListener("scrollend", () => { autoScroll = false; });
  window.addEventListener("scroll", () => {
    if (pinned && !autoScroll) unpin();
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; spy(); });
  }, { passive: true });
  window.addEventListener("resize", syncVisibility);
  const ro = new ResizeObserver(syncVisibility);
  ro.observe(header);
  ro.observe(wrap);
  const watched = ["head", ...links.map((a) => a.dataset.target).filter((t) => t !== "top")];
  const mo = new MutationObserver(syncVisibility);
  for (const id of watched) mo.observe($(id), { attributes: true, attributeFilter: ["hidden"] });
  syncVisibility();
})();

// ---------- кто вошёл (0.7.0) ----------
// Если вход на сайт включён: имя и роль в шапке, «Выйти», «Админ-панель» для администратора; кнопки, которых нет
// в правах роли, скрыты (классы no-* на <body>). Без входа (LOGIN_REQUIRED=0) ничего не меняется.
async function loadMe() {
  let d;
  try {
    const r = await fetch("/api/auth/state");
    if (!r.ok) return;                      // страница входа выключена — вход не требуется
    d = await r.json();
  } catch (_) { return; }
  if (!d.required) return;
  if (!d.me || d.me.must_change) return toLogin();
  const allowed = new Set(d.access.permissions.filter((p) => p.allowed).map((p) => p.key));
  // Выключенные разделы (админ-панель, 0.11.0): «Документы» — и без загрузки (новый конкурс, дозагрузка изменений),
  // «Цены» — и без прайсов в чате, «Анализ» — без запуска анализа.
  const off = new Set((d.access.blocks || []).filter((b) => !b.on).map((b) => b.key));
  for (const b of d.access.blocks || []) document.body.classList.toggle("off-" + b.key, off.has(b.key));
  if (off.has("docs")) allowed.delete("upload");
  if (off.has("prices")) allowed.delete("prices");
  if (off.has("analysis")) allowed.delete("analyze");
  if (off.has("chat")) allowed.delete("chat");
  for (const k of ["upload", "analyze", "prices", "jobs", "chat"]) document.body.classList.toggle("no-" + k, !allowed.has(k));
  $("me-name").textContent = d.me.name === d.access.role_title ? d.me.name : `${d.me.name} · ${d.access.role_title}`;
  $("me-name").title = `Логин: ${d.me.login}`;
  $("me-admin").hidden = !d.access.admin_panel;
  $("me-box").hidden = false;
}
$("me-logout").addEventListener("click", async () => {
  try { await fetch("/api/auth/logout", { method: "POST", headers: { "X-SupplyBro": "1" } }); } catch (_) { /* всё равно на вход */ }
  location.href = "/login";
});

loadMe().finally(() => loadClients().then(pollActivity));
