# TokenPool — Production Deployment Guide

This guide describes the complete, production-grade deployment procedure for **TokenPool** (AI Usage & Capacity Marketplace). Follow these instructions to deploy the application securely with external PostgreSQL, Gunicorn/Uvicorn, Nginx reverse proxy, and SSL/TLS.

---

## 1. Prerequisites

Before starting the deployment, ensure the production server meets the following requirements:

- **Operating System:** Linux (Ubuntu 22.04 LTS / Debian 12 / RHEL 9 recommended)
- **Runtime:** Python 3.10+ (or Docker Engine 24+ & Docker Compose v2)
- **Database:** Dedicated PostgreSQL 14+ instance (managed cloud DB like AWS RDS, GCP Cloud SQL, or hardened self-hosted instance)
- **System Dependencies:**
  - `tesseract-ocr` and `tesseract-ocr-eng` (required for payment receipt OCR verification)
  - `libtesseract-dev`
  - `curl` (for healthcheck probes)
- **Networking:**
  - Fully Qualified Domain Name (FQDN) pointed via DNS A/AAAA records to your server IP
  - Ports `80` (HTTP) and `443` (HTTPS) open on the firewall
  - Port `8000` bound only to `127.0.0.1` or internal Docker network (never exposed publicly)

---

## 2. PostgreSQL Setup

TokenPool requires a persistent, production-grade PostgreSQL database.

### 2.1 Database Provisioning
Run the following SQL commands as `postgres` superuser on your PostgreSQL host:

```sql
CREATE USER tokenpool_prod WITH PASSWORD '<GENERATE_STRONG_DB_PASSWORD>';
CREATE DATABASE tokenpool_production OWNER tokenpool_prod;
GRANT ALL PRIVILEGES ON DATABASE tokenpool_production TO tokenpool_prod;
```

### 2.2 Connection String Format
Construct the production connection string:
```text
postgresql://tokenpool_prod:<STRONG_DB_PASSWORD>@<DB_HOST>:5432/tokenpool_production
```

> **Important:** Do NOT use SQLite in production. Never wipe or recreate existing schemas.

---

## 3. Environment Variables

Create your production configuration file by copying the template:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

Populate the configuration with production values:

| Variable | Description | Production Example |
| :--- | :--- | :--- |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://tokenpool_prod:<PASSWORD>@db.internal:5432/tokenpool_production` |
| `JWT_SECRET` | Cryptographically secure secret for user session tokens | Generated 64-byte random string |
| `FERNET_KEY` | 32-byte URL-safe base64 Fernet key for credential vault | Generated Fernet key |
| `DEMO_MODE` | Global demo/mock execution toggle | `false` |
| `ENVIRONMENT` | Deployment environment name | `production` |
| `CORS_ORIGINS` | Permitted browser origins (comma-separated) | `https://tokenpool.yourdomain.com` |
| `COMMISSION_RATE` | Platform commission rate (default: 20%) | `0.20` |
| `REQUIRE_PAYMENT_GATEWAY` | Gating for direct wallet modifications | `true` |
| `PAYMENT_WEBHOOK_SECRET` | Secret for payment gateway HMAC signatures | Generated random secret string |
| `GEMINI_API_KEY` | Optional TokenPool platform Gemini key | Platform key or empty |
| `OPENAI_API_KEY` | Optional TokenPool platform OpenAI key | Platform key or empty |
| `ANTHROPIC_API_KEY` | Optional TokenPool platform Anthropic key | Platform key or empty |
| `OPENAI_DELEGATED_PLAN_ENABLED` | Delegated plan toggle | `false` |
| `OPENAI_PARTICIPATING_APP_STATUS` | Official OpenAI status | `unregistered` |

---

## 4. Secret Generation

Never reuse development secrets. Generate fresh production secrets using Python's standard `secrets` and `cryptography` libraries:

### 4.1 Generate `JWT_SECRET`
Run:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```
Place the output in `JWT_SECRET`.

### 4.2 Generate `FERNET_KEY`
Run:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Place the output in `FERNET_KEY`.

### 4.3 Generate `PAYMENT_WEBHOOK_SECRET`
Run:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
Place the output in `PAYMENT_WEBHOOK_SECRET`.

---

## 5. Database Migrations (Alembic)

Before starting the application, run database migrations to bring the PostgreSQL schema up to head:

```bash
# Verify Alembic target head
alembic heads
# Expected output: f4a17d5e8b3c (head)

