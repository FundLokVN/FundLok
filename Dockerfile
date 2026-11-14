FROM python:3.12-slim

# No .pyc writes: the app user cannot write to /app, by design.
ENV PYTHONDONTWRITEBYTECODE=1

# Debian security updates the upstream python image has not been rebuilt with
# yet. On 2026-11-14 python:3.12-slim (Debian 13.7) still shipped zlib1g
# 1.3.1-1+b1 (CVE-2026-76844, CVE-2026-85091, CVE-2026-27171) although Debian
# had released +deb13u1. Whenever this layer is rebuilt it picks up every
# security fix Debian has published by then.
#
# Tesseract and its Vietnamese data read investors' transfer receipts on this
# server, so no receipt is sent to an outside OCR service (Q-025;
# docs/specs/direct_transfer/receipt-ocr.md). Used only when
# RECEIPT_OCR_ENABLED is on.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The base image's pip 25.0.1 has six advisories (CVE-2025-8869,
# CVE-2026-1703, -3219, -6357, -8643, -13346; all fixed by 26.2.0). Replace it
# before it installs anything. Pinned exactly, like requirements.txt.
RUN pip install --no-cache-dir pip==26.3

# Production dependencies only; the test tools are in requirements-dev.txt.
COPY requirements.txt .

# pip is then removed: nothing at runtime uses it (docker/entrypoint.sh runs
# only alembic and uvicorn; scripts/docker_test.sh uses its own python:3.11
# container). It would otherwise stay as attack surface, and pip >= 26 ships an
# SBOM listing the setuptools 70.3.0 it vendors pkg_resources from, so image
# scanners report setuptools CVE-2025-47273 / CVE-2026-59890 /
# GHSA-xmj3-9gf7-h52w against it. None of them is in the vendored code.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip

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
