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
const seq = { tenders: 0, card: 0, spec: 0, docs: 0, an: 0, chat: 0, chatStatus: 0, pr: 0, ps: 0, pl: 0, bid: 0, rq: 0, of: 0 };
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
  // Запросы по конкурсу — с юр. лицом (0.19.0): задачи и чат пойдут с промтами этого юр. лица (админ-панель).
  if (query.folder && !("entity" in query) && promptEntity()) u.searchParams.set("entity", promptEntity());
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

// Юр. лицо для промтов (0.19.0): выбранное на странице, а если выбраны все — участник конкурса из NocoDB, когда он
// один. Пусто — промты клиента. Сервер передаёт его задаче и всей её цепочке (анализ → запись → поиск цен).
function promptEntity() {
  if (state.entity) return state.entity;
  const p = (currentTender() || {}).participants || [];
  return p.length === 1 ? p[0] : "";
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
  $("head-rename").hidden = !(t && t.internal);
  $("head-delete").hidden = !t || USER_MODE;
  if (t) $("head-delete").textContent = t.source === "nocodb" ? "🗑 Удалить из NocoDB" : "🗑 Удалить конкурс";
  $("docs").hidden = !t || blockOff("docs");
  $("analysis").hidden = !(t && t.folder) || blockOff("analysis");
  $("prices").hidden = !(t && t.folder) || blockOff("prices");
  $("offers").hidden = !(t && t.folder) || blockOff("prices");
  $("bid").hidden = !(t && t.folder) || blockOff("bid");
  $("chat").hidden = !(t && t.folder) || blockOff("chat");
  $("card").hidden = !(t && t.source === "nocodb") || blockOff("card");
  // «Спецификация» (0.40.0): «Позиции» — таблица NocoDB, «Цены» — все цены по позициям (нужна папка конкурса)
  $("spec").hidden = !(t && ((t.source === "nocodb" && !blockOff("spec")) || (t.folder && !blockOff("prices"))));
  specModeInit(t);
  $("requests").hidden = !(t && t.source === "nocodb") || blockOff("prices");
}

