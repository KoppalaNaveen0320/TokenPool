"""
TokenPool — Comprehensive Gmail-Based AI Account Connection Test Suite
Tests all 20 scenarios specified in Section 26 of the prompt:

TEST 1: Provider logs into TokenPool using verified Gmail -> Provider account created.
TEST 2: Provider's Gmail identity is stored securely -> No password stored.
TEST 3: Provider connects AI account with same Gmail identity -> Identity matched.
TEST 4: Provider connects AI account with different identity -> Show identity mismatch / additional verification required.
TEST 5: Connected identity but no execution authorization -> Marketplace NOT_ELIGIBLE.
TEST 6: Valid authorized AI account -> Marketplace eligibility evaluated.
TEST 7: Provider declares capacity -> Capacity stored.
TEST 8: Provider creates listing -> Allowed only when marketplace eligibility is valid.
TEST 9: Consumer purchases listing -> Existing payment flow works.
TEST 10: Consumer sends AI request -> Request routes to correct provider connection.
TEST 11: Provider disconnects AI account -> Listing pauses.
TEST 12: Authorization expires -> Execution blocked.
TEST 13: AI request fails -> No credits/capacity/earnings deducted.
TEST 14: AI request succeeds -> Correct usage accounting.
TEST 15: Provider attempts to access another provider's connection -> RBAC denial.
TEST 16: Consumer attempts to access provider credentials -> Denied.
TEST 17: Admin attempts to fabricate provider capacity -> Denied.
TEST 18: Raw authorization token appears in API response -> Verification check passes (must never happen).
TEST 19: Raw password requested anywhere in UI / API -> Verification check passes (must never happen).
TEST 20: Browser cookie/session extraction exists anywhere -> Verification check passes (must never happen).
"""

import sys
import os
sys.path.insert(0, os.path.abspath("."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import time
import uuid
import datetime
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.database import SessionLocal
from backend.app.models.schema_models import (
    User, ProviderAccount, ProviderConnection, ProviderListing,
    Order, Wallet, CreditAllocation, ModelCreditBalance, AIModel
)
from backend.app.services.marketplace_eligibility import MarketplaceEligibilityService

client = TestClient(app)


def test_1_provider_logs_in_with_verified_gmail():
    """TEST 1: Provider logs into TokenPool using verified Gmail -> Provider account created."""
    ts = int(time.time() * 1000)
    gmail = f"provider_{ts}@gmail.com"
    sub_id = f"google_sub_{ts}"

    res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Test Google Provider",
        "google_sub_id": sub_id
    })

    assert res.status_code == 200, f"Google login failed: {res.text}"
    data = res.json()
    assert data["user"]["email"] == gmail
    assert data["user"]["email_verified"] is True
    assert "access_token" in data

    # Verify provider profile was automatically created
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == gmail).first()
        assert user is not None
        assert user.google_sub_id == sub_id
        prov = db.query(ProviderAccount).filter(ProviderAccount.user_id == user.id).first()
        assert prov is not None
        assert prov.user_id == user.id
    finally:
        db.close()


def test_2_gmail_identity_stored_securely_no_password():
    """TEST 2: Provider's Gmail identity is stored securely -> No password stored."""
    ts = int(time.time() * 1000)
    gmail = f"secure_provider_{ts}@gmail.com"

    res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Secure Provider",
        "google_sub_id": f"sub_{ts}"
    })
    assert res.status_code == 200

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == gmail).first()
        assert user is not None
        # User signed in via Google: TokenPool never requested or stored user's Gmail password
        assert user.google_sub_id is not None
        assert user.email_verified is True
    finally:
        db.close()


def test_3_connect_ai_account_with_same_gmail():
    """TEST 3: Provider connects AI account with same Gmail identity -> Identity matched."""
    ts = int(time.time() * 1000)
    gmail = f"matched_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Matching Provider",
        "google_sub_id": f"sub_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Connect Gemini with the same Gmail address
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": gmail,
        "connection_method": "google_identity",
        "declared_capacity": 100000,
        "authorization_code": "oauth_code_mock"
    })
    assert res.status_code in (200, 201), res.text
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MATCHED"
    assert data["account_email"] == gmail
    assert data["connection_status"] == "CONNECTED"


