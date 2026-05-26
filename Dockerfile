# ============================================================
# SignalPipe - B2B Travel Price Protection
# Production Dockerfile
# ============================================================
#
# Base: Official Microsoft Playwright Python image
#   mcr.microsoft.com/playwright/python:v1.44.0-jammy
#   Ships with Chromium + all browser OS dependencies
#   pre-installed — no manual apt-get or playwright install
#   steps required.
#
# Build targets:
#   api    - FastAPI / Uvicorn           (default)
#   worker - Celery consumer + Playwright browser scraper
#
# Build:
#   docker build --target api    -t signalpipe-api:latest .
#   docker build --target worker -t signalpipe-worker:latest .
#
# Run locally:
#   docker run --env-file .env -p 8000:8000 signalpipe-api:latest
#   docker run --env-file .env signalpipe-worker:latest
# ============================================================

# ─────────────────────────────────────────────────────────────
# BASE  — shared layers for both api and worker images
# ─────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Python dependencies — copied first so Docker can cache this
# layer independently of application source changes.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application source — copy only src/ to keep the image lean
# and avoid leaking .env files, test fixtures, or IDE artefacts.
COPY src/ ./src/

# Non-root runtime user for production hardening.
# Playwright browsers in the MS base image are installed at
# /ms-playwright and are world-readable; no extra permissions needed.
RUN groupadd --system --gid 1001 appgroup \
 && useradd  --system --uid 1001 --gid 1001 --no-create-home appuser

USER appuser

# ─────────────────────────────────────────────────────────────
# API  — FastAPI served by Uvicorn  (default build target)
# ─────────────────────────────────────────────────────────────
FROM base AS api

EXPOSE 8000

# Health check via FastAPI's built-in OpenAPI schema endpoint.
# The ALB / Fargate target group runs its own check in parallel.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/openapi.json || exit 1

CMD ["uvicorn", "src.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--log-level", "info"]

# ─────────────────────────────────────────────────────────────
# WORKER  — Celery consumer + Playwright browser scraper
#
# Override CMD in the ECS task definition to select run mode:
#   Celery consumer : ["celery", "-A", "src.worker", "worker",
#                      "--loglevel=info", "--concurrency=4"]
#   SQS inline mode : ["python", "-m", "src.tasks"]
# ─────────────────────────────────────────────────────────────
FROM base AS worker

CMD ["celery", "-A", "src.worker", "worker", \
     "--loglevel=info", \
     "--concurrency=4"]
