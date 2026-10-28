// SupplyBro — логика страницы: выпадающие списки «клиент → юр. лицо → конкурс»,
// документы конкурса (папка в Dropbox), карточка из NocoDB и спецификация.
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
};
// Счётчики запросов: ответ на устаревший выбор не перерисовывает страницу.
const seq = { tenders: 0, card: 0, spec: 0, docs: 0, an: 0, chat: 0, pr: 0 };
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

async function api(path, query = {}, options = {}) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(query)) if (v) u.searchParams.set(k, v);
  if (state.fresh) u.searchParams.set("fresh", "1");
  const r = await fetch(u, options);
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
    $("version").textContent = "версия " + data.version;
    state.clients = data.clients;
  } catch (err) {
    setPickersHint("Не удалось загрузить список клиентов: " + err.message);
    return;
  }
  if (!state.clients.some((c) => c.id === state.client)) state.client = state.clients[0] ? state.clients[0].id : "";
  fillSelect($("client"), state.clients.map((c) => ({ value: c.id, label: c.name })), state.client);
  fillEntities();
  await loadTenders();
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
  if (!state.client) return;
  let data;
  try {
    data = await api(clientPath("/tenders"), { entity: state.entity });
  } catch (err) {
    if (my !== seq.tenders) return;
    fillSelect(select, [{ value: "", label: "Не удалось загрузить конкурсы" }]);
    setPickersHint(err.message);
    return;
  }
  if (my !== seq.tenders) return;

  const hints = [];
  if (data.noco_error) hints.push("NocoDB: " + data.noco_error);
  if (data.folders_error) hints.push("Папки конкурсов: " + data.folders_error);
  if (!data.participant_column_exists && !data.noco_error) {
    hints.push(`В «Реестре конкурсов» нет колонки «${data.participant_column}», поэтому юр. лицо известно только там, ` +
      `где агент записал «Участник: …» в перечне документов заявки. Остальные конкурсы — в группе «${UNKNOWN_GROUP}».`);
  }
  setPickersHint(hints.join(" "));
  state.nextNumber = data.next_number || "";
  state.tenders = new Map(data.tenders.map((t) => [t.id, t]));

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
    await loadTender();
  } else {
    state.tender = "";
    showTender(null);
    state.fresh = false;
  }
  saveUrl();
}

// ---------- новый конкурс ----------

// Файлы документации, выбранные в форме нового конкурса (загрузятся сразу после создания папки).
let newTenderFiles = [];

function showNewTenderForm(on) {
  $("new-tender").hidden = !on;
  $("new-tender-btn").hidden = on;
  if (on) {
    $("nt-number").value = state.nextNumber;
    $("nt-title").value = "";
    setNewTenderFiles([]);
    setStatus("nt-status", "");
    $("nt-title").focus();
  }
}

function setNewTenderFiles(files) {
  newTenderFiles = [...files];
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
  return data.folder;
}

// ---------- конкурс ----------

function currentTender() {
  return state.tenders.get(state.tender) || null;
}

function showTender(t) {
  $("head").hidden = $("docs").hidden = !t;
  $("analysis").hidden = !(t && t.folder);
  $("prices").hidden = !(t && t.folder);
  $("chat").hidden = !(t && t.folder);
  $("card").hidden = $("spec").hidden = !(t && t.source === "nocodb");
  $("empty").hidden = Boolean(t);
}