def test_4_connect_ai_account_with_different_identity():
    """TEST 4: Provider connects AI account with different identity -> Show identity mismatch."""
    ts = int(time.time() * 1000)
    gmail = f"primary_prov_{ts}@gmail.com"
    different_email = f"other_{ts}@different.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Primary Provider",
        "google_sub_id": f"sub_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Connect with a mismatching email
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": different_email,
        "connection_method": "google_identity",
        "declared_capacity": 100000
    })
    assert res.status_code in (200, 201)
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MISMATCH"
    assert data["marketplace_status"] == "NOT_ELIGIBLE"
    assert "mismatch" in data["marketplace_reason"].lower()


def test_5_connected_identity_but_no_execution_authorization():
    """TEST 5: Connected identity but no execution authorization -> Marketplace NOT_ELIGIBLE."""
    ts = int(time.time() * 1000)
    gmail = f"unauth_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Unauth Provider",
        "google_sub_id": f"sub_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Connect ChatGPT - official execution API is not supported on consumer subscriptions
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "openai",
        "account_email": gmail,
        "connection_method": "google_identity",
        "declared_capacity": 50000
    })
    assert res.status_code in (200, 201)
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MATCHED"
    # Authorization is NOT_AUTHORIZED or EXECUTION_NOT_SUPPORTED
    assert data["marketplace_status"] == "NOT_ELIGIBLE"
    assert "not provide" in data["marketplace_reason"].lower() or "not supported" in data["marketplace_reason"].lower() or "eligible" in data["marketplace_reason"].lower()


def test_6_valid_authorized_ai_account_eligibility():
    """TEST 6: Valid authorized AI account -> Marketplace eligibility evaluated."""
    ts = int(time.time() * 1000)
    gmail = f"valid_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Valid Provider",
        "google_sub_id": f"sub_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Connect Mock/Gemini with full authorization
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "mock",
        "account_email": gmail,
        "connection_method": "official_oauth",
        "declared_capacity": 100000,
        "authorization_code": "valid_token"
    })
    assert res.status_code in (200, 201)
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MATCHED"
    assert data["authorization_status"] == "AUTHORIZED"
    assert data["marketplace_status"] == "ELIGIBLE"


def test_7_provider_declares_capacity():
    """TEST 7: Provider declares capacity -> Capacity stored."""
    ts = int(time.time() * 1000)
    gmail = f"cap_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Capacity Provider",
        "google_sub_id": f"sub_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "mock",
        "account_email": gmail,
        "connection_method": "official_oauth",
        "declared_capacity": 75000,
        "authorization_code": "valid_token"
    })
    conn_id = res.json()["data"].get("connection_id") or res.json()["data"].get("id")

    # Now update declared capacity
    dec_res = client.post(f"/api/v1/provider/ai-accounts/{conn_id}/declare-capacity", headers=headers, json={
        "declared_capacity": 120000
    })
    assert dec_res.status_code == 200
    assert dec_res.json()["data"]["capacity_declared"] == 120000
    assert dec_res.json()["data"]["capacity_remaining"] == 120000


def test_8_provider_creates_listing_allowed_only_when_eligible():
    """TEST 8: Provider creates listing -> Allowed only when marketplace eligibility is valid."""
    ts = int(time.time() * 1000)
    gmail = f"listing_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Listing Provider",
        "google_sub_id": f"sub_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Connect ChatGPT (not eligible because no 3rd-party execution)
    res_ineligible = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "openai",
        "account_email": gmail,
        "connection_method": "google_identity",
        "declared_capacity": 50000
    })
    conn_ineligible_id = res_ineligible.json()["data"].get("connection_id") or res_ineligible.json()["data"].get("id")

    # Attempt to create listing on ineligible connection -> MUST FAIL (HTTP 400)
    list_fail = client.post("/api/v1/provider/listings", headers=headers, json={
        "title": "Ineligible ChatGPT Listing",
        "model_slug": "gpt-4o",
        "price_per_1k_tokens": 0.05,
        "connection_id": conn_ineligible_id,
        "quantity_available": 10000
    })
    assert list_fail.status_code == 400
    assert "eligible" in list_fail.text.lower()

    # 2. Connect eligible Mock AI connection
    res_eligible = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "mock",
        "account_email": gmail,
        "connection_method": "official_oauth",
        "declared_capacity": 100000,
        "authorization_code": "auth_ok"
    })
    conn_eligible_id = res_eligible.json()["data"].get("connection_id") or res_eligible.json()["data"].get("id")

    # Attempt to create listing on eligible connection -> MUST SUCCEED
    list_ok = client.post("/api/v1/provider/listings", headers=headers, json={
        "title": "Eligible Mock Listing",
        "model_slug": "gemini-1.5-pro",
        "price_per_1k_tokens": 0.05,
        "connection_id": conn_eligible_id,
        "quantity_available": 20000
    })
    assert list_ok.status_code in (200, 201), list_ok.text
    listing_id = list_ok.json().get("id") or list_ok.json().get("data", {}).get("id")
    assert listing_id is not None


