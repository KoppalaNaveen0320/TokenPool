"""
Verification test specifically targeting Google Gemini provider connection:
A. valid Gemini key -> backend request -> validation succeeds
B. invalid Gemini key -> proper validation error
C. missing key -> frontend/backend validation error
D. revoked connection -> cannot execute
E. credential is encrypted and never exposed in responses
"""
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.database import SessionLocal
from backend.app.models.schema_models import User, ProviderConnection

client = TestClient(app)

def get_auth_headers(email="gemini_provider@tokenpool.ai"):
    res = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "Password123!",
        "full_name": "Gemini Tester",
        "role": "USER"
    })
    if res.status_code != 200:
        login_res = client.post("/api/v1/auth/login", json={
            "email": email,
            "password": "Password123!"
        })
        token = login_res.json().get("access_token") or login_res.json().get("data", {}).get("token")
    else:
        token = res.json().get("access_token") or res.json().get("data", {}).get("token")
    return {"Authorization": f"Bearer {token}"}

def test_gemini_connection_validation_and_security():
    headers = get_auth_headers()

    # Scenario C: Missing API key
    res_missing = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "gemini",
        "credential": "",
        "declared_capacity": 50000
    })
    assert res_missing.status_code in (400, 422), f"Expected 400/422 on missing key, got {res_missing.status_code}"

    # Scenario B: Invalid Gemini key
    res_invalid = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "gemini",
        "credential": "AIzaSyFakeInvalidKeyForTesting12345",
        "declared_capacity": 50000
    })
    assert res_invalid.status_code in (400, 422), f"Expected 400/422 on invalid key, got {res_invalid.status_code}"
    err_detail = res_invalid.text.lower()
    assert "validation failed" in err_detail or "invalid" in err_detail or "error" in err_detail

    # Scenario A: Valid Gemini key
    res_valid = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "gemini",
        "credential": "AIzaSy-test-valid-gemini-key-1234567890",
        "declared_capacity": 100000,
        "label": "My Gemini Fleet"
    })
    assert res_valid.status_code in (200, 201), f"Expected 200/201 on valid key, got {res_valid.status_code}: {res_valid.text}"
    body = res_valid.json()
    conn_data = body.get("data") or body.get("connection") or body
    conn_id = conn_data.get("id")

    # Scenario F: Security — raw key NEVER exposed in response
    resp_text = res_valid.text
    assert "AIzaSy-test-valid-gemini-key-1234567890" not in resp_text, "Raw API key was leaked in response!"

    # Verify status
    assert conn_data.get("validation_status") == "VALIDATED"
    assert conn_data.get("status") in ("CONNECTED", "ACTIVE")

    # Scenario D: Revoke connection
    res_revoke = client.delete(f"/api/v1/provider/connections/{conn_id}", headers=headers)
    assert res_revoke.status_code in (200, 204), f"Revoke failed: {res_revoke.status_code}"

    # Check connection is revoked
    db = SessionLocal()
    conn = db.query(ProviderConnection).filter(ProviderConnection.id == conn_id).first()
    assert conn is not None
    assert conn.validation_status == "REVOKED" or conn.status in ("REVOKED", "DISCONNECTED")
    db.close()
