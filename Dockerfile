# Из чего собирается «коробка» сайта.
# Одна и та же на компьютере и на будущем сервере.

# Основа: официальный образ Python 3.12 на Debian (bookworm), облегчённый вариант.
FROM python:3.12-slim-bookworm

# Не писать .pyc-файлы и сразу выводить журнал (без буфера).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Обычный пользователь вместо root. UID 1000 совпадает с пользователем Ubuntu (user_1),
# поэтому файлы в папке данных будут принадлежать вам, а не root.
RUN useradd --create-home --uid 1000 app

WORKDIR /app

# Сначала только список библиотек: при правке кода этот шаг берётся из кэша и не скачивается заново.
# Прокси для скачивания (если нужен) передаётся при сборке из run.sh.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код сайта и номер версии.
COPY VERSION .
COPY app ./app

USER app
EXPOSE 8000

# Внутри контейнера сайт слушает все адреса — наружу его выпускает только compose.yml,
# и только на 127.0.0.1 этого компьютера.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