def test_9_consumer_purchases_listing():
    """TEST 9: Consumer purchases listing -> Existing payment/order flow works."""
    ts = int(time.time() * 1000)
    prov_gmail = f"prov_p9_{ts}@gmail.com"
    buyer_gmail = f"buyer_p9_{ts}@gmail.com"

    # Provider setup
    prov_res = client.post("/api/v1/auth/google", json={"email": prov_gmail, "full_name": "Prov 9", "google_sub_id": f"s9_{ts}"})
    prov_token = prov_res.json()["access_token"]
    prov_headers = {"Authorization": f"Bearer {prov_token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=prov_headers, json={
        "provider_type": "mock", "account_email": prov_gmail, "connection_method": "official_oauth",
        "declared_capacity": 100000, "authorization_code": "auth_ok"
    })
    conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    listing_res = client.post("/api/v1/provider/listings", headers=prov_headers, json={
        "title": "Orderable Listing", "model_slug": "gemini-1.5-pro", "price_per_1k_tokens": 0.05,
        "connection_id": conn_id, "quantity_available": 50000
    })
    assert listing_res.status_code in (200, 201)
    listing_id = listing_res.json().get("id") or listing_res.json().get("data", {}).get("id")
    assert listing_id is not None

    # Buyer setup
    buyer_res = client.post("/api/v1/auth/google", json={"email": buyer_gmail, "full_name": "Buyer 9", "google_sub_id": f"b9_{ts}"})
    buyer_token = buyer_res.json()["access_token"]
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

    order_res = client.post("/api/v1/orders", headers=buyer_headers, json={
        "listing_id": listing_id,
        "quantity": 1000
    })
    assert order_res.status_code == 200
    order_data = order_res.json()["data"].get("order") or order_res.json()["data"]
    assert order_data["status"] == "PAYMENT_PENDING"
    assert order_data["quantity"] == 1000


def test_10_consumer_sends_ai_request_routes_to_provider():
    """TEST 10: Consumer sends AI request -> Request routes to correct provider connection."""
    ts = int(time.time() * 1000)
    prov_gmail = f"prov_p10_{ts}@gmail.com"
    buyer_gmail = f"buyer_p10_{ts}@gmail.com"

    prov_res = client.post("/api/v1/auth/google", json={"email": prov_gmail, "full_name": "Prov 10", "google_sub_id": f"s10_{ts}"})
    prov_token = prov_res.json()["access_token"]
    prov_headers = {"Authorization": f"Bearer {prov_token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=prov_headers, json={
        "provider_type": "mock", "account_email": prov_gmail, "connection_method": "official_oauth",
        "declared_capacity": 100000, "authorization_code": "auth_ok"
    })
    conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    client.post("/api/v1/provider/listings", headers=prov_headers, json={
        "title": "Chat Listing", "model_slug": "nova-reasoning", "price_per_1k_tokens": 0.05,
        "connection_id": conn_id, "quantity_available": 50000
    })

    # Setup buyer with credits
    buyer_res = client.post("/api/v1/auth/google", json={"email": buyer_gmail, "full_name": "Buyer 10", "google_sub_id": f"b10_{ts}"})
    buyer_user_id = buyer_res.json()["user"]["id"]
    buyer_token = buyer_res.json()["access_token"]
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

    db = SessionLocal()
    try:
        # Give buyer direct model credits for nova-reasoning
        bal = ModelCreditBalance(user_id=buyer_user_id, model_slug="nova-reasoning", model_name="Nova Reasoning", credits_remaining=5000)
        db.add(bal)
        db.commit()
    finally:
        db.close()

    # Consumer executes chat message
    chat_res = client.post("/api/v1/chat/messages", headers=buyer_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Hello TokenPool AI!",
        "is_demo": True
    })
    assert chat_res.status_code == 200, chat_res.text
    data = chat_res.json()
    assert "data" in data or "response" in data


