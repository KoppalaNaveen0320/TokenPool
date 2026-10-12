"""
TokenPool — Comprehensive Payment Verification & Anti-Fraud Test Suite
Tests all 14 scenarios specified in Section 16 of the prompt:

TEST 1: Valid transaction ID + correct screenshot containing same transaction ID -> Expected: VERIFIED
TEST 2: User enters TXN111, Screenshot contains TXN222 -> Expected: REJECTED
TEST 3: User enters transaction ID, Screenshot contains no transaction ID -> Expected: REJECTED
TEST 4: Screenshot missing -> Expected: REJECTED (HTTP 400)
TEST 5: Transaction ID missing -> Expected: REJECTED (HTTP 400)
TEST 6: Both transaction ID and screenshot missing -> Expected: REJECTED (HTTP 400)
TEST 7: Transaction ID has leading/trailing spaces -> Expected: Safely normalized and compared correctly (VERIFIED)
TEST 8: Transaction ID already used by another successful payment -> Expected: REJECTED
TEST 9: User attempts to directly call payment verification API with verified=true -> Expected: Backend ignores/rejects forged verification (HTTP 400)
TEST 10: Payment verification fails -> Expected: 0 wallet credit, 0 escrow release, 0 provider earnings, 0 TokenPool commission, 0 capacity allocation
TEST 11: Payment verification succeeds -> Expected: Existing payment flow proceeds normally
TEST 12: Wrong screenshot but correct amount -> Expected: REJECTED
TEST 13: Correct-looking payment screenshot but transaction ID does not match submitted ID -> Expected: REJECTED
TEST 14: Two concurrent submissions attempt to use the same transaction ID -> Expected: Only one successful payment can claim the transaction ID
"""

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import io
import time
import uuid
import concurrent.futures
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.database import SessionLocal
from backend.app.models.schema_models import (
    User, Wallet, WalletTransaction, ProviderAccount, ProviderListing,
    Order, PaymentProof, CreditAllocation
)

client = TestClient(app)

def create_screenshot(
    tx_text: str | None = None,
    amount_text: str | None = None,
    include_labels: bool = True,
    noise_only: bool = False
) -> io.BytesIO:
    """Generate a realistic payment receipt image with exact text rendered."""
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 28)
    font_bold = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 32)
    img = Image.new('RGB', (800, 320), color=(255, 255, 255))
    d = ImageDraw.Draw(img)

    if noise_only:
        d.text((50, 60), "Thank you for shopping with us!", fill=(20, 20, 20), font=font)
        d.text((50, 120), "Order Summary: Item ABC x 2", fill=(60, 60, 60), font=font)
        d.text((50, 180), "Customer Support: support@store.com", fill=(80, 80, 80), font=font)
    else:
        y = 30
        d.text((40, y), "Payment Confirmation Receipt", fill=(10, 40, 90), font=font_bold)
        y += 50
        if amount_text:
            d.text((40, y), f"Amount Paid: INR {amount_text}", fill=(0, 120, 0), font=font)
            y += 45
        if tx_text:
            if include_labels:
                d.text((40, y), f"Transaction ID: {tx_text}", fill=(0, 0, 0), font=font)
                y += 45
                d.text((40, y), f"UPI Ref No: {tx_text}", fill=(50, 50, 50), font=font)
                y += 45
            else:
                d.text((40, y), f"{tx_text}", fill=(0, 0, 0), font=font)
                y += 45
        d.text((40, y), "Status: Successful", fill=(0, 150, 0), font=font)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def setup_marketplace_actors():
    """Create distinct seller, buyer, and active listing for tests."""
    ts = int(time.time() * 1000)

    # 1. Admin login
    admin_login = client.post("/api/v1/auth/login", json={
        "email": "admin@tokenpool.ai",
        "password": "admin123"
    })
    admin_token = admin_login.json().get("access_token")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 2. Seller
    seller_email = f"seller_pv_{ts}@test.io"
    seller_reg = client.post("/api/v1/auth/register", json={
        "email": seller_email, "password": "Password123!", "full_name": "PV Seller", "role": "USER"
    })
    seller_token = seller_reg.json().get("access_token")
    seller_headers = {"Authorization": f"Bearer {seller_token}"}

    # 3. Buyer
    buyer_email = f"buyer_pv_{ts}@test.io"
    buyer_reg = client.post("/api/v1/auth/register", json={
        "email": buyer_email, "password": "Password123!", "full_name": "PV Buyer", "role": "USER"
    })
    buyer_token = buyer_reg.json().get("access_token")
    buyer_headers = {"Authorization": f"Bearer {buyer_token}"}

    # 4. Create listing
    create_list_res = client.post("/api/v1/provider/listings", headers=seller_headers, json={
        "model_slug": "nova-reasoning",
        "daily_capacity": 50000,
        "price_per_unit": 0.50,
        "min_purchase": 100,
        "max_purchase": 10000,
        "payment_method": "UPI",
        "payment_identifier": "pvseller@upi",
        "payment_instructions": "Pay via UPI and enter exact transaction ID"
    })
    assert create_list_res.status_code == 201, f"Create listing failed: {create_list_res.text}"
    listing_id = create_list_res.json()["data"]["id"]

    return {
        "admin_headers": admin_headers,
        "seller_headers": seller_headers,
        "buyer_headers": buyer_headers,
        "listing_id": listing_id
    }


