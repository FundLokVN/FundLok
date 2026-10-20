FROM python:3.12-slim

# No .pyc writes: the app user cannot write to /app, by design.
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Production dependencies only; the test tools are in requirements-dev.txt.
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# The API runs as an unprivileged user, not root (compliance audit SEC-07).
# The code stays root-owned and read-only to it, so a compromised process
# cannot rewrite the app it is running. Port 8080 needs no privilege.
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin app

COPY . .

ENV PORT=8080

RUN chmod +x /app/docker/entrypoint.sh

USER app

# Runs `alembic upgrade head` then starts uvicorn (see docker/entrypoint.sh).
CMD ["/app/docker/entrypoint.sh"]
