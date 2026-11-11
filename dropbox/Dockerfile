# «Коробка» Dropbox (0.22.0) — официальная программа Dropbox для Linux без окна.
# Нужна на сервере: папки конкурсов приходят туда через Dropbox, как на компьютер с Dropbox для Windows.
# Включается строкой COMPOSE_PROFILES=dropbox в .env (см. compose.yml, сервис dropbox). На компьютере
# с Dropbox для Windows не нужна — там папку синхронизирует сам Dropbox для Windows.

# Та же основа, что у сайта: образ уже скачан, второй раз не качается.
FROM python:3.12-slim-bookworm

# Библиотеки, без которых программа Dropbox не запускается даже без окна (список с форума Dropbox,
# «Headless install on Debian»). Зеркало пакетов Debian (APT_MIRROR) и прокси — из run.sh, как у сайта.
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates wget \
    libatomic1 libglib2.0-0 libglapi-mesa libxdamage1 libxfixes3 libxcb-glx0 libxcb-dri2-0 libxcb-dri3-0 \
    libxcb-present0 libxcb-sync1 libxshmfence1 libxxf86vm1 \
 && rm -rf /var/lib/apt/lists/*

# Пользователь dropbox с тем же номером (UID 1000), что у сайта: файлы, которые скачивает Dropbox,
# сайт может менять, а файлы сайта Dropbox может отправлять в облако.
RUN useradd --create-home --uid 1000 dropbox

# Программа Dropbox (последняя версия с dropbox.com) и её командная строка dropbox.py.
# Точную версию закрепить нельзя: программа обновляется сама, старые версии Dropbox перестаёт пускать.
RUN wget -qO- "https://www.dropbox.com/download?plat=lnx.x86_64" | tar xz -C /opt \
 && mv /opt/.dropbox-dist /opt/dropbox-dist \
 && wget -qO /opt/dropbox.py "https://www.dropbox.com/download?dl=packages/dropbox.py" \
 && test -x /opt/dropbox-dist/dropboxd

COPY start.sh /usr/local/bin/dropbox-start
COPY dropbox-cli.sh /usr/local/bin/dropbox
RUN chmod 755 /usr/local/bin/dropbox-start /usr/local/bin/dropbox

# Запускается от root только чтобы отдать папки пользователю dropbox, сама программа — от dropbox.
CMD ["dropbox-start"]
