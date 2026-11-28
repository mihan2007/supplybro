# Вход, роли, права, сотрудники

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний.

## Учётные записи и админ-панель

- `app/auth.py` + `app/admin.py` + `static/admin.*` — учётные записи сотрудников и админ-панель `/admin` (0.6.0).
  Таблицы `users` / `sessions` / `user_events` в app.db (с 0.11.0 у `users` колонка `blocks_off`); пароли — scrypt; cookie `sb_session`
  (HttpOnly, SameSite=Strict; с 0.17.0 — Lax), запросы админ-панели и входа — с заголовком `X-SupplyBro: 1`. Роли `auth.ROLES` (admin,
  client_admin, tender, buyer, logist, viewer), права `ROLE_PERMISSIONS`, клиент — `["id"]` (с 0.12.0 один) или `["*"]`
  (администратор сайта). Пароль: ≥ 10
  символов, без логина внутри. Сброс пароля: `python -m app.auth reset-password ЛОГИН`. Учётные записи для начала
  (решение пользователя 03.11.2026 — по одной на клиента): `python -m app.auth setup-accounts` → admin (все клиенты),
  nasha (tender, main), sargsyan (tender, sargsyan); существующие не трогает (`auth.STANDARD_ACCOUNTS`).
- `app/login.py` + `static/login.*` — страница входа `/login` (0.6.4). Адреса `/api/auth/state|login|logout|password`;
  помощники (`_guard`, `_set_cookie`) — из `admin.py`, сеанс общий с админ-панелью. При `LOGIN_REQUIRED` после входа
  и смены временного пароля — сразу на `?next=` (только свой адрес, не `/login`) или на свою страницу (admin → «/»,
  остальные → `/user`); без него — карточка «что будет доступно». Тестовые учётные записи: `python -m app.auth
  demo-users` (`auth.DEMO_USERS`, узнаются по заметке `auth.DEMO_NOTE`, `--delete` — удалить только тестовые);
  `setup-accounts` предупреждает, если они есть (при включённом входе ими можно войти).
- Страницы: «/» — страница администратора (решение пользователя 29.10.2026); `/user` — страница сотрудника (0.6.2):
  та же `index.html`, сервер ставит в `<body>` `class="user-mode" data-mode="user"`; элементы `.admin-only` (выбор
  модели анализа, модели чата, поисковика цен) скрыты, `app.js` (`USER_MODE`) их значения не отправляет.

## Вход и защита сайта

