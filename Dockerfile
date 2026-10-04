FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
ENV DB_PATH=/data/exchange.db PORT=8000 PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8000
# One process only: the order books live in memory behind a single lock.
CMD uvicorn app.main:app_from_env --factory --host 0.0.0.0 --port ${PORT} \
    --proxy-headers --forwarded-allow-ips='*'
