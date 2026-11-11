#!/bin/sh
# Команды Dropbox внутри контейнера, от имени пользователя dropbox (0.22.0). Например:
#   docker compose exec dropbox dropbox status      — синхронизация: «Up to date» — всё скачано
#   docker compose exec dropbox dropbox filestatus   — состояние файлов в папке Dropbox
if [ "$(id -u)" = 0 ]; then
  exec setpriv --reuid=1000 --regid=1000 --init-groups \
    env HOME=/home/dropbox USER=dropbox LOGNAME=dropbox python3 /opt/dropbox.py "$@"
fi
exec python3 /opt/dropbox.py "$@"
