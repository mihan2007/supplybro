#!/usr/bin/env bash
# Собрать «коробку» сайта и запустить её.
# Запуск (в Ubuntu, из папки проекта):  bash run.sh
#
# Почему сборка не через docker compose: шагам сборки нужен прокси Windows (127.0.0.1:порт),
# а он виден только из сети самой Ubuntu. Обычный docker build умеет собирать в этой сети
# (--network host), а docker compose без особых разрешений — нет.

set -euo pipefail
cd "$(dirname "$0")"

# 0. Docker запускается вместе с Ubuntu — если Ubuntu только что включилась, ждём его до 30 секунд.
for _ in $(seq 1 30); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "Docker не отвечает. Проверьте: systemctl is-active docker" >&2
  exit 1
fi

# 1. Настройки из .env. Если файла нет — создаём из образца; если в образце появились
#    новые настройки — дописываем их в .env (уже заданные значения не трогаем).
#    Образец (0.25.0): на ноутбуке (Ubuntu в WSL) — .env.example; на сервере — он же, но настройки машины
#    (пути, прокси, compose-файлы) — из server/server.env.example. Где мы — по /proc/version
#    (в WSL там «microsoft»); подсказать вручную — SB_MACHINE=laptop|server bash run.sh.
SERVER_TEMPLATE=server/server.env.example
machine="${SB_MACHINE:-}"
if [ -z "$machine" ]; then
  if grep -qi microsoft /proc/version 2>/dev/null; then machine=laptop; else machine=server; fi
fi
[ "$machine" = server ] && [ ! -f "$SERVER_TEMPLATE" ] && machine=laptop   # старая папка без образца сервера

