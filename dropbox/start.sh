#!/bin/sh
# Запуск Dropbox в контейнере (0.22.0).
set -eu
H=/home/dropbox

# Настройки (привязка к учётной записи) и программа — в томе dropbox-home, папка Dropbox — папка
# сервера DROPBOX_DIR. Docker создаёт её от root, поэтому отдаём пользователю dropbox (UID 1000, как у
# сайта). Только верхние папки — файлы внутри не перебираются.
mkdir -p "$H/Dropbox" "$H/.dropbox"
chown 1000:1000 "$H" "$H/Dropbox" "$H/.dropbox"

# Программа — копией в том: так её самообновления сохраняются после перезапуска контейнера.
if [ ! -x "$H/.dropbox-dist/dropboxd" ]; then
  rm -rf "$H/.dropbox-dist"
  cp -a /opt/dropbox-dist "$H/.dropbox-dist"
  chown -R 1000:1000 "$H/.dropbox-dist"
fi

echo "Dropbox: запуск. Если сервер ещё не привязан к учётной записи Dropbox, ниже появится ссылка"
echo "https://www.dropbox.com/cli_link_nonce?... — откройте её в браузере, где вы вошли в нужную учётную запись."
exec setpriv --reuid=1000 --regid=1000 --init-groups \
  env HOME="$H" USER=dropbox LOGNAME=dropbox "$H/.dropbox-dist/dropboxd"
