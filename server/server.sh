#!/usr/bin/env bash
# Работа с сервером SupplyBro с этого компьютера (0.24.0). Двойной щелчок по «Сервер - ….bat» в папке проекта
# или вручную в Ubuntu из папки проекта:
#   bash server/server.sh deploy           — отправить текущую версию на сервер и перезапустить там сайт
#   bash server/server.sh console          — терминал сервера, сразу в папке сайта
#   bash server/server.sh status           — состояние сервера: версия, контейнеры, Dropbox, место на диске
#   bash server/server.sh dropbox-connect  — подключить учётную запись Dropbox (ссылку открыть в браузере)
#   bash server/server.sh key              — вход на сервер без пароля (один раз)
# Куда — server/server.conf.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=server/server.conf
. server/server.conf
: "${SERVER:?В server/server.conf не указан SERVER}"
: "${SERVER_DIR:?В server/server.conf не указан SERVER_DIR}"

say() { printf '\n %s\n\n' "$*"; }

case "${1:-}" in
  deploy)
    say "Отправляю версию $(cat VERSION) на сервер $SERVER (папка $SERVER_DIR)…"
    # Настройки (.env) и ключи (secrets) у сервера свои — их не трогаем. Ничего не удаляем.
    rsync -av --chmod=D755,F644 --exclude .env --exclude secrets/ --exclude .git/ --exclude __pycache__/ \
      ./ "$SERVER:$SERVER_DIR/"
    say "Файлы на месте. Собираю и перезапускаю сайт на сервере (bash run.sh)…"
    ssh -t "$SERVER" "cd '$SERVER_DIR' && bash run.sh"
    say "Готово: на сервере версия $(cat VERSION)."
    ;;
  console)
    say "Терминал сервера, папка сайта $SERVER_DIR. Выйти — команда exit."
    exec ssh -t "$SERVER" "cd '$SERVER_DIR' && exec bash -l"
    ;;
  status)
    say "Сервер $SERVER:"
    # shellcheck disable=SC2029  # SERVER_DIR подставляется здесь, на ноутбуке, — так и надо
    ssh "$SERVER" "cd '$SERVER_DIR' && bash -s" <<'EOS'
echo " Версия сайта: $(cat VERSION)"
echo " Папка конкурсов сайта: $(grep '^TENDERS_DIR=' .env | cut -d= -f2-)"
echo
echo " Контейнеры:"
docker compose ps --format '   {{.Service}}: {{.Status}}'
echo
echo " Dropbox: $(docker compose exec -T dropbox dropbox status 2>/dev/null || echo 'не запущен')"
echo
df -h / | awk 'NR==2 {print " Диск: занято " $3 " из " $2 ", свободно " $4}'
echo " Docker при старте сервера: $(systemctl is-enabled docker 2>/dev/null || echo '?')"
EOS
    ;;
  dropbox-connect)
    say "Подключение Dropbox. Ниже появится ссылка http://127.0.0.1:53682/auth?state=… — откройте её в браузере
 (приватное окно), войдите в учётную запись Dropbox сервера и нажмите «Разрешить».
 Окно не закрывайте, пока не появится «Готово»."
    # Туннель (-L) открыт ровно столько, сколько идёт подключение: ответ Dropbox приходит через него на сервер.
    ssh -t -L 53682:127.0.0.1:53682 "$SERVER" "cd '$SERVER_DIR' && docker compose exec dropbox dropbox connect"
    ;;
  key)
    if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
      say "Создаю ключ для входа на сервер ($HOME/.ssh/id_ed25519)…"
      ssh-keygen -t ed25519 -N "" -C "supplybro $(hostname)" -f "$HOME/.ssh/id_ed25519"
    fi
    say "Отправляю ключ на сервер — введите пароль root сервера (последний раз)."
    ssh-copy-id -i "$HOME/.ssh/id_ed25519.pub" "$SERVER"
    if ssh -o BatchMode=yes "$SERVER" true; then
      say "Готово: теперь вход на сервер — без пароля."
    else
      say "Без пароля пока не входит — смотрите сообщения выше."
      exit 1
    fi
    ;;
  *)
    echo "Команды: deploy, console, status, dropbox-connect, key (подробнее — в начале server/server.sh)" >&2
    exit 2
    ;;
esac
