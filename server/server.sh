#!/usr/bin/env bash
# Работа с сервером SupplyBro с этого компьютера (0.24.0). Двойной щелчок по «Сервер - ….bat» в папке проекта
# или вручную в Ubuntu из папки проекта:
#   bash server/server.sh deploy           — отправить версию из git (последний коммит) на сервер и перезапустить сайт
#   bash server/server.sh console          — терминал сервера, сразу в папке сайта
#   bash server/server.sh status           — состояние сервера: версия, контейнеры, Dropbox, диск, сверка настроек
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

# Отправлять можно только то, что лежит в git (0.25.0): проверили на ноутбуке → коммит → push → на сервер.
git_ready() {
  if ! command -v git >/dev/null 2>&1; then
    say "В Ubuntu нет git — установите: sudo apt install -y git"
    exit 1
  fi
  local dirty ahead
  if ! dirty="$(git -c core.quotepath=false status --porcelain 2>&1)"; then
    say "git не смог проверить папку проекта:"
    printf '%s\n' "$dirty"
    exit 1
  fi
  if [ -n "$dirty" ]; then
    say "Не отправляю: в папке проекта есть изменения, которых нет в git:"
    printf '%s\n' "$dirty" | head -n 30
    say "Порядок: проверить на ноутбуке → коммит → push → снова «Сервер - отправить версию»."
    exit 1
  fi
  if ahead="$(git rev-list --count '@{u}..HEAD' 2>/dev/null)" && [ "$ahead" -gt 0 ]; then
    say "Не отправляю: коммитов, которых ещё нет на GitHub, — $ahead. Сначала push, потом снова «Сервер - отправить версию»."
    exit 1
  fi
}

# Настройки из файла .env (KEY=значение) — в ассоциативный массив, имя которого в $1; кавычки вокруг значения
# снимаются, строки-комментарии пропускаются.
read_env() {
  local -n into="$1"
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
    # shellcheck disable=SC2034  # into — ссылка на массив вызывающего (local -n)
    into["$key"]="$value"
  done
}
env_keys() { sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$1"; }
# Значение для показа: что похоже на пароль или ключ (не путь к файлу ключа) — не показываем.
shown() {
  case "$1" in
    *_FILE) printf '«%s»' "$2" ;;
    *PASSWORD*|*SECRET*|*TOKEN*|*KEY*) printf '(скрыто)' ;;
    *) printf '«%s»' "$2" ;;
  esac
}

