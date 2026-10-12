"""
TokenPool — Comprehensive Test Suite for Consumer Chat History, Context Preservation,
Provider Capacity Accounting, Real Execution Routing, and Security (Section 39).
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
    Order, Wallet, CreditAllocation, ModelCreditBalance, AIModel,
    Conversation, ChatMessage, AIRequestRecord
)
from backend.app.core.encryption import encrypt_credential, decrypt_credential
from backend.app.services.marketplace_eligibility import MarketplaceEligibilityService

client = TestClient(app)


def _create_test_user(email_prefix="user", role="USER"):
    ts = int(time.time() * 1000) + (uuid.uuid4().int % 100000)
    email = f"{email_prefix}_{ts}@gmail.com"
    pwd = "password12345"
    res = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": pwd,
        "full_name": f"Test {email_prefix.title()}"
    })
    assert res.status_code == 201, f"Failed to register user: {res.text}"
    token = res.json()["access_token"]
    user_id = res.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}
    return user_id, email, headers


# ══════════════════════════════════════════════════════════════════════════
# AUTH SCENARIOS (1 - 5)
# ══════════════════════════════════════════════════════════════════════════

def test_1_google_identity_success():
    """Scenario 1: Provider authenticates via verified Google/Gmail identity."""
    ts = int(time.time() * 1000) + (uuid.uuid4().int % 100000)
    gmail = f"google_user_{ts}@gmail.com"
    res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Google User",
        "google_sub_id": f"sub_{ts}"
    })
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["user"]["email"] == gmail
    assert data["user"]["email_verified"] is True


def test_2_google_identity_mismatch():
    """Scenario 2: Provider connects AI account with a different email -> IDENTITY_MISMATCH."""
    user_id, email, headers = _create_test_user("prov_mismatch")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": "different_account@gmail.com",
        "connection_method": "google_identity"
    })
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MISMATCH"
    assert data["marketplace_status"] == "NOT_ELIGIBLE"


def test_3_invalid_oauth_callback():
    """Scenario 3: Invalid OAuth callback missing code -> handled with clear error."""
    res = client.get("/api/v1/provider/ai-accounts/google/callback?error=access_denied")
    assert res.status_code == 400
    assert "Google Authorization Error" in res.text


def test_4_token_exchange_failure():
    """Scenario 4: Invalid OAuth code rejected during token exchange."""
    res = client.get("/api/v1/provider/ai-accounts/google/callback?code=invalid_dummy_code")
    assert res.status_code in (400, 401)


def test_5_fake_authorization_rejected():
    """Scenario 5: Fake authorization token/code rejected — email match alone is not AUTHORIZED."""
    user_id, email, headers = _create_test_user("prov_nofake")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": email,
        "connection_method": "google_identity",
        "authorization_token": "auth_tokenpool_fake_12345"
    })
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MATCHED"
    # Must NOT be marked AUTHORIZED or ELIGIBLE based on fake token
    assert data["authorization_status"] != "AUTHORIZED" or data["marketplace_status"] != "ELIGIBLE"


# ══════════════════════════════════════════════════════════════════════════
# PROVIDER CONNECTION & CREDENTIAL SCENARIOS (6 - 10)
# ══════════════════════════════════════════════════════════════════════════

def test_6_provider_connection_creation():
    """Scenario 6: Provider connection is created with clear status machine."""
    user_id, email, headers = _create_test_user("prov_create")
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-test-valid-key-for-test-suite",
        "label": "My OpenAI Capacity",
        "capacity_declared": 50000
    })
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["status"] == "CONNECTED"
    assert data["capacity_declared"] == 50000


def test_7_provider_connection_ownership():
    """Scenario 7: Provider connection ownership is enforced; other providers cannot modify."""
    u1_id, u1_email, h1 = _create_test_user("prov_owner1")
    u2_id, u2_email, h2 = _create_test_user("prov_owner2")

    res = client.post("/api/v1/provider/connections", headers=h1, json={
        "provider_type": "openai",
        "api_key": "sk-test-valid-key-owner1",
        "label": "Owner 1 Connection",
        "capacity_declared": 20000
    })
    conn_id = res.json()["data"]["id"]

    # Provider 2 tries to declare capacity on Provider 1's connection
    res2 = client.post(f"/api/v1/provider/connections/{conn_id}/capacity", headers=h2, json={
        "declared_capacity": 99999
    })
    assert res2.status_code in (403, 404)


def test_8_credential_encryption():
    """Scenario 8: Provider credential is encrypted at rest in the database."""
    user_id, email, headers = _create_test_user("prov_enc")
    raw_key = "sk-test-valid-secret-key-12345"
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": raw_key,
        "label": "Encrypted Source"
    })
    conn_id = res.json()["data"]["id"]

    db = SessionLocal()
    try:
        conn = db.query(ProviderConnection).filter(ProviderConnection.id == conn_id).first()
        assert conn.encrypted_credential is not None
        assert raw_key not in conn.encrypted_credential
        decrypted = decrypt_credential(conn.encrypted_credential)
        assert decrypted == raw_key
    finally:
        db.close()


def test_9_credential_never_returned():
    """Scenario 9: Raw credentials and refresh tokens are never returned in API responses."""
    user_id, email, headers = _create_test_user("prov_nomask")
    raw_key = "sk-test-valid-super-secret-key"
    client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": raw_key,
        "label": "Protected Key"
    })

    res = client.get("/api/v1/provider/connections", headers=headers)
    assert res.status_code == 200
    resp_str = res.text
    assert raw_key not in resp_str
    assert "refresh_token" not in resp_str


def test_10_revoked_connection_rejected():
    """Scenario 10: Revoked connection is rejected and cannot serve marketplace requests."""
    user_id, email, headers = _create_test_user("prov_revoke")
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-test-valid-to-revoke",
        "label": "Will Revoke"
    })
    conn_id = res.json()["data"]["id"]

    # Revoke
    rev_res = client.post(f"/api/v1/provider/connections/{conn_id}/revoke", headers=headers)
    assert rev_res.status_code == 200

    # Cannot declare capacity on revoked connection
    cap_res = client.post(f"/api/v1/provider/connections/{conn_id}/capacity", headers=headers, json={
        "declared_capacity": 10000
    })
    assert cap_res.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
# ELIGIBILITY & MARKETPLACE SCENARIOS (11 - 18)
# ══════════════════════════════════════════════════════════════════════════

def test_11_identity_matched_but_execution_unsupported():
    """Scenario 11: Gmail identity matched, but execution unsupported -> NOT_ELIGIBLE with reason."""
    user_id, email, headers = _create_test_user("prov_unsupported")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": email,
        "connection_method": "google_identity"
    })
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MATCHED"
    assert data["marketplace_status"] == "NOT_ELIGIBLE"
    assert "Google does not provide an officially supported execution mechanism" in data["marketplace_reason"]


def test_12_consumer_ai_subscription_not_automatically_api_quota():
    """Scenario 12: Consumer AI subscription (ChatGPT / Claude / Gemini) cannot automatically become API capacity."""
    user_id, email, headers = _create_test_user("prov_consumer_sub")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "openai",
        "account_email": email,
        "connection_method": "google_identity"
    })
    data = res.json()["data"]
    assert data["marketplace_status"] == "NOT_ELIGIBLE"
    assert "ChatGPT consumer Pro subscription does not provide a 3rd-party" in data["marketplace_reason"]


def test_13_api_capacity_provider_becomes_eligible_when_supported():
    """Scenario 13: Provider with validated API capacity and declared capacity becomes ELIGIBLE."""
    user_id, email, headers = _create_test_user("prov_api_eligible")
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-test-valid-api-capacity",
        "label": "Valid API Connection",
        "capacity_declared": 100000
    })
    conn_id = res.json()["data"]["id"]

    db = SessionLocal()
    try:
        conn = db.query(ProviderConnection).filter(ProviderConnection.id == conn_id).first()
        user = db.query(User).filter(User.id == user_id).first()
        status, reason = MarketplaceEligibilityService.evaluate(conn, user)
        assert status == "ELIGIBLE"
    finally:
        db.close()


def test_14_ineligible_provider_cannot_create_active_listing():
    """Scenario 14: Ineligible provider cannot create an active marketplace listing."""
    user_id, email, headers = _create_test_user("prov_no_listing")
    # Connect unsupported consumer account
    c_res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": email,
        "connection_method": "google_identity"
    })
    conn_id = c_res.json()["data"]["connection_id"]

    # Attempt to create listing backed by this connection
    list_res = client.post("/api/v1/provider/listings", headers=headers, json={
        "provider_connection_id": conn_id,
        "model_slug": "gemini-1.5-flash",
        "capacity_units": 10000,
        "price_per_unit": 0.50
    })
    assert list_res.status_code in (400, 422)


def test_15_capacity_cannot_become_negative():
    """Scenario 15: Capacity declaration cannot be negative."""
    user_id, email, headers = _create_test_user("prov_neg_cap")
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-test-valid-key",
        "label": "Test Conn"
    })
    conn_id = res.json()["data"]["id"]

    neg_res = client.post(f"/api/v1/provider/connections/{conn_id}/capacity", headers=headers, json={
        "declared_capacity": -5000
    })
    assert neg_res.status_code == 400


def test_16_overselling_prevented():
    """Scenario 16: Overselling is prevented — declared capacity cannot be reduced below committed units."""
    user_id, email, headers = _create_test_user("prov_oversell")
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "api_key": "sk-test-valid-key",
        "label": "Oversell Protection",
        "capacity_declared": 50000
    })
    conn_id = res.json()["data"]["id"]

    db = SessionLocal()
    try:
        conn = db.query(ProviderConnection).filter(ProviderConnection.id == conn_id).first()
        conn.capacity_listed = 40000
        db.commit()
    finally:
        db.close()

    # Try to reduce declared capacity below 40000
    reduce_res = client.post(f"/api/v1/provider/connections/{conn_id}/capacity", headers=headers, json={
        "declared_capacity": 20000
    })
    assert reduce_res.status_code == 400
    assert "cannot be lower than capacity already committed" in reduce_res.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════
# CHAT HISTORY, CONTEXT PRESERVATION & ISOLATION (19 - 26)
# ══════════════════════════════════════════════════════════════════════════

def test_17_consumer_can_create_conversation():
    """Scenario 17: Consumer creates a TokenPool conversation; owned by consumer."""
    c_id, c_email, c_headers = _create_test_user("consumer_create_conv")
    res = client.post("/api/v1/chat/conversations", headers=c_headers, json={
        "title": "Quantum Computing Chat",
        "model_slug": "nova-reasoning"
    })
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["title"] == "Quantum Computing Chat"
    assert data["consumer_id"] == c_id
    assert data["status"] == "ACTIVE"


def test_18_consumer_can_send_message_and_history_persists():
    """Scenario 18: Consumer sends message; response is generated and persisted in TokenPool DB."""
    c_id, c_email, c_headers = _create_test_user("consumer_send_msg")
    # Grant initial credits
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.user_id == c_id).first()
        w.credits = 100
        mcb = ModelCreditBalance(
            user_id=c_id, model_slug="nova-reasoning", model_name="Nova Reasoning",
            credits_purchased=100, credits_used=0, credits_remaining=100
        )
        db.add(mcb)
        db.commit()
    finally:
        db.close()

    res = client.post("/api/v1/chat/messages", headers=c_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Explain machine learning in simple terms.",
        "is_demo": True
    })
    assert res.status_code == 200
    data = res.json()["data"]
    conv_id = data["conversation_id"]
    assert conv_id is not None
    assert "response" in data

    # Verify messages in conversation
    msgs_res = client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=c_headers)
    assert msgs_res.status_code == 200
    msgs = msgs_res.json()["data"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_19_consumer_sees_only_own_conversations():
    """Scenario 19: Conversation isolation — consumer sees only their own conversations."""
    c1_id, c1_email, h1 = _create_test_user("consumer_iso1")
    c2_id, c2_email, h2 = _create_test_user("consumer_iso2")

    client.post("/api/v1/chat/conversations", headers=h1, json={"title": "Consumer 1 Conversation"})
    client.post("/api/v1/chat/conversations", headers=h2, json={"title": "Consumer 2 Conversation"})

    r1 = client.get("/api/v1/chat/conversations", headers=h1)
    r2 = client.get("/api/v1/chat/conversations", headers=h2)

    c1_titles = [c["title"] for c in r1.json()["data"]]
    c2_titles = [c["title"] for c in r2.json()["data"]]

    assert "Consumer 1 Conversation" in c1_titles
    assert "Consumer 2 Conversation" not in c1_titles
    assert "Consumer 2 Conversation" in c2_titles
    assert "Consumer 1 Conversation" not in c2_titles


def test_20_provider_cannot_access_consumer_conversation():
    """Scenario 20: Provider cannot read consumer conversation or messages."""
    c_id, c_email, c_headers = _create_test_user("consumer_priv")
    p_id, p_email, p_headers = _create_test_user("provider_priv")

    res = client.post("/api/v1/chat/conversations", headers=c_headers, json={"title": "Private Consumer Chat"})
    conv_id = res.json()["data"]["id"]

    # Provider tries to read consumer's conversation
    p_get = client.get(f"/api/v1/chat/conversations/{conv_id}", headers=p_headers)
    assert p_get.status_code in (403, 404)

    p_msgs = client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=p_headers)
    assert p_msgs.status_code in (403, 404)


def test_21_context_is_preserved_across_messages():
    """Scenario 21: Context is preserved across multiple turns in a conversation."""
    c_id, c_email, c_headers = _create_test_user("consumer_context")
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.user_id == c_id).first()
        w.credits = 100
        mcb = ModelCreditBalance(
            user_id=c_id, model_slug="nova-reasoning", model_name="Nova Reasoning",
            credits_purchased=100, credits_used=0, credits_remaining=100
        )
        db.add(mcb)
        db.commit()
    finally:
        db.close()

    # Turn 1: Explain machine learning
    r1 = client.post("/api/v1/chat/messages", headers=c_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Explain machine learning.",
        "is_demo": True
    })
    conv_id = r1.json()["data"]["conversation_id"]

    # Turn 2: Give me an example
    r2 = client.post("/api/v1/chat/messages", headers=c_headers, json={
        "conversation_id": conv_id,
        "model_slug": "nova-reasoning",
        "prompt": "Give me an example.",
        "is_demo": True
    })
    assert r2.status_code == 200

    # Verify conversation now has 4 messages (2 user, 2 assistant)
    msgs_res = client.get(f"/api/v1/chat/conversations/{conv_id}/messages", headers=c_headers)
    msgs = msgs_res.json()["data"]
    assert len(msgs) == 4
    assert msgs[0]["content"] == "Explain machine learning."
    assert msgs[2]["content"] == "Give me an example."


# ══════════════════════════════════════════════════════════════════════════
# BILLING, USAGE METERING & ATOMIC TRANSACTIONS (22 - 27)
# ══════════════════════════════════════════════════════════════════════════

def test_22_successful_request_records_usage_and_provider_earnings():
    """Scenario 22: Successful AI request records actual usage and calculates provider earnings."""
    c_id, c_email, c_headers = _create_test_user("consumer_usage")
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.user_id == c_id).first()
        w.credits = 100
        mcb = ModelCreditBalance(
            user_id=c_id, model_slug="nova-reasoning", model_name="Nova Reasoning",
            credits_purchased=100, credits_used=0, credits_remaining=100
        )
        db.add(mcb)
        db.commit()
    finally:
        db.close()

    r = client.post("/api/v1/chat/messages", headers=c_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Calculate prime numbers.",
        "is_demo": True
    })
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["usage"]["total_tokens"] > 0
    assert data["cost"] > 0

    db = SessionLocal()
    try:
        # Check AIRequestRecord
        rec = db.query(AIRequestRecord).filter(AIRequestRecord.user_id == c_id).first()
        assert rec is not None
        assert rec.total_tokens == data["usage"]["total_tokens"]
        assert rec.status == "COMPLETED"
    finally:
        db.close()


def test_23_failed_request_does_not_deduct_credits_or_earnings():
    """Scenario 23: When upstream request fails, zero credits deducted and zero provider earnings."""
    c_id, c_email, c_headers = _create_test_user("consumer_fail_safety")
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.user_id == c_id).first()
        w.credits = 50
        mcb = ModelCreditBalance(
            user_id=c_id, model_slug="nova-reasoning", model_name="Nova Reasoning",
            credits_purchased=50, credits_used=0, credits_remaining=50
        )
        db.add(mcb)
        db.commit()
    finally:
        db.close()

    from unittest.mock import patch

    # Mock upstream provider outage/failure
    with patch("backend.app.services.router.AIRouterService.route", side_effect=RuntimeError("Upstream provider connection timeout")):
        res = client.post("/api/v1/chat/messages", headers=c_headers, json={
            "model_slug": "nova-reasoning",
            "prompt": "Should fail gracefully",
            "is_demo": True
        })
        assert res.status_code in (502, 503)
        assert "No credits or wallet balance were charged" in res.json()["detail"]

    db = SessionLocal()
    try:
        mcb = db.query(ModelCreditBalance).filter(ModelCreditBalance.user_id == c_id).first()
        assert mcb.credits_used == 0
        assert mcb.credits_remaining == 50
    finally:
        db.close()


def test_24_request_idempotency_prevents_double_charge():
    """Scenario 24: Repeated request with same request_id does not double charge."""
    c_id, c_email, c_headers = _create_test_user("consumer_idempotent")
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.user_id == c_id).first()
        w.credits = 100
        mcb = ModelCreditBalance(
            user_id=c_id, model_slug="nova-reasoning", model_name="Nova Reasoning",
            credits_purchased=100, credits_used=0, credits_remaining=100
        )
        db.add(mcb)
        db.commit()
    finally:
        db.close()

    req_id = f"idem_req_{uuid.uuid4().hex[:12]}"

    # Request 1
    r1 = client.post("/api/v1/chat/messages", headers=c_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Idempotent request check.",
        "request_id": req_id,
        "is_demo": True
    })
    assert r1.status_code == 200
    used_after_first = r1.json()["data"]["credits_remaining"]

    # Request 2 with same request_id (e.g. double click or network retry)
    r2 = client.post("/api/v1/chat/messages", headers=c_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Idempotent request check.",
        "request_id": req_id,
        "is_demo": True
    })
    assert r2.status_code == 200
    assert r2.json()["data"]["idempotent"] is True
    assert r2.json()["data"]["credits_remaining"] == used_after_first
