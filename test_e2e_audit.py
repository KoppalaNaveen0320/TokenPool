import os
import sys
import unittest
from fastapi.testclient import TestClient

# Ensure backend can import correctly
sys.path.insert(0, os.path.abspath("."))

from backend.app.core.config import settings
from backend.app.core.database import SessionLocal, engine
from backend.app.main import app
from backend.app.models.schema_models import *

client = TestClient(app)

def run_tests():
    print("=" * 60)
    print("RUNNING TOKENPOOL COMPREHENSIVE END-TO-END AUDIT SUITE")
    print(f"Database URL: {settings.DATABASE_URL}")
    print("=" * 60)

    # ---------------------------------------------------------
    # TEST 1: AUTHENTICATION AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 1: AUTHENTICATION & USER REGISTRATION AUDIT ---")
    test_email = f"audit_user_{os.getpid()}@example.com"
    test_password = "SecurePassword123!"
    
    # 1. Register new user
    reg_res = client.post("/api/v1/auth/register", json={
        "email": test_email,
        "password": test_password,
        "full_name": "Audit Test User"
    })
    assert reg_res.status_code == 201, f"Register failed: {reg_res.text}"
    reg_data = reg_res.json()
    assert "access_token" in reg_data, "No access token in registration response"
    assert reg_data["user"]["role"] == "USER", f"Expected USER role, got {reg_data['user']['role']}"
    user_id = reg_data["user"]["id"]
    print("  [PASS] User registration succeeded (role=USER, no consumer/provider prompt)")

    # Verify user stored in PostgreSQL with hashed password
    db = SessionLocal()
    db_user = db.query(User).filter(User.email == test_email).first()
    assert db_user is not None, "User not found in PostgreSQL!"
    assert db_user.hashed_password != test_password, "Password is not hashed!"
    assert db_user.hashed_password.startswith("$2b$") or len(db_user.hashed_password) > 20, "Invalid hash format"
    # Verify wallet created with 0 balance
    db_wallet = db.query(Wallet).filter(Wallet.user_id == db_user.id).first()
    assert db_wallet is not None, "Wallet not created in PostgreSQL!"
    assert db_wallet.balance == 0.0, f"Expected 0.0 balance, got {db_wallet.balance}"
    assert db_wallet.credits == 0, f"Expected 0 credits, got {db_wallet.credits}"
    db.close()
    print("  [PASS] User and wallet verified in PostgreSQL database (hashed password & 0 starting balance)")

    # 2. Reject duplicate email
    dup_res = client.post("/api/v1/auth/register", json={
        "email": test_email,
        "password": test_password,
        "full_name": "Duplicate User"
    })
    assert dup_res.status_code == 400, f"Expected 400 for duplicate email, got {dup_res.status_code}"
    print("  [PASS] Duplicate registration rejected with 400")

    # 3. Reject invalid login
    bad_login = client.post("/api/v1/auth/login", json={
        "email": test_email,
        "password": "WrongPassword!"
    })
    assert bad_login.status_code == 401, f"Expected 401 for wrong password, got {bad_login.status_code}"
    print("  [PASS] Invalid login rejected with 401")

    # 4. Successful login
    login_res = client.post("/api/v1/auth/login", json={
        "email": test_email,
        "password": test_password
    })
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    token = login_res.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {token}"}
    print("  [PASS] Login successful and returned JWT Bearer token")

    # 5. Current user /auth/me
    me_res = client.get("/api/v1/auth/me", headers=user_headers)
    assert me_res.status_code == 200, f"Get me failed: {me_res.text}"
    me_data = me_res.json()
    assert me_data["email"] == test_email
    assert me_data["wallet"]["balance"] == 0.0
    print("  [PASS] /api/v1/auth/me returns authenticated user details from PostgreSQL")

    # ---------------------------------------------------------
    # TEST 2 & 3: ADMIN SECURITY & RBAC AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 2: RBAC - NORMAL USER BLOCKED FROM ADMIN APIS ---")
    admin_endpoints = [
        "/api/v1/admin/users",
        "/api/v1/admin/providers",
        "/api/v1/admin/transactions",
        "/api/v1/admin/listings",
        "/api/v1/admin/overview",
    ]
    for ep in admin_endpoints:
        res = client.get(ep, headers=user_headers)
        assert res.status_code == 403, f"Expected 403 on {ep} for normal user, got {res.status_code}"
        print(f"  [PASS] Normal user blocked with 403 Forbidden on {ep}")

    print("\n--- TEST 3: RBAC - ADMIN USER AUTHORIZED ---")
    admin_login = client.post("/api/v1/auth/login", json={
        "email": "admin@tokenpool.ai",
        "password": "admin123"
    })
    assert admin_login.status_code == 200, f"Admin login failed: {admin_login.text}"
    admin_token = admin_login.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    
    for ep in admin_endpoints:
        res = client.get(ep, headers=admin_headers)
        assert res.status_code == 200, f"Expected 200 on {ep} for admin user, got {res.status_code}"
        print(f"  [PASS] Admin user authorized with 200 OK on {ep}")

    # ---------------------------------------------------------
    # TEST 4: PROVIDER AUDIT (LISTING LIFECYCLE)
    # ---------------------------------------------------------
    print("\n--- TEST 4: PROVIDER LISTING LIFECYCLE (CREATE -> GET -> EDIT -> PAUSE -> RESUME -> DELETE) ---")
    # User creates listing
    create_listing_res = client.post("/api/v1/provider/listings", headers=user_headers, json={
        "model_slug": "nova-reasoning",
        "price_per_1k_input": 0.10,
        "price_per_1k_output": 0.20,
        "daily_capacity": 50000,
        "max_context": 128000
    })
    assert create_listing_res.status_code == 201, f"Create listing failed: {create_listing_res.text}"
    listing_id = create_listing_res.json()["data"]["id"]
    print(f"  [PASS] Provider listing created (ID: {listing_id})")

    # Verify listing in PostgreSQL
    db = SessionLocal()
    db_listing = db.query(ProviderListing).filter(ProviderListing.id == listing_id).first()
    assert db_listing is not None, "Listing not found in PostgreSQL!"
    assert db_listing.status == "ACTIVE"
    assert db_listing.price_input == 0.10
    db.close()
    print("  [PASS] Listing verified in PostgreSQL database table provider_listings")

    # Verify listing appears in marketplace /api/v1/listings
    public_listings = client.get("/api/v1/listings?search=nova")
    assert public_listings.status_code == 200
    l_list = public_listings.json()["data"]["listings"]
    assert any(l["id"] == listing_id for l in l_list), "Created listing not found in public listings"
    print("  [PASS] Listing appears in GET /api/v1/listings with search & filtering")

    # Edit listing
    edit_res = client.put(f"/api/v1/provider/listings/{listing_id}", headers=user_headers, json={
        "price_per_1k_input": 0.12,
        "daily_capacity": 75000
    })
    assert edit_res.status_code == 200, f"Edit listing failed: {edit_res.text}"
    print("  [PASS] Listing edited successfully via PUT /api/v1/provider/listings/{id}")

    # Pause listing
    pause_res = client.patch(f"/api/v1/provider/listings/{listing_id}/status?new_status=PAUSED", headers=user_headers)
    assert pause_res.status_code == 200
    assert pause_res.json()["data"]["status"] == "PAUSED"
    print("  [PASS] Listing paused via PATCH /api/v1/provider/listings/{id}/status")

    # Inactive listing test: does not appear in active marketplace
    act_listings = client.get("/api/v1/listings?status=ACTIVE")
    assert all(l["id"] != listing_id for l in act_listings.json()["data"]["listings"]), "Paused listing should not be in ACTIVE listings"
    print("  [PASS] Paused listing is excluded from active marketplace filter")

    # Resume listing
    resume_res = client.patch(f"/api/v1/provider/listings/{listing_id}/status?new_status=ACTIVE", headers=user_headers)
    assert resume_res.status_code == 200
    assert resume_res.json()["data"]["status"] == "ACTIVE"
    print("  [PASS] Listing resumed to ACTIVE")

    # Delete listing
    del_res = client.delete(f"/api/v1/provider/listings/{listing_id}", headers=user_headers)
    assert del_res.status_code == 200
    db = SessionLocal()
    db_listing = db.query(ProviderListing).filter(ProviderListing.id == listing_id).first()
    assert db_listing.status == "DELETED", f"Expected DELETED status, got {db_listing.status}"
    db.close()
    print("  [PASS] Listing deleted (soft delete in PostgreSQL with status=DELETED)")

    # ---------------------------------------------------------
    # TEST 5: WALLET AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 5: WALLET AUDIT & TOP-UP ---")
    wallet_res = client.get("/api/v1/consumer/wallet", headers=user_headers)
    assert wallet_res.status_code == 200
    w_data = wallet_res.json()["data"]
    assert w_data["balance"] == 0.0
    print("  [PASS] Initial wallet balance is INR 0.00 from PostgreSQL")

    # Top up wallet
    topup_res = client.post("/api/v1/consumer/wallet/topup", headers=user_headers, json={
        "amount": 200.0,
        "payment_method": "UPI"
    })
    assert topup_res.status_code == 200
    topup_data = topup_res.json()["data"]
    assert topup_data["balance"] == 200.0
    assert topup_data["credits"] > 0
    print(f"  [PASS] Wallet top-up succeeded. New balance: INR {topup_data['balance']}, Credits: {topup_data['credits']}")

    # Verify wallet transaction in PostgreSQL
    db = SessionLocal()
    topup_tx = db.query(WalletTransaction).filter(
        WalletTransaction.tx_type == "TOP_UP",
        WalletTransaction.amount == 200.0
    ).first()
    assert topup_tx is not None, "TOP_UP transaction not found in PostgreSQL!"
    assert topup_tx.status == "COMPLETED"
    db.close()
    print("  [PASS] TOP_UP WalletTransaction record verified in PostgreSQL")

    # ---------------------------------------------------------
    # TEST 6 & 11: AI CHAT & CREDIT DEDUCTION AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 6: AI CHAT REQUEST & CREDIT DEDUCTION AUDIT ---")
    # Check balance before chat
    wallet_before = client.get("/api/v1/consumer/wallet", headers=user_headers).json()["data"]
    bal_before = wallet_before["balance"]
    credits_before = wallet_before["credits"]

    # Send chat message
    chat_res = client.post("/api/v1/chat/messages", headers=user_headers, json={
        "model_slug": "swift-chat",
        "prompt": "How does capacity routing work on TokenPool?",
        "is_demo": True
    })
    assert chat_res.status_code == 200, f"Chat failed: {chat_res.text}"
    chat_data = chat_res.json()["data"]
    assert "response" in chat_data
    assert chat_data["is_demo"] is True
    assert "DEMO MODE" in chat_data["provider"]
    cost = chat_data["cost"]
    print(f"  [PASS] AI Chat returned response from {chat_data['provider']} (tokens: {chat_data['usage']['total_tokens']}, cost: INR {cost})")

    # Check balance after chat
    wallet_after = client.get("/api/v1/consumer/wallet", headers=user_headers).json()["data"]
    bal_after = wallet_after["balance"]
    credits_after = wallet_after["credits"]
    assert credits_after < credits_before or bal_after < bal_before, "Credits or balance were not deducted!"
    print(f"  [PASS] Credits deducted: {credits_before} -> {credits_after}")

    # Verify usage record in PostgreSQL
    db = SessionLocal()
    req_record = db.query(AIRequestRecord).filter(AIRequestRecord.user_id == user_id).first()
    assert req_record is not None, "AIRequestRecord not stored in PostgreSQL!"
    assert req_record.status == "COMPLETED"
    assert req_record.consumer_cost > 0
    # Verify usage transaction in PostgreSQL
    usage_tx = db.query(WalletTransaction).filter(
        WalletTransaction.tx_type == "USAGE",
        WalletTransaction.wallet_id == db_wallet.id
    ).first()
    assert usage_tx is not None, "USAGE WalletTransaction not recorded in PostgreSQL!"
    db.close()
    print("  [PASS] AIRequestRecord & USAGE WalletTransaction verified in PostgreSQL")

    # ---------------------------------------------------------
    # TEST 7: TRANSACTIONS API AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 7: TRANSACTIONS API AUDIT ---")
    tx_res = client.get("/api/v1/consumer/transactions", headers=user_headers)
    assert tx_res.status_code == 200
    tx_list = tx_res.json()["data"]["transactions"]
    assert len(tx_list) >= 2, f"Expected at least 2 transactions (TOP_UP + USAGE), got {len(tx_list)}"
    types = [t["type"] for t in tx_list]
    assert "TOP_UP" in types
    assert "USAGE" in types
    print(f"  [PASS] GET /api/v1/consumer/transactions returned real transaction types: {types}")

    # ---------------------------------------------------------
    # TEST 8: NOTIFICATIONS AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 8: NOTIFICATIONS API AUDIT ---")
    notif_res = client.get("/api/v1/notifications", headers=user_headers)
    assert notif_res.status_code == 200
    notifs = notif_res.json()["data"]
    assert len(notifs) > 0, "No notifications found for user"
    first_notif = notifs[0]
    notif_id = first_notif["id"]

    # Mark single notification as read
    read_one = client.patch(f"/api/v1/notifications/{notif_id}/read", headers=user_headers)
    assert read_one.status_code == 200
    assert read_one.json()["success"] is True

    # Mark all as read
    read_all = client.patch("/api/v1/notifications/read-all", headers=user_headers)
    assert read_all.status_code == 200
    assert read_all.json()["success"] is True

    # Verify in PostgreSQL
    db = SessionLocal()
    unread_db = db.query(Notification).filter(Notification.user_id == user_id, Notification.is_read == False).count()
    assert unread_db == 0, "Unread notifications still exist in PostgreSQL!"
    db.close()
    print("  [PASS] Notifications mark read and read-all verified in PostgreSQL")

    # ---------------------------------------------------------
    # TEST 9: SUPPORT TICKETS AUDIT
    # ---------------------------------------------------------
    print("\n--- TEST 9: SUPPORT TICKET CREATION AUDIT ---")
    ticket_res = client.post("/api/v1/support/tickets", headers=user_headers, json={
        "subject": "Audit Verification Ticket",
        "category": "Billing",
        "message": "Testing real PostgreSQL database persistence for support tickets."
    })
    assert ticket_res.status_code == 201, f"Ticket creation failed: {ticket_res.text}"
    ticket_id = ticket_res.json()["data"]["id"]

    # Verify ticket in PostgreSQL
    db = SessionLocal()
    db_ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    assert db_ticket is not None, "SupportTicket not found in PostgreSQL!"
    assert db_ticket.subject == "Audit Verification Ticket"
    assert db_ticket.status == "OPEN"
    db.close()

    # Verify ticket in GET /api/v1/support/tickets
    get_tickets = client.get("/api/v1/support/tickets", headers=user_headers)
    assert get_tickets.status_code == 200
    ticket_list = get_tickets.json()["data"]
    assert any(t["id"] == ticket_id for t in ticket_list)
    print(f"  [PASS] Support ticket #{ticket_id} created and verified in PostgreSQL")

    # ---------------------------------------------------------
    # TEST 10: INSUFFICIENT CREDITS REJECTION
    # ---------------------------------------------------------
    print("\n--- TEST 10: INSUFFICIENT CREDITS REJECTION AUDIT ---")
    # Register a broke user with 0 balance
    broke_email = f"broke_{os.getpid()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": broke_email, "password": "Password123!", "full_name": "Zero Balance User"
    })
    broke_login = client.post("/api/v1/auth/login", json={"email": broke_email, "password": "Password123!"})
    broke_headers = {"Authorization": f"Bearer {broke_login.json()['access_token']}"}

    # Attempt AI chat with 0 credits and 0 balance
    reject_chat = client.post("/api/v1/chat/messages", headers=broke_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "This request should be rejected due to no credits.",
        "is_demo": True
    })
    assert reject_chat.status_code == 402, f"Expected 402 Insufficient Balance, got {reject_chat.status_code}: {reject_chat.text}"
    print("  [PASS] Request with zero balance and zero credits correctly rejected with 402 Payment Required")

    print("\n" + "=" * 60)
    print("ALL 10 END-TO-END AUDIT TESTS PASSED SUCCESSFULLY ON POSTGRESQL!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
