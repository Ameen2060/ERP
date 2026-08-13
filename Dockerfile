FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    AUTH_ENABLED=true \
    RESET_EXPOSE_TOKEN=false \
    # Data (SQLite DB, attachments, org logo) lives on a mounted persistent disk at /data.
    DATABASE_URL=sqlite:////data/accounting.sqlite3 \
    ATTACHMENTS_DIR=/data/attachments \
    ORG_DIR=/data/org

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./

# Ensure the data directory exists even before a persistent disk is mounted (local runs).
RUN mkdir -p /data

EXPOSE 8100

# Bind to the platform-provided $PORT (Render/Railway/Fly set it); default 8100 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