# Сверка настроек (0.25.0): .env сервера — с образцом сервера (настройки машины) и с .env ноутбука (общие).
env_check() {
  local remote key
  local -A srv=() lap=() tpl=() def=()
  local -a machine_keys=() common_keys=() missing=() machine_diff=() common_diff=()
  # shellcheck disable=SC2029  # SERVER_DIR подставляется здесь, на ноутбуке, — так и надо
  if ! remote="$(ssh -n "$SERVER" "cat '$SERVER_DIR/.env'")"; then
    echo " Настройки сервера (.env) прочитать не удалось."
    return 0
  fi
  read_env srv <<<"$remote"
  read_env tpl < server/server.env.example
  read_env def < .env.example
  if [ -f .env ]; then read_env lap < .env; fi
  mapfile -t machine_keys < <(env_keys server/server.env.example)
  mapfile -t common_keys < <(env_keys .env.example)

  for key in "${machine_keys[@]}"; do
    if [[ ! -v "srv[$key]" ]]; then
      missing+=("$key=${tpl[$key]}")
    elif [ "${srv[$key]}" != "${tpl[$key]}" ]; then
      machine_diff+=("$key: на сервере $(shown "$key" "${srv[$key]}"), в образце $(shown "$key" "${tpl[$key]}")")
    fi
  done
  for key in "${common_keys[@]}"; do
    [[ -v "tpl[$key]" ]] && continue
    if [[ ! -v "srv[$key]" ]]; then
      missing+=("$key=${def[$key]}")
    elif [[ -v "lap[$key]" ]] && [ "${srv[$key]}" != "${lap[$key]}" ]; then
      common_diff+=("$key: ноутбук $(shown "$key" "${lap[$key]}"), сервер $(shown "$key" "${srv[$key]}")")
    fi
  done

  echo " Настройки сервера (.env):"
  if [ ${#machine_diff[@]} -eq 0 ]; then
    echo "   настройки машины — как в образце сервера (server/server.env.example)"
  else
    echo "   ! отличаются от образца сервера (server/server.env.example):"
    printf '       %s\n' "${machine_diff[@]}"
  fi
  if [ ${#common_diff[@]} -eq 0 ]; then
    echo "   общие настройки — как на ноутбуке"
  else
    echo "   ! общие настройки отличаются от ноутбука (проверенное на ноутбуке может работать на сервере иначе):"
    printf '       %s\n' "${common_diff[@]}"
  fi
  if [ ${#missing[@]} -gt 0 ]; then
    echo "   нет в .env сервера — допишутся при следующей отправке версии:"
    printf '       %s\n' "${missing[@]}"
  fi
}

case "${1:-}" in
  deploy)
    git_ready
    version="$(git show HEAD:VERSION | tr -d '\r\n')"
    commit="$(git rev-parse --short HEAD)"
    say "Отправляю версию $version (коммит $commit «$(git log -1 --format=%s)») на сервер $SERVER (папка $SERVER_DIR)…"
    # Ровно то, что в коммите (0.25.0): выгрузка из git во временную папку, к ней — номер коммита для /health.
    export_dir="$(mktemp -d)"
    trap 'rm -rf "$export_dir"' EXIT
    git archive --format=tar HEAD | tar -x -C "$export_dir"
    printf '%s\n' "$commit" > "$export_dir/app/COMMIT"
    # Настройки (.env) и ключи (secrets) у сервера свои — их не трогаем. Ничего не удаляем.
    rsync -av --checksum --chmod=D755,F644 --exclude .env --exclude secrets/ --exclude .git/ --exclude __pycache__/ \
      "$export_dir/" "$SERVER:$SERVER_DIR/"
    say "Файлы на месте. Собираю и перезапускаю сайт на сервере (bash run.sh)…"
    ssh -t "$SERVER" "cd '$SERVER_DIR' && bash run.sh"
    say "Готово: на сервере версия $version (коммит $commit)."
    ;;
  console)
    say "Терминал сервера, папка сайта $SERVER_DIR. Выйти — команда exit."
    exec ssh -t "$SERVER" "cd '$SERVER_DIR' && exec bash -l"
    ;;
  status)
    say "Ноутбук: версия $(cat VERSION), последний коммит $(git log -1 --format='%h «%s»' 2>/dev/null || echo '?')"
    echo " Сервер $SERVER:"
    echo
    # shellcheck disable=SC2029  # SERVER_DIR подставляется здесь, на ноутбуке, — так и надо
    ssh "$SERVER" "cd '$SERVER_DIR' && bash -s" <<'EOS'
echo " Версия сайта: $(cat VERSION), коммит $(cat app/COMMIT 2>/dev/null || echo '— (отправлена до 0.25.0)')"
echo " Папка конкурсов сайта: $(grep '^TENDERS_DIR=' .env | cut -d= -f2-)"
echo
echo " Контейнеры:"
docker compose ps --format '   {{.Service}}: {{.Status}}'
echo
# </dev/null: docker compose exec читает ввод, а ввод здесь — этот же список команд; без него остаток списка
# (диск, Docker при старте) проглатывался (0.24.0).
echo " Dropbox: $(docker compose exec -T dropbox dropbox status 2>/dev/null </dev/null || echo 'не запущен')"
echo
df -h / | awk 'NR==2 {print " Диск: занято " $3 " из " $2 ", свободно " $4}'
echo " Docker при старте сервера: $(systemctl is-enabled docker 2>/dev/null || echo '?')"
EOS
    echo
    env_check
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
