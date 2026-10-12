"""
TokenPool — Production Gunicorn Configuration
Runs FastAPI application with high-performance asynchronous Uvicorn worker processes.
"""
import multiprocessing
import os

# Binding
bind = os.getenv("BIND_ADDRESS", "0.0.0.0:8000")

# Worker concurrency
# Recommended formula: (2 * CPU cores) + 1, or override via WEB_CONCURRENCY
workers = int(os.getenv("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count() * 2 + 1)))
worker_class = "uvicorn.workers.UvicornWorker"

# Timeouts & Keepalive
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "65"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# Worker recycling to mitigate potential memory fragmentation
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "2000"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "200"))

# Logging
loglevel = os.getenv("LOG_LEVEL", "info")
accesslog = "-"
errorlog = "-"
capture_output = True