# Execute migration to head
alembic upgrade head

# Verify current revision in database
alembic current
# Expected output: f4a17d5e8b3c (head)
```

> **Note:** Alembic reads `DATABASE_URL` directly from your environment. Do NOT run destructive migrations or table drops.

---

## 6. DEMO_MODE Configuration

Production configuration **MUST** set:
```env
DEMO_MODE=false
```

### Behavioral Guarantees when `DEMO_MODE=false`:
1. `MockAIProvider` is strictly disabled and will never execute consumer requests.
2. If a model listing has no active, eligible real provider connection:
   - The request is immediately rejected with HTTP `503 Service Unavailable`.
   - Error code / message: `EXECUTION_NOT_SUPPORTED`.
   - Zero credits or wallet balance are deducted.
3. The platform will never silently fall back to mock or sandbox execution in production.

---

## 7. CORS Configuration

Wildcard `*` and `localhost` origins are strictly prohibited in production.

Configure your actual deployed frontend and API domain:
```env
CORS_ORIGINS=https://tokenpool.yourdomain.com
```

If multiple domains are used (e.g. separate app and API domains), list them separated by commas:
```env
CORS_ORIGINS=https://app.yourdomain.com,https://api.yourdomain.com
```

---

## 8. Provider API Credential Model

TokenPool uses a decentralized marketplace capacity model:
- **No Global Key Requirement:** TokenPool does NOT require or use a single global API key for provider listings.
- **Provider-Owned Capacity:** Providers connect their own authorized API credentials (OpenAI, Google Gemini, Anthropic) via `/api/v1/provider/connections`.
- **Encryption at Rest:** All credentials are symmetrically encrypted using AES-128-CBC + HMAC-SHA256 (Fernet) via `FERNET_KEY` before being written to PostgreSQL.
- **Strict Masking:** Frontend responses only receive masked strings (e.g., `sk-proj-••••••••9842` or `AIza••••••••3981`).
- **Zero Leakage:** Raw credentials are never logged, never returned in API payloads, and never embedded in HTML/JS.

---

## 9. Application Startup

### Option A: Direct Host Deployment (Systemd + Gunicorn)

Create a systemd unit file `/etc/systemd/system/tokenpool.service`:

```ini
[Unit]
Description=TokenPool Production Gunicorn Application
After=network.target

[Service]
User=tokenpool
Group=tokenpool
WorkingDirectory=/opt/tokenpool
EnvironmentFile=/opt/tokenpool/.env.production
ExecStart=/opt/tokenpool/venv/bin/gunicorn -c gunicorn.conf.py backend.app.main:app
Restart=always
RestartSec=5
KillSignal=SIGQUIT
Type=notify
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tokenpool
sudo systemctl start tokenpool
```

#### Exact Production Start Command:
```bash
gunicorn -c gunicorn.conf.py backend.app.main:app
```
Or directly specifying parameters:
```bash
gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 backend.app.main:app
```

### Option B: Docker Compose Deployment

```bash
# 1. Build and start containers in detached mode
docker compose -f docker-compose.production.yml up -d --build

# 2. Check service status
docker compose -f docker-compose.production.yml ps

# 3. View logs
docker compose -f docker-compose.production.yml logs -f app
```

---

## 10. Reverse Proxy Architecture

Production architecture follows a strict layered proxy topology:

```text
Internet
   ↓ (Port 443 / HTTPS)
Reverse Proxy (Nginx / Caddy with TLS Termination)
   ↓ (Port 8000 / HTTP loopback or Docker bridge)
Gunicorn Master Process (with UvicornWorker pool)
   ↓ (Asynchronous ASGI execution)
FastAPI Application
   ↓ (Connection Pool / SQLAlchemy)
PostgreSQL Database
```

> **Security Rule:** Never expose the Uvicorn development server (`uvicorn ... --reload`) directly to the public internet.

The provided `nginx/tokenpool.conf` configures:
- Automatic HTTP to HTTPS redirection (Port 80 -> 443)
- Modern TLS cipher suites (TLS 1.2 and TLS 1.3)
- HSTS, X-Content-Type-Options, X-Frame-Options security headers
- Client upload limit up to 10MB (for payment receipt verification screenshots)
- Reverse proxy pass to `http://app:8000`

