FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-lock.txt \
    && useradd --create-home --uid 10001 bot \
    && mkdir /data \
    && chown bot:bot /data

COPY --chown=bot:bot order_bot ./order_bot
USER bot
CMD ["python", "-m", "order_bot"]

