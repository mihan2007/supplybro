# SupplyBro — локальный сайт для работы с конкурсами

Инструкция для Claude при работе с этим проектом. Состояние на 11.11.2026, версия 0.24.0 (этапы 1–3, чат по документации, стоимость, запасной DeepSeek, поиск цен,
полная цепочка без кнопок до NocoDB, пять очередей обработчика, состояние обработчика в шапке, отмена и пауза задач,
вход на сайт по логину и паролю с правами ролей и доступом к клиентам, вкладки-переходы, второй клиент ИП Саргсян,
прайсы поставщиков через чат с записью в «Отдел закупок», общая таблица цен, цены из выдачи «не проверено»,
изменения и разъяснения к разобранному конкурсу, цены из спецификации NocoDB и своей базы в «Все цены» и в чате,
разделы страницы для каждого сотрудника в админ-панели, клиенты и их сотрудники, администратор клиента, панель
«Конкурсы» с открытием конкурса в новой вкладке, поиск цен и без анализа на сайте — по спецификации NocoDB, подготовка
заявки одной кнопкой — по умолчанию на DeepSeek Flash, вход для сайта в интернете: без входа — только страница входа, «кто запустил» у задач, сайт — на копии базы NocoDB, кнопка «наверх», промты клиентов и юр. лиц в админ-панели, личные чаты, право «вносить цены поставщиков», базы NocoDB клиентов в админ-панели с проверкой перед переключением,
тестовый клиент «Тест» — папка ServerTest и тестовая база NocoDB, синхронизация с Dropbox на сервере через rclone, сервер двойным щелчком).

## Общение

- Отвечать по-русски.
- Пользователь — тендерный специалист, не программист: объяснять простыми словами, команды для Ubuntu
  давать готовыми к копированию и по одной (несколько команд в одном блоке он вставляет целиком — бывают ошибки).
- Ключи API в чат не просить и не выводить: только файлами в `secrets/`.

## Что это за проект

Перенос функционала проекта «Тендер Заполняй» (`C:\ClaudeProject\StrokTenderDataExtractor\StrokTenderDataExtractor`:
агент в приложении Claude по `CLAUDE.md`, скрипты `tools\*.py` — NocoDB, архивы, XML, формы, СДЭК, проверка цен)
в локальный сайт. **Проект `StrokTenderDataExtractor` существует отдельно и не меняется** — из него только читать
и копировать нужное в SupplyBro. План — страница Notion «Агент SupplyBro», этап 1: сайт на localhost — загрузка документов, разбор
и запись в NocoDB, чат по документам, выбор конкурса, клиенты и юр. лица, полный автомат без вопросов, защита от инъекций.
Потом — доступ сотрудникам (VPN) и, возможно, сервер.

## Окружение (компьютер desktop-41d1ipv, Windows 11)

- WSL2, `Ubuntu-24.04`, пользователь `user_1`. `C:\Users\User 1\.wslconfig`: `[wsl2] networkingMode=mirrored`,
  `[general] instanceIdleTimeout=-1` (Ubuntu не выключается сама).
- Пакеты Ubuntu — с `mirror.yandex.ru` (archive/security.ubuntu.com с этого адреса зависают).
- Docker 29.1.3, Compose 2.40.3, docker-buildx — из репозитория Ubuntu (не Docker Desktop), запускается systemd.
- Образы: зеркало `mirror.gcr.io` (`/etc/docker/daemon.json`) + прокси Windows `127.0.0.1:10809`
  (`/etc/systemd/system/docker.service.d/proxy.conf`). Docker Hub и dockerhub.timeweb.cloud отсюда не отвечают.
- Сеть: часть зарубежных сайтов из WSL напрямую недоступна, `mirror.yandex.ru` работает. Сборка (`BUILD_PROXY`)
  и запросы сайта (`OUTBOUND_PROXY`, NocoDB) идут через прокси Windows; без него новые библиотеки не скачать и NocoDB не ответит.

## Запуск

- Двойной щелчок: «Запустить SupplyBro.bat» (проверяет прокси, `run.sh`, ждёт `/health`, открывает браузер),
  «Остановить SupplyBro.bat». Автозагрузка: «Добавить в автозагрузку.bat» (кладёт файл в `shell:startup`,
  запуск с `/autostart` — свёрнуто, без браузера, прокси ждёт до 2 минут), «Убрать из автозагрузки.bat».
- Вручную в Ubuntu: `cd /mnt/c/ClaudeProject/supplybro && bash run.sh`.
- `run.sh`: ждёт Docker, дописывает в `.env` новые настройки из `.env.example`, проверяет файл ключа NocoDB,
  `docker build --network host` (прокси шагам сборки виден только в сети Ubuntu; compose так не умеет), `docker compose up -d`.
- Сайт: http://127.0.0.1:8000, только с этого компьютера. `compose.local.yml` (через `COMPOSE_FILE` в `.env`):
  `network_mode: host` + uvicorn на 127.0.0.1. На сервере — только `compose.yml` (bridge, порт на 127.0.0.1).
- Сервер (пользователь 08.11.2026): Kamatera, Ubuntu, папка `/opt/supplybro`, только `compose.yml` (без прокси),
  в интернет — через Caddy с HTTPS (`reverse_proxy 127.0.0.1:8000`). Поэтому вход обязателен (0.16.0): без входа —
  только `/login`. uvicorn верит `X-Forwarded-*` от `FORWARDED_ALLOW_IPS` (compose.yml: 127.0.0.1 и сети Docker).
- Адрес сайта (12.11.2026): https://www.supplybro.ru — домен в reg.ru (DNS ns1/ns2.reg.ru), A-запись `www` →
  113.30.189.244; у `supplybro.ru` (без www) A-записи 12.11.2026 не было — рекомендовано добавить `@` → тот же IP,
  после появления — `systemctl restart caddy` (сразу получить сертификат). Caddy на сервере — v2.11.9 из хранилища
  Cloudsmith (было 2.6.2 из Ubuntu — без сертификатов на IP). `/etc/caddy/Caddyfile` (копии: `.bak` — только xip,
  `.bak2` — без блока IP): `www.supplybro.ru` → `reverse_proxy 127.0.0.1:8000`; `supplybro.ru`,
  `113-30-189-244.eu-cloud-xip.com`, `http://113.30.189.244` → `redir https://www.supplybro.ru{uri}`;
  `https://113.30.189.244` — тоже redir, сертификат Let's Encrypt на IP (`tls { issuer acme { profile shortlived } }`,
  160 ч, Caddy продлевает сам; проверено 12.11.2026). Вход (cookie) у каждого адреса свой. В коде сайта адресов нет.
- Сервер с ноутбука (0.24.0): `server/server.sh` + `server/server.conf` (`SERVER=root@113.30.189.244`,
  `SERVER_DIR=/opt/supplybro`), `.bat` «Сервер - отправить версию» (deploy: `rsync -av --chmod=D755,F644
  --exclude .env --exclude secrets/ --exclude .git/ --exclude __pycache__/` + `ssh -t … bash run.sh`), «Сервер -
  терминал» (console), «Сервер - состояние» (status), «Сервер - подключить Dropbox» (`ssh -t -L
  53682:127.0.0.1:53682 … docker compose exec dropbox dropbox connect`), «Сервер - вход без пароля» (key:
  ssh-keygen ed25519 + ssh-copy-id). ssh — через `~/.ssh/config` Ubuntu (`Host 113.30.189.244`, ProxyCommand nc на
  прокси Windows); из PowerShell напрямую сервер не отвечает. Пользователь часто вставляет команды не в то окно —
  предлагать эти `.bat`.
- Синхронизация с Dropbox на сервере (0.22.0; с 0.23.0 — rclone): сервис `dropbox` в `compose.yml` с
  `profiles: ["dropbox"]` — включается только `COMPOSE_PROFILES=dropbox` в `.env` сервера (на ноутбуке — нет: там
  Dropbox для Windows). 0.22.0–0.22.1 — программа Dropbox для Linux в контейнере (Debian 12, затем Ubuntu 24.04 с
  приёмами islamdiaa/dropbox-docker): на сервере после привязки висела на «Starting...» (CPU ≈ 0, сеть < 1 МБ), после
  перезапуска теряла привязку (`instance2` в `~/.dropbox`), повторная чистая привязка не помогла — 11.11.2026
  заменена. С 0.23.0 `dropbox/Dockerfile` — `ubuntu:24.04` + rclone 1.75.2 с GitHub (сверка SHA256SUMS; 1.76.0 от
  06.11 моложе двух недель; версия — `ARG SB_RCLONE_VERSION`: имена `RCLONE_*` rclone читает как свои флаги), пользователь `ubuntu` (UID 1000) удаляется, `dropbox` — UID 1000. `dropbox/start.sh`
  (root: `chown 1000` `/config` и `/data`, затем `setpriv` в 1000, HOME=/config) → `dropbox/sync.sh`: нет remote
  `dropbox:` — ждёт (статус «не подключён»); первый раз и после кода 7 — `rclone bisync dropbox:$DROPBOX_FOLDER
  /data/$DROPBOX_FOLDER --resync --resync-mode newer` (отметка `/config/bisync/resync.done`), иначе bisync раз в
  `DROPBOX_SYNC_SECONDS` (60) с `--workdir /config/bisync --filters-file /config/dropbox-filters.txt` (копия
  `/etc/supplybro/dropbox-filters.txt` при запуске: bisync пишет рядом `.md5`, в `/etc` нельзя)
  --create-empty-src-dirs --resilient --recover --max-lock 2m --conflict-resolve newer --conflict-loser num
  --max-delete 25 -v`; статус — `/config/status.txt`. `dropbox/dropbox-cli.sh` → `/usr/local/bin/dropbox`:
  `connect` (`rclone config create dropbox dropbox` — без `--auth-no-open-browser`, его `config create` не знает; строки
  с token не выводятся; OAuth на
  127.0.0.1:53682 сервера — `network_mode: host`, с ноутбука туннель `ssh -L 53682:127.0.0.1:53682`), `status`,
  `ls`, `disconnect`. `dropbox/filters.txt` — `*.tmp`, `*.tmp.xlsx` (временные файлы сайта), `~$*`, `.~lock.*#`,
  `desktop.ini` и т. п. Тома: `dropbox-config` → `/config`, `DROPBOX_DIR` (`/opt/supplybro-dropbox`) → `/data`;
  сайту `TENDERS_DIR=/opt/supplybro-dropbox/Агент`. Журнал — json-file 3 × 10 МБ. `run.sh`: с профилем сначала
  `docker build … -t supplybro-dropbox:dev dropbox` и `up -d dropbox`; нет `TENDERS_DIR` — подсказка и выход 0.
  У Claude проверено только с подставным rclone (rclone, Docker Hub и dropbox.com из песочницы недоступны).