async function loadTender() {
  const t = currentTender();
  if (state.opened !== state.tender) { cfReset(); specData = null; psLast = null; }   // фильтры — свои у конкурса (0.40.0)
  state.opened = t ? state.tender : "";
  showTender(t);
  saveUrl();
  stopDocsPolling();
  stopAnalysisPolling();
  stopPricesPolling();
  stopBidPolling();
  stopOffersPolling();
  resetStages();
  if (!t) return;
  anLastJob = null;
  prLastJob = null;
  rq.d = null;                            // письма другого конкурса — не показывать, загрузить заново при открытии
  rq.for = "";
  rqPos.sel.clear();
  setMailBadge(0);
  bidLastJob = null;
  bidShowAll = false;
  delete $("bid-price").dataset.touched;
  delete $("bid-entity").dataset.touched;
  if (t.source === "nocodb") {
    await Promise.all([loadCard(), loadSpec(), loadDocs(), loadAnalysis(), loadPrices(), loadBid(), loadChat(), loadMailSummary(), loadOffers()]);
    ensureRequests();                     // вкладка «Письма» (или «одной страницей» / «по позициям») уже открыта
  } else {
    renderFolderHead(t);
    await Promise.all([loadDocs(), loadAnalysis(), loadPrices(), loadBid(), loadChat(), loadOffers()]);
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
  stg.docs = { n: docs.length, pending: files.filter((f) => f.status === "pending").length };
  $("docs").classList.toggle("has-docs", docs.length > 0);   // загружены — раздел внизу вкладки «Конкурс» (0.39.0)
  renderStages();

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

// Кто запустил задачу (0.16.0, логин; у задач до 0.16.0 и без входа — пусто).
function byText(job) {
  return job && job.started_by ? ` · запустил ${job.started_by}` : "";
}

// «В очереди» — за кем стоит задача или что обработчик не запущен.
function queuedText(job) {
  if (job.worker_alive === false && job.age > 20) {
    return "в очереди" + byText(job) + " — обработчик не отвечает" +
      (job.worker_seen ? ` (последний отклик ${fmtDur(job.worker_seen)} назад)` : "") +
      ". Запустите сайт заново: «Запустить SupplyBro.bat».";
  }
  if (job.waiting_for) {
    const w = job.waiting_for;
    return `в очереди «${job.lane_title}»${byText(job)} — ждёт, пока закончится: ${w.folder} — ${w.kind_title} (идёт ${fmtDur(w.elapsed)})` +
      (job.ahead ? `; перед ней ещё ${job.ahead}` : "");
  }
  return "в очереди…" + (job.ahead ? ` (перед ней ${job.ahead})` : "") + byText(job);
}

// «Идёт» — кто запустил, сколько идёт, когда было последнее действие; предупреждение, если давно нет движения.
function runningTail(job) {
  if (job.elapsed === undefined) return byText(job);
  let t = byText(job) + ` · идёт ${fmtDur(job.elapsed)} · последнее действие ${fmtDur(job.quiet)} назад`;
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
    clearTimeout(bidTimer); bidTimer = null; loadBid();
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
  if (promptEntity()) q.set("entity", promptEntity());
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
  stg.analysis = r ? "done" : busy ? "busy" : "none";
  renderStages();
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
  // 0.14.0 (пользователь 04.11.2026: «искать цены всегда, даже если они были извлечены ранее»): кнопка доступна
  // всегда, кроме: поиск уже идёт; позиций нет совсем (ни анализа на сайте, ни строк в спецификации NocoDB); нет ключа.
  // Без анализа на сайте — позиции из спецификации NocoDB; идёт анализ — поиск запустится сам после него.
  const t = currentTender();
  const summaryHere = psLast && t && psLast.folder === t.folder;     // «Все цены» этого конкурса уже прочитаны
  const noPositions = !d.has_analysis && !d.analysis_pending &&
    (d.noco_positions === 0 || (summaryHere && !psLast.has_positions));
  const blocker = noPositions ? "Позиций нет: анализа документов на сайте нет, а в спецификации NocoDB нет строк — " +
      "запустите анализ документов"
    : d.analysis_pending && !d.auto_prices ? "Идёт анализ документов — позиции ещё меняются; поиск цен — после него"
    : needYandex && !s.yandex_key ? "Нет ключа Яндекса (secrets/API_key_Yandex.txt) — выберите поиск DeepSeek"
    : needDeepseek && !s.deepseek_key ? "Нет ключа DeepSeek (secrets/API_key_DeepSeek.txt)" : "";
  run.disabled = busy || Boolean(blocker);
  const est = priceEstimate(d);
  run.title = blocker || (d.analysis_pending ? "Идёт анализ — поиск цен запустится сам сразу после него"
    : !d.has_analysis ? "Позиции — из спецификации NocoDB (анализа документов на сайте нет)" + (est ? ". Оценка: " + est : "")
    : est ? "Оценка: " + est : "");
  $("pr-stale").hidden = !(d.stale && !busy);
  if (r) {
    const c = r.cost || {};
    const total = c.usd !== undefined ? c.usd : (c.claude_usd || 0);
    $("pr-info").textContent = `· ${r.at_text} · ${Math.max(1, Math.round((r.seconds || 0) / 60))} мин · ≈ ${usd(total)}` +
      (c.yandex_rub ? ` + ${fmtRub.format(c.yandex_rub)} ₽` : "") + (r.source === "nocodb" ? " · позиции из NocoDB" : "");
  } else {
    $("pr-info").textContent = "";
  }
  if (blocker) {
    setStatus("pr-status", blocker + ".", true);
  } else if (!busy && d.analysis_pending) {
    setStatus("pr-status", "Идёт анализ документов." +
      (d.auto_prices ? " Поиск цен запустится сам сразу после него; найденные цены попадут в спецификацию NocoDB."
        : " После него можно будет искать цены."));
  } else if (!r && !busy && !d.has_analysis) {
    setStatus("pr-status", "Анализа документов на сайте нет — «Искать цены» найдёт цены по позициям из спецификации " +
      "NocoDB" + (d.noco_positions ? ` (${d.noco_positions} поз.)` : "") + ". Найденное — здесь и во «Всех ценах»; " +
      "в спецификацию, которую заполнял агент, сайт цены не пишет." + (est ? " Оценка: " + est + "." : ""));
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

function applyPriceTab() {
  // 0.40.0: «Все цены» — во вкладке «Спецификация» → «Цены»; здесь — только итоги поиска в интернете.
  $("pr-wrap").hidden = !(prLast && prLast.report);
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
  psLast = { ...d, folder: t.folder };
  renderPriceSummary(d);
  if (of.d && (of.d.suppliers || []).length) renderOffers();   // отборы «Закупки» берут интернет из «Цен» (0.41.1)
  applyPriceTab();
  // Конкурс без анализа на сайте: теперь известно, есть ли позиции в спецификации NocoDB, — обновить кнопку поиска
  // (а /prices — сколько их, для подсказки и оценки стоимости).
  if (prLast && !prLast.has_analysis) {
    if (prLast.noco_positions == null && d.positions_source === "nocodb") loadPrices();
    else renderPrices(prLast, prBusy);
  }
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
  const rows = (d.rows || []).map(psDb);
  const withPrice = rows.filter((r) => r.best).length;
  const withSup = rows.filter((r) => (r.suppliers || []).length).length;
  const showAgent = rows.some((r) => (r.agent || []).length || r.market);
  const showHist = rows.some((r) => (r.history || []).length);
  const parts = [`Позиций: ${rows.length}; с ценой — ${withPrice}; с ценами поставщиков — ${withSup}.`];
  if (d.positions_source === "nocodb") parts.push("Позиции — из спецификации NocoDB (анализа документов на сайте у этого конкурса нет).");
  parts.push("Показано только записанное в NocoDB; найденное сайтом и цены поставщиков до записи — во вкладке «Закупка».");
  if (d.noco_read) parts.push("Цены из спецификации NocoDB учтены (агент, рынки РФ / зарубеж, отдел закупок, доставка).");
  if (d.noco_error) parts.push("⚠ Спецификация NocoDB не прочитана: " + d.noco_error);
  const h = d.history || {};
  if (h.at) parts.push(`«Прошлые конкурсы» — тот же артикул в других спецификациях NocoDB (своя база цен от ${h.at}` + (h.building ? ", обновляется" : "") + ").");
  else if (h.building) parts.push("Своя база цен (прошлые конкурсы) собирается — обновите страницу через минуту.");
  parts.push("Лучшая — самая низкая из проверенных (интернет, агент, поставщики; 🔴 — с пометкой; «не проверено» и прошлые конкурсы — не учитываются).");
  setStatus("ps-status", parts.join(" "), Boolean(d.noco_error));
  renderPsTable();
}

// Таблица «Цены» (бывшие «Все цены») с фильтрами колонок и быстрыми отборами (0.40.0).
// «Спецификация» — только данные из базы (0.42.0): найденное сайтом и цены из прайсов до записи — во вкладке «Закупка».
function psDb(r) {
  const rf = (r.rf || []).filter((a) => a.from_noco), abroad = (r.abroad || []).filter((a) => a.from_noco);
  const suppliers = (r.suppliers || []).filter((x) => x.source === "NocoDB" || x.in_noco);
  const cands = [...rf.slice(0, 1).map((a) => [a.price_rub, `РФ: ${a.domain || "NocoDB"}`, a.url]),
    ...abroad.slice(0, 1).map((a) => [a.price_rub, `зарубеж: ${a.domain || "NocoDB"}`, a.url]),
    ...(r.agent || []).map((a) => [a.price_rub, `агент (цена ${a.i})`, a.url]),
    ...(r.market ? [[r.market.price_rub, "рыночная цена (NocoDB)", r.market.url]] : []),
    ...suppliers.filter((x) => x.price_rub && !(x.problems || []).length).map((x) => [x.price_rub, `поставщик: ${x.supplier}` + (x.match && x.match !== "exact" ? " 🔴" : ""), ""])]
    .filter((c) => c[0]);
  const b = cands.length ? cands.reduce((m, c) => (c[0] < m[0] ? c : m)) : null;
  return { ...r, rf, abroad, suppliers, unverified: [], best: b ? { price_rub: b[0], source: b[1], url: b[2] } : null,
    ratio: b && r.nmc ? b[0] / r.nmc : null };
}
function psDbRows() { return ((psLast && psLast.rows) || []).map(psDb); }

function renderPsTable() {
  const d = psLast;
  if (!d) return;
  const all = psDbRows();
  const showAgent = all.some((r) => (r.agent || []).length || r.market);
  const showHist = all.some((r) => (r.history || []).length);
  const defs = psDefs(showAgent, showHist);
  const rows = all.filter((r) => cfPass("ps", defs, r));
  cfShowInfo("ps", rows.length, all.length);
  renderCfChips();
  const cols = [["№"], ["Позиция"], ["Бренд", "brand"], ["Страна", "country"], ["Кол-во", "unit"], ["НМЦ за ед.", "nmc"],
    ["Интернет: Россия", "rf"], ["Интернет: зарубеж", "abroad"]];
  if (showAgent) cols.push(["Агент (NocoDB)", "agent"]);
  cols.push(["Поставщики (прайсы, КП, счета, NocoDB)", "sup"]);
  if (showHist) cols.push(["Прошлые конкурсы", "hist"]);
  cols.push(["Лучшая цена", "best"], ["К НМЦ", "ratio"]);
  const byKey = new Map(defs.map((x) => [x.key, x]));
  const head = el("tr", {}, ...cols.map(([h, key], i) => cfTh("ps", byKey.get(key), all, defs, renderPsTable, h,
    i === 0 ? "stick0" : (i === 1 ? "stick1" : null))));
  const body = el("tbody");
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: String(cols.length), class: "muted", text: "Нет позиций с такими фильтрами." })));
  for (const r of rows) {
    const pos = el("td", { class: "stick1" }, el("div", { text: r.name }));
    if (r.brand_model) pos.append(el("div", { class: "muted", text: r.brand_model }));
    const none = (text) => el("span", { class: "empty-val", text });
    const rfCell = el("td", {}, r.rf.length ? offerList(r.rf, false) : none(r.searched ? "не найдено" : "—"),
      unverifiedList(r.unverified.filter((a) => a.market === "rf")));
    const abCell = el("td", {}, r.abroad.length ? offerList(r.abroad, true) : none(r.searched && d.abroad !== false ? "не найдено" : "—"),
      unverifiedList(r.unverified.filter((a) => a.market === "abroad")));
    // «Спецификация» — только данные из базы, ничего не правится (0.41.1): окна позиции отсюда нет.
    const cells = [el("td", { class: "stick0", text: r.n }), pos,
      el("td", { text: r.maker || "" }), el("td", { text: r.country || "" }),
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

// ---------- подготовка заявки (0.15.0) ----------
// Одна кнопка «Подготовить документы заявки» — дальше всё само: требования к участнику, состав заявки, формы из
// документации заполнены сведениями участника, документы из его библиотеки скопированы. От пользователя — цена
// (по желанию) и то, чего нет в сведениях («Нужно от вас»).

let bidTimer = null;
let bidLastJob = null;
let bidTab = "docs";
let bidLast = null;
const BID_MODEL_KEY = "supplybro.bidModel2";   // 0.15.1: новый ключ — прежний выбор Sonnet не держится
const BID_STATUS = {
  ready: ["✅ готово", "ok"], copied: ["🟢 из библиотеки", "ok"], draft: ["черновик", "warn"],
  update: ["⚠ обновить", "warn"], urgent: ["🚩 запросить", "bad"], manual: ["⚠ подготовить вручную", "warn"],
  third_party: ["🚩 у производителя / поставщика", "bad"], accountant: ["бухгалтер", "warn"],
  user: ["готовит участник", "warn"], not_needed: ["не требуется", "muted"], conditional: ["при условии", "muted"],
};
const BID_PART = { single: "", "1": "1-я часть", "2": "2-я часть", price: "ценовое предложение", after_win: "после победы" };
const BID_REQ = { ok: ["✅ соответствует", "ok"], fail: ["❌ не соответствует", "bad"], no_data: ["нет данных", "warn"],
  declared: ["декларируется", "muted"] };
// Модели подготовки заявки (0.15.1): DeepSeek — по умолчанию, в 15–30 раз дешевле Claude (оценки — по К933).
const BID_MODEL_LABEL = {
  "deepseek-flash": "DeepSeek Flash · ≈ $0,1",
  "deepseek-v4-pro": "DeepSeek Pro · ≈ $0,3–0,5",
  "claude-haiku-5-5": "Claude Haiku",
  "claude-sonnet-5-5": "Claude Sonnet · ≈ $1,5–2",
  "claude-opus-5-5": "Claude Opus · дороже всего",
};
const BID_PARTS_TEXT = { single: "одна часть", two_parts: "две части", three_parts: "три части (с ценовым предложением)" };

function stopBidPolling() {
  if (bidTimer) { clearTimeout(bidTimer); bidTimer = null; }
}

function bidFileLink(path, text) {
  const t = currentTender();
  return el("a", { href: clientPath("/bid/file") + "?" + new URLSearchParams({ folder: t.folder, path }),
    text, title: "Скачать" });
}

async function loadBid() {
  const t = currentTender();
  const my = ++seq.bid;
  stopBidPolling();
  if (!t || !t.folder || blockOff("bid")) { $("bid").hidden = true; return; }
  $("bid").hidden = false;
  let d;
  try {
    d = await api(clientPath("/bid"), { folder: t.folder });
  } catch (err) {
    if (my === seq.bid) setStatus("bid-status", "Не удалось загрузить заявку: " + err.message, true);
    return;
  }
  if (my !== seq.bid) return;
  setStatus("bid-status", "");
  const busy = renderBidJob(d.job, d.noco_job);
  renderBid(d, busy);
  bidLast = d;
  if (busy || d.analysis_pending) bidTimer = setTimeout(loadBid, busy ? 2500 : 5000);
}

function renderBidJob(job, nocoJob) {
  // Показываем последнюю из двух задач: подготовку (или пересчёт цены) или запись в NocoDB.
  const j = nocoJob && (!job || (nocoJob.created_at || 0) > (job.created_at || 0)) ? nocoJob : job;
  const box = $("bid-job");
  lastJobs["bid-job-ctl"] = j;
  renderJobControls($("bid-job-ctl"), j);
  if (!j) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done", "paused", "canceled");
  const what = j.kind === "noco_bid" ? "Запись заявки в NocoDB"
    : (j.reapply ? "Пересчёт цены в формах" : "Подготовка документов заявки");
  if (j.status === "queued") {
    $("bid-bar").style.width = "0%";
    $("bid-job-text").textContent = `${what}: ` + queuedText(j);
    return true;
  }
  if (j.status === "running") {
    $("bid-bar").style.width = Math.round((j.progress || 0) * 100) + "%";
    $("bid-job-text").textContent = `${what}: ${j.step || "…"}` + stoppingText(j) +
      (j.kind === "bid" && !j.reapply && !j.control ? ". Обычно 3–8 минут — можно уйти и вернуться позже" : "");
    return true;
  }
  if (j.status === "paused" || j.status === "canceled") {
    box.classList.add(j.status);
    $("bid-bar").style.width = Math.round((j.progress || 0) * 100) + "%";
    $("bid-job-text").textContent = `${what}: ` + stoppedText(j);
    return false;
  }
  $("bid-bar").style.width = "100%";
  const when = j.finished_at ? new Date(j.finished_at * 1000).toLocaleString("ru-RU") : "";
  box.classList.add(j.status === "error" ? "error" : "done");
  $("bid-job-text").textContent = j.status === "error" ? `${what} — ошибка (${when}): ${j.message}`
    : `${what} — готово (${when}). ${j.message}`;
  const finished = bidLastJob && bidLastJob.id === j.id && ["queued", "running"].includes(bidLastJob.status);
  bidLastJob = { id: j.id, status: j.status };
  if (finished && j.status === "done") toast(j.kind === "noco_bid" ? "Заявка записана в NocoDB" : "Документы заявки готовы");
  return false;
}

function renderBid(d, busy) {
  const r = d.report;
  if (!$("bid-entity").dataset.touched || !d.entities.some((e) => e.id === $("bid-entity").value)) {
    fillSelect($("bid-entity"), d.entities.map((e) => ({ value: e.id, label: e.name + (e.materials ? "" : " (нет сведений)") })), d.entity);
  }
  if (d.models && d.models.length) {
    const saved = storageGet(BID_MODEL_KEY);
    fillSelect($("bid-model"), d.models.map((x) => ({ value: x, label: BID_MODEL_LABEL[x] || x })),
      d.models.includes(saved) ? saved : d.default_model);
  }
  const run = $("bid-run");
  run.textContent = r ? "Подготовить заново" : "Подготовить документы заявки";
  run.disabled = busy || !d.docs_with_text || d.analysis_pending;
  run.title = !d.docs_with_text ? "Сначала загрузите документацию конкурса"
    : d.analysis_pending ? "Идёт анализ документов — подготовка заявки после него"
    : "Модель прочитает документацию и сведения участника; сайт заполнит формы, скопирует документы из библиотеки " +
      "участника и соберёт пакет в папку AIExtract/Заявка" + (r ? ". Прежний пакет сохранится в AIExtract/history" : "");
  $("bid-stale").hidden = !d.stale;
  stg.bid = r ? ((r.gaps || []).length ? "draft" : "done") : busy ? "busy" : "none";
  renderStages();
  if (!r) {
    $("bid-body").hidden = true;
    $("bid-info").textContent = "";
    if (!busy) {
      setStatus("bid-status", !d.docs_with_text ? "Подготовка заявки — после загрузки документации."
        : (d.has_analysis ? "" : "Анализа документов ещё нет — позиции для технического предложения возьмутся из спецификации NocoDB (если есть). ") +
          "«Подготовить документы заявки» — сайт найдёт требования к участнику и состав заявки, заполнит формы из " +
          "документации сведениями участника, скопирует документы из его библиотеки и скажет, что нужно от вас.");
    }
    return;
  }
  const p = r.participant || {};
  $("bid-info").textContent = `· ${p.name || ""} · ${r.at_text || ""}` + (r.model ? ` · ${r.model}` : "") +
    (r.usd ? ` · ≈ ${usd(r.usd)}` : "") + (r.seconds ? ` · ${Math.max(1, Math.round(r.seconds / 60))} мин` : "");
  $("bid-body").hidden = false;
  renderBidSummary(r, busy);
  renderBidPrice(r, busy);
  const checks = [...(p.problems || []).map((x) => "⚠ " + x), ...(r.checks || []), ...(r.errors || []).map((x) => "⚠ " + x)];
  $("bid-checks").replaceChildren(...checks.map((c) => el("li", { class: c.startsWith("✅") ? "" : "warn", text: c })));
  renderBidDocs(r);
  renderBidNeed(r);
  renderBidReq(r);
  renderBidFiles(r);
  renderBidNotes(r);
  showBidTab(bidTab);
  $("bid-usage").textContent = r.usage_totals ? usageText(r.usage_totals) : "";
}

function renderBidSummary(r, busy) {
  const s = r.summary || {};
  const st = r.structure || {};
  const box = $("bid-summary");
  const line = el("div", { class: "bid-ready" },
    el("strong", { text: `Готово ${s.ready || 0} из ${s.total || 0}` }),
    s.drafts ? el("span", { class: "badge warn", text: `черновиков: ${s.drafts}` }) : null,
    (s.urgent || []).length ? el("span", { class: "badge bad", text: `🚩 запросить: ${s.urgent.length}` }) : null,
    el("span", { class: "muted", text: `файлов в пакете: ${s.files || 0}` }));
  const tools = el("div", { class: "bid-tools" });
  if (s.files) {
    const t = currentTender();
    tools.append(el("a", { class: "btn small", href: clientPath("/bid/zip") + "?" + new URLSearchParams({ folder: t.folder }),
      text: "⤓ Скачать архив", title: "Весь пакет заявки одним архивом (папки частей сохранятся)" }));
  }
  const noco = el("button", { class: "btn small need-analyze", type: "button", text: "Записать в NocoDB",
    title: "Перечень документов — в колонку «Перечень документов заявки», формы Word — в «ДС: декларативные формы» " +
      "(прежние файлы ячейки остаются, кроме файлов с теми же именами)" });
  noco.disabled = busy;
  noco.addEventListener("click", async () => {
    noco.disabled = true;
    const t = currentTender();
    try {
      const x = await api(clientPath("/bid/noco"), { folder: t.folder }, { method: "POST" });
      if (x.already) toast("Запись в NocoDB уже идёт.");
    } catch (err) { setStatus("bid-status", "Не удалось записать в NocoDB: " + err.message, true); }
    loadBid();
  });
  tools.append(noco);
  const facts = [];
  if (st.parts) facts.push("заявка: " + (BID_PARTS_TEXT[st.parts] || st.parts) + (st.part1_anonymous ? ", первая часть — без сведений об участнике" : ""));
  if (st.deadline) facts.push("подача до " + st.deadline);
  if (st.bid_security) facts.push("обеспечение заявки: " + st.bid_security);
  if (st.price_basis && st.price_basis !== "не указано") facts.push("цена " + st.price_basis);
  const extra = [];
  if (r.noco && r.noco.status) {
    extra.push(el("div", { class: "muted", text: `NocoDB (${r.noco.at_text || ""}): ` +
      ([...(r.noco.done || []), ...(r.noco.notes || [])].join("; ") || "ничего не записано") }));
  }
  if (r.backup) extra.push(el("div", { class: "muted", text: "Прежний пакет сохранён: " + r.backup }));
  box.replaceChildren(el("div", { class: "bid-head" }, line, tools),
    facts.length ? el("div", { class: "bid-facts", text: facts.join(" · ") }) : null, ...extra);
}

function renderBidPrice(r, busy) {
  const price = r.price || null;
  const mode = $("bid-price-mode");
  if (!$("bid-price").dataset.touched) {
    mode.value = price ? price.mode : "percent";
    $("bid-price-value").value = price ? String(price.value).replace(".", ",") : "";
    $("bid-price-vat").value = price && price.vat === "without" ? "without" : "with";
  }
  $("bid-price-vat").hidden = mode.value !== "total";
  $("bid-price-value").hidden = mode.value === "none";
  $("bid-price-value").placeholder = mode.value === "total" ? "например, 1 250 000" : "например, 5";
  $("bid-price-apply").disabled = busy;
  const v = r.price_values || {};
  const parts = [];
  if (v["ЦЕНА_С_НДС"]) parts.push(`без НДС ${v["ЦЕНА_БЕЗ_НДС"]} · НДС ${v["НДС"]} · с НДС ${v["ЦЕНА_С_НДС"]} руб.`);
  else if (!price) parts.push("цена не задана — в формах цены отмечены «<нет данных>»");
  if (v["СТАВКА_НДС"]) parts.push("ставка: " + v["СТАВКА_НДС"]);
  $("bid-price-now").textContent = parts.join(" · ");
}

let bidShowAll = false;     // показывать документы «не требуется»

function renderBidDocs(r) {
  const all = r.documents || [];
  const hiddenCount = all.filter((x) => x.status === "not_needed").length;
  const docs = bidShowAll ? all : all.filter((x) => x.status !== "not_needed");
  $("bid-docs-count").textContent = all.length ? `(${all.length - hiddenCount})` : "";
  const parts = all.some((x) => x.part && x.part !== "single");
  const cols = ["№", "Документ", ...(parts ? ["Часть"] : []), "Состояние", "Файлы", "Что сделать"];
  const head = el("tr", {}, ...cols.map((h) => el("th", { text: h })));
  const rows = docs.map((x) => {
    const [label, cls] = BID_STATUS[x.status] || [x.status, "muted"];
    const files = el("td", { class: "bid-files" });
    for (const f of x.files || []) {
      files.append(el("div", {}, bidFileLink(f.file, f.file.split("/").pop()),
        f.marks ? el("span", { class: "badge warn", text: `<нет данных> ×${f.marks}`, title: "Пометки — сведения, которых нет" }) : null));
    }
    const title = el("td", {}, el("div", { text: x.title + (x.form ? ` (${x.form})` : "") }));
    if (x.required === "conditional" && x.condition && x.status !== "conditional") title.append(el("div", { class: "muted", text: "если: " + x.condition }));
    if (x.note) title.append(el("div", { class: "muted bid-note", text: x.note, title: x.note }));
    const todo = x.status === "draft" ? "дописать сведения — «Нужно от вас»" : (x.todo || "");
    return el("tr", { class: x.status === "not_needed" ? "muted" : "" },
      el("td", { text: x.n || "" }), title,
      parts ? el("td", { text: BID_PART[x.part] || "" }) : null,
      el("td", {}, el("span", { class: "badge " + cls, text: label })),
      files, el("td", { text: todo }));
  });
  const foot = [];
  if (hiddenCount) {
    const a = el("button", { class: "btn small ghost", type: "button",
      text: bidShowAll ? "Скрыть документы, которые не требуются" : `Показать документы, которые не требуются (${hiddenCount})` });
    a.addEventListener("click", () => { bidShowAll = !bidShowAll; renderBidDocs(r); });
    foot.push(el("tr", {}, el("td", { colspan: String(cols.length) }, a)));
  }
  $("bid-docs-table").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows, ...foot));
}

function renderBidNotes(r) {
  const st = r.structure || {};
  const items = [];
  if (st.submission) items.push(["Как подаётся", st.submission]);
  if (st.naming) items.push(["Имена и формат файлов", st.naming]);
  if (st.offer_validity) items.push(["Срок действия заявки", st.offer_validity]);
  for (const n of r.notes || []) items.push(["", n]);
  $("bid-notes-count").textContent = items.length ? `(${items.length})` : "";
  $("bid-notes").replaceChildren(items.length
    ? el("ul", { class: "bid-list bid-notes" }, ...items.map(([k, v]) => el("li", {}, k ? el("strong", { text: k + ": " }) : null, v)))
    : el("p", { class: "muted", text: "Замечаний нет." }));
}

function renderBidNeed(r) {
  const gaps = r.gaps || [];
  const docs = (r.documents || []).filter((x) => ["urgent", "update", "third_party", "accountant", "user", "manual"].includes(x.status)
    && x.required !== "not_needed");
  const files = new Set(gaps.map((g) => g.file));
  $("bid-need-count").textContent = files.size + docs.length ? `(${files.size + docs.length})` : "";
  const box = $("bid-need");
  const parts = [];
  if (docs.length) {
    parts.push(el("h3", { text: "Документы, которые нужно получить или подготовить" }),
      el("ul", { class: "bid-list" }, ...docs.map((x) => el("li", {},
        el("span", { class: "badge " + (BID_STATUS[x.status] || ["", "warn"])[1], text: (BID_STATUS[x.status] || [x.status])[0] }),
        " " + x.title + (x.todo ? ` — ${x.todo}` : "") + (x.note ? `. ${x.note}` : "")))));
  }
  if (gaps.length) {
    const byFile = new Map();
    for (const g of gaps) { if (!byFile.has(g.file)) byFile.set(g.file, []); byFile.get(g.file).push(g.need); }
    parts.push(el("h3", { text: "Сведения, которых нет (в формах — «<нет данных>», жёлтым)" }),
      el("ul", { class: "bid-list" }, ...[...byFile].map(([file, needs]) => el("li", {},
        el("strong", { text: file }), ": " + [...new Set(needs)].join("; ")))));
    parts.push(el("p", { class: "muted", text: "Впишите сведения прямо в файлы Word (их можно скачать на вкладке «Документы») " +
      "и уберите из имени «ЧЕРНОВИК ». Если сведения стоит запомнить для следующих заявок — добавьте их в «Выжимку» участника." }));
  }
  if (!parts.length) parts.push(el("p", { class: "muted", text: "Всё, что нужно для заявки, есть — проверьте файлы и подпишите." }));
  box.replaceChildren(...parts);
}

function renderBidReq(r) {
  const reqs = r.requirements || [];
  $("bid-req-count").textContent = reqs.length ? `(${reqs.length})` : "";
  const head = el("tr", {}, ...["Требование", "Участник", "Основание", "Цитата"].map((h) => el("th", { text: h })));
  const rows = reqs.map((x) => {
    const [label, cls] = BID_REQ[x.status] || [x.status, "muted"];
    const q = el("td", { class: "quote" });
    if (x.quote) {
      q.append(el("div", { text: "«" + x.quote + "»" }));
      q.append(el("div", { class: "muted", text: (x.file || "") + (x.quote_status && !["found", "found_other"].includes(x.quote_status) ? " · ⚠ цитата не найдена" : "") }));
    }
    return el("tr", {}, el("td", { text: x.text }), el("td", {}, el("span", { class: "badge " + cls, text: label })),
      el("td", { text: x.basis || "" }), q);
  });
  $("bid-req-table").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

function renderBidFiles(r) {
  const files = r.files || [];
  $("bid-files-count").textContent = files.length ? `(${files.length})` : "";
  const sup = (r.supplier_files || {}).files || [];
  const parts = [el("ul", { class: "bid-list" }, ...files.map((f) => el("li", {}, bidFileLink(f.path, f.name),
    el("span", { class: "muted", text: " · " + fmtSize(f.size) }),
    f.marks ? el("span", { class: "badge warn", text: `<нет данных> ×${f.marks}` }) : null)))];
  if (sup.length) {
    parts.push(el("h3", { text: "Запросы поставщикам (по производителям)" }),
      el("ul", { class: "bid-list" }, ...sup.map((p) => el("li", {}, bidFileLink(p, p.split("/").pop())))));
  } else if ((r.supplier_files || {}).note) {
    parts.push(el("p", { class: "muted", text: r.supplier_files.note }));
  }
  $("bid-files").replaceChildren(...parts);
}

function showBidTab(tab) {
  bidTab = tab;
  for (const b of document.querySelectorAll("[data-btab]")) b.classList.toggle("active", b.dataset.btab === tab);
  for (const k of ["docs", "need", "req", "files", "notes"]) $("bid-" + k).hidden = k !== tab;
}

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
  $("chat-own").hidden = !d.personal;                    // личный чат (0.20.0): чужие вопросы не видны
  const purchase = !document.body.classList.contains("no-purchase");
  $("chat-hint").textContent = (d.personal ? "Это ваш чат: вопросы и ответы видите только вы. " : "") +
    "Ответы — только по документам этого конкурса и найденным ценам, с источниками; цитаты программа сверяет с текстом. " +
    "Задавать вопросы можно в любое время, как только документы подготовлены, — анализ для этого не нужен." +
    (purchase ? " Прайс, КП или счёт поставщика — скрепкой 📎 или перетащите файл сюда; сообщение поставщика с ценами — " +
      "вставьте в поле и нажмите «Цены из сообщения»: сайт найдёт позиции конкурса и цены, записать их в спецификацию " +
      "NocoDB — кнопкой на карточке." : "");
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
  // 0.30.0: в чате — только вопросы и ответы; карточки прайсов, КП и писем — во вкладке «Закупка»
  // («Документы поставщиков» под таблицей предложений).
  $("chat-log").replaceChildren(...chatMsgs.map(renderChatMessage));
}

// Письмо поставщика (или вложение письма), где цен не нашлось, — не карточкой, а в строке «Письма без цен» со
// ссылкой на переписку (0.32.1: вопросы поставщиков читают и отвечают в окне переписки).
const plQuiet = (f) => Boolean(f.mail) && f.status === "done" && !(f.matches || []).length;

function quietMailRow(files) {
  const by = new Map();
  for (const f of files) {
    const k = f.mail.inbox_id || f.mail.from + "|" + f.mail.subject;
    if (!by.has(k)) by.set(k, { f, names: [] });
    by.get(k).names.push(f.name);
  }
  const items = [...by.values()].map(({ f, names }) => {
    const who = f.supplier || f.mail.supplier || f.mail.from_name || f.mail.from;
    const key = thKeyOfInbox({ request_id: f.mail.request_id, from_addr: f.mail.from });
    const t = thFind(key);
    const label = `${who}${t && t.waiting ? " · ждёт ответа" : ""} 💬`;
    const b = thOpenBtn(key, label);
    b.title = `Открыть переписку · ${fmtWhen(f.uploaded_at)} · разобрано: ${names.join(", ")}`;
    return b;
  });
  const row = el("div", { class: "pl-quiet small" }, el("span", { class: "muted", text: `📧 Письма без цен (${items.length}): ` }));
  items.forEach((b, i) => { if (i) row.append(document.createTextNode(" · ")); row.append(b); });
  return row;
}

function renderSupplierDocs() {
  const all = ((plData && plData.files) || []).slice().sort((a, b) => (b.uploaded_at || 0) - (a.uploaded_at || 0));
  const files = all.filter((f) => !plQuiet(f));
  const quiet = all.filter(plQuiet);
  plCards.clear();
  const nodes = files.map((f) => { const n = pricelistCard(f); plCards.set(f.id, n); return n; });
  if (quiet.length) nodes.unshift(quietMailRow(quiet));
  const busy = files.filter(PL_BUSY).length;
  $("of-docs-title").textContent = `Документы поставщиков · ${files.length}` + (quiet.length ? ` · писем без цен: ${new Set(quiet.map((f) => f.mail.inbox_id || f.mail.from)).size}` : "") + (busy ? ` · разбирается: ${busy}` : "");
  replaceSafe($("of-docs-list"), ...(nodes.length ? nodes : [el("div", { class: "muted small", text: "Прайсов, КП, счетов и писем поставщиков пока нет." })]));
  if (busy) $("of-docs").open = true;
}

async function loadPricelists() {
  const t = currentTender();
  // Карточки прайсов в чате — тем, кто вносит цены поставщиков (право «purchase», 0.20.0); цены — и в «Все цены».
  if (!t || !t.folder || blockOff("prices") || document.body.classList.contains("no-purchase")) return;
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
    if (plQuiet(f) !== plQuiet(old)) { added = true; continue; }
    const card = plCards.get(f.id);
    if (card) {
      const fresh = pricelistCard(f);
      card.replaceWith(fresh);
      plCards.set(f.id, fresh);
    }
  }
  if (added || d.files.length !== before.size) renderSupplierDocs();
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
  box.append(el("div", { class: "pl-head" }, el("span", { text: f.mail ? "📧 Письмо поставщика: " : "📎 Документ поставщика: " }), link,
    el("span", { class: "muted", text: " · " + fmtWhen(f.uploaded_at) + (f.by ? " · " + f.by : "") })));
  if (f.mail) {
    // Ответ на запрос по почте (0.28.0): от кого, тема и почему письмо отнесено к этому конкурсу.
    box.append(el("div", { class: "muted small", text: `от ${f.mail.from_name ? f.mail.from_name + " <" + f.mail.from + ">" : f.mail.from}` + (f.mail.supplier ? ` (запрос — ${f.mail.supplier})` : "") +
      ` · «${f.mail.subject}» · отнесено к конкурсу: ${IB_HOW[f.mail.how] || f.mail.how}` }));
  }
  const status = (cls, text) => el("div", { class: "pl-status " + cls, text });
  if (f.status === "queued") box.append(status("wait", "В очереди на разбор…"));
  if (f.status === "running") box.append(status("wait", "Читаю файл и ищу позиции конкурса… обычно до минуты."));
  if (f.status === "error") box.append(el("div", { class: "notice bad", text: "Не удалось разобрать: " + (f.error || "ошибка") }));
  if (f.status === "done") {
    const ms = f.matches || [];
    const mailWho = f.mail ? (f.mail.supplier || f.mail.from_name || f.mail.from || "") : "";
    const facts = [f.supplier ? "Поставщик: " + f.supplier + (f.supplier_inn ? ` (ИНН ${f.supplier_inn})` : "")
      : mailWho ? `Поставщик: ${mailWho} (по письму — в документе название не указано)` : "Поставщик не указан в документе — запишется именем файла",
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
      const head = el("tr", {}, ...["", "№", "Позиция конкурса", "Строка документа", "Цена за ед.", "К НМЦ", "Совпадение"].map((h) => el("th", { text: h })));
      const body = el("tbody");
      for (const m of ms) {
        // 0.29.0: выбор и запись — только в «Предложениях поставщиков»; здесь — что найдено.
        const cb = m.selected ? document.createTextNode("☑") : document.createTextNode("");
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
      // Запись в NocoDB — из таблицы «Предложения поставщиков» (сравнение с другими поставщиками, комментарии).
      const toOffers = el("a", { class: "btn small", href: "#offers", text: "Сравнить и записать — «Предложения поставщиков» →" });
      toOffers.addEventListener("click", (e) => { e.preventDefault(); goTo("offers"); });
      const actions = el("div", { class: "pl-actions" }, toOffers);
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
    const rm = el("button", { class: "btn small ghost need-purchase pl-remove", type: "button", text: "Убрать из списка",
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
  if (document.body.classList.contains("no-purchase")) return toast("Вносить цены поставщиков вашей роли недоступно", true);
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  const u = new URL(clientPath("/pricelists/upload"), location.origin);
  u.searchParams.set("folder", t.folder);
  if (promptEntity()) u.searchParams.set("entity", promptEntity());
  toast(files.length === 1 ? `Загружаю «${files[0].name}»…` : `Загружаю файлов: ${files.length}…`);
  try {
    const r = await fetch(u, { method: "POST", body: form });
    if (r.status === 401) return toLogin();
    let b = {};
    try { b = await r.json(); } catch (_) { /* не JSON */ }
    if (!r.ok) throw new Error(b.detail || `ошибка ${r.status}`);
    toast("Загружено — ищу в документе позиции конкурса и цены");
    $("of-docs").open = true;
  } catch (err) {
    toast("Не удалось загрузить: " + err.message, true);
  }
  $("chat-files").value = "";
  $("of-files").value = "";
  await loadPricelists();
  renderSupplierDocs();
  if (currentTab() !== "purchase") toast("Документ — во вкладке «Закупка» → «Документы поставщиков»");
  else $("of-docs").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("of-files").addEventListener("change", (e) => uploadPricelists(e.target.files));

// Поле загрузки под таблицей «Закупки» (0.43.0): щелчок — выбор файлов, перетаскивание, Ctrl+V — картинка из буфера
// (скриншот) или текст письма поставщика. Всё уходит туда же, куда 📎 в чате (/pricelists/upload).
function stamp() {
  const d = new Date(), z = (n) => String(n).padStart(2, "0");
  return `${z(d.getDate())}.${z(d.getMonth() + 1)}.${d.getFullYear()} ${z(d.getHours())}-${z(d.getMinutes())}-${z(d.getSeconds())}`;
}
function pastedFiles(cd) {
  const files = [];
  let k = 0;
  for (const it of cd.items || []) {
    if (it.kind !== "file") continue;
    const f = it.getAsFile();
    if (!f) continue;
    if (f.type.startsWith("image/") && (!f.name || /^image\.\w+$/i.test(f.name))) {
      const ext = (f.type.split("/")[1] || "png").replace("jpeg", "jpg");
      files.push(new File([f], `Скриншот ${stamp()}${k++ ? " (" + k + ")" : ""}.${ext}`, { type: f.type }));
    } else files.push(f);
  }
  return files;
}
const ofDrop = $("of-drop");
ofDrop.addEventListener("click", () => $("of-files").click());
ofDrop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("of-files").click(); } });
for (const ev of ["dragenter", "dragover"]) ofDrop.addEventListener(ev, (e) => { e.preventDefault(); ofDrop.classList.add("over"); });
ofDrop.addEventListener("dragleave", (e) => { if (!ofDrop.contains(e.relatedTarget)) ofDrop.classList.remove("over"); });
ofDrop.addEventListener("drop", (e) => { e.preventDefault(); ofDrop.classList.remove("over"); uploadPricelists(e.dataTransfer.files); });
document.addEventListener("paste", (e) => {
  if (currentTab() !== "purchase" || document.body.classList.contains("no-purchase") || !currentTender()) return;
  const a = document.activeElement;
  const inField = a && a !== ofDrop && a.matches("input, textarea, select, [contenteditable]");
  if (inField || document.querySelector("dialog[open]")) return;       // обычная вставка в поле ввода / окно
  const cd = e.clipboardData;
  if (!cd) return;
  const files = pastedFiles(cd);
  if (files.length) { e.preventDefault(); uploadPricelists(files); return; }
  // текст — только если вставили именно в поле загрузки (чтобы случайный Ctrl+V не создавал файлов)
  const text = (cd.getData("text/plain") || "").trim();
  if (a !== ofDrop) return;
  e.preventDefault();
  if (text.length < 10) return toast("В буфере нет картинки или текста с ценами", true);
  uploadPricelists([new File([text], `Сообщение поставщика ${stamp()}.txt`, { type: "text/plain" })]);
});

$("chat-files").addEventListener("change", (e) => uploadPricelists(e.target.files));
// Сообщение поставщика с ценами (почта, мессенджер) — как прайс (0.20.0): текст уходит файлом «Сообщение поставщика
// <дата>.txt» в «Прайсы, счета, КП» и разбирается так же.
$("chat-as-prices").addEventListener("click", () => {
  const text = $("chat-input").value.trim();
  if (text.length < 10) {
    toast("Вставьте в поле сообщение поставщика с ценами и нажмите ещё раз", true);
    $("chat-input").focus();
    return;
  }
  const d = new Date(), z = (n) => String(n).padStart(2, "0");
  const name = `Сообщение поставщика ${z(d.getDate())}.${z(d.getMonth() + 1)}.${d.getFullYear()} ${z(d.getHours())}-${z(d.getMinutes())}.txt`;
  $("chat-input").value = "";
  uploadPricelists([new File([text], name, { type: "text/plain" })]);
});
// Перетащить прайс прямо в чат.
for (const ev of ["dragover", "dragenter"]) {
  $("chat").addEventListener(ev, (e) => {
    if (document.body.classList.contains("no-purchase") || !e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    $("chat").classList.add("drag-over");
  });
}
$("chat").addEventListener("dragleave", (e) => { if (!$("chat").contains(e.relatedTarget)) $("chat").classList.remove("drag-over"); });
$("chat").addEventListener("drop", (e) => {
  $("chat").classList.remove("drag-over");
  if (document.body.classList.contains("no-purchase") || !e.dataTransfer || !e.dataTransfer.files.length) return;
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
    if (promptEntity()) u.searchParams.set("entity", promptEntity());
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

// ---------- фильтры колонок, как в Excel (0.40.0) ----------
// Пользователь 26.11.2026: «фильтры как в Excel у столбца — небольшой выпадающий список, где можно по брендам отобрать,
// по ценам есть или нет, и прочие фильтры». «▾» в заголовке колонки — список значений с галочками и числами (по
// строкам, прошедшим остальные фильтры); снятая галочка прячет строки. Быстрые отборы — чипы над таблицей «Цены»;
// всё вместе работает как «и». Фильтры — на конкурс (сбрасываются при смене конкурса и кнопкой «Сбросить фильтры»).

// 0.41.0: и «Предложения поставщиков» (scope of) — свои фильтры, чипы и поиск.
const cf = { ps: {}, spec: {}, of: {}, chips: { ps: new Set(), of: new Set() }, q: { spec: "", ps: "", of: "" } };
let cfPop = null;

function cfReset() {
  cf.ps = {}; cf.spec = {}; cf.of = {};
  cf.chips = { ps: new Set(), of: new Set() };
  cf.q = { spec: "", ps: "", of: "" };
  for (const id of ["cf-search", "of-cf-search"]) if ($(id)) $(id).value = "";
  cfClose();
}
function cfChipList(scope) { return scope === "ps" ? PS_CHIPS : scope === "of" ? OF_CHIPS : []; }
function cfHay(scope, r) {
  if (scope === "ps") return [r.n, r.name, r.brand_model, r.maker, r.country].join(" ");
  if (scope === "of") return [r.n, r.name, r.brand, r.maker, r.country, ...ofSupNames(r)].join(" ");
  return Object.values(r).map(cellText).join(" ");
}
function cfNorm(x) { return String(x || "").toLowerCase().replace(/ё/g, "е"); }

function cfPass(scope, defs, r, skip) {
  for (const d of defs) {
    if (d.key === skip) continue;
    const want = cf[scope][d.key];
    if (want && !d.values(r).some((v) => want.has(v))) return false;
  }
  for (const k of cf.chips[scope] || []) { const c = cfChipList(scope).find((x) => x[0] === k); if (c && !c[2](r)) return false; }
  const q = cf.q[scope];
  if (q) {
    const hay = cfNorm(cfHay(scope, r));
    if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

function cfActive(scope) { return Object.keys(cf[scope]).length + ((cf.chips[scope] || new Set()).size) + (cf.q[scope] ? 1 : 0); }

function cfShowInfo(scope, shown, total) {
  const n = cfActive(scope);
  if (scope === "of") {
    $("of-cf-info").textContent = n ? `показано ${shown} из ${total}` : "";
    $("of-cf-clear").hidden = !n;
    return;
  }
  if (specMode() !== scope.replace("ps", "prices").replace("spec", "pos")) return;
  $("cf-info").textContent = n ? `показано ${shown} из ${total}` : "";
  $("cf-clear").hidden = !n;
}

// Чипы быстрых отборов: box — куда, all — все строки, rerender — перерисовать таблицу.
function renderChipsInto(box, scope, all, rerender) {
  const set = cf.chips[scope];
  box.replaceChildren(...cfChipList(scope).map(([k, label, fn]) => {
    const n = all.filter(fn).length;
    const b = el("button", { type: "button", class: "chip-btn" + (set.has(k) ? " on" : ""), text: `${label} · ${n}`, disabled: n || set.has(k) ? null : "" });
    b.addEventListener("click", () => { set.has(k) ? set.delete(k) : set.add(k); rerender(); });
    return b;
  }));
}

// Заголовок колонки с кнопкой фильтра (def нет — просто заголовок).
function cfTh(scope, def, allRows, defs, rerender, text, cls) {
  const th = el("th", { class: cls || null });
  th.append(document.createTextNode(text));
  if (def) {
    const on = Boolean(cf[scope][def.key]);
    const b = el("button", { type: "button", class: "cf-btn" + (on ? " on" : ""), title: on ? "Фильтр включён — изменить" : "Фильтр колонки" }, on ? "▼" : "▾");
    b.addEventListener("click", (e) => { e.stopPropagation(); cfOpen(scope, def, allRows, defs, rerender, b); });
    th.append(b);
  }
  return th;
}

function cfClose() { if (cfPop) { cfPop.remove(); cfPop = null; } }
document.addEventListener("click", (e) => { if (cfPop && !cfPop.contains(e.target)) cfClose(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") cfClose(); });

function cfOpen(scope, def, allRows, defs, rerender, btn) {
  cfClose();
  const counts = new Map();
  for (const r of allRows) {
    if (!cfPass(scope, defs, r, def.key)) continue;
    for (const v of new Set(def.values(r))) counts.set(v, (counts.get(v) || 0) + 1);
  }
  const cur = cf[scope][def.key];
  // выбранные значения, которых сейчас нет среди строк, тоже показать (чтобы их можно было снять)
  if (cur) for (const v of cur) if (!counts.has(v)) counts.set(v, 0);
  const empty = (v) => /^\(/.test(v);
  const values = [...counts.keys()].sort((a, b) => (def.order ? def.order.indexOf(a) - def.order.indexOf(b) : 0) ||
    (empty(a) - empty(b)) || (counts.get(b) - counts.get(a)) || a.localeCompare(b, "ru"));
  const pop = el("div", { class: "cf-pop" });
  const search = values.length > 8 ? el("input", { type: "search", placeholder: "Найти значение" }) : null;
  const list = el("div", { class: "cf-list" });
  const isOn = (v) => !cur || cur.has(v);
  const apply = (sel) => {
    if (sel.size === values.length) delete cf[scope][def.key]; else cf[scope][def.key] = sel;
    rerender();
  };
  const selected = () => new Set([...list.querySelectorAll("input[data-v]")].filter((x) => x.checked).map((x) => x.dataset.v));
  const allBox = el("input", { type: "checkbox" });
  allBox.checked = !cur;
  allBox.addEventListener("change", () => {
    for (const x of list.querySelectorAll("input[data-v]")) if (!x.closest("label").hidden) x.checked = allBox.checked;
    apply(selected());
  });
  for (const v of values) {
    const box = el("input", { type: "checkbox", "data-v": v });
    box.checked = isOn(v);
    box.addEventListener("change", () => { allBox.checked = [...list.querySelectorAll("input[data-v]")].every((x) => x.checked); apply(selected()); });
    list.append(el("label", { class: "cf-item" }, box, el("span", { text: v }), el("span", { class: "muted", text: String(counts.get(v)) })));
  }
  if (search) search.addEventListener("input", () => {
    const q = cfNorm(search.value);
    for (const lab of list.children) lab.hidden = Boolean(q) && !cfNorm(lab.textContent).includes(q);
  });
  const only = (v) => apply(new Set([v]));
  list.addEventListener("dblclick", (e) => { const lab = e.target.closest(".cf-item"); if (lab) only(lab.querySelector("input").dataset.v); });
  pop.append(...[el("div", { class: "cf-title", text: def.label }), search,
    el("label", { class: "cf-item cf-all" }, allBox, el("span", { text: "Выделить всё" })), list,
    el("div", { class: "muted small", text: "Двойной щелчок — только это значение" })].filter(Boolean));
  pop.addEventListener("click", (e) => e.stopPropagation());
  document.body.append(pop);
  const r = btn.getBoundingClientRect();
  pop.style.top = (r.bottom + window.scrollY + 4) + "px";
  pop.style.left = Math.max(8, Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8)) + "px";
  cfPop = pop;
  if (search) search.focus();
}

// Цены: значения фильтров колонок.
const PS_NONE = "(нет)";
function psMarket(r, key) {
  if ((r[key] || []).length) return "есть цена";
  if ((r.unverified || []).some((a) => a.market === key)) return "только «не проверено»";
  return r.searched ? "не найдено" : "не искали";
}
function psDefs(showAgent, showHist) {
  const has = (x) => (x ? "есть" : PS_NONE);
  const defs = [
    { key: "brand", label: "Бренд", values: (r) => [r.maker || "(не указан)"] },
    { key: "country", label: "Страна", values: (r) => [r.country || "(не указана)"] },
    { key: "unit", label: "Ед. изм.", values: (r) => [r.unit || "(не указана)"] },
    { key: "nmc", label: "НМЦ за ед.", values: (r) => [r.nmc ? "есть НМЦ" : "нет НМЦ"] },
    { key: "rf", label: "Интернет: Россия", order: ["есть цена", "только «не проверено»", "не найдено", "не искали"], values: (r) => [psMarket(r, "rf")] },
    { key: "abroad", label: "Интернет: зарубеж", order: ["есть цена", "только «не проверено»", "не найдено", "не искали"], values: (r) => [psMarket(r, "abroad")] },
    { key: "sup", label: "Поставщики", values: (r) => [!(r.suppliers || []).length ? PS_NONE
      : r.suppliers.some((x) => x.match !== "exact" || (x.problems || []).length) ? "есть, с замечанием" : "есть"] },
    { key: "best", label: "Лучшая цена", values: (r) => [r.best ? "есть" : PS_NONE] },
    { key: "ratio", label: "К НМЦ", order: ["≤ 85 % НМЦ", "85–100 % НМЦ", "выше НМЦ", "нет цены", "нет НМЦ"],
      values: (r) => [!r.nmc ? "нет НМЦ" : !r.best ? "нет цены" : r.ratio <= 0.85 ? "≤ 85 % НМЦ" : r.ratio <= 1 ? "85–100 % НМЦ" : "выше НМЦ"] },
  ];
  if (showAgent) defs.push({ key: "agent", label: "Агент", values: (r) => [has((r.agent || []).length || r.market)] });
  if (showHist) defs.push({ key: "hist", label: "Прошлые конкурсы", values: (r) => [has((r.history || []).length)] });
  return defs;
}
// Быстрые отборы (чипы) — самые ходовые.
const PS_CHIPS = [
  ["nothing", "без цены вообще", (r) => !r.best && !(r.suppliers || []).length && !(r.rf || []).length && !(r.abroad || []).length],
  ["nosup", "без цен поставщиков", (r) => !(r.suppliers || []).length],
  ["nonet", "без цен в интернете", (r) => !(r.rf || []).length && !(r.abroad || []).length],
  ["over", "выше НМЦ", (r) => r.ratio > 1],
  ["unwritten", "есть цены — не записаны", (r) => (r.suppliers || []).some((x) => x.source === "прайс" && !x.in_noco && !x.written)],
  ["unverified", "только «не проверено»", (r) => !(r.rf || []).length && !(r.abroad || []).length && (r.unverified || []).length > 0],
];
function renderCfChips() {
  const box = $("cf-chips");
  if (specMode() !== "prices" || !psLast) { box.replaceChildren(); return; }
  renderChipsInto(box, "ps", psDbRows(), renderPsTable);
}

// Спецификация (Позиции): фильтр у каждой колонки — значения ячеек; длинный текст и числа — «заполнено / (пусто)».
const SPEC_LONG = new Set(["LongText", "Attachment", "JSON"]);
const SPEC_NO_FILTER = new Set(["№", "Наименование позиции", "Id"]);   // у каждой строки своё — для них поиск
function specDefs(columns, brands) {
  const defs = columns.filter((c) => !SPEC_NO_FILTER.has(c.title)).map((c) => {
    const yesNo = NUMERIC.has(c.uidt) || SPEC_LONG.has(c.uidt) || c.uidt === "URL";
    return { key: c.title, label: c.title, values: (r) => {
      const t = cellText(r[c.title]).trim();
      if (yesNo) return [t ? "заполнено" : "(пусто)"];
      return [t ? (t.length > 80 ? t.slice(0, 78) + "…" : t) : "(пусто)"];
    } };
  });
  const byRow = new Map(((brands && brands.rows) || []).map((m) => [m.i, m]));
  if (byRow.size) defs.push({ key: "__brands", label: "Наши поставщики", values: (r) => {
    const m = byRow.get(r._i);
    return m && (m.brands || []).length ? m.brands.map((b) => b.brand) : ["(нет)"];
  } });
  return defs;
}

// Режим вкладки: «Позиции» (pos) или «Цены» (prices); выбор — в браузере.
const SPEC_MODE_KEY = "sb.spec.mode";
function specMode() { return $("spec").dataset.mode || "pos"; }
function specModeInit(t) {
  const canPos = Boolean(t && t.source === "nocodb" && !blockOff("spec"));
  const canPrices = Boolean(t && t.folder && !blockOff("prices"));
  $("spec-mode").querySelector('[data-mode="pos"]').hidden = !canPos;
  $("spec-mode").querySelector('[data-mode="prices"]').hidden = !canPrices;
  $("spec-mode").hidden = !(canPos && canPrices);
  let m = storageGet(SPEC_MODE_KEY) || "pos";
  if (m === "pos" && !canPos) m = "prices";
  if (m === "prices" && !canPrices) m = "pos";
  setSpecMode(m, false);
}
function setSpecMode(m, remember = true) {
  $("spec").dataset.mode = m;
  for (const b of $("spec-mode").querySelectorAll("button")) b.classList.toggle("on", b.dataset.mode === m);
  for (const x of $("spec").querySelectorAll(".spec-pos-only")) x.hidden = m !== "pos";
  for (const x of $("spec").querySelectorAll(".spec-prices-only")) x.hidden = m !== "prices";
  $("spec-count").hidden = m !== "pos";
  $("cf-search").placeholder = m === "pos" ? "Поиск по всем колонкам" : "Поиск: №, позиция, марка, бренд, страна";
  if (remember) storageSet(SPEC_MODE_KEY, m);
  cfClose();
  if (m === "prices") renderPsTable(); else renderSpecFiltered();
  renderCfChips();
}
for (const b of $("spec-mode").querySelectorAll("button")) b.addEventListener("click", () => setSpecMode(b.dataset.mode));
$("cf-search").addEventListener("input", (e) => {
  cf.q.spec = cf.q.ps = cfNorm(e.target.value.trim());
  if (specMode() === "prices") renderPsTable(); else renderSpecFiltered();
});
$("cf-clear").addEventListener("click", () => {
  const m = specMode();
  if (m === "prices") { cf.ps = {}; cf.chips.ps = new Set(); } else cf.spec = {};
  cf.q.spec = cf.q.ps = ""; $("cf-search").value = "";
  cfClose();
  if (m === "prices") renderPsTable(); else renderSpecFiltered();
});

// ---------- спецификация ----------

// Удаление конкурса из NocoDB (0.37.0): запись реестра и спецификация вместе; только администратор сайта (кнопка —
// на его странице, сервер проверяет сам).
let specInfo = null;
function specNeed() { const t = currentTender(); return (t && (t.internal || t.number || t.folder)) || ""; }
$("head-delete").addEventListener("click", () => {
  const t = currentTender();
  if (!t) return;
  const noco = t.source === "nocodb";
  // Конкурс-папка (нет записи в NocoDB, 0.37.0): удаляется только папка — в «#Удалённые конкурсы».
  $("sd-h").textContent = noco ? "Удалить конкурс из NocoDB" : "Удалить конкурс";
  $("sd-rec-li").hidden = !noco;
  $("sd-lead").replaceChildren(...(noco ? [document.createTextNode("Будут удалены "), el("b", { text: "вместе" }), document.createTextNode(":")]
    : [document.createTextNode("Конкурса нет в NocoDB — с сайта уберётся только папка:")]));
  $("sd-note").hidden = !noco;
  $("sd-go").textContent = noco ? "Удалить запись и спецификацию" : "Убрать папку конкурса";
  $("sd-folder-li").hidden = noco;
  $("sd-folder").textContent = t.folder ? `«${t.folder}»` : "";
  $("sd-with-folder-box").hidden = !(noco && t.folder);
  $("sd-with-folder").checked = false;
  $("sd-num").textContent = specNeed() || "без номера";
  const has = noco && specInfo && !specInfo.none;
  $("sd-spec-li").hidden = !noco || (specInfo && specInfo.none);
  $("sd-title").textContent = has ? `«${specInfo.title}»` : "";
  $("sd-rows").textContent = has ? `${specInfo.rows} строк` : "проверю перед удалением";
  $("sd-need").textContent = specNeed();
  $("sd-confirm").value = "";
  $("sd-go").disabled = true;
  setStatus("sd-status", "");
  $("sd-dlg").showModal();
  $("sd-confirm").focus();
});
$("sd-close").addEventListener("click", () => $("sd-dlg").close());
$("sd-confirm").addEventListener("input", () => {
  const norm = (x) => String(x || "").trim().toUpperCase().replace(/^K/, "К").replace(/\s|-/g, "");
  $("sd-go").disabled = norm($("sd-confirm").value) !== norm(specNeed());
});
$("sd-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("sd-go").disabled = true;
  const t = currentTender();
  const noco = t && t.source === "nocodb";
  setStatus("sd-status", noco ? "Сохраняю копию, удаляю спецификацию и запись…" : "Убираю папку конкурса…");
  let r;
  try {
    r = noco
      ? await api(clientPath(`/tenders/${state.tender}/delete`), {}, jsonPost({ confirm: $("sd-confirm").value, with_folder: $("sd-with-folder").checked }))
      : await api(clientPath("/folders/delete"), {}, jsonPost({ folder: t.folder, confirm: $("sd-confirm").value }));
  } catch (err) {
    setStatus("sd-status", "Не удалено: " + err.message, true);
    $("sd-go").disabled = false;
    return;
  }
  $("sd-dlg").close();
  toast((noco ? "Удалено из NocoDB: " : "Удалено: ") + r.done.join("; ") + (r.warnings.length ? ". ⚠ " + r.warnings.join("; ") : "") +
    (noco ? ". Копия сохранена." : ""));
  specInfo = null;
  // Записи больше нет: есть папка — конкурс открывается как «папка без записи в NocoDB», нет — список конкурсов.
  state.tender = r.folder ? "f:" + r.folder : "";
  state.opened = "";
  state.fresh = true;
  saveUrl();
  await loadTenders();
});

async function loadSpec(suppliersFresh = false) {
  const my = ++seq.spec;
  if (blockOff("spec")) return;
  setStatus("spec-status", "Загрузка спецификации…");
  $("spec-table").replaceChildren();
  $("spec-count").textContent = "";
  $("spec-link").hidden = true;
  specInfo = null;
  $("spec-brands").hidden = true;
  $("head-brands").hidden = true;
  let d;
  try {
    d = await api(clientPath(`/tenders/${state.tender}/spec`), { view: state.specView, suppliers_fresh: suppliersFresh ? "1" : "" });
  } catch (err) {
    if (my === seq.spec) setStatus("spec-status", "Не удалось загрузить спецификацию: " + err.message, true);
    return;
  }
  if (my !== seq.spec) return;
  if (!d.found) {
    specInfo = { none: true };
    setStatus("spec-status", "У этого конкурса нет таблицы спецификации в NocoDB.");
    fillSelect($("spec-view"), []);
    return;
  }
  setStatus("spec-status", d.rows.length ? "" : "В спецификации нет строк.");
  $("spec-count").textContent = `· ${d.title} · ${d.rows.length} поз.`;
  $("spec-link").href = d.link;
  $("spec-link").hidden = false;
  specInfo = { title: d.title, rows: d.rows.length };
  fillSelect($("spec-view"), d.views.map((v) => ({ value: v.id, label: v.title })), d.view ? d.view.id : "");
  state.specView = d.view ? d.view.id : "";
  specData = { columns: d.columns, rows: d.rows.map((r, i) => ({ ...r, _i: i })), brands: d.brands };
  renderSpecFiltered();
  renderBrands(d.brands);
  saveUrl();
}

// Текст значения NocoDB для подсказки (вложения и объекты — без подсказки).
function cellText(v) {
  if (isEmpty(v)) return "";
  if (Array.isArray(v)) return typeof v[0] === "object" ? "" : v.join(", ");
  if (typeof v === "object") return "";
  return String(v);
}

const SPEC_RH = ["1", "2", "3", "all"];
function specRowHeight() {
  let v = "2";
  try { v = localStorage.getItem("sb.spec.rh") || "2"; } catch (_) { /* хранилище недоступно */ }
  return SPEC_RH.includes(v) ? v : "2";
}
function applySpecRowHeight(v) {
  const t = $("spec-table");
  for (const x of SPEC_RH) t.classList.toggle("rh-" + x, x === v);
  $("spec-rh").value = v;
}
$("spec-rh").addEventListener("change", (e) => {
  try { localStorage.setItem("sb.spec.rh", e.target.value); } catch (_) { /* не запомнится — не страшно */ }
  applySpecRowHeight(e.target.value);
});
// Щелчок по обрезанной ячейке — показать весь текст (ещё раз — свернуть); ссылки и вложения работают как раньше.
$("spec-table").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const cell = e.target.closest("td .cell");
  if (cell) cell.classList.toggle("open");
});
applySpecRowHeight(specRowHeight());

let specData = null;
function renderSpecFiltered() {
  if (!specData) { $("spec-table").replaceChildren(); return; }
  const defs = specDefs(specData.columns, specData.brands);
  const rows = specData.rows.filter((r) => cfPass("spec", defs, r));
  cfShowInfo("spec", rows.length, specData.rows.length);
  renderSpec(specData.columns, rows, specData.brands, defs);
}

function renderSpec(columns, rows, brands, defs = []) {
  const table = $("spec-table");
  const byKey = new Map(defs.map((x) => [x.key, x]));
  const allRows = specData ? specData.rows : rows;
  const th = (key, text, cls, title) => { const h = cfTh("spec", byKey.get(key), allRows, defs, renderSpecFiltered, text, cls); if (title) h.title = title; return h; };
  const sticky = (i) => (i === 0 && columns[0] && columns[0].title === "№") ? "stick0"
    : (i === 1 && columns[0] && columns[0].title === "№" && columns[1].title === "Наименование позиции") ? "stick1" : "";
  // Колонка «Наши поставщики» (0.26.0) — сразу после закреплённых «№» и «Наименования».
  const byRow = new Map(((brands && brands.rows) || []).map((m) => [m.i, m]));
  const withBrands = byRow.size > 0;
  const brandAt = !withBrands ? -1 : sticky(1) ? 2 : sticky(0) ? 1 : 0;
  const brandTh = () => th("__brands", "Наши поставщики", "sb-col", "Поставщики из таблицы NocoDB «Поставщики», у которых есть бренд позиции");
  const headCells = columns.map((c, i) => th(c.title, c.title, sticky(i) || null));
  if (withBrands) headCells.splice(brandAt, 0, brandTh());
  const head = el("tr", {}, ...headCells);
  const body = el("tbody");
  rows.forEach((r, ri) => {
    const m = byRow.get(r._i !== undefined ? r._i : ri);
    const tr = el("tr", { class: m ? `sb-${m.level}${m.strong ? "" : " sb-weak"}` : null });
    columns.forEach((c, i) => {
      if (i === brandAt) tr.append(brandCell(m, brands));
      const v = r[c.title];
      const numeric = NUMERIC.has(c.uidt) || typeof v === "number";
      const cls = [sticky(i), numeric ? "num" : ""].filter(Boolean).join(" ") || null;
      // Высота строк (0.29.1): содержимое — в .cell, её обрезает класс таблицы rh-*; полный текст — подсказкой при
      // наведении и щелчком по ячейке (развернуть / свернуть).
      const full = cellText(v);
      tr.append(el("td", { class: cls, title: full.length > 30 ? full.slice(0, 3000) : null },
        el("div", { class: "cell" }, renderValue(v, c.uidt))));
    });
    if (brandAt >= columns.length) tr.append(brandCell(m, brands));
    body.append(tr);
  });
  table.replaceChildren(el("thead", {}, head), body);
  // Вторая закреплённая колонка начинается там, где кончается первая.
  const first = table.querySelector("th.stick0");
  if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
  if (!rows.length && (specData && specData.rows.length)) body.append(el("tr", {}, el("td", { colspan: String(headCells.length), class: "muted", text: "Нет позиций с такими фильтрами." })));
}

// ---------- наши поставщики по брендам позиций (0.26.0) ----------

const SB_LEVEL = { green: "✅", yellow: "🟡", grey: "⛔" };

function supStatus(s) {
  if (s.level === "grey") return "не работаем";
  return s.status ? s.status.replace(/^Надёжный\.\s*/i, "") : "статус не указан";
}

function supLine(s) {
  return `${SB_LEVEL[s.level] || ""} ${s.name} — ${supStatus(s)}` + (s.method ? ` · ${s.method}` : "") + (s.site ? ` · ${s.site}` : "");
}

// Ячейка: «Siemens → Говэй, Владимир, ещё 9»; подсказка — полный список; «?» — бренд найден только в наименовании.
function brandCell(m, brands) {
  const td = el("td", { class: "sb-col" });
  if (!m) return td;
  const sup = brands.suppliers || {};
  for (const b of m.brands) {
    const list = b.ids.map((id) => sup[id]).filter(Boolean);
    const ok = list.filter((s) => s.level !== "grey");
    const names = ok.slice(0, 2).map((s) => s.name).join(", ") + (ok.length > 2 ? `, ещё ${ok.length - 2}` : "");
    const tip = (b.strong ? "" : "Бренд найден только в наименовании позиции — проверьте.\n") +
      list.map(supLine).join("\n");
    td.append(el("div", { class: "sb-item" + (b.strong ? "" : " weak"), title: tip },
      el("b", { text: b.brand + (b.strong ? "" : "?") }), document.createTextNode(" → " + (names || "только «не работаем»"))));
  }
  return td;
}

// Блок «Кому писать» над спецификацией и строка в шапке конкурса.
function renderBrands(b) {
  // 0.30.0: вместо блока «Кому писать» (повторял «Запросы поставщикам») — одна строка со ссылкой на «Закупку».
  const box = $("spec-brands");
  $("head-brands").hidden = true;
  stg.brands = b && !b.error && (b.rows || []).length ? b.summary : null;
  renderStages();
  if (!b || (!b.error && !(b.rows || []).length)) { box.hidden = true; return; }
  if (b.error) {
    replaceSafe(box, el("div", { class: "muted small", text: "Наши поставщики по брендам: " + b.error }));
    box.hidden = false;
    return;
  }
  const s = b.summary;
  const green = s.positions_green ? ` (у ${s.positions_green} — поставщик «Работаем»)` : "";
  const link = el("a", { href: "#requests", text: "Запросы поставщикам →" });
  link.addEventListener("click", (e) => { e.preventDefault(); goTo("requests"); });
  const refresh = el("a", { href: "#", class: "muted", text: "↻", title: "Перечитать таблицу «Поставщики» из NocoDB" });
  refresh.addEventListener("click", (e) => { e.preventDefault(); loadSpec(true); });
  replaceSafe(box, document.createTextNode(`🏷 Наши бренды: ${s.brands.join(", ")} — ${s.positions_matched} из ${s.positions_total} поз.${green} · `),
    link, document.createTextNode(" "), refresh);
  box.hidden = false;
}

// ---------- почтовые ящики сотрудника (0.27.0) ----------

const mailState = { boxes: [], autoHosts: true };
const jsonPost = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function smallBtn(text, onClick, cls = "btn ghost small") {
  const b = el("button", { class: cls, type: "button", text });
  b.addEventListener("click", onClick);
  return b;
}

async function openMailboxes() {
  resetMailForm();
  $("mail-dlg").showModal();
  await loadMailboxes();
}

async function loadMailboxes() {
  try {
    const d = await api("/api/me/mailboxes");
    mailState.boxes = d.boxes || [];
    mailState.pollMinutes = d.poll_minutes || 5;
    $("mail-shared").textContent = d.shared_with ? `Ящики общие с учётной записью «${d.shared_with}» (админ-панель): ` +
      "те же ящики, ответы поставщиков и неразобранная почта; изменения видны обоим." : "";
    $("mail-shared").hidden = !d.shared_with;
    setStatus("mail-status", "");
  } catch (err) {
    setStatus("mail-status", "Не удалось загрузить ящики: " + err.message, true);
    return;
  }
  renderMailboxes();
  fillRqBoxes();
  loadInbox();
}

// ---------- ответы поставщиков (0.28.0) ----------

const IB_HOW = { reply: "ответ на наш запрос", tag: "метка запроса в теме", sender: "по отправителю",
  content: "по номеру конкурса в письме", manual: "выбрано вручную" };
const ibState = { letters: [], skipped: [], tenders: [], skippedDays: 14 };

function inboxLine(b) {
  if (!b.imap_host) return el("div", { class: "small muted", text: "почту не читаем — не указан сервер IMAP" });
  const i = b.inbox || {};
  if (i.poll_now) return el("div", { class: "small muted", text: "📥 проверка почты запрошена — в ближайшие секунды…" });
  if (!i.checked_at) return el("div", { class: "small muted", text: `📥 почта ещё не проверялась (сайт проверяет раз в ${mailState.pollMinutes || 5} мин)` });
  const when = new Date(i.checked_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
  return i.ok === 0
    ? el("div", { class: "small warn-text", text: `📥 почта не прочитана (${when}): ${i.error}` })
    : el("div", { class: "small ok-text", text: `📥 почта проверена ${when}` + (i.new_count ? ` · писем по делу: ${i.new_count}` : "") });
}

async function loadInbox() {
  try {
    const d = await api("/api/me/inbox");
    ibState.letters = d.letters || [];
    ibState.skipped = d.skipped || [];
    ibState.tenders = d.tenders || [];
    ibState.skippedDays = d.skipped_days || 14;
  } catch (err) {
    $("ib-list").replaceChildren(el("div", { class: "muted", text: "Не удалось загрузить почту: " + err.message }));
    return;
  }
  renderInbox();
  renderRqUnmatched();
}

function renderInbox() {
  $("ib-count").textContent = ibState.letters.length ? `· ${ibState.letters.length}` : "";
  if (!ibState.letters.length) $("ib-list").replaceChildren(el("div", { class: "muted small", text: "Неразобранных писем нет." }));
  else $("ib-list").replaceChildren(...ibState.letters.map(ibRow));
  // Пропущенные (0.28.1): письмо было, но не похоже на ответ поставщика — видно, почему, и можно отнести вручную.
  $("ib-skipped").hidden = !ibState.skipped.length;
  $("ib-skipped-title").textContent = `Пропущенные письма за ${ibState.skippedDays} дней · ${ibState.skipped.length}`;
  $("ib-skipped-list").replaceChildren(...ibState.skipped.map(ibRow));
}

function ibRow(L) {
  {
    const when = new Date(L.received_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
    const cands = new Set((L.candidates || []).map((c) => c.client_id + "|" + c.internal));
    const opts = [{ value: "", label: "— к какому конкурсу? —" },
      ...ibState.tenders.map((t) => ({ value: t.client_id + "|" + t.internal, label: (cands.has(t.client_id + "|" + t.internal) ? "★ " : "") + t.internal + (t.folder ? " · " + t.folder : "") }))];
    const sel = el("select");
    fillSelect(sel, opts, cands.size === 1 ? [...cands][0] : "");
    const go = smallBtn("Отнести к конкурсу", async () => {
      if (!sel.value) return toast("Выберите конкурс", true);
      const [client_id, internal] = sel.value.split("|");
      go.disabled = true;
      try {
        const r = await api(`/api/me/inbox/${L.id}/assign`, {}, jsonPost({ client_id, internal }));
        toast(r.result.status === "delivered" ? `Письмо отнесено к ${internal} — цены появятся в чате конкурса` : "Не получилось: " + r.result.error, r.result.status !== "delivered");
      } catch (err) { toast(err.message, true); }
      loadInbox();
      if (rq.d) loadRequests();
    }, "btn small");
    const skip = smallBtn("Не относится", async () => {
      try { await api(`/api/me/inbox/${L.id}/ignore`, {}, jsonPost({})); } catch (err) { toast(err.message, true); }
      loadInbox();
    });
    return el("div", { class: "ib-row" + (L.status === "error" ? " bad" : "") },
      el("div", {}, el("b", { text: L.from_name ? `${L.from_name} <${L.from_addr}>` : L.from_addr }),
        el("span", { class: "muted small", text: " · " + when })),
      el("div", { text: L.subject || "(без темы)" }),
      L.attachments.length ? el("div", { class: "muted small", text: "Вложения: " + L.attachments.join(", ") }) : null,
      L.preview ? el("div", { class: "ib-preview muted small", text: L.preview.slice(0, 300) }) : null,
      L.error ? el("div", { class: "small " + (L.status === "skipped" ? "muted" : "warn-text"), text: (L.status === "skipped" ? "" : "⚠ ") + L.error }) : null,
      cands.size > 1 ? el("div", { class: "small muted", text: "★ — по этим конкурсам вы писали этому отправителю" }) : null,
      el("div", { class: "ib-tools" }, sel, go, skip));
  }
}

function renderRqUnmatched() {
  const n = ibState.letters.length;
  const box = $("rq-unmatched");
  if (!n || !rq.d) { box.hidden = true; return; }
  const a = el("a", { href: "#", text: "разобрать" });
  a.addEventListener("click", (e) => { e.preventDefault(); openMailboxes(); });
  box.replaceChildren(document.createTextNode(`📬 Неразобранная почта: ${n} — письма поставщиков, которые сайт не смог отнести к конкурсу: `), a);
  box.hidden = false;
}

$("ib-poll").addEventListener("click", async () => {
  try {
    const r = await api("/api/me/inbox/poll", {}, jsonPost({}));
    toast(r.boxes ? "Почта будет проверена в ближайшие секунды" : "Нет ящиков с сервером IMAP", !r.boxes);
  } catch (err) { toast(err.message, true); return; }
  await loadMailboxes();
  setTimeout(loadMailboxes, 15000);
});

function boxState(b) {
  if (b.check_ok === null) return { cls: "muted", text: "не проверен" };
  return b.check_ok ? { cls: "ok-text", text: "✅ " + b.check_text } : { cls: "warn-text", text: "⚠ " + b.check_text };
}

function renderMailboxes() {
  const list = $("mail-list");
  if (!mailState.boxes.length) {
    list.replaceChildren(el("div", { class: "muted", text: "Ящиков пока нет — добавьте первый ниже." }));
    return;
  }
  list.replaceChildren(...mailState.boxes.map((b) => {
    const st = boxState(b);
    const servers = `SMTP ${b.smtp_host}:${b.smtp_port}` + (b.imap_host ? ` · IMAP ${b.imap_host}:${b.imap_port}` : " · IMAP не указан");
    return el("div", { class: "mb-row" + (b.check_ok === false ? " bad" : "") },
      el("div", { class: "mb-info" },
        el("div", {}, el("b", { text: b.address }), b.is_default ? el("span", { class: "badge ok", text: "основной" }) : null),
        el("div", { class: "muted small", text: (b.sender_name ? b.sender_name + " · " : "") + servers }),
        el("div", { class: "small " + st.cls, text: st.text }),
        inboxLine(b)),
      el("div", { class: "mb-tools" },
        smallBtn("Проверить", () => mailboxAction(b, "check")),
        b.is_default ? null : smallBtn("Сделать основным", () => mailboxAction(b, "default")),
        smallBtn("Изменить", () => editMailbox(b)),
        smallBtn("Удалить", () => mailboxAction(b, "delete"))));
  }));
}

async function mailboxAction(b, action) {
  if (action === "delete" && !confirm(`Удалить ящик ${b.address}? Отправленные с него запросы останутся в журналах.`)) return;
  setStatus("mail-status", action === "check" ? `Проверяю ${b.address}…` : "");
  try {
    const d = await api(`/api/me/mailboxes/${b.id}/${action}`, {}, jsonPost({}));
    if (action === "check") setStatus("mail-status", `${b.address}: ${d.check.text}`, !d.check.ok);
  } catch (err) {
    setStatus("mail-status", err.message, true);
  }
  await loadMailboxes();
}

function resetMailForm() {
  for (const id of ["mf-id", "mf-address", "mf-name", "mf-password", "mf-login", "mf-smtp", "mf-smtp-port", "mf-imap", "mf-imap-port", "mf-signature"]) $(id).value = "";
  $("mf-title").textContent = "Добавить ящик";
  $("mf-password").placeholder = "пароль приложения, не обычный пароль";
  $("mf-cancel").hidden = true;
  $("mf-preset").textContent = "";
  $("mf-more").open = false;
  mailState.autoHosts = true;
}

function editMailbox(b) {
  resetMailForm();
  $("mf-id").value = b.id;
  $("mf-address").value = b.address;
  $("mf-name").value = b.sender_name;
  $("mf-login").value = b.login;
  $("mf-smtp").value = b.smtp_host;
  $("mf-smtp-port").value = b.smtp_port;
  $("mf-imap").value = b.imap_host;
  $("mf-imap-port").value = b.imap_port;
  $("mf-signature").value = b.signature;
  $("mf-password").placeholder = "пусто — пароль не меняется";
  $("mf-title").textContent = "Изменить ящик " + b.address;
  $("mf-cancel").hidden = false;
  $("mf-more").open = true;
  mailState.autoHosts = false;
  $("mf-address").focus();
}

// Адрес ввели — серверы по домену (Яндекс, Mail.ru, Gmail…), если сотрудник их не менял.
async function presetForAddress() {
  const address = $("mf-address").value.trim();
  if (!address.includes("@")) { $("mf-preset").textContent = ""; return; }
  let p;
  try { p = await api("/api/me/mailboxes/preset", { address }); } catch (_) { return; }
  if (p.known) {
    if (mailState.autoHosts || !$("mf-smtp").value) {
      $("mf-smtp").value = p.smtp_host; $("mf-smtp-port").value = p.smtp_port;
      $("mf-imap").value = p.imap_host; $("mf-imap-port").value = p.imap_port;
    }
    $("mf-preset").textContent = `Серверы определены по адресу: ${p.smtp_host} и ${p.imap_host}.`;
  } else {
    $("mf-preset").textContent = "Домен незнакомый — укажите серверы в «Серверы, логин и подпись». Почта компании на Яндекс 360 — " +
      "smtp.yandex.ru (465) и imap.yandex.ru (993).";
    $("mf-more").open = true;
  }
}

async function saveMailbox(e) {
  e.preventDefault();
  const body = {
    id: $("mf-id").value ? Number($("mf-id").value) : undefined,
    address: $("mf-address").value.trim(), sender_name: $("mf-name").value.trim(), password: $("mf-password").value,
    login: $("mf-login").value.trim(), smtp_host: $("mf-smtp").value.trim(), smtp_port: $("mf-smtp-port").value.trim(),
    imap_host: $("mf-imap").value.trim(), imap_port: $("mf-imap-port").value.trim(), signature: $("mf-signature").value,
  };
  $("mf-save").disabled = true;
  setStatus("mail-status", "Сохраняю и проверяю вход в ящик…");
  try {
    const d = await api("/api/me/mailboxes", {}, jsonPost(body));
    setStatus("mail-status", `${d.box.address}: ${d.check.text}`, !d.check.ok);
    resetMailForm();
  } catch (err) {
    setStatus("mail-status", err.message, true);
  } finally {
    $("mf-save").disabled = false;
    $("mf-password").value = "";
  }
  await loadMailboxes();
}

$("me-mail").addEventListener("click", openMailboxes);
$("rq-boxes").addEventListener("click", openMailboxes);
$("mail-close").addEventListener("click", () => $("mail-dlg").close());

// ---------- база поставщиков (0.34.0) ----------
// Таблица «Поставщики» клиента из NocoDB: поиск, добавление, правка, «Не работаем» / вернуть, удаление насовсем.

const sup = { d: null, edit: null, confirm: false, limit: 200 };

function supStatusClass(s) { return s.off ? "grey" : (s.level || "yellow"); }

function supText(s) {
  return [s["Название"], s["Бренды"], s["E-mail"], s["ИНН"], s["Город"], s["Телефон"], s["Контактное лицо"],
          s["Категория товаров"], s["Сайт"], (s.makers || []).join(" ")].join(" ").toLowerCase().replace(/ё/g, "е");
}

async function openSuppliers() {
  if (!state.client) { toast("Сначала выберите клиента", true); return; }
  const c = state.clients.find((x) => x.id === state.client);
  $("sup-client").textContent = c ? "· " + c.name : "";
  $("sup-form").hidden = true;
  if (!$("sup-dlg").open) $("sup-dlg").showModal();
  // Список (у «Нашей» ≈ 1000 поставщиков) грузится раз на клиента; «↻ из NocoDB» — заново.
  if (!sup.d || sup.client !== state.client) await loadSuppliers(false);
  else renderSuppliers();
  $("sup-search").focus();
}

async function loadSuppliers(fresh) {
  setStatus("sup-status", fresh ? "Читаю таблицу из NocoDB…" : "Загрузка…");
  try {
    sup.d = await api(clientPath("/suppliers"), fresh ? { fresh: "1" } : {});
    sup.client = state.client;
  } catch (err) {
    setStatus("sup-status", "Не удалось загрузить: " + err.message, true);
    return;
  }
  setStatus("sup-status", sup.d.error || "", Boolean(sup.d.error));
  $("sup-noco").hidden = !sup.d.link;
  if (sup.d.link) $("sup-noco").href = sup.d.link;
  renderSuppliers();
}

function renderSuppliers() {
  const d = sup.d;
  if (!d) return;
  const q = $("sup-search").value.trim().toLowerCase().replace(/ё/g, "е");
  const showOff = $("sup-show-off").checked;
  const all = d.suppliers || [];
  const words = q.split(/\s+/).filter(Boolean);
  const list = all.filter((s) => (showOff || !s.off) && words.every((w) => supText(s).includes(w)));
  const offCount = all.filter((s) => s.off).length;
  const shown = list.slice(0, sup.limit);
  $("sup-list").replaceChildren(...shown.map(supRow));
  if (!list.length) $("sup-list").append(el("tr", {}, el("td", { colspan: "5", class: "muted", text: all.length ? "Ничего не найдено." : "Поставщиков пока нет." })));
  $("sup-more").textContent = `Всего ${all.length}` + (offCount ? `, из них «Не работаем» — ${offCount}` + (showOff ? "" : " (скрыты)") : "") +
    (list.length > shown.length ? `. Показаны первые ${shown.length} из ${list.length} — уточните поиск.` : "");
}

function supRow(s) {
  const name = el("td");
  name.append(el("div", { class: "sup-name", text: s["Название"] || "—" }));
  const sub = [s["Город"], s["Страна"], s["ИНН"] ? "ИНН " + s["ИНН"] : ""].filter(Boolean).join(" · ");
  if (sub) name.append(el("div", { class: "sup-sub", text: sub }));
  if (s["Сайт"]) name.append(el("a", { class: "sup-sub", href: s["Сайт"], target: "_blank", rel: "noopener", text: s["Сайт"].replace(/^https?:\/\//, "").slice(0, 50) }));
  const brands = [s["Бренды"], ...(s.makers || [])].filter(Boolean).join(", ");
  const contacts = el("td");
  for (const line of [s["E-mail"], s["Телефон"], s["Контактное лицо"]].filter(Boolean)) contacts.append(el("div", { text: line }));
  const st = el("td");
  st.append(el("span", { class: "sup-st " + supStatusClass(s), text: s["Надёжность: статус"] || "статус не указан" }));
  if (s["Способ расчёта"]) st.append(el("div", { class: "sup-sub", text: s["Способ расчёта"] }));
  const act = el("td", { class: "sup-act" });
  act.append(el("button", { class: "btn ghost small need-purchase", type: "button", "data-act": "edit", title: "Изменить", text: "✎" }));
  act.append(el("button", { class: "btn ghost small need-purchase", type: "button", "data-act": s.off ? "on" : "off",
    title: s.off ? "Снять «Не работаем»: статус «Требуется проверка»" : "Пометить «Не работаем»: запись остаётся, в письма не попадает",
    text: s.off ? "Вернуть" : "Не работаем" }));
  act.append(el("button", { class: "btn ghost small need-offers", type: "button", "data-act": "del", title: "Удалить из NocoDB насовсем", text: "🗑" }));
  const tr = el("tr", { class: s.off ? "off" : "", "data-id": s.id }, name, el("td", { text: brands.length > 160 ? brands.slice(0, 158) + "…" : brands }), contacts, st, act);
  return tr;
}

function supForm(s) {
  sup.edit = s ? s.id : null;
  sup.confirm = false;
  $("sup-dups").hidden = true;
  $("sup-form-title").textContent = s ? "Изменить: " + (s["Название"] || "") : "Новый поставщик";
  const box = $("sup-fields");
  box.replaceChildren();
  for (const f of sup.d.fields || []) {
    const v = s ? s[f.name] || "" : "";
    let input;
    if (f.type === "SingleSelect") {
      input = el("select", { "data-f": f.name });
      input.append(el("option", { value: "", text: "—" }));
      for (const o of f.options) input.append(el("option", { value: o, text: o }));
      if (v && !f.options.includes(v)) input.append(el("option", { value: v, text: v }));
      input.value = v;
    } else if (f.long) {
      input = el("textarea", { "data-f": f.name, rows: "2" });
      input.value = v;
    } else {
      input = el("input", { "data-f": f.name, type: "text", required: f.required ? "required" : null,
        placeholder: f.name === "E-mail" ? "sales@firma.ru, можно несколько через запятую" : f.name === "Бренды" ? "через запятую: Siemens, ABB" : "" });
      input.value = v;
    }
    const lab = el("label", { class: "field" + (f.long ? " long" : "") }, el("span", { class: "label", text: f.name + (f.required ? " *" : "") }), input);
    box.append(lab);
  }
  $("sup-form").hidden = false;
  $("sup-save").disabled = false;
  $("sup-save").textContent = "Сохранить в NocoDB";
  const first = box.querySelector("[data-f]");
  if (first) first.focus();
  $("sup-form").scrollIntoView({ block: "nearest" });
}

function supValues() {
  const out = {};
  for (const x of $("sup-fields").querySelectorAll("[data-f]")) out[x.dataset.f] = x.value;
  if (sup.edit) {
    const s = sup.d.suppliers.find((x) => x.id === sup.edit) || {};
    for (const k of Object.keys(out)) if ((s[k] || "") === out[k]) delete out[k];   // только изменённое
  }
  return out;
}

function supReplace(s) {
  const list = sup.d.suppliers;
  const i = list.findIndex((x) => x.id === s.id);
  if (i >= 0) list[i] = s; else list.unshift(s);
}

$("me-suppliers").addEventListener("click", openSuppliers);
$("rq-suppliers").addEventListener("click", openSuppliers);
$("sup-close").addEventListener("click", () => $("sup-dlg").close());
$("sup-reload").addEventListener("click", () => loadSuppliers(true));
$("sup-search").addEventListener("input", renderSuppliers);
$("sup-show-off").addEventListener("change", renderSuppliers);
$("sup-add").addEventListener("click", () => { if (sup.d && (sup.d.fields || []).length) supForm(null); });
$("sup-cancel").addEventListener("click", () => { $("sup-form").hidden = true; });

$("sup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const values = supValues();
  if (sup.edit && !Object.keys(values).length) { $("sup-form").hidden = true; return; }
  $("sup-save").disabled = true;
  setStatus("sup-status", "Сохраняю в NocoDB…");
  let r;
  try {
    r = await api(clientPath(sup.edit ? `/suppliers/${sup.edit}` : "/suppliers"), {}, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values, confirm: sup.confirm }),
    });
  } catch (err) {
    setStatus("sup-status", "Не сохранено: " + err.message, true);
    $("sup-save").disabled = false;
    return;
  }
  $("sup-save").disabled = false;
  if (r.need_confirm) {
    sup.confirm = true;
    const box = $("sup-dups");
    box.replaceChildren(el("b", { text: "Похоже, такой поставщик уже есть: " }),
      ...r.duplicates.map((x, i) => el("span", { text: (i ? "; " : "") + `${x.name} (совпадает: ${x.why})` })),
      el("div", { text: "Если это другой поставщик — нажмите «Всё равно добавить»." }));
    box.hidden = false;
    $("sup-save").textContent = "Всё равно добавить";
    setStatus("sup-status", "");
    return;
  }
  supReplace(r.supplier);
  $("sup-form").hidden = true;
  setStatus("sup-status", (sup.edit ? "Изменения сохранены: " : "Добавлен: ") + (r.supplier["Название"] || ""));
  if (!sup.edit) $("sup-search").value = r.supplier["Название"] || "";
  renderSuppliers();
});

$("sup-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const id = b.closest("tr").dataset.id;
  const s = sup.d.suppliers.find((x) => x.id === id);
  if (!s) return;
  const act = b.dataset.act;
  if (act === "edit") {
    if (s.cut && s.cut.length) {          // длинные поля в списке обрезаны — для правки взять запись целиком
      try { supReplace((await api(clientPath(`/suppliers/${id}`))).supplier); } catch (err) {
        setStatus("sup-status", "Не удалось открыть запись: " + err.message, true);
        return;
      }
    }
    supForm(sup.d.suppliers.find((x) => x.id === id));
    return;
  }
  if (act === "del" && !confirm(`Удалить «${s["Название"]}» из таблицы «Поставщики» NocoDB насовсем?\n\nВернуть запись будет нельзя. ` +
      "Если поставщик просто не нужен в письмах — лучше «Не работаем».")) return;
  b.disabled = true;
  setStatus("sup-status", "Сохраняю в NocoDB…");
  try {
    if (act === "del") {
      await api(clientPath(`/suppliers/${id}/delete`), {}, { method: "POST" });
      sup.d.suppliers = sup.d.suppliers.filter((x) => x.id !== id);
      setStatus("sup-status", `Удалён: ${s["Название"]}`);
    } else {
      const r = await api(clientPath(`/suppliers/${id}/status`), {}, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ off: act === "off" }),
      });
      supReplace(r.supplier);
      setStatus("sup-status", (act === "off" ? "«Не работаем»: " : "Возвращён: ") + s["Название"]);
    }
  } catch (err) {
    setStatus("sup-status", "Не получилось: " + err.message, true);
    b.disabled = false;
    return;
  }
  renderSuppliers();
});

// ---------- смена внутреннего номера конкурса (0.34.0) ----------

function renameFolderPreview(folder, number) {
  if (!folder) return "папки нет";
  const m = folder.match(/^\s*(?:[КK]\s*-?\s*\d{1,5}|\d{3,4})(?=\D|$)/i);
  const rest = m ? folder.slice(m[0].length) : " " + folder;
  return `«${folder}» → «${(number || "К…") + rest}»`;
}

function normNumber(text) {
  const m = String(text || "").match(/^\s*(?:[КKкk]\s*-?\s*(\d{1,5})|(\d{3,4}))\s*$/);
  return m ? "К" + Number(m[1] || m[2]) : "";
}

function openRename() {
  const t = currentTender();
  if (!t) return;
  $("rn-old").textContent = t.internal || "без номера";
  $("rn-new").value = "";
  $("rn-folder").textContent = renameFolderPreview(t.folder, "");
  setStatus("rn-status", "");
  $("rn-go").disabled = false;
  $("rn-dlg").showModal();
  $("rn-new").focus();
}

$("head-rename").addEventListener("click", openRename);
$("rn-close").addEventListener("click", () => $("rn-dlg").close());
$("rn-new").addEventListener("input", () => {
  const t = currentTender();
  $("rn-folder").textContent = renameFolderPreview(t && t.folder, normNumber($("rn-new").value));
});
$("rn-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const t = currentTender();
  const number = normNumber($("rn-new").value);
  if (!t) return;
  if (!number) { setStatus("rn-status", "Номер — буква К и цифры, например К950.", true); return; }
  $("rn-go").disabled = true;
  setStatus("rn-status", "Меняю номер: папка, NocoDB, база сайта…");
  let r;
  try {
    r = await api(clientPath("/rename"), {}, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tender: state.tender, number }),
    });
  } catch (err) {
    setStatus("rn-status", "Не получилось: " + err.message, true);
    $("rn-go").disabled = false;
    return;
  }
  $("rn-dlg").close();
  toast(`Номер изменён: ${r.old} → ${r.new}` + (r.warnings && r.warnings.length ? ". " + r.warnings.join("; ") : ""));
  state.tender = r.tender || state.tender;
  state.opened = "";
  state.fresh = true;
  saveUrl();
  await loadTenders();
});
$("mail-dlg").addEventListener("close", () => { $("mf-password").value = ""; });
$("mf-cancel").addEventListener("click", resetMailForm);
$("mf-address").addEventListener("change", presetForAddress);
for (const id of ["mf-smtp", "mf-imap"]) $(id).addEventListener("input", () => { mailState.autoHosts = false; });
$("mail-form").addEventListener("submit", saveMailbox);

// ---------- запросы цен поставщикам (0.27.0) ----------

const rq = { d: null, for: "", threads: [], sending: false, showAll: false, brands: new Set(), brandsAll: false };
const RQ_SHOW = 12;
const RQ_LEVEL = { green: "✅", yellow: "🟡", grey: "⛔" };
const isEmail = (v) => /^[^@\s<>,;"']+@[^@\s<>,;"']+\.[^@\s<>,;"']+$/.test(v);
const addrList = (v) => String(v || "").split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);

// Вкладка «Письма» (0.35.0): черновики писем и таблица позиций грузятся, только когда вкладка открыта (или «одной
// страницей» / вид «по позициям»); до этого — короткая сводка loadMailSummary (база сайта, без NocoDB).
// С 0.43.0 письма — в «Закупке» под таблицей: полные данные грузятся, когда блок показался на экране или выбрана его
// вкладка (таблица предложений открывается сразу, письма её не задерживают).
function requestsWanted() {
  return currentTab() === "purchase" && rqInView;
}
let rqInView = false;
const RQ_SUBS = ["threads", "pos", "new", "sent"];
function rqSub(sub, remember = true) {
  if (!RQ_SUBS.includes(sub)) sub = "threads";
  $("requests").dataset.sub = sub;
  for (const b of document.querySelectorAll("#rq-subtabs .rq-sub")) b.classList.toggle("active", b.dataset.sub === sub);
  if (sub === "new") $("rq-new").open = true;
  if (sub === "threads") $("rq-inbox-box").open = true;
  if (sub === "sent") $("rq-journal-box").open = true;
  if (remember) storageSet("sb.rq.sub", sub);
  rqInView = true;
  ensureRequests();
  rqSubSync();
}
// Счётчики на вкладках писем и строка «пусто», когда в выбранной вкладке ничего нет.
function rqSubSync() {
  const threads = (rq.d && rq.d.threads) || rq.threads || [];
  const wait = threads.filter((t) => t.waiting).length;
  const tc = $("rq-sub-threads");
  tc.textContent = threads.length ? `· ${threads.length}` + (wait ? ` · ждут ответа ${wait}` : "") : "";
  tc.classList.toggle("wait", Boolean(wait));
  const journal = (rq.d && rq.d.journal) || [];
  const sent = journal.filter((j) => j.status === "sent").length || (stg.req && stg.req.sent) || 0;
  $("rq-sub-sent").textContent = sent ? `· ${sent}` : "";
  const sub = $("requests").dataset.sub;
  let empty = "";
  if (rq.d) {
    if (sub === "threads" && $("rq-threads-box").hidden && $("rq-inbox-box").hidden && $("rq-unmatched").hidden)
      empty = "Ответов поставщиков пока нет. Запросы цен — во вкладке «Новый запрос цен».";
    if (sub === "sent" && $("rq-journal-box").hidden) empty = "Запросов цен по этому конкурсу ещё не отправляли.";
  }
  $("rq-sub-empty").textContent = empty;
  $("rq-sub-empty").hidden = !empty;
}
$("rq-subtabs").addEventListener("click", (e) => {
  const b = e.target.closest(".rq-sub");
  if (b) rqSub(b.dataset.sub);
});
(function rqWatch() {
  const io = new IntersectionObserver((list) => {
    rqInView = rqInView || list.some((x) => x.isIntersecting);
    if (rqInView) ensureRequests();
  }, { rootMargin: "200px" });
  io.observe($("requests"));
  // после загрузки всего скрипта (rqSubSync читает stg, объявленный ниже)
  setTimeout(() => { const v = rqInView; rqSub(storageGet("sb.rq.sub") || "threads", false); rqInView = v; }, 0);
})();
function ensureRequests() {
  const t = currentTender();
  if (!t || t.source !== "nocodb" || blockOff("prices") || !requestsWanted()) return;
  if (rq.for === state.tender) return;
  loadRequests();
}

function setMailBadge(n) {
  const b = $("mail-badge");
  if (!b) return;
  b.textContent = n ? String(n) : "";
  b.title = n ? `Ждут ответа: ${n}` : "";
  b.hidden = !n;
}

async function loadMailSummary() {
  const t = currentTender();
  if (!t || t.source !== "nocodb" || blockOff("prices") || !t.internal) return;
  const id = state.tender;
  let d;
  try { d = await api(clientPath("/requests/summary"), { internal: t.internal }); } catch (_) { return; }
  if (state.tender !== id || rq.for === id) return;       // вкладку уже открыли — там данные полнее
  rq.threads = d.threads || [];
  stg.req = { sent: d.sent, answered: d.answered, letters: d.letters, waiting: d.waiting };
  setMailBadge(d.waiting);
  renderStages();
  rqSubSync();
}

async function loadRequests() {
  const my = ++seq.rq;
  if (blockOff("prices")) return;
  const t = currentTender();
  if (!t || t.source !== "nocodb") return;
  rq.for = state.tender;
  setStatus("rq-status", "Готовлю письма по позициям спецификации…");
  for (const id of ["rq-tools", "rq-actions", "rq-journal-box", "rq-inbox-box", "rq-hint", "rq-threads-box", "rq-pos-box"]) $(id).hidden = true;
  $("rq-letters").replaceChildren();
  $("rq-count").textContent = "";
  let d;
  try {
    d = await api(clientPath(`/tenders/${state.tender}/requests`), { folder: t.folder || "" });
  } catch (err) {
    if (my === seq.rq) {
      setStatus("rq-status", "Не удалось подготовить письма: " + err.message, true);
      rq.for = "";                        // при следующем открытии вкладки — ещё раз
    }
    return;
  }
  if (my !== seq.rq) return;
  rq.d = d;
  rq.showAll = false;
  rq.brands = new Set();
  rq.brandsAll = false;
  for (const L of d.letters) { L.dirty = false; L.result = null; }
  mailState.boxes = d.boxes || [];
  const sentTo = new Set(d.journal.filter((j) => j.status === "sent").map((j) => j.supplier_id || j.to_addr));
  const answered = new Set(d.inbox.filter((x) => x.request_id).map((x) => x.request_id));
  const threads = d.threads || [];
  stg.req = { sent: sentTo.size, answered: threads.length || d.journal.filter((j) => answered.has(j.id)).length,
    letters: d.inbox.length, waiting: threads.filter((x) => x.waiting).length };
  rq.threads = threads;
  setMailBadge(stg.req.waiting);
  renderStages();
  renderRequests();
  if (!storageGet("sb.rq.sub")) rqSub(threads.length ? "threads" : "pos", false);
  rqSubSync();
  if (plData) renderSupplierDocs();      // «Письма без цен» — ссылки на переписку нужны нити из запросов
  if (d.can_send) loadInbox();
}

function fillRqBoxes() {
  const sel = $("rq-box");
  const keep = sel.value;
  const boxes = mailState.boxes;
  if (!boxes.length) {
    fillSelect(sel, [{ value: "", label: "— ящиков нет —" }]);
  } else {
    const def = boxes.find((b) => b.is_default) || boxes[0];
    fillSelect(sel, boxes.map((b) => ({ value: String(b.id), label: b.address + (b.check_ok === false ? " ⚠" : "") })),
      boxes.some((b) => String(b.id) === keep) ? keep : String(def.id));
  }
  if (rq.d) renderRqHint();
}

function renderRqHint() {
  const h = $("rq-hint");
  const parts = [];
  if (!mailState.boxes.length && rq.d.can_send) parts.push("Чтобы отправлять запросы, добавьте свой почтовый ящик: «✉ Мои ящики».");
  if (rq.d.brands_error) parts.push("Поставщики по брендам: " + rq.d.brands_error + ".");
  h.textContent = parts.join(" ");
  h.hidden = !parts.length;
}

function rqBody(L) {
  const by = new Map(rq.d.positions.map((p) => [p.n, p]));
  const lines = L.positions.map((n) => by.get(n)).filter(Boolean).map((p) => p.line).join("\n");
  return rq.d.intro + lines + ($("rq-xlsx").checked ? rq.d.outro_xlsx : rq.d.outro_plain);
}

function renderRequests() {
  const d = rq.d;
  fillRqBoxes();
  if (!d.has_spec) {
    setStatus("rq-status", "У конкурса нет спецификации в NocoDB — позиции для запроса брать неоткуда.");
    renderJournal();
    return;
  }
  renderRqBrands();
  const sup = d.letters.filter((L) => L.supplier_id && rqVisible(L));
  const allSup = d.letters.filter((L) => L.supplier_id).length;
  setStatus("rq-status", allSup ? "" : "По брендам позиций наших поставщиков не нашлось — можно отправить запрос любому адресату (внизу списка).");
  $("rq-count").textContent = !allSup ? "" : rq.brands.size
    ? `· ${[...rq.brands].join(", ")}: ${sup.length} поставщ., ${rqBrandPositions().length} поз.`
    : `· ${allSup} поставщ. по брендам`;
  const can = d.can_send && !document.body.classList.contains("no-purchase");
  $("rq-tools").hidden = !can || !sup.length;
  $("rq-actions").hidden = !can;
  const checked = d.letters.filter((L) => L.checked && rqVisible(L)).length;
  $("rq-new-info").textContent = (allSup ? `· наших поставщиков по брендам: ${allSup}` : "· письмо любому адресату") +
    (checked ? ` · отмечено писем: ${checked}` : "");
  renderRqPos();
  if (!$("rq-new").open) {                // черновики не рисуем, пока блок свёрнут (их бывает больше сотни)
    replaceSafe($("rq-letters"));
    updateSendButton();
    renderJournal();
    return;
  }
  const shown = rq.showAll ? [...sup, ...d.letters.filter((L) => !L.supplier_id)] : [...sup.slice(0, RQ_SHOW), ...d.letters.filter((L) => !L.supplier_id)];
  const nodes = shown.map((L) => letterCard(L, can));
  if (sup.length > RQ_SHOW) {
    // «Показать всех поставщиков» / «Скрыть» (0.29.2); отмеченные письма при скрытии не теряются — отправятся.
    const more = rq.showAll
      ? smallBtn(`Скрыть (оставить первых ${RQ_SHOW})`, () => { rq.showAll = false; renderRequests(); $("rq-new").scrollIntoView({ block: "start" }); })
      : smallBtn(`Показать всех поставщиков (${sup.length})` + ((n) => n ? ` · отмечено среди скрытых: ${n}` : "")(
          sup.slice(RQ_SHOW).filter((L) => L.checked).length), () => { rq.showAll = true; renderRequests(); });
    nodes.splice(rq.showAll ? sup.length : RQ_SHOW, 0, more);
  }
  replaceSafe($("rq-letters"), ...nodes);
  updateSendButton();
  renderJournal();
}

function letterCard(L, can) {
  const card = el("div", { class: `rq-card lvl-${L.level || "none"}` + (L.checked ? " on" : "") });
  const check = el("input", { type: "checkbox" });
  check.checked = L.checked;
  check.disabled = !can;
  check.addEventListener("change", () => { L.checked = check.checked; card.classList.toggle("on", L.checked); updateSendButton(); });
  const title = L.supplier_id
    ? el("span", {}, el("b", { text: `${RQ_LEVEL[L.level] || ""} ${L.supplier_name}` }),
        document.createTextNode(` — ${L.level === "grey" ? "не работаем" : (L.status || "статус не указан").replace(/^Надёжный\.\s*/i, "")}` +
          (L.method ? ` · ${L.method}` : "")))
    : el("b", { text: rq.brands && rq.brands.size ? `Другому адресату (позиции брендов: ${[...rq.brands].join(", ")})` : "Другому адресату (все позиции)" });
  const pos = L.positions.length > 15 ? L.positions.slice(0, 15).join(", ") + ` и ещё ${L.positions.length - 15}` : L.positions.join(", ");
  const meta = el("div", { class: "muted small" },
    document.createTextNode(`поз. ${pos}` + (L.brands.length ? ` · ${L.brands.join(", ")}` : "") + (L.strong ? "" : " · бренд только в наименовании")),
    L.sent_at ? el("span", { class: "badge ok", text: "запрос отправляли " + new Date(L.sent_at * 1000).toLocaleDateString("ru-RU") }) : null);
  const to = el("input", { type: "text", value: L.to, placeholder: L.supplier_id ? "адрес почты поставщика" : "адрес почты (несколько — через запятую)" });
  to.disabled = !can;
  to.addEventListener("change", () => onAddressChange(L, to, check, card));
  const head = el("label", { class: "rq-head" }, check, el("div", { class: "rq-title" }, title, meta));
  const result = el("div", { class: "rq-result small" });
  if (L.result) {
    result.className = "rq-result small " + (L.result.ok ? "ok-text" : "warn-text");
    result.textContent = L.result.ok ? `✅ отправлено (${L.result.tag})` + (L.result.sent_copy ? ` · ${L.result.sent_copy}` : "")
      : "⚠ " + L.result.error;
  }
  // Почты из NocoDB (0.31.2): если в поле другой адрес — показать их и дать подставить; несколько — письмо уйдёт на все.
  const baseList = L.emails || [];
  const same = baseList.length && addrList(L.to).map((a) => a.toLowerCase()).sort().join() === baseList.map((a) => a.toLowerCase()).sort().join();
  let hint = null;
  if (baseList.length && !same) {
    const use = el("a", { href: "#", text: "подставить" });
    use.addEventListener("click", (e) => { e.preventDefault(); to.value = baseList.join(", "); onAddressChange(L, to, check, card).then(renderRequests); });
    hint = el("div", { class: "muted small" }, document.createTextNode(`в базе: ${baseList.join(", ")} · `), use);
  } else if (baseList.length > 1) {
    hint = el("div", { class: "muted small", text: `в базе ${baseList.length} адреса — письмо уйдёт на все; лишние можно убрать` });
  }
  card.append(head, el("div", { class: "rq-to" }, el("span", { class: "label", text: "Кому" }), to), ...(hint ? [hint] : []), result,
    letterDetails(L, can));
  return card;
}

function letterDetails(L, can) {
  const det = el("details", { class: "rq-text" }, el("summary", { text: "Текст письма и позиции" }));
  const subject = el("input", { type: "text", value: L.subject });
  subject.disabled = !can;
  subject.addEventListener("input", () => { L.subject = subject.value; });
  const body = el("textarea", { rows: "12" });
  body.value = L.dirty ? L.body : (L.body = rqBody(L));
  body.disabled = !can;
  body.addEventListener("input", () => { L.body = body.value; L.dirty = true; note.hidden = false; });
  const note = el("div", { class: "muted small", text: "Текст изменён вручную — при смене позиций список в тексте поправьте сами." });
  note.hidden = !L.dirty;
  const all = new Set(L.positions);
  const choices = (L.supplier_id ? L.positions : rq.d.positions.map((p) => p.n)).map((n) => {
    const p = rq.d.positions.find((x) => x.n === n);
    const box = el("input", { type: "checkbox" });
    box.checked = all.has(n);
    box.disabled = !can;
    box.addEventListener("change", () => {
      if (box.checked) all.add(n); else all.delete(n);
      L.positions = rq.d.positions.map((x) => x.n).filter((x) => all.has(x));
      if (!L.dirty) body.value = L.body = rqBody(L);
    });
    return el("label", { class: "rq-pos small" }, box, document.createTextNode(" " + (p ? p.line : n)));
  });
  det.append(el("label", { class: "field" }, el("span", { class: "label", text: "Тема (метка [SB-…] для ответов добавится при отправке)" }), subject),
    el("label", { class: "field" }, el("span", { class: "label", text: "Текст" }), body), note,
    el("div", { class: "label", text: "Позиции в письме и во вложении" }), el("div", { class: "rq-poslist" }, ...choices));
  return det;
}

async function onAddressChange(L, input, check, card) {
  L.to = input.value.trim();
  const ok = addrList(L.to).length > 0 && addrList(L.to).every(isEmail);
  input.classList.toggle("bad", Boolean(L.to) && !ok);
  if (ok && !L.checked) { L.checked = true; check.checked = true; card.classList.add("on"); }
  updateSendButton();
  if (!ok || !L.supplier_id) return;
  try {
    await api(clientPath(`/tenders/${state.tender}/requests/email`), {}, jsonPost({ supplier_id: L.supplier_id, supplier_name: L.supplier_name, email: L.to }));
    toast(`Адрес ${L.supplier_name} сохранён`);
  } catch (err) {
    toast("Адрес не сохранён: " + err.message, true);
  }
}

function updateSendButton() {
  if (!rq.d) return;
  const n = rq.d.letters.filter((L) => L.checked && rqVisible(L)).length;
  $("rq-send").textContent = n ? `Отправить выбранные (${n})` : "Отправить выбранные";
  $("rq-send").disabled = rq.sending || !n || !$("rq-box").value;
}

async function sendRequests() {
  const d = rq.d;
  const chosen = d.letters.filter((L) => L.checked && rqVisible(L));
  const box = mailState.boxes.find((b) => String(b.id) === $("rq-box").value);
  if (!box) return toast("Выберите почтовый ящик (кнопка «✉ Мои ящики»)", true);
  const bad = chosen.filter((L) => !addrList(L.to).length || !addrList(L.to).every(isEmail));
  if (bad.length) return toast("Нет адреса или адрес неверный: " + bad.map((L) => L.supplier_name || "другой адресат").join(", "), true);
  const empty = chosen.filter((L) => !L.positions.length);
  if (empty.length) return toast("В письме нет позиций: " + empty.map((L) => L.supplier_name || "другой адресат").join(", "), true);
  if (!confirm(`Отправить ${chosen.length} писем с ящика ${box.address}?`)) return;
  rq.sending = true;
  updateSendButton();
  setStatus("rq-status", `Отправляю ${chosen.length} писем (по одному в секунду)…`);
  try {
    const t = currentTender();
    const r = await api(clientPath(`/tenders/${state.tender}/requests/send`), { folder: (t && t.folder) || "" }, jsonPost({
      box_id: box.id, with_xlsx: $("rq-xlsx").checked,
      letters: chosen.map((L) => ({ key: L.key, supplier_id: L.supplier_id, supplier_name: L.supplier_name, to: L.to,
        subject: L.subject, body: L.dirty ? L.body : rqBody(L), positions: L.positions })),
    }));
    const byKey = new Map(r.results.map((x) => [x.key, x]));
    let ok = 0;
    for (const L of d.letters) {
      const x = byKey.get(L.key);
      if (!x) continue;
      L.result = x;
      if (x.ok) { ok++; L.checked = false; L.sent_at = Date.now() / 1000; }
    }
    d.journal = r.journal;
    setStatus("rq-status", `Отправлено ${ok} из ${chosen.length}.` + (ok < chosen.length ? " Причины — в карточках писем." : ""), ok < chosen.length);
  } catch (err) {
    setStatus("rq-status", "Не отправлено: " + err.message, true);
  } finally {
    rq.sending = false;
    renderRequests();
  }
}

function renderInboxTable() {
  const rows = (rq.d && rq.d.inbox) || [];
  $("rq-inbox-box").hidden = !rows.length;
  $("rq-ih").textContent = `Ответы поставщиков (${rows.length})`;
  if (!rows.length) return;
  const head = el("tr", {}, ...["Получено", "От кого", "Тема", "Как отнесено", "Файлы — в разбор цен", "Итог"].map((x) => el("th", { text: x })));
  const body = rows.map((r) => el("tr", {},
    el("td", { text: new Date(r.received_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) }),
    el("td", {}, thOpenBtn(thKeyOfInbox(r), r.from_name ? `${r.from_name} <${r.from_addr}>` : r.from_addr)),
    el("td", { text: r.subject }),
    el("td", { text: IB_HOW[r.how] || r.how }),
    el("td", { text: r.files.map((f) => f.split("/").pop()).join(", ") || "—" }),
    el("td", { class: r.status === "error" ? "warn-text" : "ok-text",
      text: r.status === "delivered" ? "✅ в чате конкурса" + (r.error ? " · " + r.error : "") : r.status === "processing" ? "разбирается…" : "⚠ " + r.error })));
  $("rq-inbox").replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
}

function renderJournal() {
  renderThreads();
  rqSubSync();
  renderInboxTable();
  const j = (rq.d && rq.d.journal) || [];
  $("rq-journal-box").hidden = !j.length;
  $("rq-jh").textContent = `Отправленные запросы (${j.length})`;
  if (!j.length) return;
  const answers = new Map();
  for (const a of (rq.d && rq.d.inbox) || []) if (a.request_id && !answers.has(a.request_id)) answers.set(a.request_id, a);
  const head = el("tr", {}, ...["Когда", "С ящика", "Кому", "Поставщик", "Позиции", "Итог", "Ответ", "Метка"].map((x) => el("th", { text: x })));
  const rows = j.map((r) => el("tr", {},
    el("td", { text: new Date((r.sent_at || r.created_at) * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) }),
    el("td", { text: r.mailbox_address + (r.owner ? ` (${r.owner})` : "") }),
    el("td", { text: r.to_addr }),
    el("td", { text: r.supplier_name || "—" }),
    el("td", { text: r.kind === "reply" ? "↩ ответ поставщику" : r.positions.join(", ") }),
    el("td", { class: r.status === "sent" ? "ok-text" : "warn-text",
      text: r.status === "sent" ? "✅ отправлено" + (r.sent_copy ? " · " + r.sent_copy : "") : r.status === "sending" ? "отправляется…" : "⚠ " + r.error }),
    el("td", {}, answers.has(r.id) ? thOpenBtn(thKeyOfRequest(r), "📩 " + new Date(answers.get(r.id).received_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }))
      : document.createTextNode(r.status === "sent" && r.kind !== "reply" ? "ждём" : "")),
    el("td", { class: "muted", text: r.tag })));
  $("rq-journal").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

// ---------- письма: таблица «Позиции и поставщики» (0.35.0) ----------
// Пользователь 25.11.2026: «во вкладке «Письма» — таблица, где видно, по каким позициям у нас есть поставщики, были
// ли отправлены письма и прочая информация». Строка — позиция спецификации: наши поставщики по брендам (из черновиков
// писем), кому и когда писали (журнал), кто ответил (ответы / переписка), цены поставщиков (вкладка «Закупка», если
// загружены), статус. Галочки → «Запросить по выбранным» (те же письма, только эти позиции).

const rqPos = { filter: "all", q: "", sel: new Set(), showAll: false };
const RQ_POS_SHOW = 150;
const RQ_POS_FILTERS = [["all", "все"], ["ours", "есть наши поставщики"], ["notreq", "не запрашивали"], ["wait", "ждём ответа"],
  ["answered", "ответили"], ["price", "есть цена"], ["none", "нет поставщиков"]];
const shortDate = (ts) => new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });

