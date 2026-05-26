# ---------- Base ----------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies required by Playwright & psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
    libxcomposite1 libxdamage1 libxrandr2 libpango-1.0-0 \
    libasound2 libcups2 libxshmfence1 libx11-xcb1 \
    libgcc-s1 libstdc++6 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Playwright Chromium
RUN playwright install chromium && playwright install-deps chromium

# Application source
COPY . .

# ---------- API target ----------
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

# ---------- Worker target ----------
FROM base AS worker
CMD ["celery", "-A", "src.worker", "worker", "--loglevel=info", "--concurrency=4"]
