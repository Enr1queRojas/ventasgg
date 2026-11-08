FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_PORT=8003 \
    DATABASE_PATH=/data/sales_bot.db \
    TZ=America/Mexico_City

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Usuario sin privilegios. /data se crea con su dueño para que el volumen nombrado
# herede los permisos al crearse por primera vez.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data

COPY app ./app
COPY scripts ./scripts
COPY data/seed ./data/seed

USER app

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"APP_PORT\", \"8003\")}/health', timeout=4)"

# Una sola instancia: el worker interno no admite varios procesos consumiendo la cola.
CMD ["python", "-m", "app.main"]
