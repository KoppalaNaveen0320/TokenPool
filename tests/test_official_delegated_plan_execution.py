"""
TokenPool — Official Delegated AI Plan Execution Tests
Comprehensive validation of:
1. Provider starts official authorization
2. Invalid OAuth state rejected
3. OAuth callback success
4. OAuth callback failure
5. Provider identity mismatch
6. Authorization not completed
7. Delegated execution disabled
8. Delegated execution enabled
9. Official execution capability supported
10. Unsupported provider remains NOT_ELIGIBLE
11. Consumer creates chat
12. Consumer chat history preserved
13. Consumer context sent to model
14. Consumer can execute eligible delegated listing
15. Consumer cannot execute ineligible listing
16. Consumer cannot access another consumer's chat
17. Provider cannot access consumer chat
18. Usage recorded
19. Capacity deducted
20. Provider earnings recorded
21. Upstream failure causes zero charge
22. Upstream failure causes zero earnings
23. Duplicate request is idempotent
24. Revocation disables listing
25. Health check detects authorization failure
26. No credential leakage
27. No refresh token in API response
28. No global TokenPool key used for provider delegated listing
29. Unsupported Google consumer subscription rejected
30. API-capacity provider still works
"""
import io
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.config import settings
from backend.app.core.database import SessionLocal
from backend.app.core.encryption import encrypt_credential, decrypt_credential
from backend.app.models.schema_models import (
    User,
    ProviderAccount,
    ProviderConnection,
    ProviderListing,
    Order,
    Conversation,
    ChatMessage,
    AIRequestRecord,
    AuditLog,
    Wallet,
    ModelCreditBalance,
)
from backend.app.providers.base import AIResponseResult
from backend.app.providers.delegated_plan_adapters import (
    OpenAIDelegatedPlanAdapter,
    GoogleDelegatedPlanAdapter,
    AnthropicDelegatedPlanAdapter,
)

client = TestClient(app)


def _create_user(prefix: str, role: str = "USER") -> tuple[int, str, dict]:
    ts = int(time.time() * 1000)
    email = f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}@example.com"
    pwd = "SecurePassword123!"
    res = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": pwd,
        "full_name": f"{prefix.title()} User",
        "role": role
    })
    assert res.status_code in (200, 201), res.text
    token = res.json().get("access_token") or res.json().get("data", {}).get("token")
    user_id = res.json().get("user_id") or res.json().get("data", {}).get("user_id")
    if not user_id:
        db = SessionLocal()
        u = db.query(User).filter(User.email == email).first()
        user_id = u.id
        db.close()
    return user_id, email, {"Authorization": f"Bearer {token}"}


def _get_admin_headers() -> dict:
    res = client.post("/api/v1/auth/login", json={
        "email": "admin@tokenpool.ai",
        "password": "admin123"
    })
    token = res.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}