## Устройство

- `app/main.py` — адреса сайта и `/api/...`; `app/nocodb.py` — чтение NocoDB (кэш 2 мин; записи — только с
  `cache=True`; страницы — через одно соединение; 429 — повтор); `app/config.py` — настройки;
  `app/static/` — страница (`index.html`, `app.js`, `app.css`, без внешних библиотек).
- Порядок разделов страницы (0.6.6): pickers → вкладки → head → docs → analysis → prices → bid → card → spec → chat
  (чат внизу — решение пользователя 03.11.2026); без выбранного конкурса — pickers → `#tlist`.
- Панель «Конкурсы» (0.13.0, пользователь 04.11.2026: «вместо выпадающего списка при входе — панель с тендерами
  выбранного лица, чтобы открывать в отдельной вкладке; номер, заказчик, наименование оборудования, минимум сведений»).
  `/api/clients/{id}/tenders` читает ещё «Заказчик», «Начальная цена», «Валюта», «Статус конкурса (выиграли -
  проиграли)» (только существующие колонки) и добавляет к каждой записи `tender_list.brief(...)`: `customer` (без
  «Заказчик:», полного названия в скобках, организатора), `subject` (без ОКПД2 / КТРУ и «Список позиций»),
  `stage_short`, `deadline_at` {date, time, local}, `price` {value, currency, vat}, `result`, и `*_full` для подсказок;
  «Не указано…» — пусто (`NOT_FOUND`). `app.js`: `showTender(null)` → `#tlist` и `#pickers.mode-list` (поле «Конкурс»
  и «☰ Все конкурсы» скрыты), с конкурсом — `mode-tender`; `renderTenderList()` — фильтр `#tl-filter` (data — есть
  заказчик / предмет / номер или папка; open — срок подачи впереди; all), поиск `#tl-search` (`tlNorm`: k→к, ё→е),
  сортировка `tlSort` (no / price / deadline, без значения — внизу), localStorage `sb.tl.filter` / `sb.tl.sort`;
  строка — `tr[data-id]`, номер — `a.tl-no` с `target=_blank`, щелчок по строке — `window.open(tlHref(t))`
  (`?client&entity&tender`). `document.title` — номер · оборудование. Телефон — строки карточками (CSS grid-areas).
  Выпадающий список `#tender` остался на странице конкурса (смена в этой же вкладке).
  0.13.1 (пользователь: «жму на конкурс — карточка не открывается»: новая вкладка ~12 с читала весь реестр, 940
  записей, и была пустой): `loadClients` при `?tender=` — `openFast()` → `GET /api/clients/{id}/tender-brief?id=`
  (одна запись NocoDB или папка, `_noco_item` / `_folder_item` — те же строки, что в списке) → `loadTender()`, список
  грузится одновременно; `state.opened` — какой конкурс загружен, `loadTenders` не грузит его второй раз (кроме
  `state.fresh`); `#opening` — «Открываю конкурс…». `NocoClient.records`: все страницы через одно соединение
  `httpx.AsyncClient` (`_get(..., http)`; новое соединение через прокси — ~1 с на запрос: 940 записей 14 с → 5 с),
  `PARALLEL_PAGES` = 1 (NocoDB на 10 запросов сразу отвечает 429 — проверено), шаг — сколько NocoDB реально отдал,
  дочитывание без totalRows, без повторов по Id; `_get` на 429 ждёт 1,5 / 3 / 4,5 с и повторяет (`RETRY_429`);
  `cache=True` — список реестра в `_cache` на `CACHE_SECONDS` (fresh — заново). «/» и «/user» —
  `_page_html()`: `app.js` / `app.css` с `?v=<версия>`, `Cache-Control: no-cache`.
- Вкладки-переходы (0.6.5, панель — 0.6.6): `#jump-wrap` (подложка цвета фона, `position: sticky`, `top:
  var(--topbar-h)`) с `nav#jump-nav.panel` — панель под выбором конкурса, при прокрутке прилипает к шапке (класс
  `stuck` — тень). Ссылки `.jump[data-target]` на разделы `main > section` («top» — к началу страницы). Блок
  `jumpNav()` в конце `app.js`: `--topbar-h` и `--sticky-h` (шапка + панель) — из ResizeObserver; прокрутка
  `scrollIntoView` (отступ — `scroll-margin-top`), подсветка по положению (`pinned` — после нажатия, до прокрутки
  пользователем), вкладка скрыта вместе с разделом (MutationObserver на `hidden`), адрес не меняется. Телефон
  (≤ 760px): вкладки переносятся в две строки, шапка — `flex-wrap`. Не путать с `.tabs/.tab` внутри «Анализа».
  Решение пользователя 03.11.2026: страница одна, вкладки только переходят к разделам — не делать отдельные страницы;
  вкладки — заметной панелью, как блоки с данными.
  Кнопка «наверх» (0.17.3, бэклог Notion, пункт 1): `#to-top` (index.html, после `main`), `.to-top` в app.css —
  fixed справа внизу (z-index 40, под `.toast`), появляется при `scrollY` ≥ max(400, 0,8 экрана), блок `toTop()`
  после `jumpNav()` в app.js; `main` снизу 84px (телефон — 68px), чтобы кнопка не закрывала «Спросить» в чате.
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
- Личный чат и цены поставщиков (0.20.0, пользователь 10.11.2026: «у закупщика нет прав вносить цены… у логиста и
  закупщика должны быть чаты… вопросы друг друга они не должны видеть. Чат у закупщика должен уметь воспринимать
  прайс-листы, чтобы добавлять цены, в рамках закупочной процедуры»). Право `purchase` («Вносить цены поставщиков в
  NocoDB») — admin, client_admin, tender, buyer; `access.RULES` /pricelists/upload|write|remove → purchase (было
  prices); страница — `.need-purchase` / `body.no-purchase` (📎, «Цены из сообщения», галочки и кнопки карточек),
  карточки прайсов в чате (`loadPricelists`) — только с purchase. Прайсы без анализа на сайте —
  `pricelists.tender_positions` (анализ, иначе `prices.run.noco_positions`), `write_noco` — внутренний номер из
  анализа или `norm_number(папка)`. «Цены из сообщения» (`#chat-as-prices`) — текст поля → File «Сообщение
  поставщика ДД.ММ.ГГГГ ЧЧ-ММ.txt» → тот же `/pricelists/upload`. Личный чат: `chat_messages.user_login` (NULL —
  до 0.20.0, '' — без автора), `db.chat_add(owner)`, `chat_history` / `chat_clear(owner, with_unowned)`;
  `main._chat_owner()` — логин вошедшего, сообщения без автора видит admin (и сайт без входа); `chat.ask(owner)` —
  история для модели только своя; `cache_status` — по всем (кэш документов общий). `db.assign_chat_owners()` (из
  `auth.init`) — автор старых вопросов по журналу «задал вопрос в чате» («клиент · папка», 0–15 мин после вопроса;
  запись журнала — по окончании ответа), ответ — автору вопроса. Кнопка «наверх» прячется, пока поле ввода в фокусе.
