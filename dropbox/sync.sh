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

LONG=/config/long_names.txt
# 0.44.1: имя файла или папки на Linux — не длиннее 255 байт (русская буква — 2 байта, т. е. ≈ 120 букв), а в
# Dropbox и Windows длиннее можно. Один такой файл (К947, имя 381 байт) останавливал всю сверку: «file name too
# long … Bisync aborted». Такие файлы и папки пропускаются (правила «-» в фильтрах), список — в $LONG
# (docker compose exec dropbox dropbox long); после переименования в Dropbox они придут сами при полной сверке.
# Запас под временное имя rclone («.1b39018e.partial»).
MAXB=${DROPBOX_MAX_NAME_BYTES:-230}

mkdir -p "$WORK"
# Фильтры — копией в /config: rclone bisync кладёт рядом с файлом фильтров его отпечаток (.md5), а в /etc
# пользователю dropbox писать нельзя (0.23.1). К ним — правила для длинных имён (build_filters).
build_filters() {
  list="$WORK/remote.lst"
  if ! rclone lsf -R "$REMOTE" > "$list.tmp" 2>/dev/null; then
    rm -f "$list.tmp"
    [ -f "$FILTERS" ] || cp /etc/supplybro/dropbox-filters.txt "$FILTERS"
    return 0                                     # Dropbox не ответил — фильтры прежние
  fi
  mv "$list.tmp" "$list"
  # Первая слишком длинная часть пути: «папка/» — вся папка, иначе файл. Байты считает awk в локали C.
  LC_ALL=C awk -v max="$MAXB" '{
      n = split($0, a, "/"); path = ""
      for (i = 1; i <= n; i++) {
        if (a[i] == "") continue
        path = path (path == "" ? "" : "/") a[i]
        if (length(a[i]) > max) {
          isdir = (i < n) ? 1 : 0
          key = path (isdir ? "/" : "")
          if (!(key in seen)) { seen[key] = 1; print key }
          break
        }
      }
    }' "$list" > "$LONG.tmp"
  mv "$LONG.tmp" "$LONG"
  {
    # Правила исключения — раньше общих (в rclone срабатывает первое подходящее правило).
    sed -e 's/[][\\*?{}]/\\&/g' "$LONG" | while IFS= read -r line; do
      case "$line" in
        */) printf -- '- /%s**\n' "$line" ;;
        *)  printf -- '- /%s\n' "$line" ;;
      esac
    done
    cat /etc/supplybro/dropbox-filters.txt
  } > "$FILTERS.new"
  if ! cmp -s "$FILTERS.new" "$FILTERS"; then
    mv "$FILTERS.new" "$FILTERS"
    rm -f "$DONE"                                # другие фильтры — rclone требует полной сверки
  else
    rm -f "$FILTERS.new"
  fi
}
long_note() {
  c=$(grep -c . "$LONG" 2>/dev/null); c=${c:-0}
  [ "$c" -gt 0 ] && printf '; пропущено из-за слишком длинного имени: %s (список — dropbox long; переименуйте в Dropbox)' "$c"
}
build_filters
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
    build_filters
    say "Полная сверка $REMOTE <-> $LOCAL (в первый раз — скачивание всей папки)"
    state "идёт полная сверка (в первый раз — скачивание всей папки)"
    if rclone bisync "$REMOTE" "$LOCAL" --resync --resync-mode newer "$@"; then
      touch "$DONE"
      say "Полная сверка готова$(long_note)"
      state "Up to date$(long_note)"
    else
      say "Полная сверка не удалась — повтор через $INTERVAL с"
      state "ошибка полной сверки — подробности: docker compose logs --tail 50 dropbox"
    fi
  else
    rclone bisync "$REMOTE" "$LOCAL" "$@"
    rc=$?
    case $rc in
      0) state "Up to date$(long_note)" ;;
      7) say "Сверка остановлена (код 7: изменились фильтры, слишком много удалений или сбой) — следующая будет полной"
         rm -f "$DONE"
         state "следующая сверка — полная (код 7)" ;;
      *) say "Сверка не удалась (код $rc) — повтор через $INTERVAL с"
         state "ошибка (код $rc) — подробности: docker compose logs --tail 50 dropbox" ;;
    esac
  fi
  sleep "$INTERVAL"
done