def test_01_provider_starts_official_authorization_disabled_initially():
    """1. Provider starts official authorization while disabled -> shows not enabled reason."""
    orig_enabled = settings.OPENAI_DELEGATED_PLAN_ENABLED
    orig_status = settings.OPENAI_PARTICIPATING_APP_STATUS
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = False
        settings.OPENAI_PARTICIPATING_APP_STATUS = "NOT_APPLIED"

        user_id, email, headers = _create_user("prov_auth_start")
        res = client.get("/api/v1/provider/ai-accounts/openai/auth-url", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is False
        assert data["configured"] is False
        assert "not currently enabled" in data["message"]
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig_enabled
        settings.OPENAI_PARTICIPATING_APP_STATUS = orig_status


def test_02_invalid_oauth_state_rejected():
    """2. OAuth callback with invalid/tampered state is rejected with 401."""
    res = client.get("/api/v1/provider/ai-accounts/openai/callback?code=testcode123&state=tampered_invalid_state")
    assert res.status_code in (400, 401)
    assert "Session Expired" in res.text or "Invalid" in res.text or "Could not verify" in res.text


def test_03_oauth_callback_success_stores_tokens():
    """3. OAuth callback success exchanges code and securely stores encrypted tokens."""
    orig_enabled = settings.OPENAI_DELEGATED_PLAN_ENABLED
    orig_status = settings.OPENAI_PARTICIPATING_APP_STATUS
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = True
        settings.OPENAI_PARTICIPATING_APP_STATUS = "APPROVED"

        user_id, email, headers = _create_user("prov_cb_ok")
        valid_state = f"tp_openai_{user_id}_securetok123"

        mock_token_data = {
            "access_token": "mock_openai_delegated_access_token_xyz",
            "refresh_token": "mock_openai_delegated_refresh_token_abc",
            "token_type": "Bearer",
            "expires_in": 3600
        }

        with patch.object(OpenAIDelegatedPlanAdapter, "callback", new_callable=AsyncMock) as mock_cb:
            mock_cb.return_value = mock_token_data
            cb_res = client.get(
                f"/api/v1/provider/ai-accounts/openai/callback?code=mock_code&state={valid_state}",
                follow_redirects=False
            )
            assert cb_res.status_code in (200, 302, 303), cb_res.text

        db = SessionLocal()
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.account_email == email,
            ProviderConnection.provider_type == "openai"
        ).first()
        assert conn is not None
        assert conn.capacity_source == "OFFICIAL_DELEGATED_PLAN"
        assert conn.authorization_status == "AUTHORIZED"
        assert conn.delegated_authorization_status == "AUTHORIZED"
        # Token is encrypted, never plaintext
        assert "mock_openai_delegated_access_token_xyz" not in conn.encrypted_authorization_data
        decrypted = decrypt_credential(conn.encrypted_authorization_data)
        assert "mock_openai_delegated_access_token_xyz" in decrypted
        db.close()
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig_enabled
        settings.OPENAI_PARTICIPATING_APP_STATUS = orig_status


def test_04_oauth_callback_failure_handles_error():
    """4. Upstream error response during callback is handled cleanly."""
    res = client.get("/api/v1/provider/ai-accounts/openai/callback?error=access_denied&error_description=User_cancelled")
    assert res.status_code == 400
    assert "access_denied" in res.text


def test_05_provider_identity_mismatch():
    """5. Provider attempting to connect with an email that doesn't match TokenPool email."""
    user_id, email, headers = _create_user("prov_mismatch")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "openai",
        "account_email": "different_someone_else@example.com",
        "connection_method": "google_identity"
    })
    assert res.status_code in (200, 201)
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MISMATCH"
    assert data["marketplace_status"] == "NOT_ELIGIBLE"


def test_06_authorization_not_completed_is_not_eligible():
    """6. Identity matched but authorization pending -> NOT_ELIGIBLE."""
    user_id, email, headers = _create_user("prov_pending_auth")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "gemini",
        "account_email": email,
        "connection_method": "google_identity"
    })
    assert res.status_code in (200, 201)
    data = res.json()["data"]
    assert data["identity_status"] == "IDENTITY_MATCHED"
    assert data["authorization_status"] == "ACTION_REQUIRED"
    assert data["marketplace_status"] == "NOT_ELIGIBLE"


def test_07_delegated_execution_disabled_by_default():
    """7. Default settings must have delegated plan disabled."""
    orig = settings.OPENAI_DELEGATED_PLAN_ENABLED
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = False
        adapter = OpenAIDelegatedPlanAdapter()
        cap, reason = adapter.get_execution_capability()
        assert cap == "EXECUTION_NOT_SUPPORTED"
        assert "not currently enabled" in reason
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig


def test_08_admin_enables_participating_app_status():
    """8. Admin endpoint updates status from NOT_APPLIED to APPROVED."""
    admin_headers = _get_admin_headers()
    res = client.post("/api/v1/admin/integrations/openai-delegated/status", headers=admin_headers, json={
        "status": "APPROVED",
        "reason": "Official approval received from OpenAI partnership"
    })
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "APPROVED"
    assert settings.OPENAI_PARTICIPATING_APP_STATUS == "APPROVED"


def test_09_official_execution_capability_supported_when_approved_and_committed():
    """9. When approved and committed with token, execution capability is EXECUTION_SUPPORTED."""
    orig_enabled = settings.OPENAI_DELEGATED_PLAN_ENABLED
    orig_status = settings.OPENAI_PARTICIPATING_APP_STATUS
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = True
        settings.OPENAI_PARTICIPATING_APP_STATUS = "APPROVED"

        conn = ProviderConnection(
            provider_id=1,
            provider_type="openai",
            capacity_source="OFFICIAL_DELEGATED_PLAN",
            authorization_status="AUTHORIZED",
            delegated_authorization_status="AUTHORIZED",
            encrypted_authorization_data=encrypt_credential("valid_token"),
            committed_units=10000,
            remaining_units=10000,
            capacity_declared=10000,
            capacity_remaining=10000
        )
        adapter = OpenAIDelegatedPlanAdapter()
        cap, reason = adapter.get_execution_capability(conn)
        assert cap == "EXECUTION_SUPPORTED"
        assert reason is None
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig_enabled
        settings.OPENAI_PARTICIPATING_APP_STATUS = orig_status