- Базы NocoDB клиентов в админ-панели (0.21.0, `app/noco_bases.py`; пользователь 11.11.2026: «возможность
  переключаться к разным базам данных NocoDB… нужно, чтобы переключение происходило без проблем»). Выбор — таблица
  `client_bases` в app.db (создаёт `db.init`); `config.load_clients` читает её через `config.admin_bases()` (кэш
  `ADMIN_BASES_TTL` = 2 с, `forget_admin_bases()` после сохранения) — порядок: админ-панель → `NOCODB_URL_<КЛИЕНТ>` →
  `clients.json`, `NocoSettings.source` = admin / env / settings; `config.noco_layers`, `config.raw_clients`. Сайт и
  обработчик видят выбор сразу (идущая задача — со старой базой: клиент берётся в её начале); у ноутбука и сервера
  свои app.db. `noco_bases.check(client, url | base_id)` — только чтение NocoDB: другой адрес NocoDB — ошибка без
  запросов (ключ не уходит на чужой сервер); база / таблицы / реестр (из адреса или, по base_id, таблица «Реестр
  конкурсов…»: одна — она, несколько — с тем же названием, что сейчас, иначе где больше записей) — нет доступа /
  таблица другой базы / нет «Внутренний номер» → ошибка; предупреждения: нет колонок / представлений по сравнению
  с реестром сейчас, нет представления `default_registry_view`, нет спецификаций «К…», другие реестры в базе, база
  другого клиента. `save` (ошибки — `BaseError` → 400; предупреждения без `confirm` → `need_confirm`), `reset`,
  `state` (базы рабочего пространства с `used_by`). Админ-панель: вкладка «Базы NocoDB» (`#t-bases`, только
  `site_admin`), API `/api/admin/bases` (GET), `…/check`, `…/save`, `…/reset` (POST, X-SupplyBro) — `_bases_admin`;
  журнал «база NocoDB переключена» / «база NocoDB: как в настройках проекта». Безопасность смены базы:
  `noco_links.base_id` — `db.noco_link(client, internal, *, base_id)` (ссылка другой базы → None; пустая, до 0.21.0,
  — достаётся базе, где спросили впервые), `save_noco_link(..., *, base_id)` (одна строка на конкурс — запись в
  другой базе заменяет прежнюю); своя база цен — `prices.history.cache_path(client)` =
  `price_history_<клиент>_<база>.json`; `price_summary._SPEC_CACHE` и `cached_spec(client, folder)` — с базой в ключе.
- `app/auth.py` + `app/admin.py` + `static/admin.*` — учётные записи сотрудников и админ-панель `/admin` (0.6.0).
  Таблицы `users` / `sessions` / `user_events` в app.db (с 0.11.0 у `users` колонка `blocks_off`); пароли — scrypt; cookie `sb_session`
  (HttpOnly, SameSite=Strict; с 0.17.0 — Lax), запросы админ-панели и входа — с заголовком `X-SupplyBro: 1`. Роли `auth.ROLES` (admin,
  client_admin, tender, buyer, logist, viewer), права `ROLE_PERMISSIONS`, клиент — `["id"]` (с 0.12.0 один) или `["*"]`
  (администратор сайта). Пароль: ≥ 10
  символов, без логина внутри. Сброс пароля: `python -m app.auth reset-password ЛОГИН`. Учётные записи для начала
  (решение пользователя 03.11.2026 — по одной на клиента): `python -m app.auth setup-accounts` → admin (все клиенты),
  nasha (tender, main), sargsyan (tender, sargsyan); существующие не трогает (`auth.STANDARD_ACCOUNTS`).