function rqPosRows() {
  const d = rq.d;
  if (!d) return [];
  const sent = (d.journal || []).filter((j) => j.status === "sent" && j.kind !== "reply");
  const answeredReq = new Set((d.inbox || []).filter((x) => x.request_id).map((x) => x.request_id));
  const thKeys = new Set(((d.threads || rq.threads) || []).map((t) => t.key));
  return (d.positions || []).map((p) => {
    const n = String(p.n);
    const ours = d.letters.filter((L) => L.supplier_id && (L._all || L.positions).map(String).includes(n));
    const reqs = sent.filter((j) => (j.positions || []).map(String).includes(n));
    const answered = reqs.filter((j) => answeredReq.has(j.id) || thKeys.has(thKeyOfRequest(j)));
    const cells = (of.d && of.d.cells && of.d.cells[p.n]) || {};
    const prices = Object.values(cells).map((list) => list[0] && list[0].price_rub).filter(Boolean);
    const status = prices.length ? "price" : answered.length ? "answered" : reqs.length ? "wait" : ours.length ? "notreq" : "none";
    return { ...p, n, ours, reqs, answered, offers: prices.length, min: prices.length ? Math.min(...prices) : null, status };
  });
}

function rqPosPass(r, f = rqPos.filter) {
  if (f === "ours" && !r.ours.length) return false;
  if (f === "notreq" && !(r.ours.length && !r.reqs.length)) return false;
  if (f === "wait" && r.status !== "wait") return false;
  if (f === "answered" && !r.answered.length) return false;
  if (f === "price" && !r.offers) return false;
  if (f === "none" && r.ours.length) return false;
  if (rqPos.q) {
    const hay = `${r.n} ${r.name} ${r.brand} ${r.maker} ${r.ours.map((L) => L.supplier_name).join(" ")} ${r.reqs.map((j) => j.supplier_name || j.to_addr).join(" ")}`;
    if (!rqPos.q.split(/\s+/).every((w) => hay.toLowerCase().replace(/ё/g, "е").includes(w))) return false;
  }
  return true;
}