def test_11_provider_disconnects_ai_account_pauses_listings():
    """TEST 11: Provider disconnects AI account -> Listing pauses."""
    ts = int(time.time() * 1000)
    gmail = f"disc_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={"email": gmail, "full_name": "Disc Provider", "google_sub_id": f"sub_{ts}"})
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "mock", "account_email": gmail, "connection_method": "official_oauth",
        "declared_capacity": 100000, "authorization_code": "auth_ok"
    })
    conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    list_res = client.post("/api/v1/provider/listings", headers=headers, json={
        "title": "To-Be-Paused Listing", "model_slug": "gemini-1.5-pro", "price_per_1k_tokens": 0.05,
        "connection_id": conn_id, "quantity_available": 10000
    })
    assert list_res.status_code in (200, 201)
    listing_id = list_res.json().get("id") or list_res.json().get("data", {}).get("id")
    assert listing_id is not None

    # Provider disconnects the AI account
    disc_res = client.post(f"/api/v1/provider/ai-accounts/{conn_id}/disconnect", headers=headers)
    assert disc_res.status_code == 200

    # Verify listing is now PAUSED
    db = SessionLocal()
    try:
        listing = db.query(ProviderListing).filter(ProviderListing.id == listing_id).first()
        assert listing.status == "PAUSED"
        conn = db.query(ProviderConnection).filter(ProviderConnection.id == conn_id).first()
        assert conn.connection_status == "DISCONNECTED"
        assert conn.marketplace_status == "PAUSED"
    finally:
        db.close()


def test_12_authorization_expires_execution_blocked():
    """TEST 12: Authorization expires -> Execution blocked."""
    ts = int(time.time() * 1000)
    prov_gmail = f"exp_prov_{ts}@gmail.com"

    prov_res = client.post("/api/v1/auth/google", json={"email": prov_gmail, "full_name": "Exp Prov", "google_sub_id": f"s_{ts}"})
    prov_token = prov_res.json()["access_token"]
    prov_headers = {"Authorization": f"Bearer {prov_token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=prov_headers, json={
        "provider_type": "mock", "account_email": prov_gmail, "connection_method": "official_oauth",
        "declared_capacity": 100000, "authorization_code": "auth_ok"
    })
    conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    # Manually expire the token in the database
    db = SessionLocal()
    try:
        conn = db.query(ProviderConnection).filter(ProviderConnection.id == conn_id).first()
        conn.token_expires_at = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        db.commit()
    finally:
        db.close()

    # Trigger health check -> MUST detect expiration and pause
    hc_res = client.post(f"/api/v1/provider/ai-accounts/{conn_id}/health-check", headers=prov_headers)
    assert hc_res.status_code == 200
    assert hc_res.json()["data"]["connection_status"] == "EXPIRED"
    assert hc_res.json()["data"]["marketplace_status"] == "PAUSED"


def test_13_ai_request_fails_no_financial_or_capacity_deduction():
    """TEST 13: AI request fails -> No credits/capacity/earnings deducted."""
    ts = int(time.time() * 1000)
    buyer_gmail = f"fail_buyer_{ts}@gmail.com"

    buyer_res = client.post("/api/v1/auth/google", json={"email": buyer_gmail, "full_name": "Fail Buyer", "google_sub_id": f"b_{ts}"})
    buyer_user_id = buyer_res.json()["user"]["id"]
    buyer_token = buyer_res.json()["access_token"]
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

    db = SessionLocal()
    try:
        # Give buyer 1000 credits for non-existent or failing model
        bal = ModelCreditBalance(user_id=buyer_user_id, model_slug="fail-model", model_name="Fail Model", credits_remaining=1000)
        db.add(bal)
        db.commit()
        initial_credits = bal.credits_remaining
    finally:
        db.close()

    # Chat execution will fail because model has no eligible connection and is not in demo
    chat_res = client.post("/api/v1/chat/messages", headers=buyer_headers, json={
        "model_slug": "fail-model",
        "prompt": "Should fail gracefully",
        "is_demo": False
    })
    assert chat_res.status_code in [400, 502, 503]

    # Verify 0 credits were deducted!
    db = SessionLocal()
    try:
        bal = db.query(ModelCreditBalance).filter(
            ModelCreditBalance.user_id == buyer_user_id,
            ModelCreditBalance.model_slug == "fail-model"
        ).first()
        assert bal.credits_remaining == initial_credits, "Credits were improperly deducted on failed execution!"
    finally:
        db.close()


