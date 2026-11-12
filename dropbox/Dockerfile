# «Коробка» синхронизации с Dropbox (0.23.0) — rclone: папка конкурсов сервера сверяется с Dropbox в обе стороны
# через его API раз в минуту (rclone bisync). Программа Dropbox для Linux (0.22.x) в контейнере после привязки
# висела на «Starting...» и теряла привязку — от неё отказались.
# Включается строкой COMPOSE_PROFILES=dropbox в .env (см. compose.yml, сервис dropbox). На компьютере
# с Dropbox для Windows не нужна — там папку синхронизирует сам Dropbox для Windows.
FROM ubuntu:24.04

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Moscow

# Зеркало пакетов (APT_MIRROR, например mirror.yandex.ru) и прокси — из run.sh, как у сайта.
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i -e "s|archive.ubuntu.com|$APT_MIRROR|g" -e "s|security.ubuntu.com|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates wget unzip tzdata \
 && rm -rf /var/lib/apt/lists/*

# Пользователь dropbox с тем же номером (UID 1000), что у сайта: скачанные файлы сайт может менять, а файлы
# сайта rclone может отправлять в Dropbox. В Ubuntu 24.04 номер 1000 занят пользователем ubuntu — он не нужен.
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && useradd --create-home --uid 1000 --user-group dropbox

# rclone — точная версия с GitHub (1.75.2 от 09.10.2026; 1.76.0 от 06.11.2026 моложе двух недель),
# архив сверяется с опубликованной контрольной суммой. Имя переменной — не RCLONE_*: такие rclone читает как свои
# настройки (RCLONE_VERSION = флаг --version — 0.23.0 на сервере так и не собрался).
ARG SB_RCLONE_VERSION=1.75.2
RUN cd /tmp \
 && wget -q "https://github.com/rclone/rclone/releases/download/v${SB_RCLONE_VERSION}/rclone-v${SB_RCLONE_VERSION}-linux-amd64.zip" \
 && wget -q "https://github.com/rclone/rclone/releases/download/v${SB_RCLONE_VERSION}/SHA256SUMS" \
 && grep " rclone-v${SB_RCLONE_VERSION}-linux-amd64.zip\$" SHA256SUMS | sha256sum -c - \
 && unzip -q "rclone-v${SB_RCLONE_VERSION}-linux-amd64.zip" \
 && install -m 755 "rclone-v${SB_RCLONE_VERSION}-linux-amd64/rclone" /usr/local/bin/rclone \
 && rm -rf /tmp/rclone-v* /tmp/SHA256SUMS \
 && rclone version

COPY filters.txt /etc/supplybro/dropbox-filters.txt
COPY start.sh /usr/local/bin/dropbox-start
COPY sync.sh /usr/local/bin/dropbox-sync
COPY dropbox-cli.sh /usr/local/bin/dropbox
RUN chmod 644 /etc/supplybro/dropbox-filters.txt \
 && chmod 755 /usr/local/bin/dropbox-start /usr/local/bin/dropbox-sync /usr/local/bin/dropbox

# Запускается от root только чтобы отдать папки пользователю dropbox, сверка — от него.
CMD ["dropbox-start"]
