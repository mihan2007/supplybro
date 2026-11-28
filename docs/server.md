# Окружение, запуск, сервер

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний.

## Компьютер (Windows 11, WSL2, Docker)


- WSL2, `Ubuntu-24.04`, пользователь `user_1`. `C:\Users\User 1\.wslconfig`: `[wsl2] networkingMode=mirrored`,
  `[general] instanceIdleTimeout=-1` (Ubuntu не выключается сама).
- Пакеты Ubuntu — с `mirror.yandex.ru` (archive/security.ubuntu.com с этого адреса зависают).
- Docker 29.1.3, Compose 2.40.3, docker-buildx — из репозитория Ubuntu (не Docker Desktop), запускается systemd.
- Образы: зеркало `mirror.gcr.io` (`/etc/docker/daemon.json`) + прокси Windows `127.0.0.1:10809`
  (`/etc/systemd/system/docker.service.d/proxy.conf`). Docker Hub и dockerhub.timeweb.cloud отсюда не отвечают.
- Сеть: часть зарубежных сайтов из WSL напрямую недоступна, `mirror.yandex.ru` работает. Сборка (`BUILD_PROXY`)
  и запросы сайта (`OUTBOUND_PROXY`, NocoDB) идут через прокси Windows; без него новые библиотеки не скачать и NocoDB не ответит.

## Запуск на ноутбуке


- Двойной щелчок: «Запустить SupplyBro.bat» (проверяет прокси, `run.sh`, ждёт `/health`, открывает браузер),
  «Остановить SupplyBro.bat». Автозагрузка: «Добавить в автозагрузку.bat» (кладёт файл в `shell:startup`,
  запуск с `/autostart` — свёрнуто, без браузера, прокси ждёт до 2 минут), «Убрать из автозагрузки.bat».
- Вручную в Ubuntu: `cd /mnt/c/ClaudeProject/supplybro && bash run.sh`.
- `run.sh`: ждёт Docker, дописывает в `.env` новые настройки из `.env.example`, проверяет файл ключа NocoDB,
  `docker build --network host` (прокси шагам сборки виден только в сети Ubuntu; compose так не умеет), `docker compose up -d`.
- Сайт: http://127.0.0.1:8000, только с этого компьютера. `compose.local.yml` (через `COMPOSE_FILE` в `.env`):
  `network_mode: host` + uvicorn на 127.0.0.1. На сервере — только `compose.yml` (bridge, порт на 127.0.0.1).

## Сервер Kamatera, домен, Caddy

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

## Выкладка на сервер

- Сервер с ноутбука (0.24.0): `server/server.sh` + `server/server.conf` (`SERVER=root@113.30.189.244`,
  `SERVER_DIR=/opt/supplybro`), `.bat` «Сервер - отправить версию» (deploy: `rsync -av --chmod=D755,F644
  --exclude .env --exclude secrets/ --exclude .git/ --exclude __pycache__/` + `ssh -t … bash run.sh`), «Сервер -
  терминал» (console), «Сервер - состояние» (status), «Сервер - подключить Dropbox» (`ssh -t -L
  53682:127.0.0.1:53682 … docker compose exec dropbox dropbox connect`), «Сервер - вход без пароля» (key:
  ssh-keygen ed25519 + ssh-copy-id). ssh — через `~/.ssh/config` Ubuntu (`Host 113.30.189.244`, ProxyCommand nc на
  прокси Windows); из PowerShell напрямую сервер не отвечает. Пользователь часто вставляет команды не в то окно —
  предлагать эти `.bat`.
- На сервер — только из git (0.25.0, пользователь 12.11.2026: «как сделать, чтобы не было конфликтов версий»):
  порядок ноутбук → проверка → коммит + push (пушит пользователь) → «Сервер - отправить версию». `deploy`:
  `git_ready` (`git status --porcelain` пусто, `@{u}..HEAD` = 0, иначе отказ со списком) → `git archive HEAD` во
  временную папку + `app/COMMIT` (короткий номер) → `rsync -av --checksum …` → `run.sh`; `/health` — `version` и
  `commit`. `status` — версия и коммит ноутбука и сервера + `env_check`: `.env` сервера (ssh cat) — настройки машины
  с `server/server.env.example`, общие — с `.env` ноутбука, недостающие; значения *PASSWORD* / *SECRET* / *TOKEN* /
  *KEY* (не *_FILE) скрыты. Образец сервера `server/server.env.example` — только настройки машины (COMPOSE_FILE,
  DATA_DIR, TENDERS_DIR, прокси, пути к ключам, COMPOSE_PROFILES); `run.sh` на сервере (не WSL по `/proc/version`,
  вручную `SB_MACHINE`) берёт новые настройки из него, иначе из `.env.example`. Новая настройка в `.env.example`,
  у которой на сервере другое значение, — и в `server/server.env.example` (там же `APT_MIRROR=`). Сверка 12.11.2026:
  настройки машины сервера — как в образце (DATA_DIR=/opt/supplybro-data, ключи в /opt/supplybro/secrets), кроме
  TENDERS_DIR (сайт ещё на копии `/opt/supplybro-tenders`); BID_MODEL ноутбука выровнен на deepseek-flash.