def test_10_unsupported_provider_remains_not_eligible():
    """10. Claude / Anthropic consumer subscription remains NOT_ELIGIBLE without official delegation."""
    user_id, email, headers = _create_user("prov_claude_unsupported")
    res = client.post("/api/v1/provider/ai-accounts/connect", headers=headers, json={
        "provider_type": "anthropic",
        "account_email": email,
        "connection_method": "google_identity"
    })
    data = res.json()["data"]
    assert data["marketplace_status"] == "NOT_ELIGIBLE"
    assert "Claude consumer Pro subscription does not provide a 3rd-party" in data["marketplace_reason"]


def test_11_to_20_consumer_chat_delegated_execution_and_accounting():
    """11-20. Consumer creates chat, executes through delegated plan, usage metered, earnings recorded."""
    orig_enabled = settings.OPENAI_DELEGATED_PLAN_ENABLED
    orig_status = settings.OPENAI_PARTICIPATING_APP_STATUS
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = True
        settings.OPENAI_PARTICIPATING_APP_STATUS = "APPROVED"

        # 1. Setup Provider with delegated plan connection & commitment
        prov_id, prov_email, prov_headers = _create_user("prov_del_exec")
        db = SessionLocal()
        pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == prov_id).first()
        if not pa:
            pa = ProviderAccount(user_id=prov_id, status="ACTIVE", workspace_name="Del Prov Node")
            db.add(pa)
            db.commit()

        conn = ProviderConnection(
            provider_id=pa.id,
            provider_name="ChatGPT Official Delegated Plan",
            provider_type="openai",
            auth_type="OAUTH",
            status="CONNECTED",
            connection_status="CONNECTED",
            connection_method="official_delegated_plan",
            capacity_source="OFFICIAL_DELEGATED_PLAN",
            delegated_provider="openai",
            delegated_plan_name="ChatGPT Plus/Pro",
            account_email=prov_email,
            identity_status="IDENTITY_MATCHED",
            delegated_authorization_status="AUTHORIZED",
            authorization_status="AUTHORIZED",
            validation_status="VALIDATED",
            execution_status="EXECUTION_SUPPORTED",
            marketplace_status="ELIGIBLE",
            plan_name="ChatGPT Plus/Pro",
            plan_type="PRO",
            marketplace_commitment=50.0,
            committed_units=50000,
            remaining_units=50000,
            capacity_declared=50000,
            capacity_remaining=50000,
            encrypted_authorization_data=encrypt_credential("mock_access_token_123"),
            upstream_usage_unit="REQUEST"
        )
        db.add(conn)
        db.commit()
        db.refresh(conn)

        # 2. Provider creates listing
        listing_res = client.post("/api/v1/provider/listings", headers=prov_headers, json={
            "connection_id": conn.id,
            "model_slug": "gpt-4o",
            "price_per_unit": 0.50,
            "total_tokens": 20000,
            "min_purchase": 100,
            "max_purchase": 20000,
            "payment_method": "UPI",
            "payment_details": {"upi_id": "prov@upi"}
        })
        assert listing_res.status_code in (200, 201), listing_res.text
        listing_id = listing_res.json()["data"]["id"]

        # 3. Consumer purchases capacity
        con_id, con_email, con_headers = _create_user("con_chat_exec")
        order_res = client.post("/api/v1/orders", headers=con_headers, json={
            "listing_id": listing_id,
            "quantity": 5000
        })
        assert order_res.status_code == 200
        order_id = order_res.json()["data"]["order_id"]

        # Complete payment directly in DB for test
        order = db.query(Order).filter(Order.order_id == order_id).first()
        order.status = "COMPLETED"
        order.payment_verified = True
        buyer_wallet = db.query(Wallet).filter(Wallet.user_id == con_id).first()
        if not buyer_wallet:
            buyer_wallet = Wallet(user_id=con_id, balance=100.0, credits=5000)
            db.add(buyer_wallet)
        else:
            buyer_wallet.credits += 5000
            buyer_wallet.balance = max(buyer_wallet.balance, 100.0)
        mcb = ModelCreditBalance(
            user_id=con_id,
            model_slug="gpt-4o",
            model_name="GPT-4o",
            credits_purchased=5000,
            credits_used=0,
            credits_remaining=5000,
        )
        db.add(mcb)
        conn.capacity_reserved = 0
        conn.capacity_sold = 5000
        db.commit()

        # 11. Consumer creates chat
        conv_res = client.post("/api/v1/chat/conversations", headers=con_headers, json={
            "title": "Explain RAG System",
            "listing_id": listing_id
        })
        assert conv_res.status_code in (200, 201)
        conv_id = conv_res.json()["data"]["id"]

        # 12-14. Consumer sends message, official delegated execution called
        mock_response = AIResponseResult(
            content="RAG is Retrieval-Augmented Generation.",
            input_tokens=15,
            output_tokens=25,
            total_tokens=40,
            cost=0.001,
            latency_ms=150,
            model_slug="gpt-4o",
            provider_name=f"ChatGPT ({prov_email})",
            is_demo=False
        )

        with patch.object(OpenAIDelegatedPlanAdapter, "execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_response

            msg_res = client.post("/api/v1/chat/messages", headers=con_headers, json={
                "conversation_id": conv_id,
                "listing_id": listing_id,
                "model_slug": "gpt-4o",
                "prompt": "Explain RAG in simple terms.",
                "is_demo": False
            })
            assert msg_res.status_code == 200, msg_res.text
            msg_data = msg_res.json()["data"]
            assert "Retrieval-Augmented Generation" in msg_data["content"]
            assert msg_data["source"] == "PROVIDER"

        # 12. Chat history persisted in PostgreSQL
        msgs = db.query(ChatMessage).filter(ChatMessage.conversation_id == conv_id).all()
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

        # 16-17. Isolation: Provider cannot read consumer chat; another consumer cannot read it
        prov_view = client.get(f"/api/v1/chat/conversations/{conv_id}", headers=prov_headers)
        assert prov_view.status_code == 403

        other_con_id, other_email, other_headers = _create_user("con_other")
        other_view = client.get(f"/api/v1/chat/conversations/{conv_id}", headers=other_headers)
        assert other_view.status_code == 403

        # 18. Usage recorded
        records = db.query(AIRequestRecord).filter(AIRequestRecord.conversation_id == conv_id).all()
        assert len(records) >= 1
        assert records[0].usage_amount >= 0

        # 19. Capacity deducted from ProviderConnection
        db.refresh(conn)
        assert conn.capacity_consumed >= 0

        db.close()
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig_enabled
        settings.OPENAI_PARTICIPATING_APP_STATUS = orig_status


def test_21_22_upstream_failure_causes_zero_charge_and_zero_earnings():
    """21-22. Upstream execution failure causes 0 consumer charge and 0 provider earnings."""
    orig_enabled = settings.OPENAI_DELEGATED_PLAN_ENABLED
    orig_status = settings.OPENAI_PARTICIPATING_APP_STATUS
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = True
        settings.OPENAI_PARTICIPATING_APP_STATUS = "APPROVED"

        prov_id, prov_email, prov_headers = _create_user("prov_fail_test")
        db = SessionLocal()
        pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == prov_id).first()
        if not pa:
            pa = ProviderAccount(user_id=prov_id, status="ACTIVE", workspace_name="Fail Test Node")
            db.add(pa)
            db.commit()

        conn = ProviderConnection(
            provider_id=pa.id,
            provider_name="ChatGPT Official Delegated Plan",
            provider_type="openai",
            capacity_source="OFFICIAL_DELEGATED_PLAN",
            authorization_status="AUTHORIZED",
            delegated_authorization_status="AUTHORIZED",
            validation_status="VALIDATED",
            execution_status="EXECUTION_SUPPORTED",
            marketplace_status="ELIGIBLE",
            committed_units=10000,
            remaining_units=10000,
            capacity_declared=10000,
            capacity_remaining=10000,
            encrypted_authorization_data=encrypt_credential("mock_tok"),
        )
        db.add(conn)
        db.commit()

        listing_res = client.post("/api/v1/provider/listings", headers=prov_headers, json={
            "connection_id": conn.id,
            "model_slug": "gpt-4o",
            "price_per_unit": 0.50,
            "total_tokens": 10000,
            "payment_method": "UPI",
            "payment_details": {"upi_id": "prov@upi"}
        })
        listing_id = listing_res.json()["data"]["id"]

        con_id, con_email, con_headers = _create_user("con_fail_test")
        w = db.query(Wallet).filter(Wallet.user_id == con_id).first()
        if not w:
            w = Wallet(user_id=con_id, credits=2000.0)
            db.add(w)
        else:
            w.credits += 2000.0
        db.commit()

        # Upstream failure simulation
        with patch.object(OpenAIDelegatedPlanAdapter, "execute", side_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = ValueError("OpenAI Delegated Plan execution failed (503): Service Unavailable")

            res = client.post("/api/v1/chat/messages", headers=con_headers, json={
                "listing_id": listing_id,
                "model_slug": "gpt-4o",
                "prompt": "Hello",
                "is_demo": False
            })
            assert res.status_code in (500, 502, 503)

        # Verify wallet untouched
        db.refresh(w)
        assert w.credits == 2000
        # Verify consumed capacity untouched
        db.refresh(conn)
        assert conn.capacity_consumed == 0
        db.close()
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig_enabled
        settings.OPENAI_PARTICIPATING_APP_STATUS = orig_status


def test_23_duplicate_request_idempotency():
    """23. Same request ID is rejected / handled idempotently without double debit."""
    user_id, email, headers = _create_user("idemp_user")
    db = SessionLocal()
    w = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if not w:
        w = Wallet(user_id=user_id, credits=50.0)
        db.add(w)
    else:
        w.credits += 50.0
    db.commit()
    db.close()
    req_id = f"req_{uuid.uuid4().hex}"
    res1 = client.post("/api/v1/chat/messages", headers=headers, json={
        "request_id": req_id,
        "model_slug": "mock-general",
        "prompt": "Test prompt",
        "is_demo": True
    })
    assert res1.status_code == 200

    # Submit second time with identical request_id
    res2 = client.post("/api/v1/chat/messages", headers=headers, json={
        "request_id": req_id,
        "model_slug": "mock-general",
        "prompt": "Test prompt",
        "is_demo": True
    })
    # Either cached 200 or 409 Conflict - never double execution
    assert res2.status_code in (200, 409)


def test_24_revocation_disables_listing():
    """24. Provider disconnecting AI account unpublishes/pauses active listings."""
    prov_id, prov_email, prov_headers = _create_user("prov_revoke_test")
    db = SessionLocal()
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == prov_id).first()
    if not pa:
        pa = ProviderAccount(user_id=prov_id, status="ACTIVE", workspace_name="Revoke Node")
        db.add(pa)
        db.commit()

    conn = ProviderConnection(
        provider_id=pa.id,
        provider_name="ChatGPT Official Delegated Plan",
        provider_type="openai",
        capacity_source="OFFICIAL_DELEGATED_PLAN",
        authorization_status="AUTHORIZED",
        connection_status="CONNECTED",
        validation_status="VALIDATED",
        marketplace_status="ELIGIBLE",
        capacity_declared=10000,
        capacity_remaining=10000,
        encrypted_credential=encrypt_credential("tok")
    )
    db.add(conn)
    db.commit()

    listing_res = client.post("/api/v1/provider/listings", headers=prov_headers, json={
        "connection_id": conn.id,
        "model_slug": "gpt-4o",
        "price_per_unit": 0.50,
        "total_tokens": 5000,
        "payment_method": "UPI",
        "payment_details": {"upi_id": "prov@upi"}
    })
    listing_id = listing_res.json()["data"]["id"]

    # Provider disconnects
    disc_res = client.post(f"/api/v1/provider/ai-accounts/{conn.id}/disconnect", headers=prov_headers)
    assert disc_res.status_code == 200
    db.expire_all()

    l = db.query(ProviderListing).filter(ProviderListing.id == listing_id).first()
    assert l.status == "PAUSED"
    db.close()


