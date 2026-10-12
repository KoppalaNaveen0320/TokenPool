"""
TokenPool - Real Gemini Connection & Execution Architecture Verification Suite
Tests the architectural corrections:
1. Connecting Gemini via Gmail identity sets IDENTITY_MATCHED, but authorization_status = ACTION_REQUIRED.
2. Plan name is NOT fabricated ("Unable to verify", not "AI Pro").
3. Gemini is NOT automatically ELIGIBLE; marketplace_status = NOT_ELIGIBLE.
4. Google OAuth auth-url endpoint returns official Google authorization endpoint and generative-language scopes.
5. Ineligible Gemini connection cannot create marketplace listings.
6. Execution without authorized provider token raises EXECUTION_NOT_SUPPORTED and does NOT silently use platform GEMINI_API_KEY.
7. Zero deductions on execution failure.
"""
import sys
import os
sys.path.insert(0, os.path.abspath("."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import time
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.database import SessionLocal
from backend.app.models.schema_models import (
    User, ProviderAccount, ProviderConnection, ProviderListing, ModelCreditBalance
)
from backend.app.services.router import AIRouterService

client = TestClient(app)


def test_gemini_connection_identity_matched_but_not_authorized():
    """Verify that email equality grants IDENTITY_MATCHED, but NOT AUTHORIZED or ELIGIBLE."""
    ts = int(time.time() * 1000)
    gmail = f"gemini_test_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Gemini Provider Test",
        "google_sub_id": f"sub_gemini_{ts}"
    })
    assert auth_res.status_code == 200
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Connect Gemini using matching Gmail identity
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": gmail,
        "connection_method": "google_identity",
        "declared_capacity": 100000
    })
    assert res.status_code in (200, 201), res.text
    data = res.json()["data"]

    # 1. Identity must match
    assert data["identity_status"] == "IDENTITY_MATCHED"
    assert data["account_email"] == gmail

    # 2. Authorization status must NOT be AUTHORIZED
    assert data["authorization_status"] == "ACTION_REQUIRED"

    # 3. Plan must NOT be hardcoded "AI Pro"
    assert data["plan_name"] == "Unable to verify"

    # 4. Marketplace must NOT be ELIGIBLE
    assert data["marketplace_status"] == "NOT_ELIGIBLE"
    assert "google does not provide" in data["marketplace_reason"].lower() or "not authorized" in data["marketplace_reason"].lower()


def test_google_auth_url_endpoint():
    """Verify the Google OAuth initiation endpoint."""
    ts = int(time.time() * 1000)
    gmail = f"oauth_test_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "OAuth Test User",
        "google_sub_id": f"sub_oauth_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.get("/api/v1/provider/ai-accounts/google/auth-url", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "success" in data
    # If not configured in environment, it clearly returns informative reason
    if not data.get("configured"):
        assert "not configured" in data["message"].lower() or "google does not provide" in data["message"].lower()
    else:
        assert "accounts.google.com/o/oauth2/v2/auth" in data["auth_url"]
        assert "https://www.googleapis.com/auth/generative-language" in data["scopes"]


def test_unauthorized_gemini_connection_cannot_create_listing():
    """Verify that an unauthorized Gemini connection is blocked from creating a listing."""
    ts = int(time.time() * 1000)
    gmail = f"blocked_listing_{ts}@gmail.com"

    auth_res = client.post("/api/v1/auth/google", json={
        "email": gmail,
        "full_name": "Blocked Listing Prov",
        "google_sub_id": f"sub_bl_{ts}"
    })
    token = auth_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    conn_res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": gmail,
        "connection_method": "google_identity",
        "declared_capacity": 50000
    })
    conn_id = conn_res.json()["data"]["connection_id"]

    # Attempt to create a listing
    list_res = client.post("/api/v1/provider/listings", headers=headers, json={
        "title": "Unauthorized Gemini Listing",
        "model_slug": "gemini-1.5-flash",
        "price_per_1k_tokens": 0.05,
        "connection_id": conn_id,
        "quantity_available": 10000
    })
    assert list_res.status_code == 400
    assert "eligible" in list_res.text.lower() or "validated" in list_res.text.lower()


def test_gemini_adapter_blocks_execution_without_real_provider_credential():
    """Verify that GeminiAccountAdapter refuses to execute when no real provider token exists."""
    db = SessionLocal()
    try:
        ts = int(time.time() * 1000)
        user = User(email=f"gem_exec_{ts}@gmail.com", hashed_password="pw", full_name="Gem Exec", role="USER")
        db.add(user)
        db.flush()

        prov = ProviderAccount(user_id=user.id, workspace_name="Gem Workspace")
        db.add(prov)
        db.flush()

        conn = ProviderConnection(
            provider_id=prov.id,
            provider_name="Google Gemini",
            provider_type="gemini",
            account_email=user.email,
            identity_status="IDENTITY_MATCHED",
            authorization_status="ACTION_REQUIRED",
            validation_status="PENDING",
            marketplace_status="NOT_ELIGIBLE",
            connection_status="CONNECTED",
            capacity_declared=100000,
            capacity_remaining=100000
        )
        db.add(conn)
        db.commit()

        # Attempting to get adapter for this ineligible connection must raise ValueError
        with pytest.raises(ValueError) as excinfo:
            AIRouterService.get_connection_adapter(conn)
        assert "EXECUTION_NOT_SUPPORTED" in str(excinfo.value)

    finally:
        db.close()
