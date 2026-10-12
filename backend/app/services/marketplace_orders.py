"""
TokenPool — Marketplace Orders & Disputes Service Layer
Encapsulates all business logic for:
1. Buy Order creation & capacity reservation
2. Secure payment proof submission with validation & duplicate detection
3. Seller payment confirmation & atomic credit release
4. Seller rejection with mandatory reason
5. Buyer dispute raising
6. Admin dispute resolution (Approve, Reject, Request Evidence)
7. Seller blocking / unblocking with audit trail
"""
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.schema_models import (
    User, Wallet, WalletTransaction, ProviderAccount, ProviderConnection, ProviderListing,
    Order, PaymentProof, MarketplaceDispute, DisputeMessage,
    CreditAllocation, ModelCreditBalance, ProviderEarningRecord, SellerStatusHistory,
    AuditLog, Notification, AICapacityPool, ProviderCapacityAllocation
)
from backend.app.services.payment_ocr import PaymentOCRService

UPLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads", "payment_proofs"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "application/pdf"}
MAX_FILE_SIZE = 5 * 1024 * 1024 # 5 MB


class MarketplaceOrderService:

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _sync_connection_capacity(cls, db: Session, listing: Optional[ProviderListing], delta_reserved: int = 0, delta_sold: int = 0):
        """Atomically synchronizes ProviderConnection capacity accounting with row-level locking."""
        if not listing or not listing.provider_connection_id:
            return
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.id == listing.provider_connection_id
        ).with_for_update().first()
        if conn:
            if delta_reserved != 0:
                conn.capacity_reserved = max(0, (conn.capacity_reserved or 0) + delta_reserved)
            if delta_sold != 0:
                conn.capacity_sold = max(0, (conn.capacity_sold or 0) + delta_sold)
            conn.capacity_remaining = max(
                0,
                (conn.capacity_declared or 0)
                - (conn.capacity_reserved or 0)
                - (conn.capacity_sold or 0)
                - (conn.capacity_consumed or 0)
            )

    @classmethod
    def _release_order_reserved_capacity(cls, db: Session, order: Order):
        """Atomically releases reserved capacity back to listing, connection, allocation, and pool."""
        listing = db.query(ProviderListing).filter(ProviderListing.id == order.listing_id).with_for_update().first()
        if listing:
            listing.reserved_capacity = max(0, (listing.reserved_capacity or 0) - order.quantity)
            cls._sync_connection_capacity(db, listing, delta_reserved=-order.quantity)
            if listing.provider_capacity_allocation_id:
                alloc = db.query(ProviderCapacityAllocation).filter(
                    ProviderCapacityAllocation.id == listing.provider_capacity_allocation_id
                ).with_for_update().first()
                if alloc:
                    alloc.reserved_capacity = max(0, (alloc.reserved_capacity or 0) - order.quantity)
                    alloc.remaining_capacity = (alloc.remaining_capacity or 0) + order.quantity
                    pool = db.query(AICapacityPool).filter(AICapacityPool.id == alloc.capacity_pool_id).with_for_update().first()
                    if pool:
                        pool.reserved_capacity = max(0, (pool.reserved_capacity or 0) - order.quantity)

    @classmethod
    def check_order_expiration(cls, order: Order, db: Session) -> bool:
        """If PAYMENT_PENDING order has passed expires_at, cancel and release reserved capacity."""
        if order.status == "PAYMENT_PENDING" and order.expires_at:
            # Handle tz-aware / naive comparison
            now = cls._utcnow()
            exp = order.expires_at if order.expires_at.tzinfo else order.expires_at.replace(tzinfo=timezone.utc)
            if now > exp:
                order.status = "CANCELLED"
                order.rejection_reason = "Order expired before payment proof was submitted."
                cls._release_order_reserved_capacity(db, order)
                db.add(AuditLog(
                    user_id=order.buyer_id,
                    actor_email="system_expiry",
                    action="CAPACITY_RELEASED",
                    resource_type="ORDER",
                    resource_id=order.order_id,
                    details=f"Released {order.quantity} reserved capacity due to order expiry."
                ))
                db.commit()
                return True
        return False

    @classmethod
    def create_buy_order(
        cls,
        db: Session,
        buyer: User,
        listing_id: int,
        quantity: int,
    ) -> Order:
        """Create a new Buy Order with server-side pricing and capacity reservation."""
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="Order quantity must be greater than zero.")

        # Row-lock the listing to ensure atomic capacity reservation
        listing = db.query(ProviderListing).filter(
            ProviderListing.id == listing_id,
            ProviderListing.status == "ACTIVE"
        ).with_for_update().first()

        if not listing:
            raise HTTPException(status_code=404, detail="Active listing not found.")

        # Check seller provider account status
        provider = db.query(ProviderAccount).filter(ProviderAccount.id == listing.provider_id).first()
        if not provider:
            raise HTTPException(status_code=404, detail="Provider account not found.")

        if provider.status in ("BLOCKED", "SUSPENDED"):
            raise HTTPException(status_code=400, detail=f"Seller is currently {provider.status.lower()} and cannot receive orders.")

        seller_user = db.query(User).filter(User.id == provider.user_id).first()
        if not seller_user:
            raise HTTPException(status_code=404, detail="Seller user not found.")

        if seller_user.id == buyer.id:
            raise HTTPException(status_code=400, detail="You cannot purchase from your own listing.")

        # Purchase limits
        min_p = listing.min_purchase or 100
        max_p = listing.max_purchase or 50000
        if quantity < min_p:
            raise HTTPException(status_code=400, detail=f"Minimum purchase quantity is {min_p} credits.")
        if quantity > max_p:
            raise HTTPException(status_code=400, detail=f"Maximum purchase quantity is {max_p} credits per order.")

        # Available capacity check
        reserved = listing.reserved_capacity or 0
        available = listing.capacity_units - reserved
        if quantity > available:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient available capacity. Requested: {quantity}, Available: {max(0, available)}."
            )

        # Calculate financial total strictly on backend
        price_per_unit = float(listing.price_per_unit or 0.50)
        total_amount = round(quantity * price_per_unit, 2)

        # Atomically reserve capacity
        listing.reserved_capacity = reserved + quantity
        cls._sync_connection_capacity(db, listing, delta_reserved=quantity)

        # Reserve in allocation and pool if linked
        if listing.provider_capacity_allocation_id:
            alloc = db.query(ProviderCapacityAllocation).filter(
                ProviderCapacityAllocation.id == listing.provider_capacity_allocation_id
            ).with_for_update().first()
            if alloc:
                alloc.reserved_capacity = (alloc.reserved_capacity or 0) + quantity
                pool = db.query(AICapacityPool).filter(AICapacityPool.id == alloc.capacity_pool_id).with_for_update().first()
                if pool:
                    pool.reserved_capacity = (pool.reserved_capacity or 0) + quantity

        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        expires_at = cls._utcnow() + timedelta(hours=2)

        order = Order(
            order_id=order_id,
            buyer_id=buyer.id,
            seller_id=seller_user.id,
            provider_id=provider.id,
            listing_id=listing.id,
            quantity=quantity,
            price_per_unit=price_per_unit,
            total_amount=total_amount,
            currency="INR",
            status="PAYMENT_PENDING",
            seller_payment_method=listing.payment_method or "UPI",
            seller_payment_identifier=listing.payment_identifier or "seller@upi",
            seller_payment_instructions=listing.payment_instructions,
            seller_qr_url=listing.payment_qr_url,
            expires_at=expires_at,
        )
        db.add(order)

        # Audit logs
        db.add(AuditLog(
            user_id=buyer.id,
            actor_email=buyer.email,
            action="ORDER_CREATED",
            resource_type="ORDER",
            resource_id=order_id,
            details=f"Buyer created order {order_id} for {quantity} credits (INR {total_amount:.2f}) from seller {seller_user.email}."
        ))
        db.add(AuditLog(
            user_id=buyer.id,
            actor_email=buyer.email,
            action="CAPACITY_RESERVED",
            resource_type="LISTING",
            resource_id=str(listing.id),
            details=f"Reserved {quantity} capacity for order {order_id} on listing #{listing.id} ({listing.model_slug})."
        ))

        # Notify seller
        db.add(Notification(
            user_id=seller_user.id,
            title="New Buy Order Created",
            message=f"Buyer created order #{order_id} for {quantity} credits (INR {total_amount:.2f}). Awaiting payment proof.",
            type="INFO",
            link="/provider/dashboard"
        ))

        db.commit()
        db.refresh(order)
        return order

    @classmethod
    async def submit_payment_proof(
        cls,
        db: Session,
        buyer: User,
        order_id: str,
        transaction_id: str,
        amount_reported: float,
        file: UploadFile,
        note: Optional[str] = None,
    ) -> PaymentProof:
        """Buyer uploads payment proof with secure file handling & duplicate tx check."""
        order = db.query(Order).filter(Order.order_id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        if order.buyer_id != buyer.id:
            raise HTTPException(status_code=403, detail="You do not own this order.")

        # Check expiration
        if cls.check_order_expiration(order, db):
            raise HTTPException(status_code=400, detail="This order has expired and was cancelled.")

        if order.status not in ("PAYMENT_PENDING", "ADMIN_REVIEW"):
            raise HTTPException(status_code=400, detail=f"Cannot submit proof for order in status '{order.status}'.")

        # Validate transaction ID
        clean_tx_id = (transaction_id or "").strip()
        if not clean_tx_id:
            raise HTTPException(status_code=400, detail="Transaction ID is required.")
        if len(clean_tx_id) < 4:
            raise HTTPException(status_code=400, detail="Please enter a valid Transaction / Reference ID (minimum 4 characters).")

        # Validate file existence
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="Payment screenshot is required.")

        # Read file contents and check size
        contents = await file.read()
        file_size = len(contents)
        if file_size == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        if file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"File exceeds maximum allowed size of 5 MB (size: {file_size / (1024*1024):.2f} MB).")

        # Validate file integrity and image content via PIL
        is_valid_img, img_err = PaymentOCRService.validate_image_file(contents, file.filename)
        if not is_valid_img:
            raise HTTPException(status_code=400, detail=img_err or "Invalid or unreadable payment screenshot.")

        # SHA-256 fingerprinting to prevent reusing screenshots across users/orders
        file_hash = PaymentOCRService.compute_sha256(contents)
        dup_screenshot = db.query(PaymentProof).filter(
            PaymentProof.file_hash == file_hash,
            PaymentProof.order_id != order.id,
            PaymentProof.verification_status == "VERIFIED"
        ).first()
        if dup_screenshot:
            raise HTTPException(
                status_code=400,
                detail="This payment screenshot has already been used for another verified order. Screenshot reuse is strictly prohibited."
            )

        # Check for duplicate transaction ID across verified or active orders
        duplicate_proof = db.query(PaymentProof).join(Order).filter(
            func.lower(PaymentProof.transaction_id) == clean_tx_id.lower(),
            Order.id != order.id,
            (
                (PaymentProof.verification_status == "VERIFIED")
                | Order.status.in_(["APPROVED", "SELLER_CONFIRMED", "PAYMENT_SUBMITTED", "SELLER_REVIEW", "DISPUTED"])
            )
        ).first()

        if duplicate_proof:
            raise HTTPException(
                status_code=400,
                detail=f"Transaction ID '{clean_tx_id}' has already been successfully submitted for another order. Reused IDs are not permitted."
            )

        ext = os.path.splitext(file.filename)[1].lower()
        safe_filename = f"proof_{order.order_id}_{uuid.uuid4().hex[:8]}{ext}"
        safe_filepath = os.path.join(UPLOAD_DIR, safe_filename)

        with open(safe_filepath, "wb") as f:
            f.write(contents)

        mime_type = file.content_type or "image/png"

        # Perform server-side OCR verification of Transaction ID against uploaded screenshot
        is_matched, extracted_id, rej_reason = PaymentOCRService.verify_transaction_id(
            submitted_tx_id=clean_tx_id,
            file_path_or_bytes=contents,
        )

        proof = db.query(PaymentProof).filter(PaymentProof.order_id == order.id).first()
        if not proof:
            proof = PaymentProof(
                order_id=order.id,
                transaction_id=clean_tx_id,
                screenshot_path=safe_filepath,
                screenshot_filename=safe_filename,
                original_filename=file.filename,
                file_size=file_size,
                mime_type=mime_type,
                amount_reported=amount_reported,
                note=note,
                file_hash=file_hash,
                submitted_at=cls._utcnow(),
            )
            db.add(proof)
        else:
            proof.transaction_id = clean_tx_id
            proof.screenshot_path = safe_filepath
            proof.screenshot_filename = safe_filename
            proof.original_filename = file.filename
            proof.file_size = file_size
            proof.mime_type = mime_type
            proof.amount_reported = amount_reported
            proof.note = note
            proof.file_hash = file_hash
            proof.submitted_at = cls._utcnow()

        if not is_matched:
            # ── AUTOMATIC REJECTION: Transaction ID mismatch or unreadable ──
            order.status = "REJECTED"
            order.rejection_reason = rej_reason
            proof.verification_status = "REJECTED"
            proof.ocr_status = "COMPLETED"
            proof.extracted_transaction_id = extracted_id
            proof.verification_reason = rej_reason

            # Atomically restore reserved capacity so seller capacity is freed
            cls._release_order_reserved_capacity(db, order)

            db.add(AuditLog(
                user_id=buyer.id,
                actor_email=buyer.email,
                action="PAYMENT_VERIFICATION_REJECTED",
                resource_type="ORDER",
                resource_id=order.order_id,
                details=f"Payment rejected for order {order.order_id}. Reason: {rej_reason}. Submitted: {clean_tx_id}, Extracted: {extracted_id}."
            ))

            db.add(Notification(
                user_id=buyer.id,
                title="Payment Verification Failed",
                message=f"Your payment proof for order #{order.order_id} was rejected: {rej_reason}",
                type="ERROR",
                link="/orders"
            ))

            db.commit()
            raise HTTPException(status_code=400, detail=f"Payment rejected. {rej_reason}")

        # ── SUCCESSFUL VERIFICATION: Screenshot contains matching Transaction ID ──
        order.status = "PAYMENT_SUBMITTED"
        proof.verification_status = "VERIFIED"
        proof.ocr_status = "COMPLETED"
        proof.extracted_transaction_id = extracted_id
        proof.verification_reason = "Transaction ID verified successfully against payment screenshot."
        proof.verified_at = cls._utcnow()

        db.add(AuditLog(
            user_id=buyer.id,
            actor_email=buyer.email,
            action="PAYMENT_VERIFIED",
            resource_type="ORDER",
            resource_id=order.order_id,
            details=f"Payment verified for order {order.order_id}: TxID={clean_tx_id}, Extracted={extracted_id}, Amount=INR {amount_reported:.2f}."
        ))

        db.add(Notification(
            user_id=order.seller_id,
            title="Payment Proof Verified",
            message=f"Buyer submitted verified payment proof for order #{order.order_id} (TxID: {clean_tx_id}, INR {amount_reported:.2f}). Please confirm fund receipt.",
            type="SUCCESS",
            link="/provider/dashboard"
        ))

        try:
            db.commit()
            db.refresh(proof)
            return proof
        except IntegrityError:
            db.rollback()
            # Release reserved capacity since this order cannot claim the transaction ID
            cls._release_order_reserved_capacity(db, order)
            order.status = "REJECTED"
            db.commit()
            raise HTTPException(
                status_code=400,
                detail="This transaction ID has already been used or claimed in another verified payment."
            )

    @classmethod
    def seller_confirm_payment(
        cls,
        db: Session,
        seller: User,
        order_id: str,
    ) -> Order:
        """Seller verifies money received in account and confirms order -> releases credits atomically."""
        order = db.query(Order).filter(Order.order_id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        if order.seller_id != seller.id:
            raise HTTPException(status_code=403, detail="You are not authorized to confirm this order.")

        # Double confirmation protection
        if order.status == "APPROVED":
            raise HTTPException(status_code=400, detail="Order has already been confirmed and approved.")

        if order.status == "REJECTED":
            raise HTTPException(status_code=400, detail=f"Cannot confirm order: Order was rejected ({order.rejection_reason}).")

        if order.status not in ("PAYMENT_SUBMITTED", "SELLER_REVIEW", "DISPUTED"):
            raise HTTPException(status_code=400, detail=f"Cannot confirm order in status '{order.status}'. Payment proof must be submitted first.")

        proof = db.query(PaymentProof).filter(PaymentProof.order_id == order.id).first()
        if not proof:
            raise HTTPException(status_code=400, detail="Cannot confirm order: No payment proof has been submitted.")

        if not proof.transaction_id:
            raise HTTPException(status_code=400, detail="Cannot confirm order: Transaction ID is missing.")

        if proof.verification_status != "VERIFIED":
            raise HTTPException(
                status_code=400,
                detail="Cannot confirm order: Payment proof has not been verified with matching screenshot."
            )

        # Atomic row-locks on listing and buyer wallet
        listing = db.query(ProviderListing).filter(ProviderListing.id == order.listing_id).with_for_update().first()
        buyer_wallet = db.query(Wallet).filter(Wallet.user_id == order.buyer_id).with_for_update().first()
        if not buyer_wallet:
            buyer_wallet = Wallet(user_id=order.buyer_id, balance=0.0, credits=0, currency="INR")
            db.add(buyer_wallet)
            db.flush()

        # Deduct from listing capacity & reserved capacity, add to sold capacity
        if listing:
            listing.capacity_units = max(0, listing.capacity_units - order.quantity)
            listing.reserved_capacity = max(0, (listing.reserved_capacity or 0) - order.quantity)
            listing.sold_capacity = (listing.sold_capacity or 0) + order.quantity
            listing.remaining_capacity = max(0, (listing.remaining_capacity or 0) - order.quantity)
            cls._sync_connection_capacity(db, listing, delta_reserved=-order.quantity, delta_sold=order.quantity)
            if listing.provider_capacity_allocation_id:
                alloc = db.query(ProviderCapacityAllocation).filter(
                    ProviderCapacityAllocation.id == listing.provider_capacity_allocation_id
                ).with_for_update().first()
                if alloc:
                    alloc.reserved_capacity = max(0, (alloc.reserved_capacity or 0) - order.quantity)
                    alloc.sold_capacity = (alloc.sold_capacity or 0) + order.quantity
                    pool = db.query(AICapacityPool).filter(AICapacityPool.id == alloc.capacity_pool_id).with_for_update().first()
                    if pool:
                        pool.reserved_capacity = max(0, (pool.reserved_capacity or 0) - order.quantity)
                        pool.sold_capacity = (pool.sold_capacity or 0) + order.quantity

        # Release model-specific credits to buyer
        model_slug = listing.model_slug if listing else "nova-reasoning"
        model_name = listing.model_name if listing else "AI Model"
        buyer_wallet.credits += order.quantity

        db.add(CreditAllocation(
            order_id=order.id,
            buyer_id=order.buyer_id,
            seller_id=order.seller_id,
            provider_connection_id=listing.provider_connection_id if listing else None,
            listing_id=listing.id if listing else None,
            model_slug=model_slug,
            model_name=model_name,
            credits=order.quantity,
            credits_used=0,
            credits_remaining=order.quantity,
            status="ACTIVE",
            released_at=cls._utcnow(),
        ))

        # Update or create model-specific credit balance
        mcb = db.query(ModelCreditBalance).filter(
            ModelCreditBalance.user_id == order.buyer_id,
            ModelCreditBalance.model_slug == model_slug,
        ).with_for_update().first()
        if not mcb:
            mcb = ModelCreditBalance(
                user_id=order.buyer_id,
                model_slug=model_slug,
                model_name=model_name,
                credits_purchased=order.quantity,
                credits_used=0,
                credits_remaining=order.quantity,
            )
            db.add(mcb)
        else:
            mcb.credits_purchased += order.quantity
            mcb.credits_remaining += order.quantity

        db.add(WalletTransaction(
            wallet_id=buyer_wallet.id,
            tx_type="CREDIT_PURCHASE",
            amount=0.0,
            balance_after=buyer_wallet.balance,
            credits_delta=order.quantity,
            description=f"Marketplace credit purchase from seller (Order #{order.order_id})",
            status="COMPLETED",
        ))

        # Financial settlement for provider
        platform_fee = round(order.total_amount * 0.10, 2) # 10% marketplace commission
        net_earning = round(order.total_amount - platform_fee, 2)

        db.add(ProviderEarningRecord(
            order_id=order.id,
            provider_id=order.provider_id,
            gross_amount=order.total_amount,
            platform_fee=platform_fee,
            net_earning=net_earning,
            status="CREDITED",
        ))

        provider = db.query(ProviderAccount).filter(ProviderAccount.id == order.provider_id).with_for_update().first()
        if provider:
            provider.total_earnings = round(provider.total_earnings + net_earning, 2)
            provider.withdrawable_balance = round(provider.withdrawable_balance + net_earning, 2)

        # If order had an active dispute, resolve it
        active_dispute = db.query(MarketplaceDispute).filter(
            MarketplaceDispute.order_id == order.id,
            MarketplaceDispute.status.in_(["OPEN", "ADMIN_REVIEW"])
        ).first()
        if active_dispute:
            active_dispute.status = "RESOLVED_APPROVED"
            active_dispute.seller_response = "Confirmed payment directly with buyer."
            active_dispute.admin_notes = "Resolved automatically upon seller confirmation."

        order.status = "APPROVED"

        # Audit logs
        db.add(AuditLog(
            user_id=seller.id,
            actor_email=seller.email,
            action="SELLER_CONFIRMED",
            resource_type="ORDER",
            resource_id=order.order_id,
            details=f"Seller confirmed payment for order {order.order_id}. Total: INR {order.total_amount:.2f}, Net Earning: INR {net_earning:.2f}."
        ))

        db.add(AuditLog(
            user_id=seller.id,
            actor_email=seller.email,
            action="CREDITS_RELEASED",
            resource_type="WALLET",
            resource_id=str(buyer_wallet.id),
            details=f"Released {order.quantity} credits to buyer {order.buyer.email if order.buyer else order.buyer_id}."
        ))
        db.add(AuditLog(
            user_id=seller.id,
            actor_email=seller.email,
            action="CAPACITY_SOLD",
            resource_type="LISTING",
            resource_id=str(listing.id) if listing else str(order.listing_id),
            details=f"Capacity sold: {order.quantity} units for order {order.order_id} ({model_slug})."
        ))
        db.add(AuditLog(
            user_id=order.buyer_id,
            actor_email=seller.email,
            action="MODEL_CREDIT_ALLOCATED",
            resource_type="MODEL_CREDIT",
            resource_id=model_slug,
            details=f"Allocated {order.quantity} model credits for {model_slug} to buyer {order.buyer_id}."
        ))

        # Notify buyer
        db.add(Notification(
            user_id=order.buyer_id,
            title="Order Confirmed — Credits Released!",
            message=f"Seller confirmed your payment for order #{order.order_id}. {order.quantity} credits have been credited to your wallet and are now ready for use!",
            type="SUCCESS",
            link="/wallet"
        ))

        db.commit()
        db.refresh(order)
        return order

    @classmethod
    def seller_reject_payment(
        cls,
        db: Session,
        seller: User,
        order_id: str,
        reason: str,
    ) -> Order:
        """Seller rejects payment submission with mandatory reason."""
        order = db.query(Order).filter(Order.order_id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        if order.seller_id != seller.id:
            raise HTTPException(status_code=403, detail="You are not authorized to reject this order.")

        if order.status not in ("PAYMENT_SUBMITTED", "SELLER_REVIEW"):
            raise HTTPException(status_code=400, detail=f"Cannot reject order in status '{order.status}'.")

        clean_reason = reason.strip()
        if not clean_reason:
            raise HTTPException(status_code=400, detail="Rejection reason is required.")

        # Release reserved capacity back to listing, allocation, and pool
        cls._release_order_reserved_capacity(db, order)

        order.status = "REJECTED"
        order.rejection_reason = clean_reason

        db.add(AuditLog(
            user_id=seller.id,
            actor_email=seller.email,
            action="CAPACITY_RELEASED",
            resource_type="LISTING",
            resource_id=str(order.listing_id),
            details=f"Released {order.quantity} reserved capacity due to payment rejection on order {order.order_id}."
        ))
        db.add(AuditLog(
            user_id=seller.id,
            actor_email=seller.email,
            action="SELLER_REJECTED",
            resource_type="ORDER",
            resource_id=order.order_id,
            details=f"Seller rejected payment for order {order.order_id}. Reason: {clean_reason}."
        ))

        db.add(Notification(
            user_id=order.buyer_id,
            title="Payment Rejected by Seller",
            message=f"Seller rejected payment for order #{order.order_id}: '{clean_reason}'. You may raise a dispute if you believe this is in error.",
            type="WARNING",
            link=f"/dashboard"
        ))

        db.commit()
        db.refresh(order)
        return order

    @classmethod
    def buyer_raise_dispute(
        cls,
        db: Session,
        buyer: User,
        order_id: str,
        reason: str,
        details: Optional[str] = None,
    ) -> MarketplaceDispute:
        """Buyer raises a dispute if seller rejects or does not confirm."""
        order = db.query(Order).filter(Order.order_id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        if order.buyer_id != buyer.id:
            raise HTTPException(status_code=403, detail="You do not own this order.")

        if order.status not in ("PAYMENT_SUBMITTED", "SELLER_REVIEW", "REJECTED"):
            raise HTTPException(status_code=400, detail=f"Cannot raise dispute for order in status '{order.status}'.")

        clean_reason = reason.strip()
        if not clean_reason:
            raise HTTPException(status_code=400, detail="Dispute reason is required.")

        # Check if active dispute already exists
        existing_disp = db.query(MarketplaceDispute).filter(
            MarketplaceDispute.order_id == order.id,
            MarketplaceDispute.status.in_(["OPEN", "ADMIN_REVIEW"])
        ).first()
        if existing_disp:
            raise HTTPException(status_code=400, detail="An active dispute already exists for this order.")

        dispute_id = f"disp_{uuid.uuid4().hex[:12]}"
        dispute = MarketplaceDispute(
            dispute_id=dispute_id,
            order_id=order.id,
            raised_by_id=buyer.id,
            reason=clean_reason,
            details=details,
            status="OPEN",
        )
        db.add(dispute)
        order.status = "DISPUTED"

        db.add(AuditLog(
            user_id=buyer.id,
            actor_email=buyer.email,
            action="DISPUTE_CREATED",
            resource_type="DISPUTE",
            resource_id=dispute_id,
            details=f"Buyer raised dispute {dispute_id} for order {order.order_id}: {clean_reason}."
        ))

        # Notify seller
        db.add(Notification(
            user_id=order.seller_id,
            title="Dispute Raised on Order",
            message=f"Buyer raised a dispute on order #{order.order_id}: '{clean_reason}'. Admin has been notified for review.",
            type="WARNING",
            link="/provider/dashboard"
        ))

        # Notify admins
        admins = db.query(User).filter(User.role == "ADMIN").all()
        for a in admins:
            db.add(Notification(
                user_id=a.id,
                title="New Marketplace Dispute",
                message=f"New dispute #{dispute_id} raised for order #{order.order_id} (INR {order.total_amount:.2f}). Review required.",
                type="ERROR",
                link="/admin/dashboard"
            ))

        db.commit()
        db.refresh(dispute)
        return dispute

    @classmethod
    def admin_approve_payment(
        cls,
        db: Session,
        admin: User,
        order_id: str,
        admin_notes: Optional[str] = "Admin verified payment and approved order.",
    ) -> Order:
        """Admin verifies payment evidence during dispute/review and releases credits."""
        order = db.query(Order).filter(Order.order_id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        if order.status == "APPROVED":
            raise HTTPException(status_code=400, detail="Order is already approved.")

        if order.status not in ("DISPUTED", "ADMIN_REVIEW", "PAYMENT_SUBMITTED", "SELLER_REVIEW", "REJECTED"):
            raise HTTPException(status_code=400, detail=f"Cannot approve order in status '{order.status}'.")

        proof = db.query(PaymentProof).filter(PaymentProof.order_id == order.id).first()
        if not proof:
            raise HTTPException(status_code=400, detail="Cannot approve order: No payment proof has been submitted.")
        if proof.verification_status != "VERIFIED":
            raise HTTPException(
                status_code=400,
                detail="Cannot approve order: Payment proof has not been verified with matching screenshot."
            )

        # Row-lock listing and buyer wallet
        listing = db.query(ProviderListing).filter(ProviderListing.id == order.listing_id).with_for_update().first()
        buyer_wallet = db.query(Wallet).filter(Wallet.user_id == order.buyer_id).with_for_update().first()
        if not buyer_wallet:
            buyer_wallet = Wallet(user_id=order.buyer_id, balance=0.0, credits=0, currency="INR")
            db.add(buyer_wallet)
            db.flush()

        if listing:
            listing.capacity_units = max(0, listing.capacity_units - order.quantity)
            listing.reserved_capacity = max(0, (listing.reserved_capacity or 0) - order.quantity)
            listing.sold_capacity = (listing.sold_capacity or 0) + order.quantity
            listing.remaining_capacity = max(0, (listing.remaining_capacity or 0) - order.quantity)
            cls._sync_connection_capacity(db, listing, delta_reserved=-order.quantity, delta_sold=order.quantity)
            if listing.provider_capacity_allocation_id:
                alloc = db.query(ProviderCapacityAllocation).filter(
                    ProviderCapacityAllocation.id == listing.provider_capacity_allocation_id
                ).with_for_update().first()
                if alloc:
                    alloc.reserved_capacity = max(0, (alloc.reserved_capacity or 0) - order.quantity)
                    alloc.sold_capacity = (alloc.sold_capacity or 0) + order.quantity
                    pool = db.query(AICapacityPool).filter(AICapacityPool.id == alloc.capacity_pool_id).with_for_update().first()
                    if pool:
                        pool.reserved_capacity = max(0, (pool.reserved_capacity or 0) - order.quantity)
                        pool.sold_capacity = (pool.sold_capacity or 0) + order.quantity

        model_slug = listing.model_slug if listing else "nova-reasoning"
        model_name = listing.model_name if listing else "AI Model"
        buyer_wallet.credits += order.quantity

        db.add(CreditAllocation(
            order_id=order.id,
            buyer_id=order.buyer_id,
            seller_id=order.seller_id,
            provider_connection_id=listing.provider_connection_id if listing else None,
            listing_id=listing.id if listing else None,
            model_slug=model_slug,
            model_name=model_name,
            credits=order.quantity,
            credits_used=0,
            credits_remaining=order.quantity,
            status="ACTIVE",
            released_at=cls._utcnow(),
        ))

        mcb = db.query(ModelCreditBalance).filter(
            ModelCreditBalance.user_id == order.buyer_id,
            ModelCreditBalance.model_slug == model_slug,
        ).with_for_update().first()
        if not mcb:
            mcb = ModelCreditBalance(
                user_id=order.buyer_id,
                model_slug=model_slug,
                model_name=model_name,
                credits_purchased=order.quantity,
                credits_used=0,
                credits_remaining=order.quantity,
            )
            db.add(mcb)
        else:
            mcb.credits_purchased += order.quantity
            mcb.credits_remaining += order.quantity

        db.add(WalletTransaction(
            wallet_id=buyer_wallet.id,
            tx_type="CREDIT_PURCHASE",
            amount=0.0,
            balance_after=buyer_wallet.balance,
            credits_delta=order.quantity,
            description=f"Marketplace credit purchase (Admin Dispute Approval #{order.order_id})",
            status="COMPLETED",
        ))

        platform_fee = round(order.total_amount * 0.10, 2)
        net_earning = round(order.total_amount - platform_fee, 2)

        db.add(ProviderEarningRecord(
            order_id=order.id,
            provider_id=order.provider_id,
            gross_amount=order.total_amount,
            platform_fee=platform_fee,
            net_earning=net_earning,
            status="CREDITED",
        ))

        provider = db.query(ProviderAccount).filter(ProviderAccount.id == order.provider_id).with_for_update().first()
        if provider:
            provider.total_earnings = round(provider.total_earnings + net_earning, 2)
            provider.withdrawable_balance = round(provider.withdrawable_balance + net_earning, 2)

        # Resolve open disputes
        disputes = db.query(MarketplaceDispute).filter(
            MarketplaceDispute.order_id == order.id,
            MarketplaceDispute.status.in_(["OPEN", "ADMIN_REVIEW"])
        ).all()
        for d in disputes:
            d.status = "RESOLVED_APPROVED"
            d.admin_notes = admin_notes

        order.status = "APPROVED"

        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="ADMIN_APPROVED",
            resource_type="ORDER",
            resource_id=order.order_id,
            details=f"Admin {admin.email} approved payment for order {order.order_id}. Notes: {admin_notes}."
        ))

        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="CREDITS_RELEASED",
            resource_type="WALLET",
            resource_id=str(buyer_wallet.id),
            details=f"Admin released {order.quantity} credits to buyer."
        ))
        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="CAPACITY_SOLD",
            resource_type="LISTING",
            resource_id=str(listing.id) if listing else str(order.listing_id),
            details=f"Capacity sold upon admin approval: {order.quantity} units for order {order.order_id} ({model_slug})."
        ))
        db.add(AuditLog(
            user_id=order.buyer_id,
            actor_email=admin.email,
            action="MODEL_CREDIT_ALLOCATED",
            resource_type="MODEL_CREDIT",
            resource_id=model_slug,
            details=f"Allocated {order.quantity} model credits for {model_slug} to buyer {order.buyer_id}."
        ))

        db.add(Notification(
            user_id=order.buyer_id,
            title="Dispute Approved — Credits Released!",
            message=f"Admin verified your payment for order #{order.order_id}. {order.quantity} credits have been released to your wallet.",
            type="SUCCESS",
            link="/wallet"
        ))

        db.add(Notification(
            user_id=order.seller_id,
            title="Dispute Resolved by Admin",
            message=f"Admin verified buyer payment for order #{order.order_id} and approved credit transfer. Net earning of INR {net_earning:.2f} credited.",
            type="INFO",
            link="/provider/dashboard"
        ))

        db.commit()
        db.refresh(order)
        return order

    @classmethod
    def admin_reject_payment(
        cls,
        db: Session,
        admin: User,
        order_id: str,
        rejection_reason: str,
    ) -> Order:
        """Admin rejects payment dispute."""
        order = db.query(Order).filter(Order.order_id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        clean_reason = rejection_reason.strip()
        if not clean_reason:
            raise HTTPException(status_code=400, detail="Rejection reason is required.")

        listing = db.query(ProviderListing).filter(ProviderListing.id == order.listing_id).with_for_update().first()
        if listing:
            listing.reserved_capacity = max(0, (listing.reserved_capacity or 0) - order.quantity)
            cls._sync_connection_capacity(db, listing, delta_reserved=-order.quantity)
            if listing.provider_capacity_allocation_id:
                alloc = db.query(ProviderCapacityAllocation).filter(
                    ProviderCapacityAllocation.id == listing.provider_capacity_allocation_id
                ).with_for_update().first()
                if alloc:
                    alloc.reserved_capacity = max(0, (alloc.reserved_capacity or 0) - order.quantity)
                    alloc.remaining_capacity = (alloc.remaining_capacity or 0) + order.quantity
                    pool = db.query(AICapacityPool).filter(AICapacityPool.id == alloc.capacity_pool_id).with_for_update().first()
                    if pool:
                        pool.reserved_capacity = max(0, (pool.reserved_capacity or 0) - order.quantity)

        disputes = db.query(MarketplaceDispute).filter(
            MarketplaceDispute.order_id == order.id,
            MarketplaceDispute.status.in_(["OPEN", "ADMIN_REVIEW"])
        ).all()
        for d in disputes:
            d.status = "RESOLVED_REJECTED"
            d.admin_notes = clean_reason

        order.status = "REJECTED"
        order.rejection_reason = clean_reason

        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="CAPACITY_RELEASED",
            resource_type="LISTING",
            resource_id=str(listing.id) if listing else str(order.listing_id),
            details=f"Released {order.quantity} reserved capacity due to admin rejection on order {order.order_id}."
        ))

        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="ADMIN_REJECTED",
            resource_type="ORDER",
            resource_id=order.order_id,
            details=f"Admin {admin.email} rejected order {order.order_id}. Reason: {clean_reason}."
        ))

        db.add(Notification(
            user_id=order.buyer_id,
            title="Dispute Rejected by Admin",
            message=f"Admin reviewed order #{order.order_id} and rejected the payment dispute: '{clean_reason}'. No credits were released.",
            type="ERROR",
            link="/dashboard"
        ))

        db.add(Notification(
            user_id=order.seller_id,
            title="Dispute Closed — Order Rejected",
            message=f"Admin rejected payment for order #{order.order_id}: '{clean_reason}'. Reserved capacity released.",
            type="INFO",
            link="/provider/dashboard"
        ))

        db.commit()
        db.refresh(order)
        return order

    @classmethod
    def admin_request_evidence(
        cls,
        db: Session,
        admin: User,
        dispute_id: str,
        message: str,
    ) -> MarketplaceDispute:
        """Admin requests more evidence from buyer/seller."""
        dispute = db.query(MarketplaceDispute).filter(MarketplaceDispute.dispute_id == dispute_id).first()
        if not dispute:
            raise HTTPException(status_code=404, detail="Dispute not found.")

        clean_msg = message.strip()
        if not clean_msg:
            raise HTTPException(status_code=400, detail="Evidence request message cannot be empty.")

        dispute.status = "ADMIN_REVIEW"
        if dispute.order:
            dispute.order.status = "ADMIN_REVIEW"

        msg = DisputeMessage(
            dispute_id=dispute.id,
            sender_id=admin.id,
            sender_role="ADMIN",
            message=clean_msg,
        )
        db.add(msg)

        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="ADMIN_REQUESTED_EVIDENCE",
            resource_type="DISPUTE",
            resource_id=dispute.dispute_id,
            details=f"Admin requested additional evidence: {clean_msg}."
        ))

        if dispute.order:
            db.add(Notification(
                user_id=dispute.order.buyer_id,
                title="Admin Requested Evidence",
                message=f"Admin reviewing dispute #{dispute.dispute_id} requested: '{clean_msg}'. Please update your payment proof.",
                type="WARNING",
                link="/dashboard"
            ))
            db.add(Notification(
                user_id=dispute.order.seller_id,
                title="Admin Review Update",
                message=f"Admin requested evidence on dispute #{dispute.dispute_id}: '{clean_msg}'.",
                type="INFO",
                link="/provider/dashboard"
            ))

        db.commit()
        db.refresh(dispute)
        return dispute

    @classmethod
    def admin_update_provider_status(
        cls,
        db: Session,
        admin: User,
        provider_id: int,
        new_status: str,
        reason: Optional[str] = None,
    ) -> ProviderAccount:
        """Block, suspend, or reactivate a provider."""
        status_norm = new_status.upper()
        if status_norm not in ("ACTIVE", "SUSPENDED", "BLOCKED"):
            raise HTTPException(status_code=400, detail=f"Invalid provider status '{new_status}'. Allowed: ACTIVE, SUSPENDED, BLOCKED.")

        provider = db.query(ProviderAccount).filter(ProviderAccount.id == provider_id).first()
        if not provider:
            raise HTTPException(status_code=404, detail="Provider account not found.")

        prev_status = provider.status
        provider.status = status_norm

        db.add(SellerStatusHistory(
            provider_id=provider.id,
            admin_id=admin.id,
            previous_status=prev_status,
            new_status=status_norm,
            reason=reason or f"Status changed by admin {admin.email}",
        ))

        action_name = "SELLER_BLOCKED" if status_norm == "BLOCKED" else ("SELLER_UNBLOCKED" if status_norm == "ACTIVE" else "SELLER_SUSPENDED")

        db.add(AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action=action_name,
            resource_type="PROVIDER",
            resource_id=str(provider.id),
            details=f"Admin changed provider #{provider.id} status from {prev_status} to {status_norm}. Reason: {reason}."
        ))

        db.add(Notification(
            user_id=provider.user_id,
            title=f"Account Status Changed: {status_norm}",
            message=f"Your provider account status has been updated to {status_norm}. Reason: {reason or 'Administrative review'}.",
            type="ERROR" if status_norm == "BLOCKED" else "INFO",
            link="/provider/dashboard"
        ))

        db.commit()
        db.refresh(provider)
        return provider
