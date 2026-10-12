import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from datetime import datetime, timezone
from sqlalchemy.sql import text

from backend.app.core.config import settings
from backend.app.core.database import engine, Base, SessionLocal
from backend.app.core.seed_data import seed_database
from backend.app.api.endpoints import api_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tokenpool")

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent.parent.parent   # d:/TokenPool
FRONTEND_DIR  = BASE_DIR / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR    = FRONTEND_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing TokenPool database and seeding verified models...")
    Base.metadata.create_all(bind=engine)
    seed_database()
    logger.info("TokenPool backend engine ready.")
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="TokenPool — Production AI Usage & Capacity Marketplace Backend",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────
cors_origins_raw = (settings.CORS_ORIGINS or "").strip()
if cors_origins_raw:
    # Explicitly configured production origins (e.g. https://<PRODUCTION_DOMAIN>)
    allowed_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
elif settings.ENVIRONMENT.lower() == "production":
    # Production safeguards: Wildcards and localhost are strictly prohibited
    allowed_origins = []
else:
    # Development fallback
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files ───────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Jinja2 templates ───────────────────────────────────────────────────────
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def r(name: str, request: Request, **ctx):
    """Shortcut: render template with standard context."""
    return templates.TemplateResponse(name, {"request": request, **ctx})


# ── REST API router ────────────────────────────────────────────────────────
app.include_router(api_router, prefix=settings.API_V1_STR)


# ══════════════════════════════════════════════════════════════════════════
#  PAGE ROUTES  (HTML templates served by Jinja2)
# ══════════════════════════════════════════════════════════════════════════

# ── Public pages ───────────────────────────────────────────────────────────
@app.get("/")
async def page_home(request: Request):
    return r("index.html", request)

@app.get("/login")
async def page_login(request: Request):
    return r("login.html", request)

@app.get("/register")
async def page_register(request: Request):
    return r("register.html", request)

@app.get("/forgot-password")
async def page_forgot(request: Request):
    return r("login.html", request)  # reuse login for now


# ── Consumer pages ─────────────────────────────────────────────────────────
@app.get("/dashboard")
async def page_dashboard(request: Request):
    return r("dashboard.html", request, active_page="dashboard")

@app.get("/chat")
async def page_chat(request: Request):
    return r("chat.html", request, active_page="chat")

@app.get("/marketplace")
async def page_marketplace(request: Request):
    return r("marketplace.html", request, active_page="marketplace")

@app.get("/wallet")
async def page_wallet(request: Request):
    return r("wallet.html", request, active_page="wallet")

@app.get("/transactions")
async def page_transactions(request: Request):
    return r("transactions.html", request, active_page="transactions")

@app.get("/orders")
async def page_orders(request: Request):
    return r("orders.html", request, active_page="orders")

@app.get("/usage")
async def page_usage(request: Request):
    return r("usage.html", request, active_page="usage")

@app.get("/notifications")
async def page_notifications(request: Request):
    return r("notifications.html", request, active_page="notifications")

@app.get("/support")
async def page_support(request: Request):
    return r("support.html", request, active_page="support")

@app.get("/settings")
async def page_settings(request: Request):
    return r("settings.html", request, active_page="settings")

@app.get("/onboarding")
async def page_onboarding(request: Request):
    return RedirectResponse("/dashboard")


# ── Provider pages ─────────────────────────────────────────────────────────
@app.get("/provider/dashboard")
async def page_prov_dashboard(request: Request):
    return r("provider_dashboard.html", request, active_page="provider_dashboard")

@app.get("/provider/connections")
async def page_prov_connections(request: Request):
    return r("provider_connections.html", request, active_page="provider_connections")

@app.get("/provider/listings")
async def page_prov_listings(request: Request):
    return r("provider_listings.html", request, active_page="provider_listings")

@app.get("/provider/earnings")
async def page_prov_earnings(request: Request):
    return r("provider_earnings.html", request, active_page="provider_earnings")


# ── Admin pages ────────────────────────────────────────────────────────────
@app.get("/admin")
async def page_admin(request: Request):
    return r("admin/dashboard.html", request, active_page="admin_overview")

@app.get("/admin/disputes")
async def page_admin_disputes(request: Request):
    return r("admin/disputes.html", request, active_page="admin_disputes")

@app.get("/admin/orders")
async def page_admin_orders(request: Request):
    return r("admin/orders.html", request, active_page="admin_orders")

@app.get("/admin/users")
async def page_admin_users(request: Request):
    return r("admin/users.html", request, active_page="admin_users")

@app.get("/admin/providers")
async def page_admin_providers(request: Request):
    return r("admin/providers.html", request, active_page="admin_providers")

@app.get("/admin/listings")
async def page_admin_listings(request: Request):
    return r("admin/listings.html", request, active_page="admin_listings")

@app.get("/admin/transactions")
async def page_admin_transactions(request: Request):
    return r("admin/transactions.html", request, active_page="admin_transactions")

@app.get("/admin/analytics")
async def page_admin_analytics(request: Request):
    return r("admin/analytics.html", request, active_page="admin_analytics")

@app.get("/admin/settings")
async def page_admin_settings(request: Request):
    return r("admin/settings.html", request, active_page="admin_settings")


# ── Health check ───────────────────────────────────────────────────────────
@app.get("/api/health")
def health_check():
    db_status = "healthy"
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error(f"Health check database ping failed: {e}")
        db_status = "unreachable"

    overall_status = "healthy" if db_status == "healthy" else "unhealthy"
    status_code = 200 if db_status == "healthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall_status,
            "database": db_status,
            "service": "TokenPool Backend",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "demo_mode": settings.DEMO_MODE_DEFAULT,
            "commission_rate": settings.DEFAULT_COMMISSION_RATE,
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": {"code": "INTERNAL_SERVER_ERROR", "message": "An internal error occurred."}},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, reload=True)