async function loadTender() {
  const t = currentTender();
  showTender(t);
  saveUrl();
  if (!t) return;
  stopDocsPolling();
  stopAnalysisPolling();
  stopPricesPolling();
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
  if (!t) return;
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
  if (!target.options.length || target._folder !== d.folder) {
    fillSelect(target, d.targets.map((x) => ({ value: x, label: x })), d.targets[0]);
    target._folder = d.folder;
  }
  renderDocs(d.files);
  const active = renderJob(d.job);
  // Файлы положили в папку вручную (не через сайт) — запускаем обработку сами, один раз за показ.
  const pending = d.files.some((f) => f.status === "pending");
  if (pending && !active && t._autoQueued !== d.folder) {
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

function renderJob(job) {
  const box = $("job");
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done");
  const bar = $("job-bar");
  if (job.status === "queued") {
    bar.style.width = "0%";
    $("job-text").textContent = "Обработка документов: " + queuedText(job);
    return true;
  }
  if (job.status === "running") {
    bar.style.width = Math.round((job.progress || 0) * 100) + "%";
    $("job-text").textContent = "Обработка: " + (job.step || "…") + runningTail(job);
    return true;
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

function uploadFiles(fileList, target) {
  const t = currentTender();
  if (!t || !t.folder || !fileList.length) return;
  const form = new FormData();
  for (const f of fileList) form.append("files", f, f.name);
  const q = new URLSearchParams({ folder: t.folder, target: target || $("upload-target").value || DOCS_TARGET });
  const xhr = new XMLHttpRequest();
  xhr.open("POST", clientPath("/docs/upload") + "?" + q);
  $("upload-progress").hidden = false;
  $("upload-bar").style.width = "0%";
  setStatus("docs-status", `Загрузка ${fileList.length} файл(ов)…`);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) $("upload-bar").style.width = Math.round((e.loaded / e.total) * 100) + "%";
  };
  xhr.onload = () => {
    $("upload-progress").hidden = true;
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch (_) { /* не JSON */ }
    if (xhr.status >= 200 && xhr.status < 300) {
      setStatus("docs-status", `Загружено: ${body.saved.length}. Документы поставлены в обработку` +
        (body.auto_analyze ? ", затем — анализ и запись в NocoDB." : "."));
      loadAnalysis();
    } else {
      setStatus("docs-status", "Не удалось загрузить: " + (body.detail || xhr.status), true);
    }
    loadDocs();
  };
  xhr.onerror = () => {
    $("upload-progress").hidden = true;
    setStatus("docs-status", "Не удалось загрузить: нет связи с сайтом", true);
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
  if (!t || !t.folder) { $("analysis").hidden = true; return; }
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
  if (finished) {
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
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done");
  const what = job.kind === "noco" ? "Запись в NocoDB" : "Анализ";
  if (job.status === "queued") {
    $("an-bar").style.width = "0%";
    $("an-job-text").textContent = `${what}: ` + queuedText(job);
    return true;
  }
  if (job.status === "running") {
    $("an-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("an-job-text").textContent = `${what}: ${job.step || "…"}` + runningTail(job) + (job.kind === "noco"
      ? ". У нового конкурса запись идёт 10–20 минут (настраиваются представления спецификации); подготовка и анализ других конкурсов её не ждут" : "");
    return true;
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
  $("an-stale").hidden = !(d.stale && !busy);
  $("an-info").textContent = r
    ? `· ${r.analyzed_at_text} · ${r.models.claude}` + (r.models.fallback ? ` → ${r.models.fallback}` : "") +
      (r.seconds ? ` · ${Math.max(1, Math.round(r.seconds / 60))} мин` : "") +
      (r.usage && r.usage.usd !== undefined ? ` · ≈ ${usd(r.usage.usd)}` : "")
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
  const btn = el("button", { class: "btn small" + (force ? "" : " primary"), type: "button", text: label });
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
    body.append(el("tr", {}, el("td", { text: f.label }), val, src, chk));
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
    const tr = el("tr");
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
  if (!t || !t.folder) { $("prices").hidden = true; return; }
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
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done");
  const what = job.kind === "noco_prices" ? "Запись цен в NocoDB" : "Поиск цен";
  if (job.status === "queued") {
    $("pr-bar").style.width = "0%";
    $("pr-job-text").textContent = `${what}: ` + queuedText(job);
    return true;
  }
  if (job.status === "running") {
    $("pr-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("pr-job-text").textContent = `${what}: ` + (job.step || "…") + runningTail(job);
    return true;
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
    engines.includes(savedEngine) ? savedEngine : s.engine);
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
      : "Поиск цен станет доступен после анализа документов.");
  } else {
    setStatus("pr-status", "");
  }
  if (!r) {
    $("pr-wrap").hidden = true;
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
  const checks = [`Артикулов: ${sm.groups}; Россия — цены у ${sm.rf_found} (все три — у ${sm.rf_full}); ` +
    (r.abroad === false ? "зарубеж не искали." : `зарубеж — у ${sm.abroad_found} (все три — у ${sm.abroad_full}).`)];
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
    line.append(el("span", { class: "muted", text: " " + a.domain + (abroad && a.currency && a.currency !== "RUB" ? ` · ${a.price} ${a.currency}` : "") +
      (abroad && a.country ? ` · ${a.country}` : "") + (a.original === "no" ? " · аналог" : ""),
      title: src ? "Нашёл: " + src + (a.via === "deepseek" ? "; страницу открыл DeepSeek" : a.via === "browser" ? "; страница открыта встроенным браузером" : "") : null }));
    box.append(line);
  }
  return box;
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
      el("td", {}, rf.length ? offerList(rf, false) : el("span", { class: "empty-val", text: "не найдено" })),
      el("td", {}, ab.length ? offerList(ab, true) : el("span", { class: "empty-val", text: r.abroad === false ? "не искали" : "не найдено" })),
      ratio, hist, how));
  }
  const table = $("pr-table");
  table.replaceChildren(el("thead", {}, head), body);
  $("pr-wrap").hidden = false;
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
  if (!t || !t.folder) return;
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
  $("chat-log").replaceChildren(...d.messages.map(renderChatMessage));
  $("chat-hint").textContent = "Ответы — только по документам этого конкурса, с источниками; цитаты программа сверяет с текстом. " +
    `Документы передаются модели один раз и дальше берутся из кэша (${d.cache_ttl === "1h" ? "час" : "5 минут"} после последнего вопроса) — около 10 % цены.`;
  scrollChat();
}

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
  const answer = el("div", { class: "msg assistant" }, el("span", { class: "typing", text: "Думаю…" }));
  log.append(answer);
  scrollChat();
  let text = "";
  let finished = false;
  try {
    const u = new URL(clientPath("/chat"), location.origin);
    u.searchParams.set("folder", t.folder);
    const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, model: $("chat-model").value }) });
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
          answer.replaceWith(renderChatMessage({ role: "assistant", content: text, meta: ev.meta }));
          finished = true;
        } else if (ev.t === "error") {
          answer.replaceWith(renderChatMessage({ role: "error", content: ev.text, meta: { error: ev.text } }));
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
  }
}

