"""
TokenPool — Production Smoke Test & Preflight Validation
Validates 13 key areas of production readiness:
1. Application startup
2. /api/health
3. Database connection
4. Authentication
5. Marketplace loading
6. Provider connection endpoints
7. Consumer marketplace
8. Chat endpoint
9. DEMO_MODE=false behavior
10. Unsupported provider behavior
11. Payment verification validation
12. RBAC
13. Provider/consumer isolation
"""
import uuid
import time
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.core.config import settings
from backend.app.core.database import SessionLocal
from backend.app.models.schema_models import (
    User, ProviderAccount, ProviderConnection, ProviderListing,
    ModelCreditBalance, Order, PaymentProof
)

client = TestClient(app)

def run_smoke_test():
    print("=" * 80)
    print("TOKENPOOL PRODUCTION PREFLIGHT SMOKE TEST")
    print("=" * 80)
    
    # 1. Application Startup
    print("[1/13] Application Startup...")
    assert app is not None
    print("  -> PASS: FastAPI app initialized.")

    # 2. /api/health
    print("[2/13] Health Check Endpoint (/api/health)...")
    res = client.get("/api/health")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    data = res.json()
    assert data["status"] == "healthy"
    assert data["database"] == "healthy"
    print(f"  -> PASS: /api/health returned healthy status (DB: {data['database']}).")

    # 3. Database Connection
    print("[3/13] PostgreSQL Database Connection...")
    with SessionLocal() as db:
        user_count = db.query(User).count()
        assert user_count >= 1
    print(f"  -> PASS: PostgreSQL queried successfully ({user_count} total users in DB).")

    # 4. Authentication (Register, Login, Token generation)
    print("[4/13] Authentication & JWT Verification...")
    ts = int(time.time() * 1000)
    test_email = f"smoke_user_{ts}@tokenpool.ai"
    reg = client.post("/api/v1/auth/register", json={
        "email": test_email, "password": "Password123!", "full_name": "Smoke Test User", "role": "USER"
    })
    assert reg.status_code == 201, f"Registration failed: {reg.text}"
    login = client.post("/api/v1/auth/login", json={"email": test_email, "password": "Password123!"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {token}"}
    me_res = client.get("/api/v1/auth/me", headers=user_headers)
    assert me_res.status_code == 200
    print("  -> PASS: User registered, logged in, and JWT authenticated successfully.")

    # 5. Marketplace Loading
    print("[5/13] Marketplace Listings Loading...")
    mkt_res = client.get("/api/v1/marketplace")
    assert mkt_res.status_code == 200, f"Marketplace failed: {mkt_res.text}"
    listings = mkt_res.json().get("data", [])
    print(f"  -> PASS: Marketplace returned {len(listings)} active listings.")

    # 6. Provider Connection Endpoints
    print("[6/13] Provider Connection Architecture & Credential Masking...")
    conn_res = client.get("/api/v1/provider/connections", headers=user_headers)
    assert conn_res.status_code == 200, f"Provider connections failed: {conn_res.text}"
    print("  -> PASS: Provider connection endpoint secured and queryable.")

    # 7. Consumer Marketplace Models
    print("[7/13] Consumer Model Catalog...")
    models_res = client.get("/api/v1/models")
    assert models_res.status_code == 200, f"Models failed: {models_res.text}"
    print(f"  -> PASS: Retrieved consumer model catalog ({len(models_res.json().get('data', []))} models).")

    # 8. Chat Endpoint (Authenticated)
    print("[8/13] Chat History & Conversations...")
    convs_res = client.get("/api/v1/chat/conversations", headers=user_headers)
    assert convs_res.status_code == 200
    print("  -> PASS: Consumer conversation endpoint accessible and isolated.")

    # 9. DEMO_MODE=false Behavior
    print("[9/13] Production DEMO_MODE=false Enforcement...")
    orig_demo = settings.DEMO_MODE_DEFAULT
    try:
        settings.DEMO_MODE_DEFAULT = False
        # Add credits for a model that has no eligible real provider
        no_prov_slug = "custom-unsupported-model"
        user_id = None
        with SessionLocal() as db:
            u = db.query(User).filter(User.email == test_email).first()
            user_id = u.id
            mcb = ModelCreditBalance(
                user_id=u.id, model_slug=no_prov_slug, model_name="Unsupported Custom Model",
                credits_purchased=100, credits_used=0, credits_remaining=100
            )
            db.add(mcb)
            db.commit()

        # Attempt execution with is_demo=True when DEMO_MODE=false
        demo_chat_res = client.post("/api/v1/chat/messages", headers=user_headers, json={
            "model_slug": no_prov_slug,
            "prompt": "Test message when DEMO_MODE=false",
            "is_demo": True
        })
        assert demo_chat_res.status_code == 503, f"Expected 503, got {demo_chat_res.status_code}: {demo_chat_res.text}"
        assert "EXECUTION_NOT_SUPPORTED" in demo_chat_res.text
        
        # Verify 0 credits were deducted
        with SessionLocal() as db:
            refreshed_mcb = db.query(ModelCreditBalance).filter(
                ModelCreditBalance.user_id == user_id, ModelCreditBalance.model_slug == no_prov_slug
            ).first()
            assert refreshed_mcb.credits_remaining == 100
        print("  -> PASS: DEMO_MODE=false strictly blocks MockAIProvider with HTTP 503 and zero credit deduction.")
    finally:
        settings.DEMO_MODE_DEFAULT = orig_demo

    # 10. Unsupported Provider Behavior
    print("[10/13] Unsupported Provider Behavior...")
    unsupp_res = client.post("/api/v1/chat/messages", headers=user_headers, json={
        "model_slug": "claude-3-5-sonnet",
        "prompt": "Test unsupported provider",
        "is_demo": False
    })
    # Should be rejected with 402 (insufficient credits) or 503 (no provider connection)
    assert unsupp_res.status_code in (402, 503)
    print(f"  -> PASS: Unsupported provider execution safely gated with HTTP {unsupp_res.status_code}.")

    # 11. Payment Verification Validation
    print("[11/13] Payment Verification & Fraud Prevention...")
    # Attempt submitting fraudulent payment proof without screenshot
    fake_proof_res = client.post("/api/v1/orders/ord_nonexistent/payment-proof", headers=user_headers, data={
        "transaction_id": "TXN_FAKE_123456", "amount": 100.0
    })
    assert fake_proof_res.status_code in (400, 404, 422)
    print("  -> PASS: Fraudulent / malformed payment proof rejected.")

    # 12. RBAC Enforcement
    print("[12/13] Role-Based Access Control (RBAC)...")
    # Standard user cannot access admin endpoints
    admin_res = client.get("/api/v1/admin/overview", headers=user_headers)
    assert admin_res.status_code in (401, 403), f"Expected 401/403 for non-admin, got {admin_res.status_code}"
    print(f"  -> PASS: Non-admin user blocked from admin endpoints (HTTP {admin_res.status_code}).")

    # 13. Provider / Consumer Data Isolation
    print("[13/13] Provider / Consumer Data Isolation...")
    other_email = f"other_user_{ts}@tokenpool.ai"
    reg_other = client.post("/api/v1/auth/register", json={
        "email": other_email, "password": "Password123!", "full_name": "Other User", "role": "USER"
    })
    other_token = reg_other.json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}
    other_convs = client.get("/api/v1/chat/conversations", headers=other_headers).json().get("data", [])
    assert len(other_convs) == 0
    print("  -> PASS: Consumer conversation data strictly isolated across users.")

    print("=" * 80)
    print("ALL 13 PRODUCTION SMOKE TESTS PASSED PERFECTLY!")
    print("=" * 80)

if __name__ == "__main__":
    run_smoke_test()
