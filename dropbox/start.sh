#!/bin/sh
# Запуск синхронизации с Dropbox (0.23.0).
set -eu
# Настройки rclone (подключение к учётной записи) — в томе dropbox-config, папка сервера DROPBOX_DIR — в /data.
# Docker создаёт их от root, поэтому отдаём пользователю dropbox (UID 1000, как у сайта). Только верхние папки.
mkdir -p /config /data
chown 1000:1000 /config /data
exec setpriv --reuid=1000 --regid=1000 --init-groups \
  env HOME=/config USER=dropbox LOGNAME=dropbox RCLONE_CONFIG=/config/rclone.conf dropbox-sync
