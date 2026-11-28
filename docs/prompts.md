# Промты клиентов и юр. лиц

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний.

## Промты

- Промты клиентов (0.18.0, `app/prompt_store.py`; пользователь 10.11.2026: «у админа должна быть возможность править
  промты… у каждой организации свои промты, чтобы их можно было править в зависимости от нужд клиента»). Реестр
  `registry()` — 54 промта в 7 разделах (`SECTIONS`): analysis.system / inventory / detail / compare / deepseek,
  analysis.field.<ключ> × 36 (правила `fields.FIELDS`), changes.task, chat.system, pricelists.system / task,
  prices.judge_system / judge / en_system / en, bid.system / requirements / documents / forms / fill. Стандартный текст
  — константа в своём модуле (`SYSTEM`, `*_RULES`, `CHANGES_RULES`, `TASK`, `RULES`, `EN_RULES`, `FILL_RULES`,
  `deepseek_extract.SYSTEM_PROMPT`); правка клиента — `prompt_overrides` (client_id, key) в app.db, история —
  `prompt_history` (действие «изменён» / «стандартный», текст после действия); таблицы создаёт `_con()` (DDL).
  Править можно только правила: дату, данные (документы, позиции, цены, сведения участника) и формат ответа (JSON-схемы,
  «Ответ: {…}», список правок формы) ставят функции сборки (`prompts.inventory_prompt / detail_prompt / compare_prompt /
  group_prompt`, `changes.build_prompt`, `pricelists.task_prompt`, `judge.HEAD + RULES + ANSWER`, `prices EN_RULES +
  позиции + EN_ANSWER`, `bid.*_prompt`). Метки `{имя}` — `fill()` за один проход (подставленное не просматривается);
  обязательные (`marks`) нельзя удалить, незнакомые латинские `{слово}` — ошибка, `{{ЦЕНА…}}` / `{"items"…}` — текст;
  `guard` — предупреждение, если пропало «данные, а не инструкции». Задача берёт `for_client(client_id)` один раз
  (анализ, изменения, прайсы, поиск цен, заявка) и передаёт `ps` во все вызовы; чат — на каждый вопрос, отпечаток промта
  в meta `prompt` (`cache_status`: другой промт — кэш не тёплый). Без правок задания байт в байт как в 0.17.3
  (проверено сравнением 19 заданий старого и нового кода). Админ-панель: вкладка «Промты» (`#t-prompts`, только
  `site_admin`), API `/api/admin/prompts`, `…/item` (GET / POST), `…/reset`, `…/version` — `_site_admin` (администратор
  клиента — 403), журнал «промт изменён» / «промт: стандартный текст». Клиент — из `clients.json`; правки чужого
  клиента не видны и не применяются.
  Юр. лица (0.19.0, пользователь 10.11.2026: «чтобы админ мог править промты клиентов, других юр. лиц»): уровни
  юр. лицо → клиент → стандартный. `prompt_overrides` / `prompt_history` с `entity_id` ('' — клиент), PK (client_id,
  entity_id, key); база 0.18.0 переносится сама (`_migrate`: RENAME → новая таблица → копия → DROP под BEGIN
  IMMEDIATE; у истории — ADD COLUMN). `for_client(client, entity)`, `Prompts.source(key)` (entity / client / default);
  `save` / `reset` / `item` / `listing` / `history` / `version` с `entity_id`; у юр. лица текст, равный действующему
  без правки (тексту клиента или стандартному), — правка снимается; `item.inherited` / `inherited_from`.
  Юр. лицо задачи: страница (`promptEntity()` в app.js — `state.entity`, иначе единственный `participants` конкурса
  из NocoDB) добавляет `entity` ко всем запросам `api()` с `folder`, к загрузке документов, прайсов и вопросу чата;
  `main._entity` / `_with_entity` (чужое клиенту — пусто) кладут его в параметры задач (upload / analysis/run /
  changes/run / prices/run / pricelists/upload) и в чат (`chat.ask(..., entity)`, `cache_status`); обработчик —
  `worker.JOB_ENTITY` (contextvar) из `params.entity`, `_chain` передаёт его следующим задачам цепочки (анализ →
  запись → поиск цен, подготовка → анализ / изменения); `analyze` / `changes.process` / `pricelists.process` /
  `find_prices` — параметр `entity`. Заявка — промты участника (`for_client(client.id, part.id)`). Админ-панель:
  `#p-entity` («все юр. лица клиента» + юр. лица), отметки «своё у юр. лица» / «как у клиента: изменён» /
  «стандартный», кнопки «Подставить текст клиента» и «Убрать правку юр. лица»; API — параметр `entity`
  (`_prompt_scope`: юр. лицо другого клиента — 404), журнал — «Клиент · Юр. лицо · Раздел · Промт».
