FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASHCOORD_DB_PATH=/data/flashcoord.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)" \
    || exit 1

# Exec form runs uvicorn as PID 1 so it receives signals and SIGTERM flushes
# SQLite WAL on graceful shutdown.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