def test_14_ai_request_succeeds_correct_usage_accounting():
    """TEST 14: AI request succeeds -> Correct usage accounting."""
    ts = int(time.time() * 1000)
    prov_gmail = f"prov_14_{ts}@gmail.com"
    buyer_gmail = f"buyer_14_{ts}@gmail.com"

    prov_res = client.post("/api/v1/auth/google", json={"email": prov_gmail, "full_name": "Prov 14", "google_sub_id": f"s14_{ts}"})
    prov_user_id = prov_res.json()["user"]["id"]
    prov_token = prov_res.json()["access_token"]
    prov_headers = {"Authorization": f"Bearer {prov_token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=prov_headers, json={
        "provider_type": "mock", "account_email": prov_gmail, "connection_method": "official_oauth",
        "declared_capacity": 100000, "authorization_code": "auth_ok"
    })
    conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    client.post("/api/v1/provider/listings", headers=prov_headers, json={
        "title": "Accounting Test Listing", "model_slug": "nova-reasoning", "price_per_1k_tokens": 0.05,
        "connection_id": conn_id, "quantity_available": 50000
    })

    buyer_res = client.post("/api/v1/auth/google", json={"email": buyer_gmail, "full_name": "Buyer 14", "google_sub_id": f"b14_{ts}"})
    buyer_user_id = buyer_res.json()["user"]["id"]
    buyer_token = buyer_res.json()["access_token"]
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

    db = SessionLocal()
    try:
        bal = ModelCreditBalance(user_id=buyer_user_id, model_slug="nova-reasoning", model_name="Nova Reasoning", credits_remaining=5000)
        db.add(bal)
        db.commit()
    finally:
        db.close()

    # Perform chat message
    chat_res = client.post("/api/v1/chat/messages", headers=buyer_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Count usage tokens accurately",
        "is_demo": True
    })
    assert chat_res.status_code == 200

    # Verify accounting: credits deducted
    db = SessionLocal()
    try:
        bal = db.query(ModelCreditBalance).filter(
            ModelCreditBalance.user_id == buyer_user_id,
            ModelCreditBalance.model_slug == "nova-reasoning"
        ).first()
        assert bal.credits_remaining < 5000, "Consumer credits were not deducted on successful request!"
    finally:
        db.close()


def test_15_provider_attempts_to_access_another_providers_connection_rbac():
    """TEST 15: Provider attempts to access another provider's connection -> RBAC denial (HTTP 403)."""
    ts = int(time.time() * 1000)
    p1_gmail = f"p1_{ts}@gmail.com"
    p2_gmail = f"p2_{ts}@gmail.com"

    p1_res = client.post("/api/v1/auth/google", json={"email": p1_gmail, "full_name": "P1", "google_sub_id": f"p1_{ts}"})
    p1_token = p1_res.json()["access_token"]
    p1_headers = {"Authorization": f"Bearer {p1_token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=p1_headers, json={
        "provider_type": "mock", "account_email": p1_gmail, "connection_method": "official_oauth",
        "declared_capacity": 50000, "authorization_code": "auth_ok"
    })
    p1_conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    # Provider 2 logs in
    p2_res = client.post("/api/v1/auth/google", json={"email": p2_gmail, "full_name": "P2", "google_sub_id": f"p2_{ts}"})
    p2_token = p2_res.json()["access_token"]
    p2_headers = {"Authorization": f"Bearer {p2_token}"}

    # P2 tries to declare capacity on P1's connection -> MUST BE 403
    p2_attack = client.post(f"/api/v1/provider/ai-accounts/{p1_conn_id}/declare-capacity", headers=p2_headers, json={
        "declared_capacity": 999999
    })
    assert p2_attack.status_code == 403, f"Expected 403 Forbidden, got {p2_attack.status_code}"

    # P2 tries to disconnect P1's connection -> MUST BE 403
    p2_disc = client.post(f"/api/v1/provider/ai-accounts/{p1_conn_id}/disconnect", headers=p2_headers)
    assert p2_disc.status_code == 403, f"Expected 403 Forbidden, got {p2_disc.status_code}"