- Вход на сайт (0.7.0, `app/access.py`) — решение пользователя 03.11.2026 «делай вход для админа и для пользователей».
  `LOGIN_REQUIRED=1` (по умолчанию в `.env.example` и `compose.yml`; 0 — сайт без входа, как до 0.7.0). Тогда
  `ADMIN_PANEL` / `USER_PAGE` / `LOGIN_PAGE` считаются включёнными (`access.*`). Одна «проходная» — ASGI
  `access.LoginMiddleware` перед всеми адресами: открыто без входа `/login`, `/health`, `/admin`, `/static/*`,
  `/api/auth/*`, `/api/admin/*` (у админ-панели свои проверки); без сеанса или с несменённым временным паролем —
  страницы на `/login?next=…` (303), `/api/` — 401; изменяющий запрос с чужим `Origin` — 403; «/» не-admin → `/user`;
  `/api/clients/<id>/…` — только клиенты из `user.clients` (403); GET — право `view`; изменяющие — таблица
  `access.RULES` (upload / analyze / prices / jobs / chat), неизвестный изменяющий — только admin; удачное действие из
  RULES — в `user_events` (actor = логин, details = клиент · папка). Кто вошёл — `access.current_user()` (contextvar,
  доходит до синхронных адресов и потоковых ответов). `/api/clients` фильтрует клиентов; `access.is_admin()` —
  модель анализа / чата и поисковик цен принимаются только от admin (иначе — настройки сайта). Страница: `loadMe()` в
  `app.js` берёт `/api/auth/state` — имя в шапке (`#me-box`), «Админ-панель», «Выйти», классы `no-upload` /
  `no-analyze` / `no-prices` / `no-jobs` / `no-chat` на `<body>` прячут `.need-*` и `.job-ctl`; 401 от `/api/` —
  `toLogin()`.
  0.16.0 (пользователь 08.11.2026: сайт на сервере открыт в интернет через Caddy — «без входа не должно открываться
  ничего, кроме страницы входа»; отдельную `REQUIRE_LOGIN` не заводили — это `LOGIN_REQUIRED`): `LOGIN_REQUIRED` по
  умолчанию 1 и в коде (`not _off(...)`: выключает только явное 0 / false / no / off). Без входа открыто только `OPEN_EXACT` (`/login`, `/health`, `/favicon.ico`),
  `OPEN_STATIC` (app.css, admin.css, login.css, login.js — что нужно странице входа), `/api/auth/*`; `/admin` → вход,
  `/api/admin/*` (`ADMIN_API`) — 401 без сеанса, с сеансом — без проверок проходной (права — `admin._admin`), `/admin`
  не-MANAGER_ROLES → `home()`; `/api/admin/setup` при входе — 403 (первый администратор — только `setup-accounts`);
  `/static/*` без входа — 401 JSON (не переход на вход); путь с «.» / «..» / «\» — 404 (`_bad_path`); websocket — закрыт;
  FastAPI `docs_url` / `redoc_url` / `openapi_url` = None. `LoginMiddleware._run` ставит `CURRENT_USER` и `db.ACTOR`.
  `access.SecurityHeaders` (всегда, снаружи проходной): nosniff, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy:
  same-origin`, HSTS при `scheme == https`. Cookie `Secure` — по `request.url.scheme` (за Caddy — из
  `X-Forwarded-Proto`, `FORWARDED_ALLOW_IPS` в compose.yml). Подбор паролей (`auth.login` под `_LOGIN_LOCK` — попытки
  строго по одной, иначе одновременные запросы обходили счёт): `ip_wait` — `MAX_FAILED_IP` = 20 «неудачный вход» с
  адреса за `IP_WINDOW_MINUTES` = 15 → адрес закрыт (пароль не проверяется, в журнал не пишется); `account_ip_wait` —
  `MAX_FAILED` = 5 подряд к учётной записи с адреса (после последнего события из `RESET_ACTIONS`: вход / вход открыт /
  пароль сброшен) за `LOCK_MINUTES` → закрыт вход в неё только с этого адреса (чужой не может держать вход сотрудника
  закрытым); `users.failed` / `locked_until` — вся учётная запись после `MAX_FAILED_TOTAL` = 50 подряд; индекс
  `user_events_ip`. Адрес возврата после входа (`login.js safeNext`, `access.safe_next`) — без пробелов и служебных
  символов («/<Tab>/чужой.сайт»), сверка `new URL(...).origin`. `/docs/file` — `main._file_block`: AIExtract → analysis
  / prices (prices.json, Цены_поиск, pricelists) / bid (Заявка, bid.json, history/Заявка, Поставщикам), папка «прайс…»
  → prices; выключенный раздел — 403. `admin.page()` — admin.html / login.html со ссылками `?v=<версия>`. `login.js`: при `required`
  в «Учётных записей ещё нет» скрыта ссылка на /admin (`#e-admin`); `admin.js`: 401 от `/api/admin/state` → `/login?next=/admin`.
  Задачи: `jobs.started_by` (логин, `db.init` добавляет колонку), `db.enqueue(..., by=None)` — по умолчанию
  `db.ACTOR` (на сайте — вошедший, в обработчике `lane()` ставит `db.job_by(job)` перед `run_job` — цепочка от его
  имени); у ждущей задачи остаётся первый `started_by` (пустой — дописывается). `main.job_view`: задача клиента вне
  доступа (`_foreign`) — `OTHER_CLIENT` «конкурс другого клиента», без шага / сообщения / логина / client_id, так же
  `waiting_for`. `app.js`: `byText()` — « · запустил <логин>» в `queuedText` / `runningTail` / `jobLine`.
- Сеанс (0.17.0, «чтобы не надо было постоянно входить»): «не выходить на этом компьютере» (`#l-remember`, по
  умолчанию) — `sessions.remember=1`, срок `SESSION_DAYS` = 30 дней от последнего посещения (`session_user` продлевает
  при обновлении `seen_at`, раз в 5 мин), не дольше `SESSION_MAX_DAYS` = 365 от входа, cookie Max-Age = год; без
  галочки — `SHORT_SESSION_HOURS` = 12, cookie до закрытия браузера. Cookie SameSite=Lax (было Strict — по ссылке
  из почты / NocoDB приходили «не вошедшими»); от CSRF — Origin в проходной и X-SupplyBro у входа и админ-панели.
