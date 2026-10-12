from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from backend.app.core.database import SessionLocal, Base, engine
from backend.app.core.security import get_password_hash
from backend.app.core.config import settings
from backend.app.services.router import AIRouterService
from backend.app.models.schema_models import (
    User, Wallet, ProviderAccount,
    AIModel, PlatformSetting, AICapacityPool, ProviderCapacityAllocation
)

def utcnow():
    return datetime.now(timezone.utc)

def seed_capacity_pools_and_allocations(db: Session):
    """
    Ensures TokenPool AI Capacity Pools exist for each model.
    Pool status is determined by REAL execution infrastructure:
    - VERIFIED: Real API key is configured for this adapter type
    - DEMO: Mock adapter (sandbox/demo mode only — clearly labeled)
    - AUTHORIZATION_REQUIRED: Real adapter but no API key configured

    IMPORTANT: This function does NOT auto-allocate capacity to providers.
    Provider allocations must be created explicitly by admin via the
    /admin/provider-allocations API endpoint.
    """
    now = utcnow()
    one_year_later = now + timedelta(days=365)

    # 1. Ensure all AI Models exist in catalog
    catalog = [
        # Real Google Gemini Models
        {
            "slug": "gemini-1.5-flash",
            "name": "Gemini 1.5 Flash",
            "provider_brand": "Google Gemini",
            "badge": "G",
            "badge_color": "#4285f4",
            "description": "High-frequency multimodal model with 1M context",
            "capabilities": "Multimodal, high speed, reasoning",
            "input_price_per_1k": 0.06,
            "output_price_per_1k": 0.12,
            "avg_latency_ms": 320,
            "latency_label": "320ms avg",
            "status": "AVAILABLE",
            "context_window": "1000k",
            "price_per_credit": 0.40,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "gemini",
        },
        {
            "slug": "gemini-1.5-pro",
            "name": "Gemini 1.5 Pro",
            "provider_brand": "Google Gemini",
            "badge": "G",
            "badge_color": "#1a73e8",
            "description": "Complex reasoning, coding, and 2M token context",
            "capabilities": "Deep reasoning, long context",
            "input_price_per_1k": 0.15,
            "output_price_per_1k": 0.35,
            "avg_latency_ms": 950,
            "latency_label": "950ms avg",
            "status": "AVAILABLE",
            "context_window": "2000k",
            "price_per_credit": 0.80,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "gemini",
        },
        # Real OpenAI Models
        {
            "slug": "gpt-4o",
            "name": "GPT-4o",
            "provider_brand": "OpenAI",
            "badge": "O",
            "badge_color": "#10a37f",
            "description": "Flagship omni model for complex reasoning and tools",
            "capabilities": "Reasoning, vision, tools",
            "input_price_per_1k": 0.20,
            "output_price_per_1k": 0.45,
            "avg_latency_ms": 780,
            "latency_label": "780ms avg",
            "status": "AVAILABLE",
            "context_window": "128k",
            "price_per_credit": 1.00,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "openai",
        },
        {
            "slug": "gpt-4o-mini",
            "name": "GPT-4o Mini",
            "provider_brand": "OpenAI",
            "badge": "O",
            "badge_color": "#10a37f",
            "description": "Affordable intelligence for fast reasoning tasks",
            "capabilities": "Fast chat, tools",
            "input_price_per_1k": 0.05,
            "output_price_per_1k": 0.10,
            "avg_latency_ms": 360,
            "latency_label": "360ms avg",
            "status": "AVAILABLE",
            "context_window": "128k",
            "price_per_credit": 0.35,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "openai",
        },
        # Real Anthropic Models
        {
            "slug": "claude-3-5-haiku-20241022",
            "name": "Claude 3.5 Haiku",
            "provider_brand": "Anthropic",
            "badge": "C",
            "badge_color": "#d97706",
            "description": "Ultra-fast response generation with high accuracy",
            "capabilities": "High speed, coding",
            "input_price_per_1k": 0.08,
            "output_price_per_1k": 0.18,
            "avg_latency_ms": 410,
            "latency_label": "410ms avg",
            "status": "AVAILABLE",
            "context_window": "200k",
            "price_per_credit": 0.45,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "anthropic",
        },
        # Demo & Sandbox Models (MOCK adapter — clearly labeled as DEMO)
        {
            "slug": "nova-reasoning",
            "name": "Nova Reasoning [DEMO]",
            "provider_brand": "Orbit AI (Demo)",
            "badge": "N",
            "badge_color": "#10b981",
            "description": "DEMO: Reasoning, tools, multi-step planning (simulated)",
            "capabilities": "Reasoning, tools (Demo)",
            "input_price_per_1k": 0.08,
            "output_price_per_1k": 0.15,
            "avg_latency_ms": 1200,
            "latency_label": "1.2s avg",
            "status": "AVAILABLE",
            "context_window": "128k",
            "price_per_credit": 0.50,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "mock",
        },
        {
            "slug": "swift-chat",
            "name": "Swift Chat [DEMO]",
            "provider_brand": "Nexus Cloud (Demo)",
            "badge": "S",
            "badge_color": "#3b82f6",
            "description": "DEMO: Fast chat, summarization, general queries (simulated)",
            "capabilities": "Chat, summarization (Demo)",
            "input_price_per_1k": 0.04,
            "output_price_per_1k": 0.09,
            "avg_latency_ms": 420,
            "latency_label": "420ms avg",
            "status": "AVAILABLE",
            "context_window": "64k",
            "price_per_credit": 0.30,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "mock",
        },
        {
            "slug": "atlas-code",
            "name": "Atlas Code [DEMO]",
            "provider_brand": "Arc Compute (Demo)",
            "badge": "A",
            "badge_color": "#f59e0b",
            "description": "DEMO: Code generation, debugging, refactoring (simulated)",
            "capabilities": "Code, analysis (Demo)",
            "input_price_per_1k": 0.12,
            "output_price_per_1k": 0.22,
            "avg_latency_ms": 860,
            "latency_label": "860ms avg",
            "status": "LIMITED",
            "context_window": "128k",
            "price_per_credit": 0.75,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "mock",
        },
        {
            "slug": "vision-pro",
            "name": "Vision Pro [DEMO]",
            "provider_brand": "Vector Labs (Demo)",
            "badge": "V",
            "badge_color": "#0d9488",
            "description": "DEMO: Visual comprehension, OCR, diagrams (simulated)",
            "capabilities": "Vision, documents (Demo)",
            "input_price_per_1k": 0.15,
            "output_price_per_1k": 0.30,
            "avg_latency_ms": 1600,
            "latency_label": "1.6s avg",
            "status": "AVAILABLE",
            "context_window": "128k",
            "price_per_credit": 0.90,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "mock",
        },
        {
            "slug": "embed-mini",
            "name": "Embed Mini [DEMO]",
            "provider_brand": "Nexus Cloud (Demo)",
            "badge": "E",
            "badge_color": "#6366f1",
            "description": "DEMO: Dense text embeddings for semantic search (simulated)",
            "capabilities": "Embeddings (Demo)",
            "input_price_per_1k": 0.02,
            "output_price_per_1k": 0.02,
            "avg_latency_ms": 180,
            "latency_label": "180ms avg",
            "status": "AVAILABLE",
            "context_window": "32k",
            "price_per_credit": 0.15,
            "capacity_unit": "TOKEN",
            "supported_periods": "DAILY,MONTHLY,YEARLY",
            "execution_adapter": "mock",
        },
    ]

    for item in catalog:
        m = db.query(AIModel).filter(AIModel.slug == item["slug"]).first()
        if not m:
            m = AIModel(**item)
            db.add(m)
            db.flush()
        else:
            # Update attributes to ensure capacity_unit and adapter are set
            m.capacity_unit = item["capacity_unit"]
            m.supported_periods = item["supported_periods"]
            m.execution_adapter = item["execution_adapter"]
            m.price_per_credit = item["price_per_credit"]
            m.provider_brand = item["provider_brand"]
            m.name = item["name"]
            m.description = item["description"]
            db.flush()

    # 2. Ensure Capacity Pools exist for each model
    # Pool status reflects REAL execution infrastructure — not arbitrary numbers
    active_models = db.query(AIModel).filter(AIModel.is_active == True).all()
    for model in active_models:
        pool = db.query(AICapacityPool).filter(AICapacityPool.model_id == model.id).first()
        exec_adapter = model.execution_adapter or "mock"
        status = AIRouterService.get_provider_status(exec_adapter)

        # For mock adapters, mark as DEMO (not VERIFIED — they are not real capacity)
        if exec_adapter == "mock":
            status = "DEMO"

        if not pool:
            total_cap = 10_000_000
            pool = AICapacityPool(
                model_id=model.id,
                model_slug=model.slug,
                provider_brand=model.provider_brand,
                execution_adapter=exec_adapter,
                capacity_unit=model.capacity_unit or "TOKEN",
                period_type="MONTHLY",
                total_capacity=total_cap,
                allocated_capacity=0,
                reserved_capacity=0,
                sold_capacity=0,
                consumed_capacity=0,
                remaining_capacity=total_cap,
                period_start=now,
                period_end=one_year_later,
                status=status,
            )
            db.add(pool)
            db.flush()
        else:
            # Update status in accordance with real execution infrastructure
            pool.status = status
            pool.execution_adapter = exec_adapter
            db.flush()

    # NOTE: Provider capacity allocations are NOT auto-created here.
    # Admin must explicitly allocate capacity to providers via:
    #   POST /api/v1/admin/provider-allocations

    db.commit()