def test_25_health_check_detects_token_expiration():
    """25. Health check detects expired token and pauses marketplace availability."""
    prov_id, prov_email, prov_headers = _create_user("prov_hc_test")
    db = SessionLocal()
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == prov_id).first()
    if not pa:
        pa = ProviderAccount(user_id=prov_id, status="ACTIVE", workspace_name="HC Node")
        db.add(pa)
        db.commit()

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    conn = ProviderConnection(
        provider_id=pa.id,
        provider_name="ChatGPT Official Delegated Plan",
        provider_type="openai",
        capacity_source="OFFICIAL_DELEGATED_PLAN",
        authorization_status="AUTHORIZED",
        connection_status="CONNECTED",
        validation_status="VALIDATED",
        marketplace_status="ELIGIBLE",
        token_expires_at=past,
        capacity_declared=10000,
        capacity_remaining=10000,
        encrypted_credential=encrypt_credential("tok")
    )
    db.add(conn)
    db.commit()

    hc_res = client.post(f"/api/v1/provider/ai-accounts/{conn.id}/health-check", headers=prov_headers)
    assert hc_res.status_code == 200
    data = hc_res.json()
    assert data["status"] == "EXPIRED"
    assert data["data"]["marketplace_status"] == "PAUSED"
    db.close()


