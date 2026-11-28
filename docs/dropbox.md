# Dropbox: папки конкурсов и синхронизация на сервере

Подробное описание для Claude. Перенесено из `CLAUDE.md` 28.11.2026 (0.45.5) без пересказа. Пункты с номером версии — как было сделано тогда; если два пункта противоречат друг другу, верен более поздний.

## Где лежат папки

- Dropbox (проверено 11.11.2026 через подключение Dropbox в Claude, учётная запись пользователя): папка конкурсов —
  общая папка «Конкурсы - агенты» (в учётной записи пользователя — «Снабжение России / Конкурсы / Конкурсы - агенты»,
  там же вся «Снабжение России»: бухгалтерия, зарплаты и т. п.), ≈ 371 МБ, ServerTest ≈ 20 МБ; клиенты-папки: Наша,
  ИП Саргсян, ServerTest, Аллтрейд, Клиент 4–6. На ноутбуке в `C:\Users\User 1\Dropbox` — только «Агент» (это она).

## Синхронизация на сервере (rclone)

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
  0.44.1: имена длиннее 255 байт Linux не хранит (К947, 381 байт, останавливал всю сверку) — `build_filters` в
  sync.sh исключает части пути > `DROPBOX_MAX_NAME_BYTES` (230), список — `/config/long_names.txt`, `dropbox long`.
  26.11.2026: сайт на сервере всё ещё на `/opt/supplybro-tenders` (TENDERS_DIR), полная сверка не проходила.
  27.11.2026 (0.44.4): сайт на сервере переключён на `/opt/supplybro-dropbox/Агент` (К951 был в Dropbox, сайт не
  видел). Перед этим результаты сайта перенесены rsync --update (одноимённые папки; «К950 К950» → К950
  СОЮЗМУЛЬТФИЛЬМ, «К952 К952» → К952 УФАВОДОКАНАЛ, К934 «…устр_» → «…устр.»). В старой копии остались
  не перенесёнными К947 / К948 / К1000 / К1001 (Наша) и К10 (ИП Саргсян) — номера менялись, сопоставит пользователь.
  sync.sh после неудачной сверки пересобирает фильтры длинных имён; run.sh и status предупреждают «!!!», если
  TENDERS_DIR ≠ DROPBOX_DIR/DROPBOX_FOLDER.
  У Claude проверено только с подставным rclone (rclone, Docker Hub и dropbox.com из песочницы недоступны).
