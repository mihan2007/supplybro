#!/usr/bin/env bash
# Перенести папку конкурса с этого компьютера на сервер (в папку конкурсов сайта на сервере).
# Только добавляет файлы, которых на сервере нет: существующие не перезаписывает, ничего не удаляет.
# Запуск: bash server/push_tender.sh 948   (номер конкурса без буквы К; клиент — «Наша»)
set -euo pipefail
cd "$(dirname "$0")/.."
. server/server.conf
NUM="${1:?укажите номер конкурса, например 948}"
CLIENT="${2:-Наша}"
LOCAL_ROOT="$(grep '^TENDERS_DIR=' .env | cut -d= -f2- | sed 's/^"//; s/"$//')"
shopt -s nullglob
src=("$LOCAL_ROOT/$CLIENT"/К"$NUM"[!0-9]*)
if [ ${#src[@]} -ne 1 ]; then
  echo " На этом компьютере папок К$NUM: ${#src[@]} (нужна ровно одна) в $LOCAL_ROOT/$CLIENT"; exit 1
fi
REMOTE_ROOT="$(ssh -n "$SERVER" "grep '^TENDERS_DIR=' '$SERVER_DIR/.env' | cut -d= -f2-" | sed 's/^"//; s/"$//')"
dst="$(ssh -n "$SERVER" "ls -d '$REMOTE_ROOT/$CLIENT'/К${NUM}[!0-9]* 2>/dev/null" || true)"
if [ "$(printf '%s\n' "$dst" | grep -c .)" -gt 1 ]; then
  echo " На сервере несколько папок К$NUM:"; printf '%s\n' "$dst"; exit 1
fi
if [ -z "$dst" ]; then dst="$REMOTE_ROOT/$CLIENT/$(basename "${src[0]}")"; fi
echo " С компьютера: ${src[0]}"
echo " На сервер:    $dst"
echo
rsync -av --protect-args --ignore-existing --chmod=D755,F644 "${src[0]}/" "$SERVER:$dst/"
echo
echo " Готово. Обновите конкурс К$NUM на сайте сервера."