const RQ_POS_STATUS = { price: ["💰 есть цена", "ok"], answered: ["📩 ответили", "ok"], wait: ["⏳ ждём ответа", "busy"],
  notreq: ["не запрашивали", "warn"], none: ["нет наших поставщиков", "muted"] };

function rqNames(list, nameOf, max = 3) {
  const names = [...new Map(list.map((x) => [nameOf(x), x])).keys()].filter(Boolean);
  const shown = names.slice(0, max).join(", ");
  return names.length > max ? `${shown} +${names.length - max}` : shown;
}

function renderRqPos() {
  const box = $("rq-pos-box");
  const rows = rqPosRows();
  box.hidden = !rows.length;
  if (!rows.length) return;
  const count = (f) => rows.filter((r) => rqPosPass(r, f)).length;
  $("rq-pos-info").textContent = `· позиций ${rows.length} · с нашими поставщиками ${count("ours")} · запрошено ${rows.filter((r) => r.reqs.length).length}` +
    ` · ответили ${count("answered")}`;
  replaceSafe($("rq-pos-filters"), ...RQ_POS_FILTERS.map(([k, label]) => {
    const b = el("button", { type: "button", class: "chip-btn" + (rqPos.filter === k ? " on" : ""), text: `${label} · ${k === "all" ? rows.length : count(k)}` });
    b.addEventListener("click", () => { rqPos.filter = k; rqPos.showAll = false; renderRqPos(); });
    return b;
  }));
  const can = rq.d.can_send && !document.body.classList.contains("no-purchase");
  const shown = rows.filter((r) => rqPosPass(r));
  const page = rqPos.showAll ? shown : shown.slice(0, RQ_POS_SHOW);
  const all = el("input", { type: "checkbox", title: "Выбрать все показанные" });
  all.checked = page.length > 0 && page.every((r) => rqPos.sel.has(r.n));
  all.addEventListener("change", () => { for (const r of page) all.checked ? rqPos.sel.add(r.n) : rqPos.sel.delete(r.n); renderRqPos(); });
  const head = el("tr", {}, can ? el("th", {}, all) : null,
    ...["№", "Позиция", "Наши поставщики", "Запрошено", "Ответили", "Цена поставщика", "Статус"].map((x) => el("th", { text: x })));
  const body = page.map((r) => {
    const cb = el("input", { type: "checkbox" });
    cb.checked = rqPos.sel.has(r.n);
    cb.addEventListener("change", () => { cb.checked ? rqPos.sel.add(r.n) : rqPos.sel.delete(r.n); updateRqPosAsk(); });
    const pos = el("td");
    pos.append(el("div", { class: "rq-pos-name", text: r.name, title: r.name }));
    const sub = [r.brand, r.maker && !r.brand.toLowerCase().includes(r.maker.toLowerCase()) ? r.maker : "", r.qty ? `${r.qty} ${r.unit || ""}`.trim() : ""].filter(Boolean).join(" · ");
    if (sub) pos.append(el("div", { class: "muted small", text: sub }));
    const ours = el("td");
    if (r.ours.length) {
      const best = r.ours.some((L) => L.level === "green") ? "green" : r.ours.every((L) => L.level === "grey") ? "grey" : "yellow";
      ours.append(el("span", { class: "sup-st " + best, text: String(r.ours.length) }), " ",
        el("span", { class: "small", text: rqNames(r.ours, (L) => L.supplier_name), title: r.ours.map((L) => `${L.supplier_name} — ${L.status || "статус не указан"}`).join("\n") }));
    } else ours.append(el("span", { class: "muted small", text: "—" }));
    const reqs = el("td", { class: "small" });
    if (r.reqs.length) {
      const last = Math.max(...r.reqs.map((j) => j.sent_at || j.created_at));
      reqs.append(el("div", { text: rqNames(r.reqs, (j) => j.supplier_name || j.to_addr), title: r.reqs.map((j) => `${j.supplier_name || "—"} <${j.to_addr}> · ${shortDate(j.sent_at || j.created_at)}`).join("\n") }),
        el("div", { class: "muted", text: `${r.reqs.length} пис. · последнее ${shortDate(last)}` }));
    } else reqs.append(el("span", { class: "muted", text: "—" }));
    const ans = el("td", { class: "small" });
    if (r.answered.length) {
      const seen = new Set();
      for (const j of r.answered) {
        const key = thKeyOfRequest(j);
        if (seen.has(key)) continue;
        seen.add(key);
        if (ans.childNodes.length) ans.append(", ");
        ans.append(thOpenBtn(key, j.supplier_name || addrList(j.to_addr)[0] || "переписка"));
      }
    } else ans.append(el("span", { class: "muted", text: "—" }));
    const price = el("td", { class: "small", text: r.min ? rub(r.min) + (r.offers > 1 ? ` · предл. ${r.offers}` : "") : "—" });
    const [stText, stCls] = RQ_POS_STATUS[r.status];
    return el("tr", { "data-n": r.n }, can ? el("td", {}, cb) : null, el("td", { text: r.n }), pos, ours, reqs, ans, price,
      el("td", {}, el("span", { class: "rq-st " + stCls, text: stText })));
  });
  if (!body.length) body.push(el("tr", {}, el("td", { colspan: can ? "8" : "7", class: "muted", text: "Нет позиций с таким отбором." })));
  $("rq-pos").replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
  const more = $("rq-pos-more");
  more.replaceChildren();
  if (shown.length > page.length) {
    more.append(`Показаны ${page.length} из ${shown.length}. `, smallBtn("Показать все", () => { rqPos.showAll = true; renderRqPos(); }));
  }
  $("rq-pos-ask").hidden = !can;
  updateRqPosAsk();
}

