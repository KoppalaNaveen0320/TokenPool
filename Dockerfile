# TokenPool — Production Dockerfile
FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Install system dependencies: tesseract-ocr (for payment verification OCR), curl (for health checks), build essentials
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Security hardening: Create non-root system user and configure upload directory permissions
RUN useradd -u 1000 -m tokenpool && \
    mkdir -p /app/backend/app/uploads && \
    chown -R tokenpool:tokenpool /app

USER tokenpool

EXPOSE 8000

# Container healthcheck querying verified /api/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Production entrypoint: Gunicorn master process with Uvicorn async workers
CMD ["gunicorn", "-c", "gunicorn.conf.py", "backend.app.main:app"]