- Страница-приветствие `/start` (0.17.0; с 0.17.1 — макет `landing.html`, одобренный пользователем 04.11.2026 в другой
  сессии по ТЗ «Стартовая страница SupplyBro», https://claude.ai/artifact/VieXAUThsvaPnGig7ZZXme): `static/start.html`
  (стили и скрипт внутри; Source Serif 4 переменный (opsz, @font-face 500–600 — как отдавал Google) + Inter
  400/500/600, `static/fonts/`, subsets cyrillic / latin / latin-ext, OFL; цвета `--ink #1B2430`, маркер `#FFD400`),
  `main.start_page`; открыта без входа (`access.OPEN_EXACT`, `/static/fonts/` в `OPEN_PREFIXES`, `/robots.txt` —
  Disallow), гость на «/» без параметров → `/start` (с параметрами — на вход с `next`); `.login-link` по
  `/api/auth/state`: вошёл — «Открыть SupplyBro» на `access.home.url`. Регистрации нет (в макете были `/register` —
  убраны до решения пользователя: убрать / «Запросить доступ» с контактом / заявка на доступ в админ-панели). Тексты —
  из макета, кроме исправленных неточностей (см. CHANGELOG 0.17.1). Данных конкурсов на странице нет, noindex.

## Разделы для каждого сотрудника

- Разделы для каждого сотрудника (0.11.0, решение пользователя 04.11.2026: «у каждого пользователя в админ-панели
  опция отключать тот или иной блок»): `auth.BLOCKS` (docs, analysis, prices, bid (0.15.0), card, spec, chat — ключи совпадают с
  `data-target` вкладок и id разделов), `users.blocks_off` (JSON-список; колонку добавляет `auth.init()` через ALTER),
  `auth.blocks_off(user)`. Сервер: `access.BLOCK_PATHS` — выключенный раздел отвечает 403 и на чтение (`/docs*`,
  `/analysis*`, `/prices*` + `/pricelists*`, `/tenders/<id>/spec`, `/chat*`); карточка `/tenders/<id>` не закрывается
  (из неё шапка) — при выключенном `card` без полей / представлений / ссылки на запись, при выключенном `spec` — без
  ссылки на спецификацию (`access.block_on`). Чат: без `prices` — в `<prices>` отказ, без `analysis` — без `<analysis>`.
  Страница: `/api/auth/state` → `access.blocks`, `loadMe()` ставит `off-<раздел>` на `<body>` (а off docs / prices /
  analysis / chat снимают и права upload / prices / analyze / chat → прячутся `.need-*`); `blockOff()` в `showTender`
  и в начале загрузчиков разделов. Админ-панель: «Разделы на странице конкурса» в карточке (`#ed-blocks`), «выключено:
  …» в списке, PATCH `blocks_off` (белый список полей в `admin.update`), журнал — «выключенные разделы: … → …».

## Клиенты и их сотрудники

- Клиенты и их сотрудники (0.12.0, решение пользователя 04.11.2026: «в админ-панели мы видим наших клиентов, у клиента
  могут быть сотрудники, у которых и должны быть эти роли»; сотрудник — в одном клиенте; управляют администратор сайта
  и администратор клиента; клиенты — только список из `clients.json`, новых добавляет Claude). `auth.ROLES` +
  `client_admin` («Администратор клиента»: права tender + `staff`), `admin` теперь «Администратор сайта»;
  `auth.MANAGER_ROLES` — кого пускать в админ-панель. `auth._one_client`: admin → `["*"]`, остальные — ровно один
  клиент (в `create_user` всегда, в `update_user` — когда пришли роль или клиенты), иначе AuthError; `auth.client_of`.
  Старые записи с `["*"]` / несколькими клиентами работают как раньше, пока их не откроют и не сохранят.
  `admin.py`: `_admin` пускает MANAGER_ROLES (администратору клиента без клиента — 403); `_scope` (None — админ сайта,
  иначе id клиента), `_in_scope` (админ клиента видит только не-admin с `clients == [свой]`), `_target` (чужой → 404) —
  на всех адресах сотрудника; `_scoped_data`: админ клиента — клиент всегда свой, роль admin — 403; своя роль — 400
  (у всех). `/api/admin/users` и `/events` фильтруются (`auth.events(..., among=ids)`); `/api/admin/state` отдаёт
  `site_admin`, `scope`, `assignable_roles`, клиенты (с `folder`) — администратору клиента только свой.
  `login._access.admin_panel` — для MANAGER_ROLES. На сайте админ клиента — как сотрудник (`/user`, модели не выбирает:
  `access.is_admin()` только admin). `static/admin.js`: `groupsOf()` — «Администраторы сайта» / клиенты / «Без клиента»
  (`.u-group[data-group]`, строки `tr[data-login]`, кнопки `button[data-add]`), карточка — `#ed-client` (один клиент,
  у админа клиента заблокирован, для роли admin скрыт `#ed-client-box`), `#ed-role` из `assignable_roles`, своя роль
  заблокирована; `openEditor(u, preset)`.
