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
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env из .env.example — проверьте настройки."
else
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    if ! grep -q "^${key}=" .env; then
      printf '\n%s\n' "$line" >> .env
      echo "В .env добавлена новая настройка: $key"
    fi
  done < .env.example
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

# 3а. Папка конкурсов (Dropbox\Агент) должна быть видна из Ubuntu.
if [ ! -d "${TENDERS_DIR:?Укажите TENDERS_DIR в .env}" ]; then
  echo "Не найдена папка конкурсов: $TENDERS_DIR — проверьте TENDERS_DIR в .env" >&2
  exit 1
fi

# 4. Сборка образа (через прокси, если он указан в BUILD_PROXY).
#    Прокси передаётся в обоих написаниях: apt читает строчные http_proxy/https_proxy, pip — любые.
#    Первая сборка после добавления LibreOffice и распознавания текста — 5–15 минут, дальше — секунды.
docker build --network host \
  --build-arg HTTP_PROXY="${BUILD_PROXY:-}" \
  --build-arg HTTPS_PROXY="${BUILD_PROXY:-}" \
  --build-arg http_proxy="${BUILD_PROXY:-}" \
  --build-arg https_proxy="${BUILD_PROXY:-}" \
  --build-arg APT_MIRROR="${APT_MIRROR:-}" \
  -t supplybro-web:dev .

# 5. Запуск (или перезапуск с новым образом) в фоне.
docker compose up -d

echo
echo "Готово. Откройте в браузере Windows: http://127.0.0.1:${WEB_PORT:-8000}"
