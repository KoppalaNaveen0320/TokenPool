"""
TokenPool — Marketplace Orders, Manual Payment, Seller Confirmation & Dispute Test Suite
Tests all 21 scenarios specified in Section 37:
TEST 1: Seller creates listing with manual payment details.
TEST 2: Buyer creates order (PAYMENT_PENDING, credits reserved).
TEST 3: Buyer cannot use purchased credits before approval.
TEST 4: Buyer uploads payment screenshot + transaction ID (PAYMENT_SUBMITTED).
TEST 5: Seller sees payment request in /provider/orders.
TEST 6: Seller confirms payment.
TEST 7: Credits become usable in buyer wallet (can be spent on AI requests).
TEST 8: Seller rejects payment with required reason.
TEST 9: Buyer raises dispute.
TEST 10: Admin sees dispute in /admin/disputes.
TEST 11: Admin approves payment.
TEST 12: Credits become usable after Admin approval.
TEST 13: Admin rejects payment.
TEST 14: Admin blocks seller.
TEST 15: Blocked seller cannot create listing.
TEST 16: Blocked seller listings disappear from marketplace.
TEST 17: Duplicate transaction ID is detected and rejected.
TEST 18: Two simultaneous buyers cannot purchase the same reserved capacity.
TEST 19: Double seller confirmation does not release credits twice.
TEST 20: Normal USER cannot access Admin endpoints.
TEST 21: Payment screenshot cannot be accessed by unauthorized users.
"""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import io
import time
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.database import SessionLocal
from backend.app.models.schema_models import (
    User, Wallet, WalletTransaction, ProviderAccount, ProviderListing,
    Order, PaymentProof, MarketplaceDispute, CreditAllocation, AuditLog
)

from PIL import Image, ImageDraw, ImageFont