function updateRqPosAsk() {
  const n = rqPos.sel.size;
  $("rq-pos-ask").textContent = n ? `✉ Запросить по выбранным (${n})` : "✉ Запросить по выбранным";
  $("rq-pos-ask").disabled = !n;
}

$("rq-pos-search").addEventListener("input", (e) => { rqPos.q = e.target.value.trim().toLowerCase().replace(/ё/g, "е"); rqPos.showAll = false; renderRqPos(); });
$("rq-pos-ask").addEventListener("click", () => askPositions([...rqPos.sel]));
$("rq-new").addEventListener("toggle", () => { if (rq.d) renderRequests(); });

// ---------- запросы: фильтр по брендам (0.33.0) ----------
// Пользователь 24.11.2026: «нужно, чтобы можно было отправлять запросы по брендам — какая-то возможность
// отфильтровать». Выбранные бренды: видны только поставщики с ними, в письмах — только позиции этих брендов
// (из L._all — исходного набора письма), «Другому адресату» — тоже только они. Скрытые письма не отправляются.

const RQ_BRANDS_SHOW = 16;

function rqBrandPositions() {
  const pb = (rq.d && rq.d.pos_brands) || {};
  return Object.keys(pb).filter((n) => pb[n].some((b) => rq.brands.has(b)));
}