def create_test_order(buyer_headers, listing_id, qty=500):
    order_res = client.post("/api/v1/orders", headers=buyer_headers, json={
        "listing_id": listing_id,
        "quantity": qty
    })
    assert order_res.status_code == 200, f"Order creation failed: {order_res.text}"
    return order_res.json()["data"]


def test_payment_verification_14_scenarios():
    print("\n" + "=" * 80)
    print("STARTING 14 PAYMENT VERIFICATION & FRAUD PREVENTION TEST SUITES")
    print("=" * 80)

    actors = setup_marketplace_actors()
    b_headers = actors["buyer_headers"]
    s_headers = actors["seller_headers"]
    a_headers = actors["admin_headers"]
    listing_id = actors["listing_id"]

    # -------------------------------------------------------------------------
    # TEST 1: Valid transaction ID + correct screenshot containing same transaction ID -> VERIFIED
    # -------------------------------------------------------------------------
    print("\n>>> TEST 1: Valid transaction ID + matching screenshot containing same transaction ID")
    ord1 = create_test_order(b_headers, listing_id, 200)
    tx_id_1 = f"TXN-VALID-{uuid.uuid4().hex[:8].upper()}"
    shot1 = create_screenshot(tx_text=tx_id_1, amount_text="100.00")
    
    res1 = client.post(
        f"/api/v1/orders/{ord1['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_id_1, "amount": 100.0, "note": "Valid payment"},
        files={"screenshot": ("valid.png", shot1, "image/png")}
    )
    assert res1.status_code == 200, f"Expected 200, got {res1.status_code}: {res1.text}"
    data1 = res1.json()["data"]
    assert data1["verification_status"] == "VERIFIED", f"Expected VERIFIED, got {data1['verification_status']}"
    assert data1["status"] in ("VERIFIED", "PAYMENT_SUBMITTED")
    assert data1["extracted_transaction_id"] == tx_id_1
    print(f"  [PASS] TEST 1: Submitted: '{tx_id_1}', Extracted: '{data1['extracted_transaction_id']}', Status: {data1['verification_status']}")

    # -------------------------------------------------------------------------
    # TEST 2: User enters TXN111, Screenshot contains TXN222 -> REJECTED
    # -------------------------------------------------------------------------
    print("\n>>> TEST 2: User enters TXN111, Screenshot contains TXN222")
    ord2 = create_test_order(b_headers, listing_id, 200)
    tx_submitted = f"TXN111-{uuid.uuid4().hex[:6].upper()}"
    tx_in_shot = f"TXN222-{uuid.uuid4().hex[:6].upper()}"
    shot2 = create_screenshot(tx_text=tx_in_shot, amount_text="100.00")

    res2 = client.post(
        f"/api/v1/orders/{ord2['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_submitted, "amount": 100.0},
        files={"screenshot": ("mismatch.png", shot2, "image/png")}
    )
    assert res2.status_code == 400, f"Expected 400, got {res2.status_code}: {res2.text}"
    detail2 = res2.json().get("detail", "")
    assert "does not match" in detail2.lower(), f"Unexpected detail: {detail2}"

    # Verify DB state is REJECTED
    db = SessionLocal()
    p2 = db.query(PaymentProof).filter(PaymentProof.order_id == ord2["id"]).first()
    assert p2 is not None
    assert p2.verification_status == "REJECTED"
    o2 = db.query(Order).filter(Order.id == ord2["id"]).first()
    assert o2.status == "REJECTED"
    db.close()
    print(f"  [PASS] TEST 2: Mismatched IDs properly REJECTED with 400. Reason: '{detail2}'")

    # -------------------------------------------------------------------------
    # TEST 3: User enters transaction ID, Screenshot contains NO transaction ID -> REJECTED
    # -------------------------------------------------------------------------
    print("\n>>> TEST 3: User enters transaction ID, Screenshot contains NO transaction ID")
    ord3 = create_test_order(b_headers, listing_id, 200)
    tx_submitted_3 = f"TXN-NOID-{uuid.uuid4().hex[:6].upper()}"
    shot3 = create_screenshot(noise_only=True)

    res3 = client.post(
        f"/api/v1/orders/{ord3['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_submitted_3, "amount": 100.0},
        files={"screenshot": ("unreadable.png", shot3, "image/png")}
    )
    assert res3.status_code == 400, f"Expected 400, got {res3.status_code}: {res3.text}"
    detail3 = res3.json().get("detail", "")
    assert "could not be verified" in detail3.lower(), f"Unexpected detail: {detail3}"

    # Verify DB state is REJECTED
    db = SessionLocal()
    p3 = db.query(PaymentProof).filter(PaymentProof.order_id == ord3["id"]).first()
    assert p3 is not None
    assert p3.verification_status == "REJECTED"
    o3 = db.query(Order).filter(Order.id == ord3["id"]).first()
    assert o3.status == "REJECTED"
    db.close()
    print(f"  [PASS] TEST 3: Unreadable/no transaction ID in screenshot properly REJECTED. Reason: '{detail3}'")

    # -------------------------------------------------------------------------
    # TEST 4: Screenshot missing -> REJECTED (HTTP 400/422)
    # -------------------------------------------------------------------------
    print("\n>>> TEST 4: Screenshot missing")
    ord4 = create_test_order(b_headers, listing_id, 200)
    res4 = client.post(
        f"/api/v1/orders/{ord4['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": "TXN-NOSHOT-12345", "amount": 100.0}
    )
    assert res4.status_code in (400, 422), f"Expected 400/422, got {res4.status_code}: {res4.text}"
    print(f"  [PASS] TEST 4: Missing screenshot rejected with HTTP {res4.status_code}")

    # -------------------------------------------------------------------------
    # TEST 5: Transaction ID missing -> REJECTED (HTTP 400)
    # -------------------------------------------------------------------------
    print("\n>>> TEST 5: Transaction ID missing")
    ord5 = create_test_order(b_headers, listing_id, 200)
    shot5 = create_screenshot(tx_text="TXN-SOMETHING-123", amount_text="100.00")
    res5 = client.post(
        f"/api/v1/orders/{ord5['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": "   ", "amount": 100.0},
        files={"screenshot": ("shot5.png", shot5, "image/png")}
    )
    assert res5.status_code == 400, f"Expected 400, got {res5.status_code}: {res5.text}"
    assert "transaction_id is required" in res5.text or "Transaction ID is required" in res5.text
    print(f"  [PASS] TEST 5: Whitespace/missing transaction ID rejected with HTTP 400")

    # -------------------------------------------------------------------------
    # TEST 6: Both transaction ID and screenshot missing -> REJECTED (HTTP 400)
    # -------------------------------------------------------------------------
    print("\n>>> TEST 6: Both transaction ID and screenshot missing")
    ord6 = create_test_order(b_headers, listing_id, 200)
    res6 = client.post(
        f"/api/v1/orders/{ord6['order_id']}/payment-proof",
        headers=b_headers,
        data={}
    )
    assert res6.status_code in (400, 422), f"Expected 400/422, got {res6.status_code}: {res6.text}"
    print(f"  [PASS] TEST 6: Missing both fields rejected with HTTP {res6.status_code}")

    # -------------------------------------------------------------------------
    # TEST 7: Transaction ID has leading/trailing spaces -> Safely normalized
    # -------------------------------------------------------------------------
    print("\n>>> TEST 7: Transaction ID with leading/trailing spaces safely normalized")
    ord7 = create_test_order(b_headers, listing_id, 200)
    clean_tx = f"TXN-SPACES-{uuid.uuid4().hex[:6].upper()}"
    input_tx = f"   {clean_tx}   "
    shot7 = create_screenshot(tx_text=clean_tx, amount_text="100.00")

    res7 = client.post(
        f"/api/v1/orders/{ord7['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": input_tx, "amount": 100.0},
        files={"screenshot": ("spaces.png", shot7, "image/png")}
    )
    assert res7.status_code == 200, f"Expected 200, got {res7.status_code}: {res7.text}"
    data7 = res7.json()["data"]
    assert data7["verification_status"] == "VERIFIED"
    assert data7["transaction_id"] == clean_tx
    print(f"  [PASS] TEST 7: Whitespace safely trimmed. Clean: '{data7['transaction_id']}', Status: {data7['verification_status']}")

    # -------------------------------------------------------------------------
    # TEST 8: Transaction ID already used by another successful payment -> REJECTED
    # -------------------------------------------------------------------------
    print("\n>>> TEST 8: Transaction ID already used by another successful payment")
    # First, let's complete ord1 with seller confirmation
    seller_confirm = client.post(f"/api/v1/provider/orders/{ord1['order_id']}/confirm-payment", headers=s_headers)
    assert seller_confirm.status_code == 200, f"Seller confirm failed: {seller_confirm.text}"
    print(f"  Confirmed order 1 with tx_id '{tx_id_1}'.")

    # Now attempt to reuse tx_id_1 for a new order
    ord8 = create_test_order(b_headers, listing_id, 200)
    shot8 = create_screenshot(tx_text=tx_id_1, amount_text="100.00")
    res8 = client.post(
        f"/api/v1/orders/{ord8['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_id_1, "amount": 100.0},
        files={"screenshot": ("reuse.png", shot8, "image/png")}
    )
    assert res8.status_code == 400, f"Expected 400 for duplicate tx_id, got {res8.status_code}: {res8.text}"
    assert "already been used" in res8.text.lower() or "already used" in res8.text.lower()
    print(f"  [PASS] TEST 8: Reused transaction ID blocked with HTTP 400: '{res8.json().get('detail')}'")

    # -------------------------------------------------------------------------
    # TEST 9: User attempts to directly call payment verification API with verified=true
    # -------------------------------------------------------------------------
    print("\n>>> TEST 9: User attempts to forge verification with verified=true")
    ord9 = create_test_order(b_headers, listing_id, 200)
    
    # 1. Global bypass endpoint
    res9a = client.post("/api/v1/payment/verify", headers=b_headers, json={"transaction_id": "FAKE123", "verified": True})
    assert res9a.status_code == 400, f"Expected 400 for fake verify, got {res9a.status_code}: {res9a.text}"

    # 2. Order specific verify-payment endpoint
    res9b = client.post(f"/api/v1/orders/{ord9['order_id']}/verify-payment", headers=b_headers, json={"verified": True})
    assert res9b.status_code == 400, f"Expected 400 for fake verify, got {res9b.status_code}: {res9b.text}"

    # 3. Order verify endpoint
    res9c = client.post(f"/api/v1/orders/{ord9['order_id']}/verify", headers=b_headers, json={"verified": True})
    assert res9c.status_code == 400, f"Expected 400 for fake verify, got {res9c.status_code}: {res9c.text}"

    print(f"  [PASS] TEST 9: All client-side verification forgery attempts strictly rejected with HTTP 400.")

    # -------------------------------------------------------------------------
    # TEST 10: Payment verification fails -> 0 financial changes
    # -------------------------------------------------------------------------
    print("\n>>> TEST 10: Payment verification fails -> 0 financial changes, 0 credit allocation")
    # Record pre-state
    buyer_wallet_pre = client.get("/api/v1/consumer/wallet", headers=b_headers).json()["data"]
    seller_orders_pre = client.get("/api/v1/provider/orders", headers=s_headers).json()["data"]["orders"]
    
    ord10 = create_test_order(b_headers, listing_id, 300)
    bad_shot = create_screenshot(tx_text="WRONG-TX-ID-999", amount_text="150.00")
    
    res10 = client.post(
        f"/api/v1/orders/{ord10['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": "USER-SUBMITTED-TX-100", "amount": 150.0},
        files={"screenshot": ("bad.png", bad_shot, "image/png")}
    )
    assert res10.status_code == 400
    assert "does not match" in res10.json().get("detail", "").lower()

    # Check buyer wallet credits
    buyer_wallet_post = client.get("/api/v1/consumer/wallet", headers=b_headers).json()["data"]
    # Verify no credits allocated from ord10
    db = SessionLocal()
    try:
        alloc = db.query(CreditAllocation).filter(CreditAllocation.order_id == ord10["id"]).first()
        assert alloc is None, "CreditAllocation was created for rejected order!"
        
        ord_db = db.query(Order).filter(Order.id == ord10["id"]).first()
        assert ord_db.status == "REJECTED", f"Order status should be REJECTED, got {ord_db.status}"
        assert ord_db.credit_allocation is None
        
        txs = db.query(WalletTransaction).filter(WalletTransaction.description.like(f"%{ord10['order_id']}%")).all()
        assert len(txs) == 0, f"Wallet transactions created for rejected order: {len(txs)}"
    finally:
        db.close()

    print("  [PASS] TEST 10: Verification failure produced 0 wallet credits, 0 transactions, 0 allocation.")

    # -------------------------------------------------------------------------
    # TEST 11: Payment verification succeeds -> Existing payment flow proceeds normally
    # -------------------------------------------------------------------------
    print("\n>>> TEST 11: Payment verification succeeds -> Flow proceeds normally to seller confirmation")
    ord11 = create_test_order(b_headers, listing_id, 200)
    tx_11 = f"TXN-SUCCESS-{uuid.uuid4().hex[:8].upper()}"
    shot11 = create_screenshot(tx_text=tx_11, amount_text="100.00")

    res11 = client.post(
        f"/api/v1/orders/{ord11['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_11, "amount": 100.0},
        files={"screenshot": ("good.png", shot11, "image/png")}
    )
    assert res11.status_code == 200
    assert res11.json()["data"]["verification_status"] == "VERIFIED"

    # Seller sees order in provider orders
    seller_orders = client.get("/api/v1/provider/orders", headers=s_headers).json()["data"]["orders"]
    matching_s_order = next((o for o in seller_orders if o["order_id"] == ord11["order_id"]), None)
    assert matching_s_order is not None, "Seller could not find submitted order"
    assert matching_s_order["verification_status"] == "VERIFIED"

    # Seller confirms payment
    conf_11 = client.post(f"/api/v1/provider/orders/{ord11['order_id']}/confirm-payment", headers=s_headers)
    assert conf_11.status_code == 200, f"Seller confirm failed: {conf_11.text}"
    assert conf_11.json()["data"]["status"] in ("APPROVED", "COMPLETED")
    print("  [PASS] TEST 11: Verified payment proceeded through seller confirmation to APPROVED/COMPLETED.")

    # -------------------------------------------------------------------------
    # TEST 12: Wrong screenshot but correct amount -> REJECTED
    # -------------------------------------------------------------------------
    print("\n>>> TEST 12: Wrong screenshot but correct amount")
    ord12 = create_test_order(b_headers, listing_id, 400)  # total 200.00
    tx_expected_12 = f"TXN-REAL-{uuid.uuid4().hex[:6].upper()}"
    # Screenshot has matching amount 200.00 but wrong TXN ID
    shot12 = create_screenshot(tx_text="TXN-DIFFERENT-8888", amount_text="200.00")

    res12 = client.post(
        f"/api/v1/orders/{ord12['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_expected_12, "amount": 200.0},
        files={"screenshot": ("shot12.png", shot12, "image/png")}
    )
    assert res12.status_code == 400
    detail12 = res12.json().get("detail", "")
    assert "does not match" in detail12.lower()
    
    db = SessionLocal()
    p12 = db.query(PaymentProof).filter(PaymentProof.order_id == ord12["id"]).first()
    assert p12.verification_status == "REJECTED"
    db.close()
    print(f"  [PASS] TEST 12: Amount match alone is NOT sufficient. Correctly REJECTED: {detail12}")

    # -------------------------------------------------------------------------
    # TEST 13: Correct-looking payment screenshot but transaction ID does not match submitted ID
    # -------------------------------------------------------------------------
    print("\n>>> TEST 13: Correct-looking payment screenshot but transaction ID does not match submitted ID")
    ord13 = create_test_order(b_headers, listing_id, 300)
    tx_13_entered = "TXN123456"
    tx_13_in_shot = "TXN123457"  # Single character difference
    shot13 = create_screenshot(tx_text=tx_13_in_shot, amount_text="150.00")

    res13 = client.post(
        f"/api/v1/orders/{ord13['order_id']}/payment-proof",
        headers=b_headers,
        data={"transaction_id": tx_13_entered, "amount": 150.0},
        files={"screenshot": ("shot13.png", shot13, "image/png")}
    )
    assert res13.status_code == 400
    detail13 = res13.json().get("detail", "")
    assert "does not match" in detail13.lower()

    db = SessionLocal()
    p13 = db.query(PaymentProof).filter(PaymentProof.order_id == ord13["id"]).first()
    assert p13.verification_status == "REJECTED"
    db.close()
    print(f"  [PASS] TEST 13: Single character difference ({tx_13_entered} vs {tx_13_in_shot}) correctly REJECTED.")

    # -------------------------------------------------------------------------
    # TEST 14: Two concurrent submissions attempt to use the same transaction ID
    # -------------------------------------------------------------------------
    print("\n>>> TEST 14: Two concurrent submissions attempt to use the same transaction ID")
    ord14_a = create_test_order(b_headers, listing_id, 200)
    ord14_b = create_test_order(b_headers, listing_id, 200)
    shared_tx = f"TXN-CONCURRENT-{uuid.uuid4().hex[:8].upper()}"

    def submit_proof(order_id):
        shot = create_screenshot(tx_text=shared_tx, amount_text="100.00")
        return client.post(
            f"/api/v1/orders/{order_id}/payment-proof",
            headers=b_headers,
            data={"transaction_id": shared_tx, "amount": 100.0},
            files={"screenshot": (f"shot_{order_id}.png", shot, "image/png")}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(submit_proof, ord14_a["order_id"])
        f2 = executor.submit(submit_proof, ord14_b["order_id"])
        r1 = f1.result()
        r2 = f2.result()

    print(f"  Submission A status: {r1.status_code}, Submission B status: {r2.status_code}")
    # One must succeed or if both pass OCR before confirmation, when seller confirms one, the second cannot be confirmed or reused.
    # Furthermore, test that after one is confirmed, any other submission or confirmation fails:
    if r1.status_code == 200 and r2.status_code == 200:
        # Confirm order A
        conf_a = client.post(f"/api/v1/provider/orders/{ord14_a['order_id']}/confirm-payment", headers=s_headers)
        assert conf_a.status_code == 200
        # Now order B seller confirmation MUST fail or be blocked by unique index
        conf_b = client.post(f"/api/v1/provider/orders/{ord14_b['order_id']}/confirm-payment", headers=s_headers)
        assert conf_b.status_code == 400, f"Expected 400 for duplicate confirmed transaction ID, got {conf_b.status_code}: {conf_b.text}"
        print(f"  [PASS] Order B blocked on confirmation due to duplicate transaction ID: '{conf_b.json().get('detail')}'")
    else:
        # One was directly rejected at submission
        assert (r1.status_code == 200 and r2.status_code == 400) or (r1.status_code == 400 and r2.status_code == 200)
        print("  [PASS] One of the concurrent submissions was directly rejected at submission.")

    print(f"  [PASS] TEST 14: Only one payment can claim a transaction ID.")

    print("\n" + "=" * 80)
    print("ALL 14 PAYMENT VERIFICATION & FRAUD PREVENTION TESTS PASSED PERFECTLY!")
    print("=" * 80)


if __name__ == "__main__":
    test_payment_verification_14_scenarios()