- Страница-приветствие `/start` (0.17.0; с 0.17.1 — макет `landing.html`, одобренный пользователем 04.11.2026 в другой
  сессии по ТЗ «Стартовая страница SupplyBro», https://claude.ai/artifact/VieXAUThsvaPnGig7ZZXme): `static/start.html`
  (стили и скрипт внутри; Source Serif 4 переменный (opsz, @font-face 500–600 — как отдавал Google) + Inter
  400/500/600, `static/fonts/`, subsets cyrillic / latin / latin-ext, OFL; цвета `--ink #1B2430`, маркер `#FFD400`),
  `main.start_page`; открыта без входа (`access.OPEN_EXACT`, `/static/fonts/` в `OPEN_PREFIXES`, `/robots.txt` —
  Disallow), гость на «/» без параметров → `/start` (с параметрами — на вход с `next`); `.login-link` по
  `/api/auth/state`: вошёл — «Открыть SupplyBro» на `access.home.url`. Регистрации нет (в макете были `/register` —
  убраны до решения пользователя: убрать / «Запросить доступ» с контактом / заявка на доступ в админ-панели). Тексты —
  из макета, кроме исправленных неточностей (см. CHANGELOG 0.17.1). Данных конкурсов на странице нет, noindex.
- Сеанс (0.17.0, «чтобы не надо было постоянно входить»): «не выходить на этом компьютере» (`#l-remember`, по
  умолчанию) — `sessions.remember=1`, срок `SESSION_DAYS` = 30 дней от последнего посещения (`session_user` продлевает
  при обновлении `seen_at`, раз в 5 мин), не дольше `SESSION_MAX_DAYS` = 365 от входа, cookie Max-Age = год; без
  галочки — `SHORT_SESSION_HOURS` = 12, cookie до закрытия браузера. Cookie SameSite=Lax (было Strict — по ссылке
  из почты / NocoDB приходили «не вошедшими»); от CSRF — Origin в проходной и X-SupplyBro у входа и админ-панели.
- `app/login.py` + `static/login.*` — страница входа `/login` (0.6.4). Адреса `/api/auth/state|login|logout|password`;
  помощники (`_guard`, `_set_cookie`) — из `admin.py`, сеанс общий с админ-панелью. При `LOGIN_REQUIRED` после входа
  и смены временного пароля — сразу на `?next=` (только свой адрес, не `/login`) или на свою страницу (admin → «/»,
  остальные → `/user`); без него — карточка «что будет доступно». Тестовые учётные записи: `python -m app.auth
  demo-users` (`auth.DEMO_USERS`, узнаются по заметке `auth.DEMO_NOTE`, `--delete` — удалить только тестовые);
  `setup-accounts` предупреждает, если они есть (при включённом входе ими можно войти).
- Страницы: «/» — страница администратора (решение пользователя 29.10.2026); `/user` — страница сотрудника (0.6.2):
  та же `index.html`, сервер ставит в `<body>` `class="user-mode" data-mode="user"`; элементы `.admin-only` (выбор
  модели анализа, модели чата, поисковика цен) скрыты, `app.js` (`USER_MODE`) их значения не отправляет.
- `app/tender_list.py` (0.13.0) — короткие поля для панели «Конкурсы» из развёрнутых полей реестра (только разбор
  текста, без NocoDB).
- `app/worker.py` — обработчик задач (сервис `worker`); `app/db.py` — SQLite в `DATA_DIR`: очередь `jobs` (с 0.16.0 —
  `started_by`, кто запустил),
  сведения о файлах `doc_files`, поисковый индекс `doc_chunks` (FTS5).
- `app/docs/` — подготовка документов без ИИ: `pipeline.py` (порядок шагов), `extract.py` (текст из Word/Excel/PDF/картинок,
  автонумерация Word, скрытый текст, пометки обращений к ИИ; `EXTRACTOR_VERSION` — при изменении правил увеличить),
  `convert.py` (LibreOffice), `names.py` (исправление имён «Па®ЂЃ¶•≠®•» → «Приложение» только для показа).
- `app/tenders.py` — папки конкурсов в `Dropbox\Агент` (`TENDERS_DIR` → `/tenders` в контейнере), шаблон «К---».
  Документы конкурса (`doc_roots`) — `DOC_SUBFOLDERS` и папки `CHANGES_FOLDER_RE` («Изменения …», 0.9.0); без
  подпапок — вся папка.
  Новый конкурс: копия шаблона без AIExtract и без папок, где внутри AIExtract (материалы чужого конкурса —
  28.10.2026 в шаблоне «Наша\К---» лежала Documents_32616424721), плюс всегда `DOC_SUBFOLDERS` — иначе
  `doc_roots` берёт всю папку (так у старых папок без подпапок).
- `app/analysis/` — анализ ИИ (задачи `analyze` и `noco` обработчика): `run.py` (порядок шагов, проверки кодом,
  файлы в `AIExtract`), `llm.py` (Claude — ответ одним JSON-объектом, схема в тексте задания после документов, код
  приводит типы и переспрашивает; не `output_config.format` — при разных схемах не работал кэш документов;
  документы — одним блоком с кэшем; DeepSeek: `deepseek_json` / `DeepSeekJSON` (0.15.1, тот же `json_task`,
  схема и кэш ответов, что у Claude); повторы; учёт токенов), `corpus.py` (тексты без скрытого текста, порядок по важности,
  проверка цитат), `fields.py` (поля блоков 1–7 и правила из старого CLAUDE.md), `prompts.py` (задания и схемы),
  `spec_xlsx.py` (`Позиции_и_цены.xlsx`), `noco_write.py` (запись в NocoDB с защитой чужих записей).
- `app/chat.py` — чат по документации (сервис `web`, ответ потоком NDJSON): документы одним блоком + результаты
  анализа и цены (`<prices>` из `price_summary.chat_text`, 0.8.0; правило 6 задания — источник цены, «НЕ ПРОВЕРЕНО»,
  🔴, сравнение с НМЦ) + история — с метками кэша Anthropic (TTL `CHAT_CACHE_TTL`, по умолчанию 1h; при отказе API — повтор с 5m);
  DeepSeek — кэш автоматический; при превышении `CHAT_MAX_CHARS` — фрагменты из FTS5. «Источники:» в ответе
  проверяются `QuoteIndex`. История — `chat_messages`. Модели — `CHAT_MODELS`, по умолчанию `claude-haiku-5-5`.
  Чат доступен всегда, как только есть документы с текстом (анализ не нужен). `chat.cache_status` (0.6.3) — строка
  под чатом: готовятся / нет документов / «в кэше ещё N мин — вопрос ≈ $» / «не в кэше — первый вопрос ≈ $»;
  кэш Claude = последний ответ этой модели + TTL, сброс при изменении документов и смене дня (дата в задании).
- `app/prices/` — поиск цен (задача `prices` обработчика, отдельная очередь-поток, чтобы ожидание Яндекса не держало
  анализ): `run.py` (порядок: своя база → английские названия (DeepSeek Flash) → поиск (DeepSeek / Яндекс / оба) →
  страницы (сами → DeepSeek Fetch →
  Chromium) → проверка Haiku (`judge.py`: match / original / цена за ед.) → рынок по сайту/валюте (.by/.kz — зарубеж),
  защита от цен вне 0,15–10× НМЦ, продавец = сайт без поддоменов, зарубеж — сначала DE/CN/US → перепроверка 5.1i →
  3 лучшие цены → xlsx / `prices.json` / NocoDB; `carry_over` — перенос цен при повторном анализе; `_unverified`
  (0.8.0) — цены из заголовка / описания выдачи со знаком валюты у кандидатов без подтверждения модели, ≤ 3 продавца,
  те же пределы к НМЦ, поле группы `unverified` — только показ, не в xlsx и NocoDB), `articles.py`
  (артикулы, набор запросов Шага 5.1g ≥ 5 для России, `queries_abroad` — набор 5.1g для зарубежа: eBay/Amazon,
  AliExpress, Preis, price, английское название; `recheck_queries` — второй проход 5.1i),
  `dsearch.py` (DeepSeek Search / Fetch API, $0,75 за 1000, кэш выдачи на сутки в `DATA_DIR/dsearch`),
  `yandex.py` (Search API v2, отложенный / мгновенный режим, кэш ответов на сутки в
  `DATA_DIR/yandex`), `abroad.py` (веб-поиск Claude `web_search_20250305`; выключен, `PRICES_CLAUDE_ABROAD`), `fetch.py` (страницы:
  тайм-ауты 6/15 с; .ru/.рф напрямую, остальное через прокси; браузер — только «страницы из скрипта», ≤ 8 за проход,
  по 3; время этапов — `prices.json["timing"]`; Россия напрямую,
  зарубеж через прокси; Chromium `--dump-dom` для цен из JavaScript), `extract.py` (цена из JSON-LD / microdata /
  meta / текста), `judge.py` (Haiku: тот ли товар, цена за единицу), `fx.py` (курс ЦБ), `history.py` (своя база цен
  из спецификаций NocoDB, кэш 12 ч). В NocoDB пишет только колонки рынков и «Примечание» и только в спецификацию,
  созданную сайтом (`noco_links`).
  Без анализа на сайте (0.14.0, пользователь 04.11.2026: «искать цены всегда, даже если они были извлечены ранее» —
  кнопка была неактивна у конкурсов агента): `find_prices` берёт позиции из спецификации NocoDB (`noco_positions` →
  `price_summary.read_spec(ttl=0)` → `positions_from_spec`: №, наименование, марка, кол-во, ед., «Стоимость НМЦ»),
  `source="nocodb"`: analysis.json не создаёт, xlsx — `NOCO_XLSX` «Цены_поиск_сайта.xlsx» (в AIExtract агента лежит
  его «Позиции_и_цены.xlsx» — не трогаем), NocoDB — `skipped` (спецификация агента), в отчёте `source`, `xlsx`,
  `positions`. `POST …/prices/run` (async): поиск уже идёт — `already`; `_analysis_pending` (analyze или prepare с
  analyze) — при `AUTO_PRICES` ответ `after_analysis` без задачи, иначе 409; без анализа — `read_spec`, нет позиций —
  400; params `source=nocodb`. `GET …/prices`: `noco_positions` (по `cached_spec`, None — ещё не прочитана),
  `analysis_pending`, `positions` — из анализа, иначе из отчёта / спецификации, `estimate` по ним. Страница
  (`renderPrices`): неактивна только при идущем поиске, нет позиций (`noco_positions === 0` или «Все цены» этого
  конкурса без позиций — `psLast.folder`), нет ключа; после «Всех цен» без анализа — `loadPrices()` / `renderPrices`.
- `app/pricelists.py` (0.8.0) — прайсы / КП / счета поставщиков, загрузка через чат (📎 или перетаскивание; решение
  пользователя 04.11.2026). Файл — в папку конкурса «Прайсы, счета, КП» (или существующую с /прайс/i), список и
  результаты — `AIExtract/pricelists.json` (блокировка `fcntl` в `DATA_DIR/locks`, запись через `os.replace`). Задача
  `pricelist` (очередь `pricelists`): текст — `docs.extract` (.xls/.doc — через LibreOffice), строки с номерами,
  длинный документ — только шапка, хвост и строки с артикулами / словами позиций (`PRICELIST_MAX_CHARS`); модель
  `PRICELIST_MODEL` (Haiku, effort low) отвечает по схеме: поставщик, ИНН, НДС, валюта, дата, совпадения [№, строка,
  цитата, цена, валюта, exact|partial, примечание]. Код проверяет цитату (строка / документ / нет), цену в строке,
  0,15–10× НМЦ, переводит в рубли (курс ЦБ); проблемы → ⚠ и без галочки. Позиции — из анализа, без него (с 0.20.0) — из спецификации NocoDB (`tender_positions`). Запись — задача
  `noco_purch` (очередь NocoDB) по кнопке: отмеченные позиции в колонки `nc.PROCUREMENT_PAIRS` («Отдел закупок:
  поставщик N / цена N, руб.», до 10 мест — только существующие колонки: у К939 10, у К934 3, проверено 04.11.2026) спецификации — `noco_links`, иначе ссылка из записи реестра, иначе таблица по
  названию; тот же поставщик (`_supplier_key`) — его место, иначе первое пустое, все заняты — пропуск; неполное
  совпадение — «🔴 » перед поставщиком; строка в «Отдел закупок: комментарий» без повторов; PATCH по 100. Структуру
  не меняет: нет таблицы или колонок — ошибка. Чужие цены не перезаписывает (только пустые места и место этого же
  поставщика), поэтому пишет и в спецификацию, созданную агентом — кнопка пользователя и есть подтверждение.
  API: `GET /api/clients/{id}/pricelists`, `POST …/pricelists/upload|write|remove` (право `prices`, журнал).
- `app/analysis/changes.py` (0.9.0) — изменения и разъяснения к разобранному конкурсу. Решение пользователя
  04.11.2026: «прочитать дозагруженные документы и понять, в каких местах были внесены изменения, обновить данные,
  вывести комментарии»; остальное — рекомендованные варианты (в NocoDB — по кнопке и только изменённое; цены — только
  новые / изменённые позиции; папка «Изменения ДД.ММ.ГГГГ»). Загрузка: `POST …/docs/upload?target=@changes`
  (`changes.UPLOAD_TARGET` → папка «Изменения <сегодня МСК>»); если анализ уже есть (или идёт) — подготовка с
  `{"changes": true, "by"}` вместо `{"analyze": true}`, после неё задача `changes` (очередь docs). Кнопка «Разобрать
  изменения» — `POST …/analysis/changes/run` (тоже через подготовку). Новые документы — `new_docs`: по
  `result["doc_index"]` {rel_path: sha256} (пишет анализ с 0.9.0), у старых анализов — по `result["documents"]`;
  странице — `new_doc_names` по базе (без чтения текстов). Модель `CHANGES_MODEL` (пусто — `CLAUDE_MODEL`) получает
  только новые документы + текущие поля и позиции кратко (`build_prompt`, правила полей из `fields.py`) и отвечает
  по `SCHEMA`: documents, summary, fields [key, new_value, quote, file, comment], positions [changed|added|removed,
  n, changes {column из `POS_COLUMNS`, new_value}], comments, full_reanalysis. Код: то же значение (`_same` — без
  ✅/🔺 и «(расхождение…)») отбрасывает, цитату ищет `QuoteIndex` по новым документам — не найдена → `applied=false`
  («не применено»); changed/removed — только существующие №, added — только новые. Применение: копия анализа в
  `AIExtract/history/analysis_<id>.json`; поля (`changed` = {date, old, change_id}), позиции (Примечание
  «🔄 Изменение <дата> (<файл>): …», ТЗ дописывается, «Аналог (статус)» следом за «Аналог допускается», added —
  «🆕 Добавлена…», removed — убирается), product_name / quantity — `_compose_*`, проверка суммы НМЦК, `_save_files`;
  `analyzed_at` — только если менялись позиции (поиск цен по старым позициям тогда не запишет их); `doc_index`,
  `corpus_hash`, `changes` (кратко, для чата и страницы). Журнал — `AIExtract/changes.json` {"sets": [...]}
  (блокировка `fcntl` в `DATA_DIR/locks`). Цены: обработчик ставит `prices` с `{"only": [№]}` (новые позиции и с
  другим названием / маркой), если отчёт поиска уже есть и `AUTO_PRICES`; `find_prices(only=…)` ищет только их и
  сливает отчёт с прошлым (`report["partial"]`). NocoDB: `POST …/analysis/changes/write?change=<id>` →
  `noco.status=requested` → задача `noco_changes` (очередь NocoDB, `write_noco`): под `_WRITE_LOCK` запись реестра
  (noco_links или поиск по внутреннему номеру), PATCH только изменённых полей в том же виде, что запись анализа
  (`_registry_row`: прочерки, НМЦ с запятой, адрес, «Файл-источник»), колонки — только существующие; спецификация
  (`pricelists._spec_table`): changed — изменённые графы из `sanitize_spec_row` текущей строки + строка в
  «Примечание» (добавляется, без повторов), added — POST, removed — пометка «❌ Исключена…» (не удаляется). Пишет и в
  записи агента — кнопка пользователя = подтверждение. Страница: `#an-changes` — карточки разборов (последний
  раскрыт; разборы до повторного анализа целиком — свёрнуты, без кнопки), `#an-stale` — «Появились документы после
  анализа» с кнопками, отметки «🔄 изменено» в полях, подсветка позиций, кнопка «📎 Дозагрузить изменения». Чат —
  `changes.chat_lines` и «было: …» у изменённых полей в `<analysis>`. При повторном анализе целиком документы из папок
  «Изменения…» / «Разъяснения…» — приоритет 1 (`corpus._priority`).
- `app/bid/` (0.15.0) — подготовка заявки (задача `bid`, своя очередь), пользователь 05.11.2026: «кнопка подготовка
  документов должна быть на сайте, далее идёт полная подготовка, с минимумом вопросов» (инструкции агента
  PARTICIPATION.md + PREPARE_DOCS.md, шаги 7.1b–7.1d). `participant.load(client, entity)` — материалы юр. лица
  (`materials`): выжимка → `profile` (строки с паспортом / СНИЛС / датой рождения выкинуты — модели не передаются),
  карточка → `requisites` (`df.load_requisites`; паспорт — только для метки `{{ПАСПОРТ_РУКОВОДИТЕЛЯ}}`), опись →
  `library` (только существующие файлы; файлы «NNN …» без описи тоже). `run.prepare_bid`: (1) два вызова параллельно
  на общем кэше документов (`build_documents_block`, как анализ): `req_prompt` → requirements / structure / notes,
  `docs_prompt` → documents (n, title, part single/1/2/price/after_win, required yes/conditional/not_needed, kind
  form/tech/price/inventory/library/third_party/user/other, library + library_status, who, file_name, note, quote);
  цитаты проверяет `QuoteIndex`; max_tokens 64000 / 32000 (у Sonnet ответ состава ~37 тыс. токенов, из них ~24 тыс. —
  размышления); (2) `forms.outline` файлов Word документации (`.doc` → копия из `_converted` по sha256; заголовки
  «Форма / Образец / Приложение», стили заголовков и абзац после «Приложение … к письму») → `forms_prompt` →
  {doc_n, source, start, end} для документов kind form/tech/price/inventory с required=yes (кто готовит — не важно:
  форму документации сайт заполняет всегда); одна форма для двух документов — один раз (`also`); (3) по форме —
  вызов `fill_prompt` (разметка `forms.markup`: [А i] абзацы, [Т i] таблицы r/c, «(номер — автоматически)» —
  автонумерация Word) → правки par / replace / cell / cell_replace / add_rows / delete, вносит `forms.apply`
  (`replace_once` — точно или нестрого: любые подчёркивания, пробелы, неразрывные пробелы, кавычки; номера в ячейки с
  автонумерацией не пишутся; прежние «<нет данных>» сохраняют подсветку). `[[что нужно]]` → MARK + строка в GAPS;
  `{{МЕТКА}}` / `{{МЕТКА:№}}` (forms.TOKENS) — цены, НДС, прописью, паспорт — подставляет код. Опись — последней со
  списком файлов пакета. (4) `_assemble` (и `reapply` — без модели, по сохранённым правкам): прежний пакет →
  `AIExtract/history/Заявка <время>` (5 последних), копии из библиотеки (`_copy_library`, TIFF/JPG → PDF, .p7s/.sig/
  .xml/.zip не копируются), формы в `AIExtract/Заявка[/1 Первая часть|2 Вторая часть|3 Ценовое предложение|После
  победы]`, имя — `file_name` / «Форма N название» / название, повтор имени — `unique`; marks > 0 → «ЧЕРНОВИК »;
  `price_values` (процент или сумма с/без НДС → коэффициент к НМЦ; есть «Стоимость НМЦ» у всех позиций — цены
  позиций, сумма по ним; базис — `structure.price_basis`, «не указано» = без НДС; ставка — `materials.vat_rate`);
  самопроверка (wrong_values, ИНН, метки, анонимность первой части, цена > НМЦ); `Нет данных.txt`, `Перечень
  документов заявки.txt` (`docs_list_text`, формат агента), `_supplier_files` (только при полной подготовке; папка
  агента «Поставщикам» — не трогается → «Поставщикам (сайт)»; «HYDAC International» + «HYDAC» — одно письмо).
  Статусы документа: ready / copied / draft / update / urgent / manual / conditional / third_party / accountant /
  user / not_needed. Отчёт `AIExtract/bid.json` (plan, forms с правками, documents, checks, gaps, price,
  price_values, usage). `bid/noco.py` (задача `noco_bid`, очередь NocoDB, только по кнопке): перечень → «Перечень
  документов заявки», формы .docx → «ДС: декларативные формы» (прежние вложения ячейки остаются, кроме тех же имён
  без «ЧЕРНОВИК »); колонок нет — не создаёт, пишет в отчёт. `bid/money.py` — суммы и прописью. Адреса:
  `GET /bid` (`_bid_view`: без правок модели; files, usage_totals; models — только admin), `POST /bid/run?entity&model`
  (409, пока идёт подготовка документов или анализ), `POST /bid/price` {mode percent|total|none, value, vat} →
  задача bid с `reapply`, `POST /bid/noco`, `GET /bid/file?path` (только AIExtract/Заявка и Поставщикам*),
  `GET /bid/zip`. Права — `analyze` (`access.RULES`), раздел — `bid` (`BLOCK_PATHS`). Модель — `BID_MODEL`
  (0.15.1: `deepseek-flash`; DeepSeek — через `DeepSeekJSON`, остальное как у Claude), выбор администратора — из
  `BID_MODELS` (подписи с ценой — `BID_MODEL_LABEL` в app.js, localStorage `supplybro.bidModel2`); `BID_PARALLEL`,
  `BID_MAX_FORMS`. Код страхует ошибки модели: «% %», «начало / конец формы» (`forms.SERVICE_RE`), cell ↔ par по виду
  элемента, несколько номеров библиотеки (`run.library_numbers`). Страница: `#bid` — `loadBid` / `renderBid*`,
  вкладки `data-btab` (docs, need, req, files, notes), «не требуется» скрыты (`bidShowAll`), цена — форма `#bid-price` (`dataset.touched`). К933: Sonnet —
  44 документа, 12 форм, ≈ 7 мин, $1,5–2; DeepSeek Flash — 35 документов, 11 форм (тот же набор, без отдельного ПДн),
  3,5 мин, $0,11.
- `app/price_summary.py` (0.8.0, 0.10.0) — общая таблица цен: `build(folder, spec, hist_index)` — по позициям анализа
  (нет анализа — по строкам спецификации NocoDB: `positions_source` = analysis / nocodb) НМЦ, интернет (`chosen`
  rf / abroad + предложения рынков из NocoDB с `from_noco`, без дублей по ссылке / сайту и цене), `unverified`,
  поставщики (`pricelists.json` + «Отдел закупок»), `agent` («Цена агента 1–3» + комментарий), `market` («Рыночная
  цена, руб.» + «Источник (ссылка)»), `delivery` (СДЭК), `history` (своя база цен: `prices.history.lookup` по
  артикулам, без своего конкурса, до 3), лучшая — минимум проверенных (интернет, агент, рыночная, поставщики; без «не
  проверено», ⚠ и прошлых конкурсов). Решение пользователя 04.11.2026: «укажи цены, если они уже есть в базе данных»
  (конкурсы агента К922–К939 анализа на сайте не имеют). `read_spec(client, folder, record)` (async, NocoClient):
  таблица — ссылка из записи реестра, иначе `noco_links`, иначе по названию (внутренний номер); строки — без `fields`
  (NocoDB делит его по запятым, а в названиях колонок они есть — так «Отдел закупок» в 0.8.0 на настоящей базе не
  читался); кэш в памяти 60 с (`_SPEC_CACHE`), чат берёт прочитанное за 30 мин или читает сам (`spec_for_chat`,
  `asyncio.run`, до 15 с). `history_index(client)` — файл `DATA_DIR/price_history_<клиент>.json` обработчика (запись
  через `os.replace`); нет или старше `PRICES_HISTORY_HOURS` — сборка в фоновом потоке сайта (не чаще раза в 10 мин;
  на настоящей базе — 48 с). `GET /api/clients/{id}/prices/summary` (+ `noco_read`, `noco_error`, `history`). Страница: раздел «Цены» —
  вкладки `#pr-tabs` «Все цены» (`#ps-table`) / «Поиск в интернете» (`#pr-wrap`); карточки прайсов — в ленте чата
  (`renderChatLog` смешивает сообщения и карточки по времени, опрос раз в 3 с, пока идёт разбор / запись).
- `tools/` — копии скриптов старого проекта без изменений, используются как библиотека: `extract_archive.py`,
  `nocodb_client.py` (запись в реестр и спецификацию, представления, проверка записанного), `save_tender.py` (XML),
  `deepseek_extract.py` (задание DeepSeek для критичных полей), `check_prices.py` (проверка цен по Шагу 5.1g),
  `declarative_forms.py` (0.15.0: реквизиты, `set_par` / `fill_cell`, MARK, GAPS), `make_supplier_files.py` (0.15.0:
  письма поставщикам по производителям).
  Обновлять — копированием из старого проекта.
- `config/clients.json` — клиенты → база NocoDB → юр. лица. `main` «Наша компания» (ООО «Простор», ИНН 7842219678 —
  участник по умолчанию; ИП Строк А. В., ИНН 650110731107). У юр. лица `materials` (0.15.0, подготовка заявки):
  `dir` (папка материалов внутри папки клиента), `profile` (выжимка, *-шаблон), `requisites` (карточка .docx),
  `inventory` (опись .xlsx), `vat_rate` (22 — ООО «Простор»; нет — неизвестно, цены в формах не считаются),
  `wrong_values` (известные ошибки реквизитов — самопроверка). Нет `materials` — формы с «<нет данных>». `sargsyan` «ИП Саргсян» (0.6.1): база `pjfas6m5chjuosv`,
  реестр «Реестр конкурсов copy» `mdrzjzpmo43ocd9` (ссылка пользователя 29.10.2026; в той же базе есть и пустой
  «Реестр конкурсов» `m0ivow0ermg5889` с 68 колонками — не используется), тот же ключ NocoDB, папка «ИП Саргсян»,
  нумерация своя с К1, ИНН ИП Саргсян неизвестен. `test` «Тест» (0.21.0): папка `ServerTest`
  (`Dropbox\Агент\ServerTest`, на сервере — через Dropbox), тестовая база NocoDB «SupplyBro Тест» `p7il150axz5cnv7`,
  реестр `mpo13igxhwk3l1f`; юр. лица prostor / strok, материалы — `../Наша/#Материалы агента/…` (папка «Наша»).
  Подключается в контейнер только для чтения, правки — без пересборки.
- Несколько баз NocoDB: `tools/nocodb_client.py` держит базу/реестр в глобальных настройках — их меняет только
  `noco_write._configure` под `_WRITE_LOCK`; поиск цен (history, `_write_noco`) их не трогает, адреса по id таблицы.
  0.16.1: при импорте `noco_write` они пустые (в tools вписана старая база «Наша») — без `_configure` запрос не пройдёт.
- База NocoDB одной строкой (0.16.1, пользователь 08.11.2026: «сделай, чтобы я мог просто поменять данные базы
  данных»): `NOCODB_URL_<КЛИЕНТ>` в `.env` (`NOCODB_URL_MAIN`, `NOCODB_URL_SARGSYAN`, передаются в compose.yml) — адрес
  реестра из браузера → `config.noco_from_url` (workspace / base `p…` / table `m…`) поверх `clients.json`. Копия базы
  «Наша» для сайта (пользователь 08.11.2026): `py03vcjrohur7u7`, реестр `m9w2qkxcvt02nkb`, тот же ключ и workspace;
  с 0.17.2 (пользователь 10.11.2026: «привяжем пока к копии») она — в `clients.json` у `main`, сайт и на ноутбуке, и
  на сервере работает с ней без `.env`; старая база — только у агента в приложении; `NOCODB_URL_MAIN` — чтобы
  переключить сайт без правки `clients.json` (например, обратно на старую).
  Спецификации — `noco_tables.own_table`: таблица из другой базы → та же по названию в базе клиента, нет — None (в копии
  все 20 ссылок вели в старую базу, у всех есть пара); везде, где берётся id спецификации (main карточка / spec,
  price_summary, pricelists._spec_table, prices/run._write_noco, noco_write — ссылка в записи и noco_links).
- `secrets/` — `API_key_NocoDB.txt`, `API_key_DeepSeek.txt` (скопированы из старого проекта), `API_key_Claude.txt`,
  `API_key_Yandex.txt` (Yandex Search API, «Authorization: Api-Key», каталог не нужен; проверен 27.10.2026 из Ubuntu;
  с 0.5.1 не обязателен). Яндекс из песочницы и Linux-VM Claude недоступен — проверять его может только пользователь.
  DeepSeek Search / Fetch доступны из песочницы Claude (тем же ключом DeepSeek).
  Исключены из GitHub и из образа (`.gitignore`, `.dockerignore`). Пути — в `.env`
  (`NOCODB_TOKEN_FILE`, `DEEPSEEK_KEY_FILE`, `CLAUDE_KEY_FILE`, `YANDEX_KEY_FILE`). Ключи монтируются в контейнеры только для чтения.
- Данные — `DATA_DIR` (`/home/user_1/supplybro-data`, внутри Ubuntu).
- GitHub: `mihan2007/supplybro` (пушит пользователь).
- Dropbox (проверено 11.11.2026 через подключение Dropbox в Claude, учётная запись пользователя): папка конкурсов —
  общая папка «Конкурсы - агенты» (в учётной записи пользователя — «Снабжение России / Конкурсы / Конкурсы - агенты»,
  там же вся «Снабжение России»: бухгалтерия, зарплаты и т. п.), ≈ 371 МБ, ServerTest ≈ 20 МБ; клиенты-папки: Наша,
  ИП Саргсян, ServerTest, Аллтрейд, Клиент 4–6. На ноутбуке в `C:\Users\User 1\Dropbox` — только «Агент» (это она).

## NocoDB

- «Наша»: workspace `wzs963xn`, база `pf3o1felxit8s5b`, «Реестр конкурсов» `mdbebnsirecunr5` (представления по ролям:
  «Вся таблица», «Тендерный специалист», «Логист», «Закупщик» и др.) — в неё пишет агент в приложении. Сайт с 0.17.2 —
  в копию: база `py03vcjrohur7u7`, реестр `m9w2qkxcvt02nkb` (10.11.2026 совпадала со старой: 941 запись, те же Id и
  номера до К941, те же представления). Записи агента после 08.11 в копию сами не попадают, записи сайта — в старую.
- Тестовая база «SupplyBro Тест» (0.21.0, пользователь 11.11.2026): `p7il150axz5cnv7`, реестр `mpo13igxhwk3l1f` —
  дубликат копии без данных (`POST /api/v2/meta/duplicate/<база>` с `fk_workspace_id`, `excludeData`): 162 колонки
  реестра, 16 представлений, «Поставщики», «Заказчики», «Производители», «Отсев конкурсов», «Сопроводительная
  документация» (пустые), спецификации К932 / К933 / К934. Записи К932–К934 и строки их спецификаций перенесены из
  копии (поля и вложения; связи — нет), пустые спецификации других конкурсов удалены. Клиент сайта — «Тест».
- Спецификация конкурса — отдельная таблица, названа внутренним номером («К939»), ссылка в «Ссылка на спецификацию»;
  представления «Сбор цен», «Закупки», «Цены агента».
- Сам сайт NocoDB только читает; пишет обработчик после анализа (`app/analysis/noco_write.py`): запись ищется по
  внутреннему номеру папки. Двойник (номер закупки уже есть у другого конкурса) — по решению пользователя
  28.10.2026 сразу отдельной записью со своим внутренним номером и своей спецификацией, двойник не меняется
  (`NOCODB_DUPLICATES=separate`, `ask` — по кнопке). Запись с этим же внутренним номером, заполненная не сайтом
  (агентом), — ничего не меняет, кнопка «Записать всё равно» (отдельная запись дала бы два конкурса с одним номером).
  Спецификацию чужой записи со строками не перезаписывает никогда. Какие записи создал сайт — `noco_links`.
- Обработчик — пять очередей (потоки `app/worker.py`, `db.LANES`): подготовка + анализ + разбор изменений
  (`docs`: prepare, analyze, changes); NocoDB (`noco`, `noco_prices`, `noco_purch`, `noco_changes`, `noco_bid`); поиск
  цен (`prices`); прайсы поставщиков (`pricelists`, задача `pricelist`, 0.8.0); подготовка заявок (`bid`, 0.15.0). Раз в 10 с каждая отмечается в `worker_lanes`; `jobs.updated_at` — последнее
  действие; `main.job_view()` даёт странице «идёт / последнее действие / ждёт за кем / жив ли обработчик»,
  `/api/clients/{id}/jobs` — состояние для шапки. Запуск анализа / цен / записи при уже идущей задаче возвращает её
  (`already`), не ставит вторую. Анализ сам ставит запись в NocoDB и поиск цен (`AUTO_PRICES=1`, решение пользователя 28.10.2026).
- Отмена и пауза (0.5.8, `app/jobctl.py`): статусы `paused` / `canceled`, `jobs.control` = cancel / pause — просьба
  остановить идущую задачу. `progress` обработчика проверяет её (не чаще раза в секунду) и бросает `JobStopped`
  (BaseException — чтобы не перехватили `except Exception` по пути); пока ждём ответа модели / страниц —
  `jobctl.result(fut, check)`, `check` — атрибут `progress.check`. Пулы потоков после остановки — `stop_pool`
  (ждущие вызовы не запускаются). Поиск DeepSeek / Яндекса прекращает отправку запросов. Пауза идущей = остановить +
  `paused`; «Продолжить» — снова `queued` (своё место по id), задача начинается заново. Кэш ответов моделей
  `jobctl.AnswerCache` (`DATA_DIR/jobcache/<конкурс>-<вид>`, `Claude.cache` / `DeepSeek.cache`) — для анализа и
  проверки цен; удаляется после успеха, старше 3 дней — при запуске обработчика. Цепочка ставит задачи с
  `enqueue(auto=True)` — приостановленную не продолжает; кнопки пользователя — продолжают. API:
  `POST /api/clients/{id}/jobs/{job_id}/cancel|pause|resume`.
  Цены пишутся в спецификацию, созданную сайтом (PATCH колонок рынков и «Примечание»); если запись анализа ещё идёт —
  задачей `noco_prices` следом за ней. После «Записать всё равно» — тоже поиск цен, если их ещё не искали.
- Запись нового конкурса: сначала строка реестра (секунды), затем `nc.sync_specification` — у новой таблицы
  представления настраиваются 10–15 мин (по колонке на запрос, `tools/nocodb_client.py` не меняем), ссылка на
  спецификацию дописывается в запись после. Перезапуск посередине: анализ не повторяется, таблица «pending:<номер>»
  или с теми же позициями считается своей.
- Структуру NocoDB менять только с согласия пользователя; новые колонки не должны дублировать существующие
  (правило старого проекта). `nocodb_client.py` сам досоздаёт колонки спецификации и представления — как у агента.
- Участник конкурса: колонки «Участник» в реестре нет. Сайт берёт участника из неё (когда появится) или из строки
  «Участник: … (ИНН …)» в «Перечне документов заявки» (сейчас так только у К933), остальные — «Участник не указан».

## Как Claude работает с проектом

- Подключить к сессии папки `C:\ClaudeProject\supplybro` и `C:\Users\User 1\Dropbox\Агент` (для клиента «Тест»
  хватит `…\Агент\ServerTest`); старый проект —
  только если нужно что-то из него прочитать или скопировать, ничего в нём не менять.
- Доступа к WSL пользователя у Claude нет: команды для Ubuntu выполняет пользователь.
- Проверять код: в облачной песочнице (Python, Playwright, скриншоты страницы с подставными данными) и в Linux-VM
  устройства (`device_bash`: есть сеть до app.nocodb.com и доступ к `secrets/`). Удаление файлов в подключённой папке
  по умолчанию запрещено; git в VM — только читающие команды с `--no-optional-locks`.
- На каждое изменение: `VERSION` + запись в `CHANGELOG.md`. Библиотеки — точные версии не моложе двух недель.
- `.bat` — UTF-8 без BOM, переводы строк CRLF, `chcp 65001` во второй строке, метки латиницей; остальные файлы — LF.

## Сделано (0.0.1–0.4.0)

Каркас в Docker; страница «Клиент → Юр. лицо → Конкурс» с карточкой конкурса (поля представления реестра, вложения
со ссылками) и спецификацией (представления, закреплённые колонки); запуск и автозагрузка из Windows; ключи в `secrets/`.
Этап 1 (0.1.0): документы конкурса — загрузка, очередь и обработчик, распаковка, конвертация, текст, распознавание,
скрытый текст, поисковый индекс, «+ Новый конкурс». 0.1.1: «+ Новый конкурс» — видимая кнопка рядом со
списком конкурсов, в её форме сразу загружается тендерная документация. Проверено у Claude на копиях К939 и К934; на компьютере
пользователя — ещё нет (первая сборка с LibreOffice). Тестовый браузер Playwright в песочнице Claude не передаёт файлы
с русскими именами через выбор файла — это особенность песочницы, сервер такие имена принимает (проверено запросом).
0.2.0 (этап 3, ядро): анализ документов ИИ — поля блоков 1–7 с цитатами, позиции частями, проверки кодом,
перепроверка DeepSeek (✅/🔺), `analysis.json` / `Позиции_и_цены.xlsx` / XML в `AIExtract`, запись в NocoDB.
Загрузка через сайт → подготовка → анализ → NocoDB без вопросов; старые папки — анализ только по кнопке.
Проверено у Claude на К992 (папка-копия документов К934) с настоящими Claude/DeepSeek; сверено с записью К934 в NocoDB.
27.10.2026 на компьютере пользователя: К993 (снова документы К934) — загрузка → анализ за 88 с (Claude и DeepSeek
через прокси работают) → в NocoDB не записано: двойник К934 (запись 50), кнопка «Записать отдельной записью».
0.2.1: правила полей «Организатор» (название обязательно) и «Срок оплаты (кратко)» (суффикс « МСП»).
0.3.0: чат по документации (этап 2 плана) — проверен у Claude на К992 с DeepSeek Flash (ответ 4–13 с, кэш
DeepSeek ~99 %, цитаты сверяются); ветка Claude проверена подменой (баланс Anthropic 27.10.2026 закончился —
API отвечал «credit balance is too low»; пополнен в тот же день).
0.4.0: стоимость в $ (`app/analysis/pricing.py`, цены Anthropic с официальной страницы 27.10.2026; у Haiku 5.5 порог
100 тыс. токенов — выше весь запрос ×5), выбор модели анализа на странице, по умолчанию Haiku (`HAIKU_EFFORT=low`;
при medium не помещалась в max_tokens), деление частей при обрезанном ответе, запасной DeepSeek при `BillingError`
(анализ — `Claude._deepseek_json`, чат — `notice` + DeepSeek). К934: Haiku ≈ $0.07 / 97 с, Sonnet ≈ $0.7–1.5,
запасной DeepSeek Flash ≈ $0.06 / 73 с; чат Haiku ≈ $0.001–0.02 за вопрос. Баланс Anthropic пополнен 27.10.2026.
Ещё не сделано из этапа 3: сохранение в общий CSV, «Перечень документов заявки» (этап 5), арбитр GPT (нет ключа),
перезапуск отдельных шагов анализа.
0.5.0: поиск цен (этап 4, первая часть) — выбор пользователя 27.10.2026: Россия — Yandex Search API (отложенный режим,
«срочно» — мгновенный), зарубеж — веб-поиск Claude; страницы открывает и цены достаёт программа. Проверено у Claude
подменой Яндекса и магазинов + настоящий Claude (5 позиций К934 ≈ $0,12; зарубеж ≈ $0,05–0,06 за артикул, в основном
плата $0,01 за поиск). На компьютере пользователя ещё не запускался.
0.5.1: пользователь (27.10.2026) — «3 рубля на позицию дорого, отдать DeepSeek»: основной поиск — DeepSeek Search API
(с 17.10.2026, тот же ключ; ≈ 0,06 ₽ за запрос), Яндекс — по выбору на странице, Claude за рубежом — выключен.
Проверено у Claude с настоящим DeepSeek и Haiku (собственное открытие страниц в песочнице закрыто — страницы
открывал DeepSeek Fetch). Ещё не решено пользователем: брать ли цену из поисковой выдачи, если страница не открылась
(сейчас — нет, только цены, которые программа увидела на странице; так и по правилу Шага 5.1g старого проекта:
«не цена из сниппета поиска без открытия страницы»). Решено 04.11.2026: цены из выдачи показывать на сайте и в чате
с пометкой «не проверено», в NocoDB и xlsx — нет (0.8.0).
0.5.2 (28.10.2026, «заверши этап»): зарубеж — полный набор запросов 5.1g и приоритет DE → CN → US, перепроверка 5.1i,
проверка предложений по правилам 5.1g (тот же артикул = тот же товар без сверки совместимости; деталь вместо узла —
нет; оригинал / аналог; цена только от модели; https), пометки в «Примечание». К992 (документы К934): Россия 69 цен у
31 поз., зарубеж 69 у 31 (агент: 45 у 34 и 14 у 13), check_prices без замечаний, ≈ $0,8–1 за конкурс.

## Следующие шаги (согласовано в общих чертах)

Подробный план по этапам с отметками — `PLAN.md`. Ниже — кратко.

1. Загрузка и подготовка документов, без ИИ: блок «Документы» на странице, распаковка архивов и конвертация
   (взять `extract_archive.py`, `convert_odf.py` из старого проекта), текст с метками страниц, распознавание сканов
   (Tesseract rus), вырезание скрытого текста с пометкой ⚠, поисковый индекс. Пункт «+ Новый конкурс».
2. ~~Чат по документам~~ — сделан в 0.3.0 (с 0.6.6 — внизу страницы).
3. Анализ: порядок задаёт код (очередь задач в SQLite + отдельный процесс-обработчик, шаги 1–9, кэш по хешу файлов,
   перезапуск отдельных шагов). Блоки 1–7 — вызовы Claude со строгой схемой «значение + цитата + источник», цитату
   проверяет код. Позиции — агент Claude с инструментами в песочнице (только папка конкурса, без сети), разбор длинных
   таблиц частями; код проверяет полноту (нумерация, число строк, сумма к НМЦ) и сверяет источники.
   Модели: Claude Sonnet 5.5 (Opus 5.5 — для тяжёлых таблиц) — извлечение; DeepSeek — перепроверка критичных полей;
   GPT — арбитр при расхождении; чат — Claude Haiku 5.5 или DeepSeek. Модель каждого шага — в настройках.
   Защита от инъекций: у модели нет инструментов (кроме агента позиций в песочнице), в NocoDB пишет код после проверки.
4. Перед встраиванием — проверка на эталонах К910 (62 поз.), К934 (43), К939 (8): извлечь и сравнить с NocoDB.

## Решения пользователя

- 12.11.2026: к серверу подключён домен supplybro.ru (reg.ru) — «чтобы при обращении к IP появлялся сайт». Главный
  адрес — www.supplybro.ru (A-запись была только у него); IP (http и https), supplybro.ru и старый адрес xip
  перекидывают на него. Ради сертификата на IP Caddy обновлён с 2.6.2 до 2.11.9 (см. «Запуск»).
- 11.11.2026: «давай заменим файловую систему на Dropbox… нам нужно, чтобы в Docker крутилось, — просто перенесём на
  сервер и всё» — Dropbox отдельным контейнером в проекте, на сервере включается одной строкой (0.22.0); на ноутбуке
  не включается (там Dropbox для Windows). Для сервера пользователь предложил учётную запись Dropbox агента (agent@
  домена компании), а не свою — в своей вся «Снабжение России» (сервер открыт в интернет). Пароль от неё был
  отправлен в чат — рекомендовано сменить; в проект и память не записан. Программа Dropbox для Linux на сервере
  зависла на «Starting...» и после повторной привязки (пользователь: «давай заново привяжем») — с 0.23.0 rclone.
- 11.11.2026: «в админ-панели — возможность переключаться к разным базам данных NocoDB… тестовая база всего с двумя
  тендерами… по образу и подобию той, что имеется сейчас, чтобы переключение происходило без проблем»; папка
  `Dropbox\Агент\ServerTest` (на сервере — через Dropbox). Выбрано пользователем: отдельный клиент «Тест»; поле базы
  у каждого клиента в админ-панели (0.21.0). Взято по умолчанию: тестовая база — дубликат копии без данных + записи и
  спецификации всех трёх конкурсов ServerTest (К932–К934; пользователь говорил о двух); переключает только
  администратор сайта; выбор админ-панели главнее .env и clients.json.

- 10.11.2026: закупщику — вносить цены поставщиков в NocoDB (отдельное право), у логиста и закупщика — свои чаты, чужих
  вопросов не видят; чат закупщика принимает прайсы (0.20.0). Взято по умолчанию: личный чат — у всех сотрудников
  (бэклог: «у каждого пользователя свой чат»); старая переписка — автору по журналу, остальное — администратору сайта;
  прайсы — и без анализа на сайте (позиции из спецификации NocoDB); «Цены из сообщения» — текст поставщика как прайс.
- 10.11.2026: «давай сделаем, чтобы админ мог править промты клиентов, других юр. лиц, всё это — в админ-панели» —
  промты и на юр. лицо (0.19.0): юр. лицо → клиент → стандартный. Юр. лицо задачи выбрано по умолчанию так: участник
  заявки; иначе выбранное на странице, при «все» — единственный участник из NocoDB (пользователь не уточнял).
- 10.11.2026: «у админа должна быть возможность править промты, сделай, чтобы это можно было делать в админ-панели;
  у каждой организации свои промты, чтобы их можно было править в зависимости от нужд клиента» — вкладка «Промты»,
  правка на клиента (0.18.0). Правит только администратор сайта (рекомендовано: промт влияет на качество, стоимость и
  защиту от подложного текста); дать это администратору клиента — по решению пользователя.
- 10.11.2026: «давай привяжем пока к копии базы данных» — сайт (ноутбук и сервер) работает с копией «Наша»
  `py03vcjrohur7u7`, агент в приложении — со старой; в `clients.json` — копия (0.17.2). Сведение двух баз — позже.
- 08.11.2026: страница-приветствие `/start` с переходом на вход и вход, который не надо повторять при каждом посещении
  (0.17.0). Сайт на сервере открывался по адресу https://113-30-189-244.eu-cloud-xip.com; с 12.11.2026 —
  https://www.supplybro.ru (старый адрес и IP перекидывают на него).
- 08.11.2026: для сайта — копия базы NocoDB «Наша» (`py03vcjrohur7u7`); базу — «просто поменять данные» одной строкой
  (0.16.1, `NOCODB_URL_MAIN`). Какая база главная — решено 10.11.2026: для сайта пока копия (0.17.2).
- 08.11.2026: папки конкурсов на сервер — копией сейчас (`/opt/supplybro-tenders`, rsync с ноутбука через прокси
  Windows: `~/.ssh/config` ProxyCommand nc на 127.0.0.1:10809), Dropbox на сервере — позже (нет доступа к паролю).

- 08.11.2026: «сайт переезжает на сервер Kamatera (Ubuntu, /opt/supplybro, compose.yml без прокси) и будет открыт в
  интернет через Caddy с HTTPS, поэтому без входа не должно открываться ничего, кроме страницы входа»; после входа —
  своя страница по роли, только клиенты из доступа, права по роли, модели / поисковик — только admin, в задачах — кто
  запустил, включение одной настройкой (по умолчанию 1 на сервере) — 0.16.0 (настройка — прежняя `LOGIN_REQUIRED`).

- 26.10.2026: файлы конкурсов храним в `C:\Users\User 1\Dropbox\Агент`, как сейчас — сайт и агент в приложении
  работают с одними папками. Устройство: `Агент\<клиент>\<К-номер Заказчик Предмет>\`, результаты — в `AIExtract\`.
  Клиенты-папки: «Наша» (наша компания; там же «#Материалы агента» — документы ООО «Простор»), «ИП Саргсян»,
  «Клиент 3», «Клиент 4». Старые конкурсы К910–К922 — ещё и в `C:\Users\User 1\Documents\Тендеры`.
  В Ubuntu путь — `/mnt/c/Users/User 1/Dropbox/Агент` (с пробелом: в `.env` значение в кавычках, `run.sh` читает
  `.env` через bash). Подключать к сессии Claude вместе с `C:\ClaudeProject\supplybro`.
- 26.10.2026: результат анализа пишется в NocoDB сразу, как сейчас у агента (выбрано по умолчанию, можно поменять).
- 04.11.2026 («закинуть прайс-листы через чат, найти позиции, поставить цены; показывать цены из интернета»): цены
  поставщиков — в спецификацию NocoDB, колонки «Отдел закупок»; сначала показать, записать по кнопке; показывать
  везде — общая таблица цен, ответы чата, цены из выдачи поиска (с пометкой «не проверено»). Про «чат-бот в папке
  проекта» (образец для прайсов) — старый проект Claude не открыл: доступ к папке не был дан; можно посмотреть позже.
  (В `tools/nocodb_client.py` есть `read_manual_fields` — «поля, которые заполняются вручную / чат-ботом»: видимо,
  тот бот заполнял колонки «Отдел закупок».) Рядом с проектом есть папка `C:\ClaudeProject\Счета для теста`.
- 05.11.2026: «модели Claude оказались очень прожорливые… давай попробуем добавить туда DeepSeek» — подготовка заявки
  по умолчанию на DeepSeek Flash, Claude — на выбор администратора (0.15.1).
- 05.11.2026: подготовка заявки по инструкции агента — «кнопка подготовка документов должна быть на сайте, далее идёт
  полная подготовка, с минимумом вопросов» (0.15.0): одна кнопка, пакет в AIExtract\Заявка, цена — от пользователя
  (процент или сумма), NocoDB — по кнопке.
- 04.11.2026: «иногда, когда позиции извлечены, кнопка поиска цен не активна — искать цены всегда, даже если они были
  извлечены ранее» — без анализа на сайте поиск по позициям из спецификации NocoDB; во время анализа — «запустится
  сам после анализа» (0.14.0).
- 04.11.2026: «вместо выпадающего списка при входе — панель, где отображаются тендеры выбранного лица, чтобы их можно
  было открыть в отдельной вкладке; номер, заказчик, наименование оборудования и минимальная информация» — панель
  «Конкурсы», щелчок открывает новую вкладку браузера (0.13.0).
- 04.11.2026: «в админ-панели мы видим наших клиентов, у клиента могут быть сотрудники, у которых и должны быть эти
  роли» — сотрудник в одном клиенте; управляют администратор сайта и новый «Администратор клиента» (только свои
  сотрудники); клиенты — список из настроек, новых добавляет Claude (0.12.0).
- 04.11.2026: «у каждого пользователя в админ-панели опция отключать тот или иной блок — чат, анализ, цены, все блоки» —
  галочки разделов в карточке сотрудника, раздел скрыт и закрыт на сервере (0.11.0).
- 04.11.2026: «у нас проблема с тендерами, которые были загружены, — укажи цены, если они уже есть в базе данных» —
  «Все цены» и чат показывают цены из спецификации NocoDB конкурса и прошлых конкурсов (0.10.0); раздел «Цены».
- 04.11.2026 (дозагрузка в разобранный конкурс): «прочитать дозагруженные документы и понять, в каких местах были
  внесены изменения, обновить данные, вывести комментарии». Запись в NocoDB, цены и папку пользователь не выбрал —
  взяты рекомендованные: NocoDB по кнопке и только изменённое; цены — только новые / изменённые позиции; «Изменения
  ДД.ММ.ГГГГ» (0.9.0).

## Открытые вопросы к пользователю

- Эталоны: какие 3–5 конкурсов, подтверждены ли их данные в NocoDB.
- Создать ли в «Реестре конкурсов» колонку «Участник» (выбор из списка) и заполнить: К933 — ООО «Простор»,
  остальные К922–К939 — ИП Строк, старые — пусто. Без прямого «да» NocoDB не трогать.
- Тестовая копия NocoDB так и не решена: 27.10.2026 пользователь попросил сразу «анализ и добавление в базу данных»;
  защита — запрет менять чужие записи без подтверждения (см. «NocoDB»).
- Доступ сотрудникам с других компьютеров — решено 08.11.2026: сервер Kamatera + Caddy с HTTPS (вход — 0.16.0).
  Открыто: как на сервер попадёт база сайта (`DATA_DIR`), резервные копии. Папки конкурсов — rclone с
  Dropbox (0.23.0); на сервере: подключить, дождаться «Up to date», сверить с копией `/opt/supplybro-tenders`,
  переключить `TENDERS_DIR`.
- Ключ Claude (`sk-ant-usr-…`) работает с API (проверено 27.10.2026: модели claude-sonnet-5-5, claude-opus-5-5,
  claude-haiku-5-5). Ключ был вставлен в чат — рекомендовано выпустить новый и заменить файл. Ключа OpenAI нет.