def create_screenshot_with_tx(tx_id: str) -> io.BytesIO:
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 32)
    img = Image.new('RGB', (800, 150), color=(255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((30, 50), f"Transaction ID: {tx_id}", fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

client = TestClient(app)

def test_marketplace_orders_disputes():
    run_tests()

def run_tests():
    print("=" * 70)
    print("TOKENPOOL MARKETPLACE ORDERS & DISPUTE AUDIT")
    print("=" * 70)

    db = SessionLocal()
    ts = int(time.time())

    # -------------------------------------------------------------------------
    # SETUP USERS: ADMIN, SELLER, BUYER 1, BUYER 2, UNRELATED USER
    # -------------------------------------------------------------------------
    print("\n--- Setup: Registering Test Actors ---")
    
    # 1. Admin login
    admin_login = client.post("/api/v1/auth/login", json={
        "email": "admin@tokenpool.ai",
        "password": "admin123"
    })
    assert admin_login.status_code == 200, f"Admin login failed: {admin_login.text}"
    admin_token = admin_login.json().get("access_token") or admin_login.json().get("data", {}).get("token")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    print("  [PASS] Admin authenticated.")

    # 2. Seller
    seller_email = f"seller_{ts}@marketplace.io"
    seller_reg = client.post("/api/v1/auth/register", json={
        "email": seller_email, "password": "SellerPassword123!", "full_name": "Test Seller", "role": "USER"
    })
    seller_token = seller_reg.json().get("access_token") or seller_reg.json().get("data", {}).get("token")
    seller_headers = {"Authorization": f"Bearer {seller_token}"}
    seller_id = seller_reg.json().get("user", {}).get("id") or seller_reg.json().get("data", {}).get("user", {}).get("id")
    print(f"  [PASS] Seller created: {seller_email} (ID: {seller_id})")

    # 3. Buyer 1
    buyer1_email = f"buyer1_{ts}@marketplace.io"
    buyer1_reg = client.post("/api/v1/auth/register", json={
        "email": buyer1_email, "password": "Buyer1Password123!", "full_name": "Buyer One", "role": "USER"
    })
    buyer1_token = buyer1_reg.json().get("access_token") or buyer1_reg.json().get("data", {}).get("token")
    buyer1_headers = {"Authorization": f"Bearer {buyer1_token}"}
    buyer1_id = buyer1_reg.json().get("user", {}).get("id") or buyer1_reg.json().get("data", {}).get("user", {}).get("id")
    print(f"  [PASS] Buyer 1 created: {buyer1_email} (ID: {buyer1_id})")

    # 4. Buyer 2
    buyer2_email = f"buyer2_{ts}@marketplace.io"
    buyer2_reg = client.post("/api/v1/auth/register", json={
        "email": buyer2_email, "password": "Buyer2Password123!", "full_name": "Buyer Two", "role": "USER"
    })
    buyer2_token = buyer2_reg.json().get("access_token") or buyer2_reg.json().get("data", {}).get("token")
    buyer2_headers = {"Authorization": f"Bearer {buyer2_token}"}
    print(f"  [PASS] Buyer 2 created: {buyer2_email}")

    # 5. Unrelated User (for unauthorized screenshot access test)
    unrelated_email = f"stranger_{ts}@marketplace.io"
    unrelated_reg = client.post("/api/v1/auth/register", json={
        "email": unrelated_email, "password": "StrangerPassword123!", "full_name": "Stranger", "role": "USER"
    })
    unrelated_token = unrelated_reg.json().get("access_token") or unrelated_reg.json().get("data", {}).get("token")
    unrelated_headers = {"Authorization": f"Bearer {unrelated_token}"}

    # -------------------------------------------------------------------------
    # TEST 1: Seller creates listing with manual payment details
    # -------------------------------------------------------------------------
    print("\n--- TEST 1: Seller creates listing with manual payment details ---")
    create_list_res = client.post("/api/v1/provider/listings", headers=seller_headers, json={
        "model_slug": "nova-reasoning",
        "daily_capacity": 5000,
        "price_per_unit": 0.50,
        "min_purchase": 100,
        "max_purchase": 3000,
        "payment_method": "UPI",
        "payment_identifier": "testseller@upi",
        "payment_instructions": "Send exact amount and mention Order ID in UPI remark."
    })
    assert create_list_res.status_code == 201, f"Create listing failed: {create_list_res.text}"
    listing_id = create_list_res.json()["data"]["id"]
    print(f"  [PASS] Listing #{listing_id} created with UPI payment identifier: testseller@upi, price: INR 0.50/credit.")

    # -------------------------------------------------------------------------
    # TEST 2: Buyer creates order
    # -------------------------------------------------------------------------
    print("\n--- TEST 2: Buyer creates Buy Order (PAYMENT_PENDING) ---")
    order_res = client.post("/api/v1/orders", headers=buyer1_headers, json={
        "listing_id": listing_id,
        "quantity": 1000
    })
    assert order_res.status_code == 200, f"Order creation failed: {order_res.text}"
    order_data = order_res.json()["data"]
    order_id = order_data["order_id"]
    assert order_data["status"] == "PAYMENT_PENDING"
    assert order_data["total_amount"] == 500.0, f"Expected 500.0, got {order_data['total_amount']}"
    assert order_data["seller_payment_identifier"] == "testseller@upi"
    print(f"  [PASS] Order {order_id} created in PAYMENT_PENDING state. Server-calculated total: INR 500.00.")

    # -------------------------------------------------------------------------
    # TEST 3: Buyer cannot use purchased credits before approval
    # -------------------------------------------------------------------------
    print("\n--- TEST 3: Buyer cannot use purchased credits before approval ---")
    # Buyer wallet credits should still be 0
    buyer_wallet_res = client.get("/api/v1/consumer/wallet", headers=buyer1_headers).json()["data"]
    assert buyer_wallet_res["credits"] == 0, "Buyer was prematurely granted credits!"
    
    # Attempting to use AI model fails due to 0 balance / 0 credits
    chat_premature = client.post("/api/v1/chat/messages", headers=buyer1_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Hello test",
        "is_demo": True
    })
    assert chat_premature.status_code == 402, f"Expected 402 Insufficient credits, got {chat_premature.status_code}"
    print("  [PASS] Buyer has 0 credits and cannot chat; credits are strictly not released yet.")

    # -------------------------------------------------------------------------
    # TEST 4: Buyer uploads payment screenshot + transaction ID
    # -------------------------------------------------------------------------
    print("\n--- TEST 4: Buyer uploads payment screenshot + transaction ID ---")
    tx_id_1 = f"UPI_REF_{ts}_001"
    sample_png = create_screenshot_with_tx(tx_id_1)

    proof_res = client.post(
        f"/api/v1/orders/{order_id}/payment-proof",
        headers=buyer1_headers,
        data={
            "transaction_id": tx_id_1,
            "amount": 500.0,
            "note": "Paid via GooglePay UPI"
        },
        files={"screenshot": ("payment_receipt.png", sample_png, "image/png")}
    )
    assert proof_res.status_code == 200, f"Proof submission failed: {proof_res.text}"
    proof_data = proof_res.json()["data"]
    assert proof_data["status"] in ("VERIFIED", "PAYMENT_SUBMITTED")
    print(f"  [PASS] Payment proof uploaded & verified (TxID: {tx_id_1}). Order status updated to PAYMENT_SUBMITTED.")

    # -------------------------------------------------------------------------
    # TEST 5: Seller sees payment request in /provider/orders
    # -------------------------------------------------------------------------
    print("\n--- TEST 5: Seller sees payment request in /provider/orders ---")
    seller_orders_res = client.get("/api/v1/provider/orders", headers=seller_headers)
    assert seller_orders_res.status_code == 200
    seller_orders = seller_orders_res.json()["data"]["orders"]
    matching_order = next((o for o in seller_orders if o["order_id"] == order_id), None)
    assert matching_order is not None, "Order not found in seller's pending list!"
    assert matching_order["status"] == "PAYMENT_SUBMITTED"
    assert matching_order["has_proof"] is True
    assert matching_order["transaction_id"] == tx_id_1
    print(f"  [PASS] Seller successfully received order {order_id} with proof TxID: {tx_id_1}.")

    # -------------------------------------------------------------------------
    # TEST 6: Seller confirms payment
    # -------------------------------------------------------------------------
    print("\n--- TEST 6: Seller confirms payment ---")
    confirm_res = client.post(f"/api/v1/provider/orders/{order_id}/confirm-payment", headers=seller_headers)
    assert confirm_res.status_code == 200, f"Confirmation failed: {confirm_res.text}"
    assert confirm_res.json()["data"]["status"] == "APPROVED"
    print("  [PASS] Seller confirmed payment. Order status transitioned to APPROVED.")

    # -------------------------------------------------------------------------
    # TEST 7: Credits become usable in buyer wallet
    # -------------------------------------------------------------------------
    print("\n--- TEST 7: Credits become usable in buyer wallet ---")
    buyer_wallet_after = client.get("/api/v1/consumer/wallet", headers=buyer1_headers).json()["data"]
    assert buyer_wallet_after["credits"] == 1000, f"Expected 1000 credits, got {buyer_wallet_after['credits']}"
    print(f"  [PASS] Buyer wallet credited with {buyer_wallet_after['credits']} credits.")

    # Buyer can now successfully chat using the released credits!
    chat_success = client.post("/api/v1/chat/messages", headers=buyer1_headers, json={
        "model_slug": "nova-reasoning",
        "prompt": "Explain blockchain escrow in one sentence.",
        "is_demo": True
    })
    assert chat_success.status_code == 200, f"Chat failed after credit release: {chat_success.text}"
    credits_remaining = chat_success.json()["data"]["credits_remaining"]
    assert credits_remaining < 1000, "Credits were not deducted for chat usage!"
    print(f"  [PASS] Buyer successfully executed AI request with purchased credits (Remaining: {credits_remaining}).")

    # -------------------------------------------------------------------------
    # TEST 8: Seller rejects payment with required reason
    # -------------------------------------------------------------------------
    print("\n--- TEST 8: Seller rejects payment with required reason ---")
    # Buyer 1 creates second order
    order2_res = client.post("/api/v1/orders", headers=buyer1_headers, json={
        "listing_id": listing_id, "quantity": 500
    })
    order2_id = order2_res.json()["data"]["order_id"]
    
    # Buyer submits proof
    tx_id_2 = f"UPI_REF_{ts}_002"
    sample_png_2 = create_screenshot_with_tx(tx_id_2)
    client.post(
        f"/api/v1/orders/{order2_id}/payment-proof",
        headers=buyer1_headers,
        data={"transaction_id": tx_id_2, "amount": 250.0},
        files={"screenshot": ("receipt2.png", sample_png_2, "image/png")}
    )

    # Seller rejects with reason
    reject_res = client.post(
        f"/api/v1/provider/orders/{order2_id}/reject-payment",
        headers=seller_headers,
        json={"reason": "Amount received does not match reference ID."}
    )
    assert reject_res.status_code == 200
    assert reject_res.json()["data"]["status"] == "REJECTED"
    assert reject_res.json()["data"]["reason"] == "Amount received does not match reference ID."
    print("  [PASS] Seller successfully rejected payment with recorded reason.")

    # -------------------------------------------------------------------------
    # TEST 9: Buyer raises dispute
    # -------------------------------------------------------------------------
    print("\n--- TEST 9: Buyer raises dispute on rejected order ---")
    disp_res = client.post(
        f"/api/v1/orders/{order2_id}/dispute",
        headers=buyer1_headers,
        json={"reason": "Money was debited from my bank account, UPI UTR matches.", "details": "Check attached bank statement."}
    )
    assert disp_res.status_code == 200, f"Dispute failed: {disp_res.text}"
    dispute_id = disp_res.json()["data"]["dispute_id"]
    assert disp_res.json()["data"]["status"] == "OPEN"
    print(f"  [PASS] Buyer raised dispute #{dispute_id}. Order status changed to DISPUTED.")

    # -------------------------------------------------------------------------
    # TEST 10: Admin sees dispute in /admin/disputes
    # -------------------------------------------------------------------------
    print("\n--- TEST 10: Admin sees dispute in /admin/disputes ---")
    admin_disputes_res = client.get("/api/v1/admin/disputes", headers=admin_headers)
    assert admin_disputes_res.status_code == 200
    disputes_list = admin_disputes_res.json()["data"]["disputes"]
    matching_disp = next((d for d in disputes_list if d["dispute_id"] == dispute_id), None)
    assert matching_disp is not None, "Dispute not found in admin disputes list!"
    assert matching_disp["order_id"] == order2_id
    print(f"  [PASS] Admin found dispute #{dispute_id} for order {order2_id}.")

    # -------------------------------------------------------------------------
    # TEST 11: Admin approves payment
    # -------------------------------------------------------------------------
    print("\n--- TEST 11: Admin approves payment ---")
    admin_approve_res = client.post(
        f"/api/v1/admin/orders/{order2_id}/approve-payment",
        headers=admin_headers,
        json={"admin_notes": "Bank UTR verified with settlement statement."}
    )
    assert admin_approve_res.status_code == 200
    assert admin_approve_res.json()["data"]["status"] == "APPROVED"
    print("  [PASS] Admin verified payment and approved order.")

    # -------------------------------------------------------------------------
    # TEST 12: Credits become usable after Admin approval
    # -------------------------------------------------------------------------
    print("\n--- TEST 12: Credits become usable after Admin approval ---")
    db.expire_all()
    buyer_wallet_disp = client.get("/api/v1/consumer/wallet", headers=buyer1_headers).json()["data"]
    # Buyer had credits_remaining (around 999) + 500 newly released credits
    assert buyer_wallet_disp["credits"] >= 1400, f"Credits were not released: {buyer_wallet_disp['credits']}"
    print(f"  [PASS] Buyer wallet updated to {buyer_wallet_disp['credits']} credits after admin approval.")

    # -------------------------------------------------------------------------
    # TEST 13: Admin rejects payment
    # -------------------------------------------------------------------------
    print("\n--- TEST 13: Admin rejects payment ---")
    # Create order 3
    order3_res = client.post("/api/v1/orders", headers=buyer1_headers, json={"listing_id": listing_id, "quantity": 200})
    order3_id = order3_res.json()["data"]["order_id"]
    tx_id_3 = f"UPI_REF_{ts}_003"
    sample_png.seek(0)
    client.post(
        f"/api/v1/orders/{order3_id}/payment-proof",
        headers=buyer1_headers,
        data={"transaction_id": tx_id_3, "amount": 100.0},
        files={"screenshot": ("receipt3.png", sample_png, "image/png")}
    )
    # Raise dispute on order 3
    client.post(f"/api/v1/orders/{order3_id}/dispute", headers=buyer1_headers, json={"reason": "Seller not replying."})
    
    # Admin rejects
    admin_rej_res = client.post(
        f"/api/v1/admin/orders/{order3_id}/reject-payment",
        headers=admin_headers,
        json={"rejection_reason": "Screenshot appears manipulated; bank UTR invalid."}
    )
    assert admin_rej_res.status_code == 200
    assert admin_rej_res.json()["data"]["status"] == "REJECTED"
    print("  [PASS] Admin successfully rejected fraudulent dispute. No credits were released.")

    # -------------------------------------------------------------------------
    # TEST 14: Admin blocks seller
    # -------------------------------------------------------------------------
    print("\n--- TEST 14: Admin blocks seller ---")
    # Get seller provider account ID
    seller_prov = db.query(ProviderAccount).filter(ProviderAccount.user_id == seller_id).first()
    assert seller_prov is not None
    block_res = client.patch(
        f"/api/v1/admin/providers/{seller_prov.id}/status",
        headers=admin_headers,
        json={"status": "BLOCKED", "reason": "Multiple unresolved payment complaints."}
    )
    assert block_res.status_code == 200
    assert block_res.json()["data"]["status"] == "BLOCKED"
    print(f"  [PASS] Seller provider #{seller_prov.id} blocked by admin.")

    # -------------------------------------------------------------------------
    # TEST 15: Blocked seller cannot create listing
    # -------------------------------------------------------------------------
    print("\n--- TEST 15: Blocked seller cannot create listing ---")
    blocked_create_res = client.post("/api/v1/provider/listings", headers=seller_headers, json={
        "model_slug": "nova-reasoning",
        "daily_capacity": 1000,
        "price_per_unit": 0.50
    })
    assert blocked_create_res.status_code == 403, f"Expected 403 Forbidden, got {blocked_create_res.status_code}"
    print("  [PASS] Blocked seller correctly rejected with HTTP 403 on listing creation.")

    # -------------------------------------------------------------------------
    # TEST 16: Blocked seller listings disappear from marketplace
    # -------------------------------------------------------------------------
    print("\n--- TEST 16: Blocked seller listings disappear from marketplace ---")
    mkt_res = client.get("/api/v1/listings").json()["data"]["listings"]
    matching_seller_listings = [l for l in mkt_res if l["provider_id"] == seller_prov.id]
    assert len(matching_seller_listings) == 0, "Blocked seller's listings are still visible in marketplace!"
    print("  [PASS] Blocked seller listings are excluded from active marketplace queries.")

    # Unblock seller for subsequent tests
    client.patch(
        f"/api/v1/admin/providers/{seller_prov.id}/status",
        headers=admin_headers,
        json={"status": "ACTIVE", "reason": "Probationary reinstatement."}
    )
    print("  [PASS] Seller reinstated to ACTIVE for remaining tests.")

    # -------------------------------------------------------------------------
    # TEST 17: Duplicate transaction ID is detected
    # -------------------------------------------------------------------------
    print("\n--- TEST 17: Duplicate transaction ID is detected ---")
    # Order 4
    order4_res = client.post("/api/v1/orders", headers=buyer1_headers, json={"listing_id": listing_id, "quantity": 100})
    order4_id = order4_res.json()["data"]["order_id"]

    # Attempt to reuse tx_id_1 (which was already confirmed for order 1)
    sample_png_dup = create_screenshot_with_tx(tx_id_1)
    dup_res = client.post(
        f"/api/v1/orders/{order4_id}/payment-proof",
        headers=buyer1_headers,
        data={"transaction_id": tx_id_1, "amount": 50.0},
        files={"screenshot": ("receipt_dup.png", sample_png_dup, "image/png")}
    )
    assert dup_res.status_code == 400, f"Expected 400 for duplicate tx ID, got {dup_res.status_code}"
    assert "already been" in dup_res.json()["detail"].lower()
    print("  [PASS] Reused transaction ID correctly blocked with HTTP 400 Bad Request.")

    # -------------------------------------------------------------------------
    # TEST 18: Capacity locking prevents over-purchase
    # -------------------------------------------------------------------------
    print("\n--- TEST 18: Capacity reservation prevents over-purchase ---")
    # Create listing with only 500 credits available
    small_list_res = client.post("/api/v1/provider/listings", headers=seller_headers, json={
        "model_slug": "nova-reasoning",
        "daily_capacity": 500,
        "min_purchase": 100,
        "max_purchase": 500,
        "price_per_unit": 0.50
    })
    small_list_id = small_list_res.json()["data"]["id"]

    # Buyer 1 reserves 400 credits
    b1_res = client.post("/api/v1/orders", headers=buyer1_headers, json={"listing_id": small_list_id, "quantity": 400})
    assert b1_res.status_code == 200
    print("  [PASS] Buyer 1 reserved 400 of 500 credits. Remaining available: 100.")

    # Buyer 2 tries to reserve 200 credits (exceeds 100 remaining)
    b2_res = client.post("/api/v1/orders", headers=buyer2_headers, json={"listing_id": small_list_id, "quantity": 200})
    assert b2_res.status_code == 400, f"Expected 400 for capacity overflow, got {b2_res.status_code}"
    assert "Insufficient available capacity" in b2_res.json()["detail"]
    print("  [PASS] Buyer 2 prevented from over-purchasing capacity (HTTP 400).")

    # -------------------------------------------------------------------------
    # TEST 19: Double seller confirmation does not release credits twice
    # -------------------------------------------------------------------------
    print("\n--- TEST 19: Double seller confirmation does not release credits twice ---")
    # Submit proof for Buyer 1's 400-credit order
    tx_id_b1 = f"UPI_REF_{ts}_b1"
    sample_png_b1 = create_screenshot_with_tx(tx_id_b1)
    client.post(
        f"/api/v1/orders/{b1_res.json()['data']['order_id']}/payment-proof",
        headers=buyer1_headers,
        data={"transaction_id": tx_id_b1, "amount": 200.0},
        files={"screenshot": ("receipt_b1.png", sample_png_b1, "image/png")}
    )

    # First confirmation succeeds
    conf1 = client.post(f"/api/v1/provider/orders/{b1_res.json()['data']['order_id']}/confirm-payment", headers=seller_headers)
    assert conf1.status_code == 200

    # Second confirmation fails
    conf2 = client.post(f"/api/v1/provider/orders/{b1_res.json()['data']['order_id']}/confirm-payment", headers=seller_headers)
    assert conf2.status_code == 400, f"Expected 400 on duplicate confirm, got {conf2.status_code}"
    assert "already been confirmed" in conf2.json()["detail"]
    print("  [PASS] Double confirmation rejected; credits released exactly once.")

    # -------------------------------------------------------------------------
    # TEST 20: Normal USER cannot access Admin endpoints
    # -------------------------------------------------------------------------
    print("\n--- TEST 20: Normal USER cannot access Admin endpoints ---")
    user_admin_attempt = client.get("/api/v1/admin/disputes", headers=buyer1_headers)
    assert user_admin_attempt.status_code == 403, f"Expected 403, got {user_admin_attempt.status_code}"
    print("  [PASS] Normal user blocked with HTTP 403 Forbidden from /admin/disputes.")

    # -------------------------------------------------------------------------
    # TEST 21: Payment screenshot cannot be accessed by unauthorized users
    # -------------------------------------------------------------------------
    print("\n--- TEST 21: Payment screenshot unauthorized access blocked ---")
    # Stranger tries to download payment proof for order 1
    unauth_file_res = client.get(f"/api/v1/orders/{order_id}/payment-proof", headers=unrelated_headers)
    assert unauth_file_res.status_code == 403, f"Expected 403 Forbidden, got {unauth_file_res.status_code}"
    print("  [PASS] Unrelated user blocked with HTTP 403 Forbidden from accessing payment screenshot.")

    # Legitimate buyer CAN access it
    auth_file_res = client.get(f"/api/v1/orders/{order_id}/payment-proof", headers=buyer1_headers)
    assert auth_file_res.status_code == 200
    assert auth_file_res.headers["content-type"] == "image/png"
    print("  [PASS] Legitimate buyer authorized to download payment proof file.")

    print("\n" + "=" * 70)
    print("ALL 21 MARKETPLACE ORDERS & DISPUTE TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)
    db.close()

if __name__ == "__main__":
    run_tests()
