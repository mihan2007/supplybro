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
const seq = { tenders: 0, card: 0, spec: 0, docs: 0, an: 0 };
let docsTimer = null;
let anTimer = null;
let anLastJob = null;   // последняя увиденная задача анализа — чтобы заметить её окончание
let anTab = "fields";

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
  anLastJob = null;
  if (t.source === "nocodb") {
    await Promise.all([loadCard(), loadSpec(), loadDocs(), loadAnalysis()]);
  } else {
    renderFolderHead(t);
    await Promise.all([loadDocs(), loadAnalysis()]);
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
function renderJob(job) {
  const box = $("job");
  if (!job) { box.hidden = true; return false; }
  box.hidden = false;
  box.classList.remove("error", "done");
  const bar = $("job-bar");
  if (job.status === "queued") {
    bar.style.width = "0%";
    $("job-text").textContent = job.age > 20
      ? "В очереди… Обработчик не отвечает — проверьте, что сайт запущен через «Запустить SupplyBro.bat»."
      : "В очереди…";
    return true;
  }
  if (job.status === "running") {
    bar.style.width = Math.round((job.progress || 0) * 100) + "%";
    $("job-text").textContent = "Обработка: " + (job.step || "…");
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
    $("an-job-text").textContent = `${what}: в очереди…` + (job.age > 30 ? " (обработчик занят другой задачей или не запущен)" : "");
    return true;
  }
  if (job.status === "running") {
    $("an-bar").style.width = Math.round((job.progress || 0) * 100) + "%";
    $("an-job-text").textContent = `${what}: ${job.step || "…"}`;
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

function renderAnalysis(d, busy) {
  const r = d.result;
  const run = $("an-run");
  run.textContent = r ? "Повторить анализ" : "Запустить анализ";
  run.disabled = busy || !d.docs_with_text;
  run.title = d.docs_with_text ? "Анализ документов ИИ (Claude + перепроверка DeepSeek) и запись в NocoDB"
    : "Сначала загрузите документы — анализировать пока нечего";
  $("an-stale").hidden = !(d.stale && !busy);
  $("an-info").textContent = r
    ? `· ${r.analyzed_at_text} · ${r.models.claude}` + (r.seconds ? ` · ${Math.max(1, Math.round(r.seconds / 60))} мин` : "")
    : "";
  if (!r) {
    $("an-body").hidden = true;
    $("an-noco").replaceChildren();
    $("an-checks").replaceChildren();
    if (!busy) {
      setStatus("an-status", d.docs_with_text
        ? "Анализа ещё нет. «Запустить анализ» — ИИ извлечёт поля конкурса и позиции, перепроверит важные поля и запишет результат в NocoDB."
        : "Анализ появится после загрузки документов.");
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
    (u.cache_read ? `, из кэша ${n(u.cache_read)}` : "") + `, ответ ${n(u.output)}`);
  return parts.length ? "Токены — " + parts.join("; ") : "";
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
    await api(clientPath("/analysis/run"), { folder: t.folder }, { method: "POST" });
  } catch (err) {
    setStatus("an-status", "Не удалось запустить анализ: " + err.message, true);
  }
  loadAnalysis();
});
for (const b of document.querySelectorAll("#analysis .tab")) b.addEventListener("click", () => showAnalysisTab(b.dataset.tab));
$("reg-view").addEventListener("change", (e) => { state.regView = e.target.value; loadCard(); });
$("spec-view").addEventListener("change", (e) => { state.specView = e.target.value; loadSpec(); });
$("hide-empty").addEventListener("change", () => { const f = $("fields")._fields; if (f) renderFields(f); });
$("refresh").addEventListener("click", () => { state.fresh = true; loadTenders(); });

loadClients();