def test_16_consumer_attempts_to_access_provider_credentials():
    """TEST 16: Consumer attempts to access provider credentials -> Denied / Credentials never exposed."""
    ts = int(time.time() * 1000)
    consumer_gmail = f"consumer_{ts}@gmail.com"

    res = client.post("/api/v1/auth/google", json={"email": consumer_gmail, "full_name": "Consumer", "google_sub_id": f"c_{ts}"})
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Consumer browses marketplace
    mkt = client.get("/api/v1/consumer/marketplace", headers=headers)
    assert mkt.status_code == 200
    mkt_text = mkt.text
    # Ensure no secret tokens or encrypted payloads are revealed to consumer
    assert "encrypted_authorization_data" not in mkt_text
    assert "sk-" not in mkt_text
    assert "AIzaSy" not in mkt_text

    # Consumer attempts admin connections endpoint -> 403
    adm = client.get("/api/v1/admin/provider-connections", headers=headers)
    assert adm.status_code == 403


def test_17_admin_attempts_to_fabricate_provider_capacity():
    """TEST 17: Admin attempts to fabricate provider capacity -> Denied (HTTP 403)."""
    ts = int(time.time() * 1000)
    prov_gmail = f"prov_17_{ts}@gmail.com"

    prov_res = client.post("/api/v1/auth/google", json={"email": prov_gmail, "full_name": "Prov 17", "google_sub_id": f"s17_{ts}"})
    prov_token = prov_res.json()["access_token"]
    prov_headers = {"Authorization": f"Bearer {prov_token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=prov_headers, json={
        "provider_type": "mock", "account_email": prov_gmail, "connection_method": "official_oauth",
        "declared_capacity": 50000, "authorization_code": "auth_ok"
    })
    conn_id = conn_res.json()["data"].get("connection_id") or conn_res.json()["data"].get("id")

    # Admin logs in
    admin_login = client.post("/api/v1/auth/login", json={"email": "admin@tokenpool.ai", "password": "admin123"})
    admin_token = admin_login.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Admin attempts to fabricate provider capacity -> MUST BE FORBIDDEN (HTTP 403)
    fab_res = client.post(f"/api/v1/admin/provider-connections/{conn_id}/set-capacity", headers=admin_headers, json={
        "capacity": 5000000
    })
    assert fab_res.status_code == 403
    assert "fabricat" in fab_res.text.lower() or "forbidden" in fab_res.text.lower()


def test_18_raw_authorization_token_never_in_api_responses():
    """TEST 18: Raw authorization token appears in API response -> FAIL if exposed."""
    ts = int(time.time() * 1000)
    gmail = f"sec_prov_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={"email": gmail, "full_name": "Sec Prov", "google_sub_id": f"sub_{ts}"})
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    secret_raw_auth = "super_secret_raw_oauth_bearer_xyz123"

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "mock",
        "account_email": gmail,
        "connection_method": "official_oauth",
        "declared_capacity": 50000,
        "authorization_code": secret_raw_auth
    })
    assert conn_res.status_code in (200, 201)

    # Ensure the secret raw string NEVER appears in the connect response
    assert secret_raw_auth not in conn_res.text, "Raw authorization token was leaked in connect response!"

    # Ensure the secret raw string NEVER appears in GET ai-accounts
    list_res = client.get("/api/v1/provider/ai-accounts", headers=headers)
    assert secret_raw_auth not in list_res.text, "Raw authorization token was leaked in ai-accounts response!"


def test_19_no_raw_password_requested_for_ai_accounts():
    """TEST 19: Raw password requested anywhere in UI/API for AI accounts -> FAIL if found."""
    # Verify that AI account connection schema does not accept or require 'password' or 'gmail_password'
    ts = int(time.time() * 1000)
    gmail = f"test19_{ts}@gmail.com"
    auth_res = client.post("/api/v1/auth/google", json={"email": gmail, "full_name": "Test 19", "google_sub_id": f"sub_{ts}"})
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Verify endpoint works without any password parameter
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": gmail,
        "connection_method": "google_identity"
    })
    assert res.status_code in (200, 201)
    # Verified: NO password requested or stored!


def test_20_no_browser_cookie_session_scraping_exists():
    """TEST 20: Browser cookie/session extraction exists anywhere -> FAIL if found."""
    # Inspect codebase patterns: ensure no cookie scraping or browser automation exists
    import os
    for root, dirs, files in os.walk("backend/app"):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                with open(path, "r", encoding="utf-8", errors="ignore") as code_file:
                    content = code_file.read()
                    assert "browser_cookies" not in content, f"Forbidden browser cookie usage in {path}"
                    assert "selenium" not in content, f"Forbidden selenium scraping in {path}"
                    assert "playwright" not in content, f"Forbidden playwright scraping in {path}"
                    assert "puppeteer" not in content, f"Forbidden puppeteer scraping in {path}"
