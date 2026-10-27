# Из чего собирается «коробка» сайта.
# Одна и та же на компьютере и на будущем сервере. Из неё запускаются два процесса:
# сайт (web) и обработчик документов (worker) — см. compose.yml.

# Основа: официальный образ Python 3.12 на Debian (bookworm), облегчённый вариант.
FROM python:3.12-slim-bookworm

# Не писать .pyc-файлы и сразу выводить журнал (без буфера).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Программы для документов:
#   LibreOffice без окна — конвертация .doc/.xls/.rtf/.odt в .docx/.xlsx;
#   Tesseract с русским языком — распознавание сканов;
#   unrar — архивы RAR (раздел non-free Debian).
# Если какого-то пакета нет — ставится запасной вариант (обычный LibreOffice, свободный unar).
# Прокси (если нужен) и зеркало пакетов Debian (APT_MIRROR, например mirror.yandex.ru) — из run.sh.
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi \
 && sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && (apt-get install -y --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui \
     || apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc) \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus fonts-dejavu-core \
 && (apt-get install -y --no-install-recommends unrar || apt-get install -y --no-install-recommends unar) \
 && rm -rf /var/lib/apt/lists/*

# Встроенный браузер без окна (Chromium) — для поиска цен: открывает страницы магазинов, где цену рисует JavaScript.
# Отдельным шагом и не обязательно: если пакет не скачался, сайт работает, просто такие страницы не открываются.
RUN apt-get update \
 && (apt-get install -y --no-install-recommends chromium fonts-liberation \
     || echo "ВНИМАНИЕ: Chromium не установлен — страницы, где цену рисует JavaScript, открываться не будут") \
 && rm -rf /var/lib/apt/lists/*

# Обычный пользователь вместо root. UID 1000 совпадает с пользователем Ubuntu (user_1),
# поэтому файлы в папке данных будут принадлежать вам, а не root.
RUN useradd --create-home --uid 1000 app

WORKDIR /app

# Сначала только список библиотек: при правке кода этот шаг берётся из кэша и не скачивается заново.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код сайта, скрипты и номер версии.
COPY VERSION .
COPY tools ./tools
COPY app ./app

USER app
EXPOSE 8000

# Внутри контейнера сайт слушает все адреса — наружу его выпускает только compose.yml,
# и только на 127.0.0.1 этого компьютера.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
