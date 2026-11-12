#!/bin/sh
# Команды синхронизации с Dropbox (0.23.0), например:
#   docker compose exec dropbox dropbox connect      — подключить учётную запись Dropbox (один раз; см. README)
#   docker compose exec dropbox dropbox status       — последняя сверка («Up to date» — всё на месте)
#   docker compose exec dropbox dropbox ls           — папки в Dropbox подключённой учётной записи
#   docker compose exec dropbox dropbox disconnect   — отключить учётную запись
if [ "$(id -u)" = 0 ]; then
  exec setpriv --reuid=1000 --regid=1000 --init-groups \
    env HOME=/config USER=dropbox LOGNAME=dropbox RCLONE_CONFIG=/config/rclone.conf "$0" "$@"
fi
export RCLONE_CONFIG="${RCLONE_CONFIG:-/config/rclone.conf}"
connected() { rclone listremotes 2>/dev/null | grep -qx 'dropbox:'; }

case "${1:-status}" in
  connect)
    if connected; then
      echo "Dropbox уже подключён. Папки:"; rclone lsd dropbox:; exit 0
    fi
    echo "Ниже появится ссылка http://127.0.0.1:53682/auth?state=… — откройте её в браузере на ноутбуке"
    echo "(приватное окно), войдите в учётную запись Dropbox сервера и разрешите доступ. Ссылка открывается"
    echo "только при туннеле: ssh -L 53682:127.0.0.1:53682 root@<адрес сервера>."
    # Строки с ключом доступа на экран не выводим.
    rclone config create dropbox dropbox 2>&1 \
      | grep --line-buffered -v -i -E 'token|"access|refresh'
    if connected; then
      echo; echo "Готово, Dropbox подключён. Папки в Dropbox:"; rclone lsd dropbox:
    else
      echo; echo "Не подключилось — попробуйте ещё раз." >&2; exit 1
    fi ;;
  status)
    cat /config/status.txt 2>/dev/null || echo "Сверка ещё не запускалась" ;;
  ls)
    rclone lsd dropbox: ;;
  disconnect)
    rclone config delete dropbox && rm -f /config/bisync/resync.done && echo "Dropbox отключён" ;;
  *)
    echo "Команды: connect, status, ls, disconnect" >&2; exit 2 ;;
esac
