#!/bin/sh
# Сверка папки конкурсов с Dropbox в обе стороны (0.23.0): rclone bisync раз в DROPBOX_SYNC_SECONDS секунд.
# Новое и изменённое в одной стороне появляется в другой, удалённое — удаляется; файл, изменённый в обеих
# сторонах, — остаются оба (новее — под своим именем, второй — с «.conflict1» в имени).
set -u
FOLDER=${DROPBOX_FOLDER:-Агент}
REMOTE="dropbox:$FOLDER"
LOCAL="/data/$FOLDER"
WORK=/config/bisync
DONE="$WORK/resync.done"
STATE=/config/status.txt
FILTERS=/config/dropbox-filters.txt
INTERVAL=${DROPBOX_SYNC_SECONDS:-60}
export RCLONE_CONFIG="${RCLONE_CONFIG:-/config/rclone.conf}"

say() { echo "$(date '+%d.%m.%Y %H:%M:%S') $*"; }
state() { printf '%s  %s\n' "$(date '+%d.%m.%Y %H:%M:%S')" "$*" > "$STATE"; }

mkdir -p "$WORK"
# Фильтры — копией в /config: rclone bisync кладёт рядом с файлом фильтров его отпечаток (.md5), а в /etc
# пользователю dropbox писать нельзя (0.23.1).
cp /etc/supplybro/dropbox-filters.txt "$FILTERS"
# Общие настройки сверки: свои папка и фильтры, пустые папки тоже, после сбоя — продолжать без полной сверки,
# брошенная блокировка — не дольше 2 минут, файл изменён в обеих сторонах — новее побеждает, второй остаётся
# с номером; удалить больше 25 % файлов за раз — отказ (защита от случайного удаления всего).
set -- --workdir "$WORK" --filters-file "$FILTERS" --create-empty-src-dirs \
  --resilient --recover --max-lock 2m --conflict-resolve newer --conflict-loser num --max-delete 25 -v

waiting=""
while :; do
  if ! rclone listremotes 2>/dev/null | grep -qx 'dropbox:'; then
    if [ -z "$waiting" ]; then
      say "Dropbox не подключён. Подключить (один раз, см. README):  docker compose exec dropbox dropbox connect"
      waiting=1
    fi
    state "не подключён — docker compose exec dropbox dropbox connect"
    sleep 15
    continue
  fi
  waiting=""
  mkdir -p "$LOCAL"
  if [ ! -f "$DONE" ]; then
    say "Полная сверка $REMOTE <-> $LOCAL (в первый раз — скачивание всей папки)"
    state "идёт полная сверка (в первый раз — скачивание всей папки)"
    if rclone bisync "$REMOTE" "$LOCAL" --resync --resync-mode newer "$@"; then
      touch "$DONE"
      say "Полная сверка готова"
      state "Up to date"
    else
      say "Полная сверка не удалась — повтор через $INTERVAL с"
      state "ошибка полной сверки — подробности: docker compose logs --tail 50 dropbox"
    fi
  else
    rclone bisync "$REMOTE" "$LOCAL" "$@"
    rc=$?
    case $rc in
      0) state "Up to date" ;;
      7) say "Сверка остановлена (код 7: изменились фильтры, слишком много удалений или сбой) — следующая будет полной"
         rm -f "$DONE"
         state "следующая сверка — полная (код 7)" ;;
      *) say "Сверка не удалась (код $rc) — повтор через $INTERVAL с"
         state "ошибка (код $rc) — подробности: docker compose logs --tail 50 dropbox" ;;
    esac
  fi
  sleep "$INTERVAL"
done