function rqVisible(L) {
  if (!rq.brands || !rq.brands.size || !L.supplier_id) return true;
  return L.positions.length > 0;
}

function applyRqBrands() {
  const d = rq.d;
  const want = rq.brands.size ? new Set(rqBrandPositions()) : null;
  for (const L of d.letters) {
    L._all = L._all || L.positions.slice();
    const base = L.supplier_id ? L._all : d.positions.map((p) => p.n);
    L.positions = want ? base.filter((n) => want.has(String(n))) : (L.supplier_id ? L._all.slice() : base);
    if (want && L.supplier_id && !L.positions.length) L.checked = false;
    L.dirty = false;
  }
  renderRequests();
}

function renderRqBrands() {
  const box = $("rq-brands");
  const pb = (rq.d && rq.d.pos_brands) || {};
  const stat = new Map();
  for (const [n, list] of Object.entries(pb)) for (const b of list) {
    if (!stat.has(b)) stat.set(b, { pos: new Set(), sup: new Set() });
    stat.get(b).pos.add(n);
  }
  for (const L of (rq.d && rq.d.letters) || []) if (L.supplier_id) for (const b of L.brands || []) if (stat.has(b)) stat.get(b).sup.add(L.supplier_id);
  const list = [...stat.entries()].sort((a, b) => b[1].pos.size - a[1].pos.size || a[0].localeCompare(b[0], "ru"));
  box.hidden = list.length < 2;
  if (box.hidden) return;
  const shown = rq.brandsAll ? list : list.filter(([b], i) => i < RQ_BRANDS_SHOW || rq.brands.has(b));
  const chip = (b, st) => {
    const c = el("button", { type: "button", class: "rq-brand" + (rq.brands.has(b) ? " on" : ""), title: `Позиции: ${[...st.pos].join(", ")}` },
      document.createTextNode(b), el("span", { class: "muted", text: `${st.pos.size} поз. · ${st.sup.size} пост.` }));
    c.addEventListener("click", () => { if (rq.brands.has(b)) rq.brands.delete(b); else rq.brands.add(b); applyRqBrands(); });
    return c;
  };
  const nodes = [el("span", { class: "muted", text: "Бренды:" })];
  const all = el("button", { type: "button", class: "rq-brand" + (rq.brands.size ? "" : " on"), text: "все" });
  all.addEventListener("click", () => { rq.brands.clear(); applyRqBrands(); });
  nodes.push(all, ...shown.map(([b, st]) => chip(b, st)));
  if (list.length > RQ_BRANDS_SHOW) {
    const more = el("a", { href: "#", text: rq.brandsAll ? "свернуть" : `ещё ${list.length - shown.length}` });
    more.addEventListener("click", (e) => { e.preventDefault(); rq.brandsAll = !rq.brandsAll; renderRqBrands(); });
    nodes.push(more);
  }
  replaceSafe(box, ...nodes);
}

// ---------- переписка с поставщиками (0.32.0) ----------
// Строка на ответившего поставщика в «Запросах» (ждущие ответа — сверху и с отметкой), щелчок — окно #th-dlg:
// письма по времени (наши справа), ответ с того ящика, с которого ушёл запрос, «Ответ не нужен».

const TH_SHOW = 8;
const th = { key: null, d: null, showAll: false };

function thKeyOfRequest(j) {
  if (j.supplier_id) return "s" + j.supplier_id;
  const a = addrList(j.to_addr).filter((x) => x.includes("@"));
  return "a" + (a.length ? a[0].toLowerCase() : j.id);
}

function thKeyOfInbox(m) {
  const j = ((rq.d && rq.d.journal) || []).find((x) => x.id === m.request_id);
  if (j) return thKeyOfRequest(j);
  const byAddr = ((rq.d && rq.d.journal) || []).find((x) => addrList(x.to_addr).map((a) => a.toLowerCase()).includes(m.from_addr));
  return byAddr ? thKeyOfRequest(byAddr) : "a" + m.from_addr;
}

function thFind(key) { return ((rq.d && rq.d.threads) || rq.threads || []).find((t) => t.key === key) || null; }

function thOpenBtn(key, text) {
  const b = el("button", { type: "button", class: "th-open", title: "Открыть переписку", text });
  b.addEventListener("click", (e) => { e.stopPropagation(); openThread(key); });
  return b;
}

function renderThreads() {
  const list = (rq.d && rq.d.threads) || [];
  $("rq-threads-box").hidden = !list.length;
  if (!list.length) return;
  const wait = list.filter((t) => t.waiting).length;
  $("rq-th-count").textContent = `· ответили: ${list.length}` + (wait ? ` · ждут ответа: ${wait}` : "");
  if (stg.req && stg.req.waiting !== wait) { stg.req.waiting = wait; renderStages(); }
  setMailBadge(wait);
  const shown = th.showAll ? list : list.slice(0, TH_SHOW);
  const nodes = shown.map((t) => {
    const b = el("button", { type: "button", class: "th-item" + (t.waiting ? " wait" : "") },
      el("span", { class: "th-name", text: t.supplier_name }),
      el("span", { class: "muted small", text: `📩 ${t.in} · ✉ ${t.out} · ${fmtWhen(t.last_at)} · ${t.last_from === "supplier" ? "последнее — от поставщика" : "последнее — наше"}` }),
      t.waiting ? el("span", { class: "badge warn", text: "ждёт ответа" }) : null);
    b.addEventListener("click", () => openThread(t.key));
    return b;
  });
  if (list.length > TH_SHOW) nodes.push(smallBtn(th.showAll ? "Свернуть" : `Показать всех (${list.length})`, () => { th.showAll = !th.showAll; renderThreads(); }));
  replaceSafe($("rq-threads"), ...nodes);
}

async function openThread(key) {
  th.key = key;
  th.d = null;
  $("thd-title").textContent = (thFind(key) || {}).supplier_name || "Переписка";
  $("thd-info").textContent = "Загружаю письма…";
  $("thd-reply").hidden = true;
  replaceSafe($("thd-msgs"));
  setStatus("thd-status", "");
  if (!$("th-dlg").open) $("th-dlg").showModal();
  const t = currentTender();
  try {
    th.d = await api(clientPath(`/tenders/${state.tender}/requests/thread`), { key, folder: (t && t.folder) || "" });
  } catch (err) {
    $("thd-info").textContent = "Не удалось загрузить переписку: " + err.message;
    return;
  }
  if (th.key === key) renderThread(true);
}

function thMsg(m, i) {
  const long = (m.text || "").length > 500 || (m.text || "").split("\n").length > 9;
  const text = el("div", { class: "th-text" + (long ? " cut" : ""), text: m.text || "(текста в письме нет)" });
  const who = m.dir === "in" ? m.who : `${m.kind === "reply" ? "Наш ответ" : "Наш запрос"} · ${m.who}` + (m.to ? ` → ${m.to}` : "");
  const box = el("div", { class: "th-msg " + m.dir + (m.status === "error" ? " err" : "") },
    el("div", { class: "th-meta", text: `${who} · ${fmtWhen(m.at)}` }),
    i === 0 && m.subject ? el("div", { class: "th-meta", text: "Тема: " + m.subject }) : null,
    text);
  if (long) {
    const more = el("a", { href: "#", class: "th-more", text: "показать полностью" });
    more.addEventListener("click", (e) => { e.preventDefault(); more.textContent = text.classList.toggle("cut") ? "показать полностью" : "свернуть"; });
    box.append(more);
  }
  if (m.attachments.length) box.append(el("div", { class: "muted small", text: "📎 " + m.attachments.join(", ") }));
  if (m.status === "error" && m.error) box.append(el("div", { class: "small warn-text", text: "⚠ " + m.error }));
  return box;
}

function renderThread(fresh) {
  const d = th.d;
  const t = d.thread;
  $("thd-title").textContent = t.supplier_name;
  $("thd-info").textContent = [t.positions.length ? "позиции запроса: " + t.positions.join(", ") : "",
    t.waiting ? "⚠ поставщик ждёт ответа" : ""].filter(Boolean).join(" · ");
  replaceSafe($("thd-msgs"), ...d.messages.map(thMsg));
  const box = $("thd-msgs");
  box.scrollTop = box.scrollHeight;
  const can = Boolean(rq.d && rq.d.can_send) && !document.body.classList.contains("no-purchase");
  const mine = (d.boxes || []).find((b) => b.id === t.mailbox_id) || (!t.mailbox_id && (d.boxes || [])[0]);
  $("thd-reply").hidden = !can;
  if (fresh) {
    const lastIn = d.messages.filter((m) => m.dir === "in").pop();
    $("thd-text").value = "";
    $("thd-to").value = (lastIn && lastIn.addr) || t.to || "";
  }
  $("thd-from").textContent = mine ? `уйдёт с ящика ${mine.address}, ответом на письмо поставщика`
    : `запрос ушёл с ящика ${t.mailbox_address || "другого сотрудника"}${t.owner ? " (" + t.owner + ")" : ""} — ответить отсюда может только его владелец`;
  $("thd-send").disabled = !mine;
  $("thd-done").hidden = !t.waiting;
}

$("thd-close").addEventListener("click", () => $("th-dlg").close());
$("thd-send").addEventListener("click", async () => {
  const body = $("thd-text").value.trim();
  const to = $("thd-to").value.trim();
  if (!body) return setStatus("thd-status", "Напишите текст ответа.", true);
  if (!addrList(to).length || !addrList(to).every(isEmail)) return setStatus("thd-status", "Проверьте адрес получателя.", true);
  const key = th.key;
  $("thd-send").disabled = true;
  setStatus("thd-status", "Отправляю…");
  try {
    const t = currentTender();
    const r = await api(clientPath(`/tenders/${state.tender}/requests/reply`), { folder: (t && t.folder) || "" }, jsonPost({ key, to, body }));
    if (rq.d) rq.d.threads = r.threads;
    if (th.key === key) {
      th.d = { thread: r.thread, messages: r.messages, boxes: th.d.boxes };
      renderThread(true);
      setStatus("thd-status", "✅ Отправлено" + (r.sent_copy ? " · " + r.sent_copy : "") + ". Ответ поставщика появится здесь, когда сайт проверит почту.");
    }
    renderThreads();
    toast("Ответ поставщику отправлен");
  } catch (err) {
    setStatus("thd-status", "Не отправлено: " + err.message, true);
    $("thd-send").disabled = false;
  }
});
$("thd-done").addEventListener("click", async () => {
  const key = th.key;
  try {
    const t = currentTender();
    const r = await api(clientPath(`/tenders/${state.tender}/requests/done`), { folder: (t && t.folder) || "" }, jsonPost({ key }));
    if (rq.d) rq.d.threads = r.threads;
    if (th.d && th.key === key) { th.d.thread.waiting = false; renderThread(false); }
    renderThreads();
  } catch (err) {
    setStatus("thd-status", "Не получилось: " + err.message, true);
  }
});

