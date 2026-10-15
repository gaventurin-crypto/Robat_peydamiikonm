FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bot.sqlite3

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY app ./app
USER bot
VOLUME ["/data"]
CMD ["python", "-m", "app.main"]