// ---------- спецификация ----------

async function loadSpec() {
  const my = ++seq.spec;
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

function jobLine(j, withFolder = true) {
  const head = (withFolder ? j.folder + " — " : "") + j.kind_title;
  if (j.status === "running") return `${head}: ${j.step || "…"}` + runningTail(j);
  if (j.status === "queued") return `${head}: ` + queuedText(j);
  const ago = j.finished_at ? fmtDur(Date.now() / 1000 - j.finished_at) + " назад" : "";
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
  }
  actSignature = sig;
}

function renderActivity(d) {
  const btn = $("activity");
  const panel = $("activity-panel");
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
  if (dead.length) {
    btn.textContent = "⚠ Обработчик не отвечает";
    btn.classList.add("bad");
  } else if (running.length || queued.length) {
    btn.textContent = `● В работе: ${running.length}` + (queued.length ? ` · в очереди: ${queued.length}` : "");
    btn.classList.add("busy");
  } else {
    btn.textContent = "● Обработчик свободен";
    btn.classList.add("idle");
  }
  const rows = [];
  for (const l of d.lanes) {
    const box = el("div", { class: "act-lane" });
    const laneState = l.seen === null ? "ещё не запускалась" : l.alive ? `работает (отклик ${fmtDur(l.seen)} назад)` : `не отвечает ${fmtDur(l.seen)}`;
    box.append(el("div", { class: "act-title", text: `Очередь «${l.title}» — ${laneState}` }));
    if (l.job) box.append(jobItem(l.job));
    for (const j of queued.filter((q) => q.lane === l.lane)) box.append(jobItem(j));
    if (!l.job && !queued.some((q) => q.lane === l.lane)) box.append(el("div", { class: "muted", text: "задач нет" }));
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
  panel.replaceChildren(...rows);
}

function jobItem(j) {
  const item = el("div", { class: "act-job " + j.status, title: "Открыть этот конкурс" });
  item.textContent = jobLine(j);
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
  setStatus("nt-status", "Создаю папку…");
  const files = newTenderFiles;
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
$("an-run").addEventListener("click", async () => {
  const t = currentTender();
  if (!t || !t.folder) return;
  $("an-run").disabled = true;
  try {
    const r = await api(clientPath("/analysis/run"), { folder: t.folder, model: $("an-model").value }, { method: "POST" });
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
    const r = await api(clientPath("/prices/run"), { folder: t.folder, engine, urgent: engine !== "deepseek" && $("pr-urgent").checked ? "1" : "",
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
$("chat-model").addEventListener("change", (e) => storageSet(CHAT_MODEL_KEY, e.target.value));
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

loadClients().then(pollActivity);