$("rq-send").addEventListener("click", sendRequests);
$("rq-box").addEventListener("change", updateSendButton);
$("rq-xlsx").addEventListener("change", () => { if (rq.d) { for (const L of rq.d.letters) if (!L.dirty) L.body = rqBody(L); renderRequests(); } });
$("rq-all").addEventListener("click", (e) => {
  e.preventDefault();
  for (const L of rq.d.letters) if (L.supplier_id && rqVisible(L) && L.level !== "grey" && addrList(L.to).length) L.checked = true;
  renderRequests();
});
$("rq-none").addEventListener("click", (e) => { e.preventDefault(); for (const L of rq.d.letters) L.checked = false; renderRequests(); });

// ---------- предложения поставщиков: сравнение перед записью в NocoDB (0.29.0) ----------

let ofTimer = null;
const of = { d: null, dlg: null };

function stopOffersPolling() { if (ofTimer) { clearTimeout(ofTimer); ofTimer = null; } }

async function loadOffers() {
  const my = ++seq.of;
  stopOffersPolling();
  if (blockOff("prices")) return;
  const t = currentTender();
  if (!t || !t.folder) return;
  if (my === 1 || !plData) loadPricelists();
  let d;
  try {
    d = await api(clientPath("/offers"), { folder: t.folder });
  } catch (err) {
    if (my === seq.of) setStatus("of-status", "Не удалось загрузить предложения: " + err.message, true);
    return;
  }
  if (my !== seq.of) return;
  of.d = d;
  stg.offers = { withPrices: Object.keys(d.cells).length, total: d.positions.length, suppliers: d.suppliers.length,
    written: Object.values(d.cells).reduce((n, c) => n + Object.values(c).reduce((m, o) => m + o.filter((x) => x.written).length, 0), 0) };
  renderStages();
  renderOffers();
  const busy = d.pending.length || (d.noco_job && ["queued", "running"].includes(d.noco_job.status));
  if (busy) ofTimer = setTimeout(loadOffers, 5000);
}

function ofComments(target) { return ((of.d && of.d.comments) || []).filter((c) => c.target === target); }

function ofCommentBtn(target, label) {
  const n = ofComments(target).length;
  const b = el("button", { class: "of-cm" + (n ? " has" : ""), type: "button", title: `Комментарии: ${label}`, text: n ? `💬 ${n}` : "💬" });
  b.addEventListener("click", (e) => { e.stopPropagation(); openOfferDialog({ target, label }); });
  return b;
}

// «Предложения поставщиков» (0.41.0): таблица как в «Спецификации» — бренд, страна, кол-во, НМЦ, фильтры колонок и
// быстрые отборы; щелчок по цене — отметить / снять; ✏ у номера — окно позиции (все предложения, исправить цену,
// комментарии, соседние позиции). Отмеченные в скрытых фильтром строках не теряются и тоже записываются.
let ofShown = [];          // номера позиций в таблице сейчас (с учётом фильтров) — для «← →» в окне позиции

function ofOffers(n, key) { return ((of.d.cells[n] || {})[key]) || []; }
function ofSupNames(r) { return of.d.suppliers.filter((s) => ofOffers(r.n, s.key).length).map((s) => s.name); }
function ofBestPrice(p) {
  const b = of.d.best[p.n];
  const o = b && ofOffers(p.n, b.supplier)[0];
  return o && o.price_rub ? o.price_rub : null;
}
function ofRows() {
  const psBy = new Map(((psLast && psLast.rows) || []).map((x) => [String(x.n), x]));
  return of.d.positions.map((p) => {
    const offers = of.d.suppliers.map((s) => ofOffers(p.n, s.key)[0]).filter(Boolean);
    const best = of.d.best[p.n];
    const bp = ofBestPrice(p);
    return { ...p, cnt: offers.length, sel: offers.some((o) => o.selected && !o.written), written: offers.filter((o) => o.written).length,
      unwritten: offers.some((o) => o.price_rub && !o.written),
      bestPrice: bp, ratio: bp && p.nmc ? bp / p.nmc : null, warnBest: Boolean(best && best.with_warn), ps: psBy.get(String(p.n)) || null };
  });
}
function ofCellState(o) {
  if (!o) return "нет цены";
  if (o.written) return "в NocoDB";
  if (o.selected) return "отмечена";
  if (!o.price_rub) return "цена не найдена";
  if ((o.warn || []).length) return "с замечанием";
  return "есть цена";
}
function ofDefs() {
  const defs = [
    { key: "brand", label: "Бренд", values: (r) => [r.maker || "(не указан)"] },
    { key: "country", label: "Страна", values: (r) => [r.country || "(не указана)"] },
    { key: "nmc", label: "НМЦ за ед.", values: (r) => [r.nmc ? "есть НМЦ" : "нет НМЦ"] },
    { key: "cnt", label: "Предложений", order: ["нет предложений", "1 предложение", "2–3 предложения", "4 и больше"],
      values: (r) => [r.cnt === 0 ? "нет предложений" : r.cnt === 1 ? "1 предложение" : r.cnt <= 3 ? "2–3 предложения" : "4 и больше"] },
  ];
  for (const s of of.d.suppliers) defs.push({ key: "s:" + s.key, label: s.name,
    order: ["отмечена", "в NocoDB", "есть цена", "с замечанием", "цена не найдена", "нет цены"],
    values: (r) => [ofCellState(ofOffers(r.n, s.key)[0])] });
  defs.push({ key: "best", label: "Лучшая цена", values: (r) => [r.bestPrice ? "есть" : "(нет)"] },
    { key: "ratio", label: "К НМЦ", order: ["≤ 85 % НМЦ", "85–100 % НМЦ", "выше НМЦ", "нет цены", "нет НМЦ"],
      values: (r) => [!r.nmc ? "нет НМЦ" : !r.bestPrice ? "нет цены" : r.ratio <= 0.85 ? "≤ 85 % НМЦ" : r.ratio <= 1 ? "85–100 % НМЦ" : "выше НМЦ"] });
  return defs;
}
// Быстрые отборы — те же, что в «Спецификация → Цены» (0.41.1, пользователь: «экспресс-фильтры такие же, как в
// спецификации»). Цены поставщиков — из этой таблицы, интернет и «не проверено» — из «Цен» (psLast).
const ofNet = (r) => Boolean(r.ps && ((r.ps.rf || []).length || (r.ps.abroad || []).length));
const OF_CHIPS = [
  ["nothing", "без цены вообще", (r) => r.cnt === 0 && !ofNet(r) && !(r.ps && r.ps.best)],
  ["nosup", "без цен поставщиков", (r) => r.cnt === 0],
  ["over", "выше НМЦ", (r) => r.ratio > 1],
  ["unwritten", "есть цены — не записаны", (r) => r.unwritten],
];

function ofToggle(p, o) {
  if (!of.d.can_write || o.written) return;
  offersAction("/offers/select", { cells: [{ file: o.file_id, pos: p.n, selected: !o.selected }] });
}

function renderOffers() {
  const d = of.d;
  const sups = d.suppliers;
  const withPrices = Object.keys(d.cells).length;
  $("of-info").textContent = sups.length ? `· поставщиков: ${sups.length} · позиций с ценами: ${withPrices} из ${d.positions.length}` : "";
  const notes = [];
  if (d.pending.length) notes.push(`Разбираются документы поставщиков: ${d.pending.map((p) => p.name).join(", ")}…`);
  const job = d.noco_job;
  if (job && job.status === "queued") notes.push("Запись в NocoDB — в очереди…");
  if (job && job.status === "running") notes.push("Записываю выбранные цены в спецификацию NocoDB…");
  setStatus("of-status", notes.join(" "));
  $("of-tools").hidden = (!sups.length && !d.pos_pending) || !d.can_write;
  $("of-write").disabled = !(d.selected || d.pos_pending) || Boolean(job && ["queued", "running"].includes(job.status));
  $("of-cf-bar").hidden = !sups.length;
  // Что есть на сайте, но ещё не в NocoDB (0.42.0)
  const unsaved = [d.selected ? `отмечено цен: ${d.selected}` : "", d.pos_pending ? `правок бренда / страны: ${d.pos_pending}` : ""].filter(Boolean);
  $("of-unsaved").textContent = unsaved.length ? `⏳ Не записано в NocoDB — ${unsaved.join(", ")}. «Спецификация» показывает только то, что уже в базе; записать — кнопкой «Записать в NocoDB».` : "";
  $("of-unsaved").hidden = !unsaved.length;
  if (!sups.length) {
    $("of-write").textContent = d.pos_pending ? `Записать в NocoDB (правок ${d.pos_pending})` : "Записать в NocoDB";
    replaceSafe($("of-wrap"), el("div", { class: "muted", text: "Предложений пока нет. Они появляются, когда поставщики отвечают на запросы по почте " +
      "(письма — ниже) или когда КП, прайс, счёт или скриншот загружают в поле ниже (📎)." }));
    $("of-legend").hidden = true;
    renderOfferComments();
    if (rq.d) renderRqPos();
    return;
  }
  $("of-legend").hidden = false;
  const defs = ofDefs();
  const all = ofRows();
  const rows = all.filter((r) => cfPass("of", defs, r));
  ofShown = rows.map((r) => r.n);
  cfShowInfo("of", rows.length, all.length);
  renderChipsInto($("of-cf-chips"), "of", all, renderOffers);
  // отмеченные среди скрытых фильтром — видно на кнопке записи
  const shownSet = new Set(ofShown);
  const hiddenSel = all.filter((r) => !shownSet.has(r.n)).reduce((n, r) =>
    n + sups.filter((s) => { const o = ofOffers(r.n, s.key)[0]; return o && o.selected && !o.written; }).length, 0);
  const wr = [d.selected ? `цен ${d.selected}` + (hiddenSel ? `, из них скрыто фильтром ${hiddenSel}` : "") : "", d.pos_pending ? `правок ${d.pos_pending}` : ""].filter(Boolean);
  $("of-write").textContent = wr.length ? `Записать в NocoDB (${wr.join("; ")})` : "Записать в NocoDB";
  const byKey = new Map(defs.map((x) => [x.key, x]));
  const th = (key, text, cls) => cfTh("of", byKey.get(key), all, defs, renderOffers, text, cls);
  const head = el("tr", {}, el("th", { class: "stick0", text: "№" }), el("th", { class: "stick1", text: "Позиция" }),
    th("brand", "Бренд"), th("country", "Страна"), el("th", { text: "Кол-во" }), th("nmc", "НМЦ за ед."),
    ...sups.map((s) => {
      const mails = s.files.filter((f) => f.mail).length;
      const docs = s.files.length - mails;
      const src = [mails ? `📧 ${mails}` : "", docs ? `📎 ${docs}` : ""].filter(Boolean).join(" ");
      const h = th("s:" + s.key, s.name, "of-sup");
      h.title = s.files.map((f) => `${f.name} · ${fmtWhen(f.at)}` + (f.mail ? ` · письмо от ${f.mail.from}` : "")).join("\n");
      h.append(el("div", { class: "muted small", text: [src, s.vat && s.vat !== "не указано" ? s.vat : "НДС не указан"].filter(Boolean).join(" · ") }),
        ofCommentBtn("supplier:" + s.key, "поставщик " + s.name));
      return h;
    }),
    th("best", "Лучшая цена"), th("ratio", "К НМЦ"));
  const body = el("tbody");
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: String(8 + sups.length), class: "muted", text: "Нет позиций с такими фильтрами." })));
  for (const r of rows) {
    const ed = el("button", { class: "op-open", type: "button", title: "Окно позиции: все предложения, исправить цену, комментарии", text: "✏" });
    ed.addEventListener("click", () => openOpDlg(r.n));
    const cm = ofComments("pos:" + r.n).length;
    const pos = el("td", { class: "stick1" }, el("div", { text: r.name }));
    if (r.brand && r.brand !== r.maker) pos.append(el("div", { class: "muted small", text: r.brand }));
    if (cm) pos.append(el("button", { class: "of-cm has", type: "button", title: "Комментарии к позиции", text: `💬 ${cm}` }));
    if (cm) pos.lastChild.addEventListener("click", () => openOpDlg(r.n));
    const best = el("td", { class: "num" });
    const b = d.best[r.n];
    if (r.bestPrice) best.append(el("b", { text: rub(r.bestPrice) }), el("div", { class: "muted small",
      text: ((sups.find((s) => s.key === b.supplier) || {}).name || "") + (b.with_warn ? " · ⚠" : "") }));
    body.append(el("tr", { class: r.cnt ? null : "of-empty" },
      el("td", { class: "stick0 num" }, el("span", { text: r.n }), ed), pos,
      ofFieldCell(r, "maker"), ofFieldCell(r, "country"),
      el("td", { class: "num", text: [r.qty, r.unit].filter((x) => x !== null && x !== undefined && x !== "").join(" ") }),
      el("td", { class: "num", text: r.nmc ? rub(r.nmc) : "" }),
      ...sups.map((s) => offerCell(r, s, b)),
      best, el("td", { class: "num" }, ratioBadge(r.bestPrice, r.nmc))));
  }
  const foot = el("tr", { class: "of-total" }, el("td", { class: "stick0" }), el("td", { class: "stick1", text: "Итого по поставщику" }),
    el("td"), el("td"), el("td"), el("td"),
    ...sups.map((s) => {
      const t = d.totals[s.key] || { positions: 0, sum: 0, best: 0 };
      return el("td", { class: "num" }, el("div", { text: `${t.positions} поз.` }), el("div", { text: "≈ " + rub(t.sum) }),
        el("div", { class: "muted small", text: `лучших цен: ${t.best}` }));
    }), el("td"), el("td"));
  const table = el("table", { class: "of-table" }, el("thead", {}, head), body, el("tfoot", {}, foot));
  replaceSafe($("of-wrap"), table);
  if (rq.d) renderRqPos();      // цены поставщиков — в таблицу «Позиции и поставщики»
  const first = table.querySelector("th.stick0");
  if (first) table.style.setProperty("--stick1-left", first.offsetWidth + "px");
  renderOfferComments();
  if ($("op-dlg").open) renderOpDlg();
}

// Бренд / страна: правка на сайте, ещё не в NocoDB, — «⏳» и прежнее значение в подсказке (0.42.0).
function ofFieldCell(r, f) {
  const pend = r.pending && r.pending[f];
  const td = el("td", { class: pend ? "of-pend" : null, text: r[f] || "" });
  if (pend) {
    td.append(el("span", { class: "op-unsaved", text: pend.status === "requested" ? " ⏳ записывается" : " ⏳" }));
    td.title = `Исправлено на сайте${pend.by ? " (" + pend.by + ")" : ""}, в NocoDB ещё не записано. В NocoDB сейчас: «${r["db_" + f] || "пусто"}»`;
  }
  return td;
}

// Ячейка поставщика: только цена и пометки; щелчок по цене — отметить / снять.
function offerCell(p, s, best) {
  const offers = ofOffers(p.n, s.key);
  if (!offers.length) return el("td", { class: "of-cell none", text: "—" });
  const o = offers[0];
  const isBest = best && best.supplier === s.key;
  const over = p.nmc && o.price_rub && o.price_rub > p.nmc;
  const can = of.d.can_write && !o.written && o.price_rub;
  const cls = ["of-cell", isBest ? "best" : "", over ? "over" : "", o.warn.length ? "warn" : "", o.written ? "written" : "",
    o.selected && !o.written ? "sel" : "", can ? "pick" : ""].filter(Boolean).join(" ");
  const td = el("td", { class: cls, title: can ? (o.selected ? "Щелчок — снять отметку" : "Щелчок — отметить для записи в NocoDB") : null });
  const line = el("div", { class: "of-price" });
  if (can || o.selected) line.append(el("span", { class: "of-mark", text: o.selected && !o.written ? "☑" : "☐" }));
  line.append(el("b", { text: o.price_rub ? rub(o.price_rub) : "цена не найдена" }));
  if (isBest) line.append(el("span", { class: "badge ok", text: best.with_warn ? "лучшая ⚠" : "лучшая" }));
  td.append(line);
  const facts = [];
  if (o.written) facts.push("✅ в NocoDB");
  else if (o.write_status === "requested" || o.write_status === "running") facts.push("записывается…");
  if (o.edited) facts.push("✏ исправлено");
  if (o.manual) facts.push("✍ вручную");
  if (!o.written && o.selected) facts.push("⏳ не в NocoDB");
  if (o.currency && o.currency !== "RUB" && o.price) facts.push(`${o.price} ${o.currency}`);
  if (offers.length > 1) facts.push(`ещё ${offers.length - 1}`);
  const cm = ofComments(`cell:${o.file_id}|${p.n}`).length;
  if (cm) facts.push(`💬 ${cm}`);
  if (facts.length) td.append(el("div", { class: "muted small", text: facts.join(" · ") }));
  if (o.warn.length) td.append(el("div", { class: "small warn-text", title: o.warn.join("\n"), text: "⚠ " + o.warn[0] + (o.warn.length > 1 ? ` (+${o.warn.length - 1})` : "") }));
  if (can) td.addEventListener("click", () => ofToggle(p, o));
  return td;
}

// ---------- окно позиции (0.41.0) ----------
let opN = null;

function openOpDlg(n) {
  if (!of.d) return toast("Предложения поставщиков ещё загружаются", true);
  opN = String(n);
  renderOpDlg();
  if (!$("op-dlg").open) $("op-dlg").showModal();
}

function renderOpDlg() {
  const d = of.d;
  const p = d.positions.find((x) => String(x.n) === opN);
  if (!p) { $("op-dlg").close(); return; }
  $("op-title").textContent = `№ ${p.n} · ${p.name}`;
  const order = ofShown.includes(opN) ? ofShown : d.positions.map((x) => String(x.n));
  const at = order.indexOf(opN);
  $("op-pos").textContent = at >= 0 ? `${at + 1} из ${order.length}` : "";
  $("op-prev").disabled = at <= 0;
  $("op-next").disabled = at < 0 || at >= order.length - 1;
  $("op-prev").onclick = () => { opN = order[at - 1]; renderOpDlg(); };
  $("op-next").onclick = () => { opN = order[at + 1]; renderOpDlg(); };
  const fact = (k, v) => (v ? el("span", { class: "chip" }, el("b", { text: k + ": " }), String(v)) : null);
  replaceSafe($("op-facts"), ...[fact("Марка/модель", p.brand),
    fact("Кол-во", [p.qty, p.unit].filter(Boolean).join(" ")), fact("НМЦ за ед.", p.nmc ? rub(p.nmc) : "")].filter(Boolean));
  // бренд и страна — поля (правка на сайте, в NocoDB — кнопкой записи)
  const pend = p.pending || {};
  if (document.activeElement !== $("op-maker")) $("op-maker").value = p.maker || "";
  if (document.activeElement !== $("op-country")) $("op-country").value = p.country || "";
  const uniq = (f) => [...new Set(d.positions.map((x) => x[f]).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ru"));
  replaceSafe($("op-maker-list"), ...uniq("maker").map((v) => el("option", { value: v })));
  replaceSafe($("op-country-list"), ...uniq("country").map((v) => el("option", { value: v })));
  const pendKeys = Object.keys(pend);
  $("op-reset-fields").hidden = !pendKeys.length;
  $("op-fields-note").hidden = !pendKeys.length;
  $("op-fields-note").textContent = pendKeys.length ? `⏳ не записано в NocoDB (в базе: ${pendKeys.map((f) => `${f === "maker" ? "бренд" : "страна"} «${p["db_" + f] || "пусто"}»`).join(", ")})` +
    (Object.values(pend).some((x) => x.status === "requested") ? " — записывается…" : "") : "";
  $("op-fields").hidden = !d.can_write;
  $("op-manual").hidden = !d.can_write;
  replaceSafe($("opm-sup-list"), ...(d.suppliers || []).map((s) => el("option", { value: s.name })));
  // предложения
  const best = d.best[p.n];
  const list = [];
  for (const s of d.suppliers) ofOffers(p.n, s.key).forEach((o, i) => list.push({ s, o, main: i === 0 }));
  list.sort((a, b) => (a.o.price_rub || 1e15) - (b.o.price_rub || 1e15));
  if (!list.length) {
    replaceSafe($("op-offers"), el("div", { class: "muted", text: "По этой позиции предложений нет." }));
  } else {
    const head = el("tr", {}, ...["Поставщик", "Цена за ед.", "К НМЦ", "Документ и цитата", "Замечания", "Статус", ""].map((x) => el("th", { text: x })));
    const body = list.map(({ s, o, main }) => {
      const tdAct = el("td", { class: "op-act" });
      if (d.can_write && !o.written && main && o.price_rub) {
        tdAct.append(smallBtn(o.selected ? "Снять отметку" : "Отметить для записи", () => ofToggle(p, o), o.selected ? "btn ghost small" : "btn small"));
      }
      if (d.can_write && !o.written) tdAct.append(smallBtn("✏ цена", () => opEditPrice(tr, p, o)));
      if (d.can_write && !o.written && o.manual) tdAct.append(smallBtn("Убрать", () => offersAction("/offers/manual/remove", { file: o.file_id, pos: p.n }, "Цена убрана")));
      const cmN = ofComments(`cell:${o.file_id}|${p.n}`).length;
      tdAct.append(ofCommentBtn(`cell:${o.file_id}|${p.n}`, `${s.name}, позиция ${p.n}`));
      if (cmN) tdAct.lastChild.textContent = `💬 ${cmN}`;
      const status = o.written ? "✅ в NocoDB" : (o.write_status === "requested" || o.write_status === "running") ? "записывается…"
        : o.selected ? "☑ отмечена · ⏳ не в NocoDB" : main ? "не отмечена · не в NocoDB" : "другой документ этого поставщика";
      const price = el("td", { class: "num" }, el("b", { text: o.price_rub ? rub(o.price_rub) : "не найдена" }));
      if (o.currency && o.currency !== "RUB" && o.price) price.append(el("div", { class: "muted small", text: `${o.price} ${o.currency}` }));
      if (o.edited) price.append(el("div", { class: "muted small", text: `исправлено (в документе ${o.orig_price_rub ? rub(o.orig_price_rub) : "—"}${o.edited_by ? ", " + o.edited_by : ""})` }));
      const tr = el("tr", { class: [best && best.supplier === s.key && main ? "op-best" : "", o.selected && !o.written ? "op-sel" : ""].filter(Boolean).join(" ") || null },
        el("td", {}, el("b", { text: s.name }), el("div", { class: "muted small", text: s.vat && s.vat !== "не указано" ? s.vat : "НДС не указан" })),
        price, el("td", { class: "num" }, ratioBadge(o.price_rub, p.nmc)),
        el("td", { class: "small" }, el("div", { text: (o.manual ? "✍ " : "📄 ") + o.file }), o.text ? el("div", { class: "muted", text: `«${o.text}»` + (o.line_no ? ` (строка ${o.line_no})` : "") }) : null),
        el("td", { class: "small" + (o.warn.length ? " warn-text" : "") }, ...(o.warn.length ? o.warn.map((w) => el("div", { text: "⚠ " + w })) : [el("span", { class: "muted", text: "—" })]),
          o.note ? el("div", { class: "muted", text: o.note }) : null),
        el("td", { class: "small", text: status }), tdAct);
      return tr;
    });
    replaceSafe($("op-offers"), el("table", { class: "op-table" }, el("thead", {}, head), el("tbody", {}, ...body)));
  }
  // интернет и все источники — из «Спецификации → Цены», если загружены
  const ps = psLast && (psLast.rows || []).find((r) => String(r.n) === opN);
  const net = [];
  if (ps) {
    if ((ps.rf || [])[0]) net.push(`интернет РФ: от ${rub(ps.rf[0].price_rub)} (${ps.rf[0].domain || "NocoDB"})`);
    if ((ps.abroad || [])[0]) net.push(`зарубеж: от ${rub(ps.abroad[0].price_rub)} (${ps.abroad[0].domain || "NocoDB"})`);
    if (ps.best) net.push(`лучшая из всех источников: ${rub(ps.best.price_rub)} — ${ps.best.source}`);
  }
  replaceSafe($("op-net"), ...(net.length ? [el("div", { class: "muted small", text: "Для сравнения: " + net.join(" · ") })] : []));
  // комментарии к позиции
  const cms = ofComments("pos:" + p.n);
  replaceSafe($("op-comments"), ...(cms.length ? cms.map((c) => {
    const row = el("div", { class: "of-comment" }, el("div", { class: "muted small", text: `${c.by || "—"} · ${fmtWhen(c.at)}` }), el("div", { text: c.text }));
    if (c.by === d.me) row.append(smallBtn("Удалить", () => offersAction("/offers/comment/delete", { id: c.id })));
    return row;
  }) : [el("div", { class: "muted small", text: "Комментариев пока нет." })]));
  $("op-add").hidden = !d.can_comment;
}

// Исправить цену прямо в строке окна: поле, «Сохранить», «Вернуть из документа».
function opEditPrice(tr, p, o) {
  const cell = tr.children[1];
  const input = el("input", { type: "text", value: o.price_rub || "", class: "op-price-in" });
  const save = smallBtn("Сохранить", () => offersAction("/offers/price", { file: o.file_id, pos: p.n, price_rub: input.value.trim() }, "Цена исправлена"), "btn small");
  const parts = [input, save];
  if (o.edited) parts.push(smallBtn("Как в документе", () => offersAction("/offers/price", { file: o.file_id, pos: p.n, price_rub: null }, "Цена — как в документе")));
  replaceSafe(cell, ...parts, el("div", { class: "muted small", text: "руб. за единицу, с учётом НДС, как в предложении" }));
  input.focus();
}

$("op-close").addEventListener("click", () => $("op-dlg").close());
$("op-save-fields").addEventListener("click", () => {
  const p = of.d.positions.find((x) => String(x.n) === opN);
  if (!p) return;
  const body = { pos: opN };
  const m = $("op-maker").value.trim(), c = $("op-country").value.trim();
  if (m !== (p.maker || "")) body.maker = m;
  if (c !== (p.country || "")) body.country = c;
  if (Object.keys(body).length === 1) return toast("Бренд и страна не изменились");
  offersAction("/offers/position", body, "Сохранено на сайте — в NocoDB запишется кнопкой «Записать в NocoDB»");
});
$("op-reset-fields").addEventListener("click", () => offersAction("/offers/position", { pos: opN, maker: null, country: null }, "Как в NocoDB"));
$("opm-add").addEventListener("click", async () => {
  const supplier = $("opm-supplier").value.trim(), price = $("opm-price").value.trim();
  if (!supplier || !price) return toast("Укажите поставщика и цену", true);
  await offersAction("/offers/manual", { pos: opN, supplier, price_rub: price, vat: $("opm-vat").value, note: $("opm-note").value.trim() },
    "Цена добавлена и отмечена к записи — в NocoDB кнопкой «Записать в NocoDB»");
  $("opm-price").value = ""; $("opm-note").value = "";
});
$("op-save-comment").addEventListener("click", async () => {
  const text = $("op-text").value.trim();
  if (!text || !opN) return;
  await offersAction("/offers/comment", { target: "pos:" + opN, text });
  $("op-text").value = "";
});
$("op-dlg").addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea")) return;
  if (e.key === "ArrowLeft" && !$("op-prev").disabled) $("op-prev").click();
  if (e.key === "ArrowRight" && !$("op-next").disabled) $("op-next").click();
});
$("of-cf-search").addEventListener("input", (e) => { cf.q.of = cfNorm(e.target.value.trim()); renderOffers(); });
$("of-cf-clear").addEventListener("click", () => {
  cf.of = {}; cf.chips.of = new Set(); cf.q.of = ""; $("of-cf-search").value = "";
  cfClose(); renderOffers();
});

