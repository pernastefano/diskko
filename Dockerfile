# syntax=docker/dockerfile:1

# ---- builder: install dependencies into an isolated venv ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PUID=1000 \
    PGID=1000 \
    PORT=8000 \
    LOG_FORMAT=json \
    DATA_DIR=/data \
    MUSIC_DIR=/music \
    DOWNLOADS_DIR=/downloads

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /music /downloads

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# The entrypoint starts as root only to align PUID/PGID, then drops privileges.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