def seed_database():
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        # Check if platform settings and users exist
        if not db.query(User).filter(User.email == "alex@company.com").first():
            print("Seeding database with TokenPool system configuration and users...")

            # 1. Platform Settings
            db.add(PlatformSetting(key="commission_rate", value="0.20", description="Platform fee fraction (20%)"))
            db.add(PlatformSetting(key="min_top_up", value="100", description="Minimum wallet top up amount in INR"))
            db.add(PlatformSetting(key="max_payout_daily", value="50000", description="Max daily payout per provider"))
            db.add(PlatformSetting(key="rate_limit_rpm", value="60", description="Rate limit requests per minute per user"))

            # 2. Users
            alex = User(
                email="alex@company.com",
                full_name="Alex Kumar",
                hashed_password=get_password_hash("password123"),
                role="BOTH",
                status="ACTIVE",
                avatar_initials="AK",
                organization="Acme AI Labs",
                bio="AI engineer and marketplace user exploring reasoning models."
            )
            db.add(alex)
            db.flush()

            admin = User(
                email="admin@tokenpool.ai",
                full_name="TokenPool Admin",
                hashed_password=get_password_hash("admin123"),
                role="ADMIN",
                status="ACTIVE",
                avatar_initials="A",
                organization="TokenPool Core",
                bio="Platform administrator"
            )
            db.add(admin)

            mira = User(
                email="mira@studio.io",
                full_name="Mira Shah",
                hashed_password=get_password_hash("password123"),
                role="PROVIDER",
                status="ACTIVE",
                avatar_initials="MS",
                organization="Studio Compute",
                bio="GPU cluster provider"
            )
            db.add(mira)

            jordan = User(
                email="jordan@labs.ai",
                full_name="Jordan Lee",
                hashed_password=get_password_hash("password123"),
                role="BOTH",
                status="SUSPENDED",
                avatar_initials="JL",
                organization="DeepLabs"
            )
            db.add(jordan)

            sam = User(
                email="sam@work.dev",
                full_name="Sam Rivera",
                hashed_password=get_password_hash("password123"),
                role="CONSUMER",
                status="ACTIVE",
                avatar_initials="SR"
            )
            db.add(sam)
            db.flush()

            # 3. Wallets
            alex_wallet = Wallet(user_id=alex.id, balance=0.00, credits=0, currency="INR")
            admin_wallet = Wallet(user_id=admin.id, balance=0.00, credits=0, currency="INR")
            mira_wallet = Wallet(user_id=mira.id, balance=0.00, credits=0, currency="INR")
            sam_wallet = Wallet(user_id=sam.id, balance=0.00, credits=0, currency="INR")
            jordan_wallet = Wallet(user_id=jordan.id, balance=0.00, credits=0, currency="INR")
            db.add_all([alex_wallet, admin_wallet, mira_wallet, sam_wallet, jordan_wallet])

            # 4. Provider Account for Alex
            alex_provider = ProviderAccount(
                user_id=alex.id,
                workspace_name="Alex's provider",
                total_capacity=0,
                available_capacity=0,
                used_capacity=0,
                reserved_capacity=0,
                total_earnings=0.00,
                pending_earnings=0.00,
                withdrawable_balance=0.00,
                status="ACTIVE"
            )
            db.add(alex_provider)
            db.flush()

            # Provider Account for Mira
            mira_provider = ProviderAccount(
                user_id=mira.id,
                workspace_name="Mira Studio Compute",
                total_capacity=0,
                available_capacity=0,
                used_capacity=0,
                reserved_capacity=0,
                total_earnings=0.00,
                pending_earnings=0.00,
                withdrawable_balance=0.00,
                status="ACTIVE"
            )
            db.add(mira_provider)
            db.flush()

            db.commit()

        # Always run capacity pools and allocations seeding
        seed_capacity_pools_and_allocations(db)
        print("Database seed completed successfully (TokenPool Capacity Pools & Allocations verified).")
    except Exception as e:
        db.rollback()
        print("Error seeding database:", e)
        raise
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