env_keys() { sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$1"; }
# Строка новой настройки: на сервере — из образца сервера, если она там есть, иначе — из .env.example.
template_line() {
  if [ "$machine" = server ] && grep -q "^$1=" "$SERVER_TEMPLATE"; then
    grep -m1 "^$1=" "$SERVER_TEMPLATE"
  else
    grep -m1 "^$1=" .env.example
  fi
}

if [ ! -f .env ]; then
  if [ "$machine" = server ]; then
    # .env.example, где строки настроек машины заменены строками образца сервера; настройки, которых
    # в .env.example нет (COMPOSE_PROFILES), — в конце.
    awk -v tpl="$SERVER_TEMPLATE" '
      BEGIN {
        while ((getline l < tpl) > 0)
          if (l ~ /^[A-Za-z_][A-Za-z0-9_]*=/) { k = l; sub(/=.*/, "", k); v[k] = l; order[++n] = k }
      }
      /^[A-Za-z_][A-Za-z0-9_]*=/ { k = $0; sub(/=.*/, "", k); if (k in v) { print v[k]; used[k] = 1; next } }
      { print }
      END {
        head = 0
        for (i = 1; i <= n; i++) if (!(order[i] in used)) {
          if (!head) { print ""; print "# Только для сервера (" tpl ")"; head = 1 }
          print v[order[i]]
        }
      }' .env.example > .env
    echo "Создан .env для сервера (.env.example + $SERVER_TEMPLATE) — проверьте настройки."
  else
    cp .env.example .env
    echo "Создан .env из .env.example — проверьте настройки."
  fi
else
  while IFS= read -r key; do
    if ! grep -q "^${key}=" .env; then
      printf '\n%s\n' "$(template_line "$key")" >> .env
      echo "В .env добавлена новая настройка: $(template_line "$key")"
    fi
  done < <({ env_keys .env.example; if [ "$machine" = server ]; then env_keys "$SERVER_TEMPLATE"; fi; } | awk '!seen[$0]++')
fi
set -a
. ./.env
set +a

# 2. Папка данных должна существовать заранее и принадлежать вам
#    (иначе Docker создаст её от root, и сайт не сможет в неё писать).
mkdir -p "${DATA_DIR:?Укажите DATA_DIR в .env}"

# 3. Ключ NocoDB должен быть на месте (сам ключ не выводится).
if [ ! -s "${NOCODB_TOKEN_FILE:?Укажите NOCODB_TOKEN_FILE в .env}" ]; then
  echo "Нет файла с ключом NocoDB: $NOCODB_TOKEN_FILE — проверьте NOCODB_TOKEN_FILE в .env" >&2
  exit 1
fi

# 3а. Ключи Claude и DeepSeek — для анализа документов (сами ключи не выводятся).
for pair in "CLAUDE_KEY_FILE:Claude" "DEEPSEEK_KEY_FILE:DeepSeek"; do
  var="${pair%%:*}"; name="${pair#*:}"; file="${!var:-}"
  if [ -z "$file" ] || [ ! -s "$file" ]; then
    echo "Нет файла с ключом $name: ${file:-(не указан)} — проверьте $var в .env" >&2
    exit 1
  fi
done

# 3а-2. Ключ Яндекса — для поиска цен через Яндекс (по выбору). Без него поиск идёт через DeepSeek:
#       создаём пустой файл, чтобы контейнер запустился (ключ — потом, в этот же файл).
if [ -z "${YANDEX_KEY_FILE:-}" ]; then
  echo "В .env не указан YANDEX_KEY_FILE" >&2
  exit 1
fi
if [ ! -s "$YANDEX_KEY_FILE" ]; then
  echo "Нет ключа Яндекса ($YANDEX_KEY_FILE) — поиск цен через Яндекс недоступен (через DeepSeek — работает)."
  [ -e "$YANDEX_KEY_FILE" ] || : > "$YANDEX_KEY_FILE"
fi

# Прокси и зеркало пакетов для сборки «коробок» (сайта и Dropbox).
#    Прокси передаётся в обоих написаниях: apt читает строчные http_proxy/https_proxy, pip — любые.
BUILD_ARGS=(
  --build-arg HTTP_PROXY="${BUILD_PROXY:-}"
  --build-arg HTTPS_PROXY="${BUILD_PROXY:-}"
  --build-arg http_proxy="${BUILD_PROXY:-}"
  --build-arg https_proxy="${BUILD_PROXY:-}"
  --build-arg APT_MIRROR="${APT_MIRROR:-}"
)

# 3б-0. Синхронизация с Dropbox (0.22.0, с 0.23.0 — rclone; сервер: COMPOSE_PROFILES=dropbox в .env) — собрать
#       и запустить первой: папка конкурсов появляется, когда Dropbox подключён и папка скачана.
if [[ ",${COMPOSE_PROFILES:-}," == *",dropbox,"* ]]; then
  docker build --network host "${BUILD_ARGS[@]}" -t supplybro-dropbox:dev dropbox
  docker compose up -d dropbox
  if [ ! -d "${TENDERS_DIR:-}" ]; then
    echo
    echo "Синхронизация с Dropbox запущена, но папки конкурсов пока нет: ${TENDERS_DIR:-(TENDERS_DIR не указан в .env)}"
    echo "1. Если Dropbox ещё не подключён — подключить (один раз; сеанс ssh с туннелем — см. README):"
    echo "     docker compose exec dropbox dropbox connect"
    echo "2. Как идёт сверка (ждать «Up to date»):"
    echo "     docker compose exec dropbox dropbox status"
    echo "3. Когда папка появится — снова: bash run.sh"
    exit 0
  fi
fi

# 3б. Папка конкурсов (Dropbox\Агент) должна быть видна из Ubuntu.
if [ ! -d "${TENDERS_DIR:?Укажите TENDERS_DIR в .env}" ]; then
  echo "Не найдена папка конкурсов: $TENDERS_DIR — проверьте TENDERS_DIR в .env" >&2
  exit 1
fi

# 4. Сборка образа (через прокси, если он указан в BUILD_PROXY).
#    Первая сборка после добавления LibreOffice и распознавания текста — 5–15 минут, дальше — секунды.
docker build --network host "${BUILD_ARGS[@]}" -t supplybro-web:dev .

# 5. Запуск (или перезапуск с новым образом) в фоне.
docker compose up -d

echo
echo "Готово. Откройте в браузере Windows: http://127.0.0.1:${WEB_PORT:-8000}"
