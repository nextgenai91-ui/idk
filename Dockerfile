# Dinero social scraper worker.
# Ships the Python-only scrapers (Instagram, TikTok, LinkedIn, X, Facebook,
# startup funding) that cannot run inside Dinero's edge backend.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# Chromium + driver for the Selenium-based LinkedIn scraper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl chromium chromium-driver ca-certificates \
        fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    PATH="/usr/lib/chromium:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Playwright browser for TikTok-Api.
RUN python -m playwright install --with-deps chromium || true

COPY app.py .

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --limit-concurrency 4"]