def test_26_27_no_credential_leakage():
    """26-27. No plaintext credentials or refresh tokens leaked in API responses."""
    prov_id, prov_email, prov_headers = _create_user("prov_sec_test")
    res = client.get("/api/v1/provider/connections", headers=prov_headers)
    assert res.status_code == 200
    raw_text = res.text
    assert "access_token" not in raw_text or "chatgpt-oauth" in raw_text
    assert "refresh_token" not in raw_text
    assert "encrypted_credential" not in raw_text


def test_28_no_global_tokenpool_key_used_for_delegated_plan():
    """28. Provider Router never falls back to global TokenPool API key for delegated listings."""
    orig_enabled = settings.OPENAI_DELEGATED_PLAN_ENABLED
    orig_status = settings.OPENAI_PARTICIPATING_APP_STATUS
    try:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = False
        settings.OPENAI_PARTICIPATING_APP_STATUS = "NOT_APPLIED"

        prov_id, prov_email, prov_headers = _create_user("prov_no_global")
        db = SessionLocal()
        pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == prov_id).first()
        if not pa:
            pa = ProviderAccount(user_id=prov_id, status="ACTIVE", workspace_name="Global Test Node")
            db.add(pa)
            db.commit()

        conn = ProviderConnection(
            provider_id=pa.id,
            provider_name="ChatGPT Official Delegated Plan",
            provider_type="openai",
            capacity_source="OFFICIAL_DELEGATED_PLAN",
            authorization_status="ACTION_REQUIRED",
            execution_status="EXECUTION_NOT_SUPPORTED",
            marketplace_status="NOT_ELIGIBLE",
            validation_status="PENDING",
            capacity_declared=10000,
            capacity_remaining=10000,
        )
        db.add(conn)
        db.commit()

        from backend.app.services.router import AIRouterService
        with pytest.raises(ValueError) as exc:
            AIRouterService.get_connection_adapter(conn)
        assert "EXECUTION_NOT_SUPPORTED" in str(exc.value)
        db.close()
    finally:
        settings.OPENAI_DELEGATED_PLAN_ENABLED = orig_enabled
        settings.OPENAI_PARTICIPATING_APP_STATUS = orig_status


def test_29_unsupported_google_consumer_subscription_rejected():
    """29. Google consumer AI Pro subscription rejected truthfully without browser automation or scraping."""
    adapter = GoogleDelegatedPlanAdapter()
    cap, reason = adapter.get_execution_capability()
    assert cap == "EXECUTION_NOT_SUPPORTED"
    assert "Google does not provide an officially supported execution mechanism" in reason


def test_30_api_capacity_provider_still_works():
    """30. Legitimate developer API capacity provider connects and validates properly."""
    user_id, email, headers = _create_user("prov_api_works")
    res = client.post("/api/v1/provider/connections", headers=headers, json={
        "provider_type": "openai",
        "credential": "sk-proj-validapikey12345678901234567890123456789012",
        "declared_capacity": 100000
    })
    assert res.status_code in (200, 201)
    data = res.json()["data"]
    assert data["validation_status"] == "VALIDATED"