function ofTargetLabel(target) {
  const d = of.d;
  if (target.startsWith("supplier:")) {
    const s = d.suppliers.find((x) => "supplier:" + x.key === target);
    return "Поставщик: " + (s ? s.name : target.slice(9));
  }
  if (target.startsWith("pos:")) return "Позиция № " + target.slice(4);
  const [fid, n] = target.slice(5).split("|");
  const s = d.suppliers.find((x) => x.files.some((f) => f.id === fid));
  return `${s ? s.name : "поставщик"}, позиция № ${n}`;
}

function renderOfferComments() {
  const list = ((of.d && of.d.comments) || []).slice().sort((a, b) => b.at - a.at);
  $("of-comments-h").hidden = !list.length;
  replaceSafe($("of-comments"), ...list.slice(0, 50).map((c) => el("div", { class: "of-comment" },
    el("div", { class: "muted small", text: `${ofTargetLabel(c.target)} · ${c.by || "—"} · ${fmtWhen(c.at)}` }),
    el("div", { text: c.text }))));
}

async function offersAction(path, body, okText) {
  const t = currentTender();
  try {
    const r = await api(clientPath(path), { folder: t.folder }, jsonPost(body || {}));
    if (okText) toast(typeof okText === "function" ? okText(r) : okText);
  } catch (err) {
    toast(err.message, true);
  }
  await loadOffers();
}

// Окно: комментарии (к цене / поставщику / позиции) и исправление цены.
function openOfferDialog(ctx) {
  of.dlg = ctx;
  $("ofd-title").textContent = ctx.edit ? "Цена: " + ctx.label : "Комментарии: " + ctx.label;
  const box = $("ofd-edit");
  box.hidden = !ctx.edit;
  if (ctx.edit) {
    const o = ctx.edit.o;
    $("ofd-price").value = o.price_rub || "";
    $("ofd-orig").textContent = `В документе «${o.file}»: ${o.orig_price_rub ? rub(o.orig_price_rub) : "цена не найдена"}` +
      (o.text ? ` — «${o.text}»` : "") + (ctx.edit.p.nmc ? ` · НМЦ ${rub(ctx.edit.p.nmc)} за ед.` : "");
    $("ofd-reset").hidden = !o.edited;
  }
  renderOfferDialogComments();
  $("ofd-text").value = "";
  $("ofd-add").hidden = !of.d.can_comment;
  $("of-dlg").showModal();
}

function renderOfferDialogComments() {
  const list = ofComments(of.dlg.target);
  replaceSafe($("ofd-list"), ...(list.length ? list.map((c) => {
    const row = el("div", { class: "of-comment" }, el("div", { class: "muted small", text: `${c.by || "—"} · ${fmtWhen(c.at)}` }), el("div", { text: c.text }));
    if (c.by === of.d.me) {
      const del = smallBtn("Удалить", async () => {
        await offersAction("/offers/comment/delete", { id: c.id });
        renderOfferDialogComments();
      });
      row.append(del);
    }
    return row;
  }) : [el("div", { class: "muted small", text: "Комментариев пока нет." })]));
}

$("ofd-close").addEventListener("click", () => $("of-dlg").close());
$("ofd-save-comment").addEventListener("click", async () => {
  const text = $("ofd-text").value.trim();
  if (!text) return;
  await offersAction("/offers/comment", { target: of.dlg.target, text });
  $("ofd-text").value = "";
  renderOfferDialogComments();
});
$("ofd-save-price").addEventListener("click", async () => {
  const { o, p } = of.dlg.edit;
  await offersAction("/offers/price", { file: o.file_id, pos: p.n, price_rub: $("ofd-price").value.trim() }, "Цена исправлена");
  $("of-dlg").close();
});
$("ofd-reset").addEventListener("click", async () => {
  const { o, p } = of.dlg.edit;
  await offersAction("/offers/price", { file: o.file_id, pos: p.n, price_rub: null }, "Цена — как в документе");
  $("of-dlg").close();
});
$("of-best").addEventListener("click", () => offersAction("/offers/best", {}, (r) => r.changed ? "Отмечены лучшие цены по каждой позиции" : "Лучшие уже отмечены"));
$("of-write").addEventListener("click", async () => {
  const n = of.d.selected;
  const shown = new Set(ofShown);
  const hidden = ofRows().filter((r) => !shown.has(r.n)).reduce((k, r) =>
    k + of.d.suppliers.filter((s) => { const o = ofOffers(r.n, s.key)[0]; return o && o.selected && !o.written; }).length, 0);
  const pp = of.d.pos_pending || 0;
  if (!confirm(`Записать в спецификацию NocoDB:` + (n ? `\n• ${n} отмеченных цен («Отдел закупок», до ${of.d.max_slots} поставщиков на позицию)` : "") +
    (pp ? `\n• правки бренда / страны: ${pp} поз. (колонки «Производитель», «Страна происхождения»)` : "") +
    (hidden ? `\n\nИз цен ${hidden} — в строках, скрытых фильтром: они тоже будут записаны.` : "") + "\n\nПродолжить?")) return;
  await offersAction("/offers/write", {}, (r) => `В очередь на запись: ${r.positions} цен` + (r.edits ? `, правок позиций ${r.edits}` : "") +
    ((r.over_limit || []).length ? `; по позициям ${r.over_limit.join(", ")} мест больше нет — записаны самые низкие` : ""));
});

// «Запросить цены по выбранным»: в письмах поставщикам оставить только эти позиции и перейти к письмам.
async function askPositions(ns) {
  if (!rq.d) {
    if (rq.for !== state.tender) await loadRequests();
    if (!rq.d) return toast("Письма ещё загружаются — попробуйте через секунду", true);
  }
  const want = new Set(ns.map(String));
  rq.brands = new Set();                  // выбор позиций в таблице главнее фильтра по брендам
  for (const L of rq.d.letters) {
    L._all = L._all || L.positions.slice();
    const base = L.supplier_id ? L._all : rq.d.positions.map((p) => p.n);
    L.positions = base.filter((n) => want.has(String(n)));
    if (!L.positions.length) L.checked = false;
    else if (L.supplier_id && L.to && L.level !== "grey") L.checked = true;
    L.dirty = false;
  }
  rq.showAll = true;
  renderRequests();
  setTab("purchase");
  rqSub("new");
  requestAnimationFrame(() => $("requests").scrollIntoView({ behavior: "smooth", block: "start" }));
  const n = rq.d.letters.filter((L) => L.supplier_id && L.positions.length).length;
  toast(`Письма — только по выбранным позициям (${want.size}); поставщиков с этими брендами: ${n}. Проверьте адреса и нажмите «Отправить».`);
}


// ---------- обработчик: что выполняется сейчас (шапка) ----------

let actTimer = null;
let actSignature = null;   // задачи текущего конкурса при прошлой проверке — заметить запуск из другой вкладки
let actLast = null;        // последний ответ /jobs — перерисовать меню без запроса (кнопки отмены)

function jobLine(j, withFolder = true) {
  const head = (withFolder ? j.folder + " — " : "") + j.kind_title;
  if (j.status === "running") return `${head}: ${j.step || "…"}` + stoppingText(j);
  if (j.status === "queued") return `${head}: ` + queuedText(j);
  const by = byText(j);
  if (j.status === "paused") return `${head}${by}: ${lc1(j.message || "Приостановлена")}`;
  const ago = j.finished_at ? fmtDur(Date.now() / 1000 - j.finished_at) + " назад" : "";
  if (j.status === "canceled") return `${head}${by}: ${lc1(j.message || "Отменена")} ${ago}`;
  return `${head}${by}: ${j.status === "error" ? "ошибка" : "готово"} ${ago}` + (j.status === "error" ? ` — ${j.message}` : "");
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
    if (!ofTimer) loadOffers();
    if (!bidTimer) loadBid();
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
    if (r.after_analysis) toast("Идёт анализ документов — поиск цен запустится сам сразу после него.");
    $("pr-urgent").checked = false;
  } catch (err) {
    setStatus("pr-status", "Не удалось запустить поиск цен: " + err.message, true);
  }
  loadPrices();
});
$("bid-run").addEventListener("click", async () => {
  const t = currentTender();
  if (!t || !t.folder) return;
  $("bid-run").disabled = true;
  try {
    const r = await api(clientPath("/bid/run"), { folder: t.folder, entity: $("bid-entity").value,
      model: USER_MODE ? "" : $("bid-model").value }, { method: "POST" });
    if (r.already) setStatus("bid-status", "Подготовка уже идёт (запущена раньше или в другой вкладке) — показываю её ход.");
  } catch (err) {
    setStatus("bid-status", "Не удалось запустить подготовку заявки: " + err.message, true);
  }
  loadBid();
});
$("bid-entity").addEventListener("change", () => { $("bid-entity").dataset.touched = "1"; });
$("bid-model").addEventListener("change", (e) => storageSet(BID_MODEL_KEY, e.target.value));
for (const b of document.querySelectorAll("[data-btab]")) b.addEventListener("click", () => showBidTab(b.dataset.btab));
for (const id of ["bid-price-mode", "bid-price-value", "bid-price-vat"]) {
  $(id).addEventListener("input", () => {
    $("bid-price").dataset.touched = "1";
    $("bid-price-vat").hidden = $("bid-price-mode").value !== "total";
    $("bid-price-value").hidden = $("bid-price-mode").value === "none";
  });
}
$("bid-price").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = currentTender();
  if (!t || !t.folder) return;
  const mode = $("bid-price-mode").value;
  const value = $("bid-price-value").value.trim();
  if (mode !== "none" && !value) { setStatus("bid-status", "Впишите снижение в процентах или сумму заявки.", true); return; }
  $("bid-price-apply").disabled = true;
  try {
    await api(clientPath("/bid/price"), { folder: t.folder }, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, value, vat: $("bid-price-vat").value }) });
    delete $("bid-price").dataset.touched;
    setStatus("bid-status", "");
  } catch (err) {
    setStatus("bid-status", "Цена не применена: " + err.message, true);
  }
  loadBid();
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
// ---------- строка этапов в шапке конкурса (0.30.0) ----------
// Документы → анализ → запросы → цены → заявка: что сделано, что идёт, что не начато; щелчок — к разделу.
let stg = {};
function resetStages() { stg = {}; renderStages(); }
function renderStages() {
  const box = $("head-stages");
  if (!box) return;
  const items = [];
  const add = (id, label, text, cls) => { if (!$(id) || $(id).hidden) return; items.push({ id, label, text, cls }); };
  if (stg.docs) add("docs", "Документы", stg.docs.n ? `${stg.docs.n} файл.` + (stg.docs.pending ? ` · ждут ${stg.docs.pending}` : "") : "нет", stg.docs.n ? (stg.docs.pending ? "busy" : "ok") : "none");
  if (stg.analysis) add("analysis", "Анализ", { done: "готов", busy: "идёт…", none: "не делали" }[stg.analysis], { done: "ok", busy: "busy", none: "none" }[stg.analysis]);
  if (stg.req) add("requests", "Письма", stg.req.sent ? `ответили ${stg.req.answered} из ${stg.req.sent}` + (stg.req.waiting ? ` · ждут ответа ${stg.req.waiting}` : "") : "не отправляли",
    stg.req.sent ? (stg.req.answered && !stg.req.waiting ? "ok" : "busy") : "none");
  if (stg.offers) add("offers", "Цены поставщиков", stg.offers.withPrices ? `${stg.offers.withPrices} из ${stg.offers.total} поз.` + (stg.offers.written ? ` · в NocoDB ${stg.offers.written}` : "") : "нет", stg.offers.withPrices ? "ok" : "none");
  if (stg.bid) add("bid", "Заявка", { done: "собрана", draft: "черновик", busy: "готовится…", none: "не готовили" }[stg.bid], { done: "ok", draft: "busy", busy: "busy", none: "none" }[stg.bid]);
  if (stg.brands) add("spec", "Наши бренды", `${stg.brands.positions_matched} из ${stg.brands.positions_total} поз.`, "info");
  replaceSafe(box, ...items.map((x) => {
    const a = el("a", { href: "#" + x.id, class: "stage " + x.cls }, el("span", { class: "stage-l", text: x.label }), document.createTextNode(" " + x.text));
    a.addEventListener("click", (e) => { e.preventDefault(); goTo(x.id); });
    return a;
  }));
  box.hidden = !items.length;
}

// ---------- вкладки конкурса (0.30.0) ----------
// «Конкурс» (документы, анализ, NocoDB, спецификация), «Закупка» (предложения, спецификация, цены), «Письма» (0.35.0),
// «Заявка», «Чат»; видна одна вкладка (разделы чужих вкладок скрыты CSS по main[data-tab] и section[data-group]).
// 0.38.0: вид «по позициям» и «☰ одной страницей» убраны (пользователь 26.11.2026: «работают некорректно, раздувают код»). Выбор запоминается в браузере (sb.tab); без выбора — по роли: кто не загружает
// документы, но вносит цены (закупщик), — «Закупка», остальные — «Конкурс». Вкладка без видимых разделов скрыта.
const TAB_KEY = "sb.tab";
const TABS = ["tender", "spec", "purchase", "web", "bid", "chat"];
// Раздел может быть в нескольких вкладках (data-group="tender purchase": спецификация — и в «Конкурсе», и в «Закупке»,
// 0.30.1 — закупщику позиции нужны под рукой).
function tabSections(tab) { return [...document.querySelectorAll(`main > section[data-group~="${tab}"]`)]; }
function groupsOf(sec) { return ((sec && sec.dataset.group) || "").split(/\s+/).filter(Boolean); }
function tabVisible(tab) {
  return tabSections(tab).some((x) => !x.hidden);
}
function defaultTab() {
  const b = document.body.classList;
  const buyer = b.contains("no-upload") && !b.contains("no-purchase");
  return buyer ? "purchase" : "tender";
}
function currentTab() { return document.querySelector("main").dataset.tab || ""; }
function setTab(tab, remember = true) {
  if (tab === "mail") tab = "purchase";           // вкладки «Письма» с 0.43.0 нет — письма в «Закупке»
  if (!TABS.includes(tab)) tab = defaultTab();
  if (!tabVisible(tab)) tab = TABS.find(tabVisible) || "tender";
  document.querySelector("main").dataset.tab = tab;
  for (const a of document.querySelectorAll("#jump-nav .jump")) {
    const on = a.dataset.tab === tab;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
  }
  if (remember) storageSet(TAB_KEY, tab);
  ensureRequests();
}
// Перейти к разделу: сначала его вкладка (если не «одной страницей»), потом прокрутка к нему.
function goTo(id) {
  const sec = $(id);
  if (!sec) return;
  const groups = groupsOf(sec.closest("section[data-group]") || sec);
  if (groups.length && !groups.includes(currentTab())) setTab(groups[0]);
  requestAnimationFrame(() => sec.scrollIntoView({ behavior: "smooth", block: "start" }));
}

(function tabsNav() {
  const wrap = $("jump-wrap");
  const nav = $("jump-nav");
  const header = document.querySelector(".topbar");
  const headerHeight = () => header.getBoundingClientRect().height;
  const stickyHeight = () => headerHeight() + (wrap.hidden ? 0 : wrap.getBoundingClientRect().height);
  function sync() {
    wrap.hidden = $("head").hidden;         // конкурс не выбран — вкладок нет
    for (const a of nav.querySelectorAll(".jump")) a.hidden = !tabVisible(a.dataset.tab);
    if (!wrap.hidden) {
      const tab = currentTab() || storageGet(TAB_KEY) || defaultTab();
      if (!currentTab() || !tabVisible(currentTab())) setTab(tab, false);
    }
    const root = document.documentElement.style;
    root.setProperty("--topbar-h", headerHeight() + "px");
    root.setProperty("--sticky-h", stickyHeight() + "px");
    wrap.classList.toggle("stuck", !wrap.hidden && window.scrollY > 0 && wrap.getBoundingClientRect().top <= headerHeight() + 0.5);
  }
  nav.addEventListener("click", (e) => {
    const a = e.target.closest(".jump");
    if (!a) return;
    e.preventDefault();
    setTab(a.dataset.tab);
    const top = $("head").getBoundingClientRect().bottom + window.scrollY - stickyHeight();
    if (window.scrollY > top) window.scrollTo({ top: Math.max(0, top), behavior: "auto" });
  });
  // Раздел показали / скрыли (выбор конкурса, права, админ-панель) — пересчитать видимые вкладки.
  const mo = new MutationObserver(sync);
  for (const sec of document.querySelectorAll("main > section")) mo.observe(sec, { attributes: true, attributeFilter: ["hidden"] });
  window.addEventListener("scroll", () => wrap.classList.toggle("stuck", !wrap.hidden && window.scrollY > 0 &&
    wrap.getBoundingClientRect().top <= headerHeight() + 0.5), { passive: true });
  window.addEventListener("resize", sync);
  const ro = new ResizeObserver(sync);
  ro.observe(header);
  ro.observe(wrap);
  sync();
})();

// ---------- кнопка «наверх» (0.17.3) ----------
// Появляется, когда страница прокручена вниз больше чем на экран; нажатие — к началу страницы (плавно, если в системе
// не отключена анимация). Вкладки-переходы подсветку пересчитают сами — по прокрутке.
(function toTop() {
  const btn = $("to-top");
  let frame = 0;
  // Пока человек печатает (поле ввода в фокусе — на телефоне открыта клавиатура), кнопка не закрывает «Спросить».
  const typing = () => { const a = document.activeElement; return Boolean(a && a.matches("textarea, input, select")); };
  const update = () => { frame = 0; btn.hidden = typing() || window.scrollY < Math.max(400, window.innerHeight * 0.8); };
  window.addEventListener("scroll", () => { if (!frame) frame = requestAnimationFrame(update); }, { passive: true });
  window.addEventListener("resize", update);
  document.addEventListener("focusin", update);
  document.addEventListener("focusout", () => setTimeout(update, 0));
  btn.addEventListener("click", () => {
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: smooth ? "smooth" : "auto" });
    btn.blur();
  });
  update();
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
  if (off.has("prices")) { allowed.delete("prices"); allowed.delete("purchase"); }
  if (off.has("analysis")) allowed.delete("analyze");
  if (off.has("chat")) allowed.delete("chat");
  if (off.has("prices")) allowed.delete("offers");
  for (const k of ["upload", "analyze", "prices", "purchase", "offers", "jobs", "chat"]) document.body.classList.toggle("no-" + k, !allowed.has(k));
  $("me-name").textContent = d.me.name === d.access.role_title ? d.me.name : `${d.me.name} · ${d.access.role_title}`;
  $("me-name").title = `Логин: ${d.me.login}`;
  $("me-admin").hidden = !d.access.admin_panel;
  $("me-mail").hidden = !allowed.has("purchase");
  $("me-suppliers").hidden = !allowed.has("purchase");
  $("me-box").hidden = false;
}
$("me-logout").addEventListener("click", async () => {
  try { await fetch("/api/auth/logout", { method: "POST", headers: { "X-SupplyBro": "1" } }); } catch (_) { /* всё равно на вход */ }
  location.href = "/login";
});

loadMe().finally(() => loadClients().then(pollActivity));