---

## 11. HTTPS / TLS Configuration

Obtain a free, auto-renewing TLS certificate via Certbot (Let's Encrypt):

```bash
# Install Certbot
sudo apt update && sudo apt install -y certbot python3-certbot-nginx

# Obtain and install certificates
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com

# Verify auto-renewal timer
sudo systemctl status certbot.timer
```

---

## 12. Health Check & Monitoring

### Endpoint: `GET /api/health`
The health check endpoint enables orchestration systems (Kubernetes, AWS ALB, Docker healthcheck) to determine system status.

### Verification Probe:
```bash
curl -i http://localhost:8000/api/health
```

### Expected Response (HTTP 200):
```json
{
  "status": "healthy",
  "database": "healthy",
  "service": "TokenPool Backend",
  "timestamp": "2026-10-08T18:30:00.000000+00:00",
  "demo_mode": false,
  "commission_rate": 0.20
}
```

If the database is unreachable, the endpoint returns:
- HTTP `503 Service Unavailable`
- `"database": "unreachable"`
- `"status": "unhealthy"`

> **Security Guarantee:** The health endpoint does not expose database credentials, API keys, or environment details.

---

## 13. Logging

- **FastAPI / Application Logs:** Structured logs streamed to standard output (`stdout`), formatted with timestamp, log level, and component name.
- **Gunicorn Logs:** Access and error logs output to `stdout`/`stderr`.
- **Systemd Journal:** Query logs with:
  ```bash
  sudo journalctl -u tokenpool -f
  ```
- **Log Masking:** All API keys, credentials, and passwords are automatically stripped or masked in all logs.

---

## 14. Backup Recommendations

1. **Automated Daily Database Dump:**
   ```bash
   pg_dump -Fc -h <DB_HOST> -U tokenpool_prod tokenpool_production > /backup/tokenpool_$(date +\%F).dump
   ```
2. **WAL Archiving / Point-In-Time Recovery (PITR):** Enable WAL archiving on the PostgreSQL host.
3. **Uploads Directory Backup:** Back up `/app/backend/app/uploads` (or Docker volume `tokenpool_uploads_data`) to secure, encrypted object storage (e.g. AWS S3 with KMS).
4. **Offsite Secret Backup:** Store `JWT_SECRET`, `FERNET_KEY`, and database credentials in an enterprise secret vault (AWS Secrets Manager, HashiCorp Vault, or 1Password).

---

## 15. Rollback Procedure

If a deployment encounters critical issues:

### 15.1 Application Rollback
```bash
# Docker: revert to previous image tag
docker compose -f docker-compose.production.yml stop app
docker tag tokenpool:previous tokenpool:production
docker compose -f docker-compose.production.yml up -d app

# Host/Systemd: revert git commit and restart
git checkout <PREVIOUS_STABLE_COMMIT>
sudo systemctl restart tokenpool
```

### 15.2 Database Rollback (Alembic)
If schema downgrade is required:
```bash
alembic downgrade -1
```

### 15.3 Database Restore from Backup
```bash
pg_restore -c -h <DB_HOST> -U tokenpool_prod -d tokenpool_production /backup/tokenpool_backup.dump
```

---

## 16. Troubleshooting

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| `HTTP 503 EXECUTION_NOT_SUPPORTED` | `DEMO_MODE=false` and no eligible real provider connection is available for the model | Provider must connect a valid, authorized API credential in Provider Dashboard. |
| `HTTP 503 (Database unreachable)` | PostgreSQL connection refused or bad credentials | Verify `DATABASE_URL` connectivity, security groups, and pg_hba.conf rules. |
| `CORS Error in Browser` | Request origin does not match `CORS_ORIGINS` | Verify `CORS_ORIGINS` in `.env.production` exactly matches browser origin (including `https://`). |
| `Tesseract OCR Error on Upload` | Tesseract binary not installed on host | Run `sudo apt install tesseract-ocr tesseract-ocr-eng`. |
| `Failed to decrypt credential` | `FERNET_KEY` changed or mismatched | Restore original `FERNET_KEY` used when the credentials were encrypted. |
| `Duplicate Transaction ID Error` | Buyer submitted a reused UPI/bank transaction ID | Enter the genuine, unique transaction ID from the payment receipt. |
