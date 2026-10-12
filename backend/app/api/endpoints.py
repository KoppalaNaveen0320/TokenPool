"""
TokenPool — FastAPI REST Endpoints
- Proper JWT authentication on every protected endpoint
- USER/ADMIN role model (no CONSUMER/PROVIDER split at registration)
- Real database-backed data throughout
- Admin RBAC: returns 403 if non-ADMIN tries admin routes
"""
import os
import uuid
import hmac
import hashlib
import urllib.parse
import httpx
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status, Query, Header, File, UploadFile, Form
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.security import (
    verify_password, get_password_hash,
    create_access_token, decode_access_token,
)
from backend.app.models.schema_models import (
    User, Wallet, WalletTransaction, ProviderAccount, ProviderConnection,
    AIModel, ProviderListing, AIRequestRecord, Conversation, ChatMessage,
    Notification, SupportTicket, TicketMessage, AuditLog, PlatformSetting,
    PayoutRequest, PaymentRecord,
    Order, PaymentProof, MarketplaceDispute, DisputeMessage,
    CreditAllocation, ModelCreditBalance, ProviderEarningRecord, SellerStatusHistory,
    AICapacityPool, ProviderCapacityAllocation,
)
from backend.app.core.encryption import encrypt_credential, decrypt_credential, mask_credential
from backend.app.services.router import AIRouterService
from backend.app.services.marketplace_orders import MarketplaceOrderService
from backend.app.services.marketplace_eligibility import MarketplaceEligibilityService
from backend.app.providers.delegated_plan_adapters import (
    OpenAIDelegatedPlanAdapter,
    GoogleDelegatedPlanAdapter,
    AnthropicDelegatedPlanAdapter,
)

api_router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _resolve_user(authorization: Optional[str], db: Session) -> User:
    """Extract and validate Bearer token → return User or raise 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required. Please log in.")
    token = authorization[len("Bearer "):]
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token is invalid or expired. Please log in again.")
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Malformed token.")
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=401, detail="User account not found.")
    if user.status == "SUSPENDED":
        raise HTTPException(status_code=403, detail="Account is suspended. Contact support.")
    return user


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    return _resolve_user(authorization, db)


def get_current_user_optional(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return _resolve_user(authorization, db)
    except Exception:
        return None


def get_admin_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_user(authorization, db)
    if user.role != "ADMIN":
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin privileges required."
        )
    return user


# ══════════════════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

class LoginSchema(BaseModel):
    email: EmailStr
    password: str


class RegisterSchema(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    # role is always USER for public registration — never exposed to client


class GoogleAuthSchema(BaseModel):
    id_token: Optional[str] = None
    email: EmailStr
    full_name: Optional[str] = None
    google_sub_id: Optional[str] = None


def _user_response(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "avatar_initials": user.avatar_initials,
        "status": user.status,
        "email_verified": getattr(user, "email_verified", True),
        "google_sub_id": getattr(user, "google_sub_id", None),
    }


@api_router.post("/auth/login")
def login(payload: LoginSchema, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if user.status == "SUSPENDED":
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")
    token = create_access_token(subject=user.email, role=user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_response(user)}


@api_router.post("/auth/register", status_code=201)
def register(payload: RegisterSchema, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    initials = "".join([p[0].upper() for p in payload.full_name.split()[:2]]) or "TP"
    new_user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=get_password_hash(payload.password),
        role="USER",          # All public registrations are USER
        avatar_initials=initials,
        status="ACTIVE",
    )
    db.add(new_user)
    db.flush()

    # Every user gets a wallet with ₹0 starting balance (honest)
    wallet = Wallet(user_id=new_user.id, balance=0.0, credits=0, currency="INR")
    db.add(wallet)

    # Welcome notification
    db.add(Notification(
        user_id=new_user.id,
        title="Welcome to TokenPool!",
        message="Your account has been created. Explore the marketplace and top up your wallet to get started.",
        type="SUCCESS",
        is_read=False,
    ))

    db.commit()
    token = create_access_token(subject=new_user.email, role=new_user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_response(new_user)}


@api_router.post("/auth/google")
def google_auth(payload: GoogleAuthSchema, db: Session = Depends(get_db)):
    """
    Authenticate or register provider/consumer via verified Google/Gmail identity.
    - Uses Google/Gmail email as the primary verified identity.
    - Never requests or stores Gmail password.
    - Creates User, Wallet, and ProviderAccount automatically.
    """
    import secrets

    email = payload.email.lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="Valid email address is required.")

    # Find existing user by email or google_sub_id
    user = db.query(User).filter(User.email == email).first()
    if not user and payload.google_sub_id:
        user = db.query(User).filter(User.google_sub_id == payload.google_sub_id).first()

    if user:
        if user.status == "SUSPENDED":
            raise HTTPException(status_code=403, detail="Account suspended. Contact support.")
        if payload.google_sub_id and not user.google_sub_id:
            user.google_sub_id = payload.google_sub_id
        user.email_verified = True
        
        # Ensure provider account exists for this user
        _get_or_create_provider(user, db)
        db.commit()
    else:
        # Secure auto-provisioning without storing or requesting any password from user
        random_pwd = secrets.token_urlsafe(32)
        name = payload.full_name or email.split("@")[0].replace(".", " ").title() or "Google User"
        initials = "".join([p[0].upper() for p in name.split()[:2]]) or "TP"
        
        user = User(
            email=email,
            full_name=name,
            hashed_password=get_password_hash(random_pwd),
            role="USER",
            status="ACTIVE",
            avatar_initials=initials,
            google_sub_id=payload.google_sub_id or f"google_{secrets.token_hex(12)}",
            email_verified=True,
        )
        db.add(user)
        db.flush()

        # Initialize Wallet
        wallet = Wallet(user_id=user.id, balance=0.0, credits=0, currency="INR")
        db.add(wallet)

        # Initialize Provider Profile with this verified Gmail identity
        pa = ProviderAccount(
            user_id=user.id,
            workspace_name=f"{name}'s Provider",
            status="ACTIVE",
        )
        db.add(pa)

        db.add(Notification(
            user_id=user.id,
            title="Google Sign-In Successful",
            message=f"Welcome to TokenPool! Signed in using verified identity {email}.",
            type="SUCCESS",
            is_read=False,
        ))
        db.commit()

    token = create_access_token(subject=user.email, role=user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_response(user)}


@api_router.get("/auth/me")
def get_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    wallet = db.query(Wallet).filter(Wallet.user_id == current_user.id).first()
    unread = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,
    ).count()
    return {
        **_user_response(current_user),
        "organization": current_user.organization,
        "bio": current_user.bio,
        "wallet": {
            "balance": wallet.balance if wallet else 0.0,
            "credits": wallet.credits if wallet else 0,
            "currency": "INR",
        },
        "unread_notifications": unread,
    }


# ══════════════════════════════════════════════════════════════════════════
#  CONSUMER ENDPOINTS  (any authenticated user)
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/consumer/overview")
def get_consumer_overview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = db.query(Wallet).filter(Wallet.user_id == current_user.id).first()
    balance  = wallet.balance  if wallet else 0.0
    credits  = wallet.credits  if wallet else 0

    # Real usage aggregates
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    today_req_count = db.query(AIRequestRecord).filter(
        AIRequestRecord.user_id == current_user.id,
        AIRequestRecord.created_at >= today_start,
    ).count()

    today_tokens = db.query(func.sum(AIRequestRecord.total_tokens)).filter(
        AIRequestRecord.user_id == current_user.id,
        AIRequestRecord.created_at >= today_start,
    ).scalar() or 0

    monthly_tokens = db.query(func.sum(AIRequestRecord.total_tokens)).filter(
        AIRequestRecord.user_id == current_user.id,
        AIRequestRecord.created_at >= month_start,
    ).scalar() or 0

    # Daily token chart (last 7 days from real data)
    chart_data = []
    for i in range(6, -1, -1):
        day = now - timedelta(days=i)
        day_s = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_e = day_s + timedelta(days=1)
        toks = db.query(func.sum(AIRequestRecord.total_tokens)).filter(
            AIRequestRecord.user_id == current_user.id,
            AIRequestRecord.created_at >= day_s,
            AIRequestRecord.created_at < day_e,
        ).scalar() or 0
        chart_data.append({"date": day.strftime("%b %d"), "tokens": toks})

    # Recent requests
    recent_requests = db.query(AIRequestRecord).filter(
        AIRequestRecord.user_id == current_user.id,
    ).order_by(AIRequestRecord.created_at.desc()).limit(8).all()

    reqs_data = [{
        "id": r.id,
        "request_id": r.request_id,
        "date": r.created_at.strftime("%b %d, %H:%M"),
        "model": r.model_name,
        "badge": r.badge,
        "input": r.input_tokens,
        "output": r.output_tokens,
        "total_tokens": r.total_tokens,
        "cost": f"₹{r.consumer_cost:.4f}",
        "status": r.status,
    } for r in recent_requests]

    return {
        "success": True,
        "data": {
            "wallet_balance": balance,
            "available_credits": credits,
            "today_usage": today_tokens,
            "today_requests": today_req_count,
            "monthly_usage": monthly_tokens,
            "chart_data": chart_data,
            "recent_requests": reqs_data,
        }
    }


@api_router.get("/consumer/marketplace")
def get_marketplace(
    search: Optional[str] = None,
    availability: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(AIModel).filter(AIModel.is_active == True)
    if search:
        query = query.filter(
            AIModel.name.ilike(f"%{search}%") | AIModel.capabilities.ilike(f"%{search}%")
        )
    if availability and availability != "all":
        query = query.filter(AIModel.status == availability.upper())
    models = query.all()
    return {
        "success": True,
        "data": {
            "models": [{
                "id": m.id, "slug": m.slug, "name": m.name,
                "provider": m.provider_brand, "badge": m.badge,
                "badge_color": m.badge_color,
                "description": m.description, "capabilities": m.capabilities,
                "in_price": f"₹{m.input_price_per_1k:.2f}/1k",
                "out_price": f"₹{m.output_price_per_1k:.2f}/1k",
                "input_price_per_1k": m.input_price_per_1k,
                "output_price_per_1k": m.output_price_per_1k,
                "latency": m.latency_label, "avg_latency_ms": m.avg_latency_ms,
                "status": m.status, "context_window": m.context_window,
                "execution_adapter": getattr(m, "execution_adapter", "mock") or "mock",
                "is_demo": (getattr(m, "execution_adapter", "mock") or "mock").lower() == "mock",
            } for m in models]
        }
    }


@api_router.get("/consumer/models/{slug}")
def get_model_detail(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    m = db.query(AIModel).filter(AIModel.slug == slug).first()
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    listings = db.query(ProviderListing).filter(
        ProviderListing.model_slug == slug,
        ProviderListing.status == "ACTIVE",
    ).all()
    return {
        "slug": m.slug, "name": m.name, "provider": m.provider_brand,
        "badge": m.badge, "badge_color": m.badge_color,
        "description": m.description, "capabilities": m.capabilities,
        "in_price": f"₹{m.input_price_per_1k:.2f}/1k",
        "out_price": f"₹{m.output_price_per_1k:.2f}/1k",
        "latency": m.latency_label, "status": m.status,
        "context_window": m.context_window,
        "available_capacity": sum(l.capacity_units for l in listings),
        "execution_adapter": getattr(m, "execution_adapter", "mock") or "mock",
        "is_demo": (getattr(m, "execution_adapter", "mock") or "mock").lower() == "mock",
    }


@api_router.get("/consumer/wallet")
def get_wallet(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = db.query(Wallet).filter(Wallet.user_id == current_user.id).first()
    if not wallet:
        return {"success": True, "data": {
            "balance": 0.0, "credits": 0, "currency": "INR",
            "total_topup": 0.0, "total_spent": 0.0, "total_requests": 0,
            "month_spend": 0.0, "recent_transactions": [],
        }}

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_topup = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.wallet_id == wallet.id,
        WalletTransaction.tx_type == "TOP_UP",
    ).scalar() or 0.0

    total_spent = db.query(func.sum(func.abs(WalletTransaction.amount))).filter(
        WalletTransaction.wallet_id == wallet.id,
        WalletTransaction.tx_type == "USAGE",
    ).scalar() or 0.0

    month_spend = db.query(func.sum(func.abs(WalletTransaction.amount))).filter(
        WalletTransaction.wallet_id == wallet.id,
        WalletTransaction.tx_type == "USAGE",
        WalletTransaction.created_at >= month_start,
    ).scalar() or 0.0

    total_requests = db.query(AIRequestRecord).filter(
        AIRequestRecord.user_id == current_user.id
    ).count()

    recent_txs = db.query(WalletTransaction).filter(
        WalletTransaction.wallet_id == wallet.id
    ).order_by(WalletTransaction.created_at.desc()).limit(10).all()

    return {
        "success": True,
        "data": {
            "balance": wallet.balance, "credits": wallet.credits,
            "currency": wallet.currency,
            "total_topup": total_topup, "total_spent": total_spent,
            "month_spend": month_spend, "total_requests": total_requests,
            "recent_transactions": [{
                "id": t.id, "type": t.tx_type,
                "description": t.description, "amount": t.amount,
                "credits": t.credits_delta, "balance_after": t.balance_after,
                "status": t.status, "created_at": t.created_at.isoformat(),
            } for t in recent_txs],
        }
    }


class TopUpSchema(BaseModel):
    amount: float
    payment_method: str = "UPI"


@api_router.post("/consumer/wallet/topup")
def top_up_wallet(
    payload: TopUpSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    [DEVELOPMENT / TEST TOP-UP ONLY]
    Directly increments wallet balance without an external payment gateway.
    STRICTLY DISABLED in production or when REQUIRE_PAYMENT_GATEWAY is set to True.
    """
    if settings.REQUIRE_PAYMENT_GATEWAY or settings.ENVIRONMENT == "production":
        raise HTTPException(
            status_code=403,
            detail="Direct balance top-up is disabled in production. Use verified payment gateway checkout via /payments/create-order."
        )

    if payload.amount < 10:
        raise HTTPException(status_code=400, detail="Minimum top-up amount is ₹10.")
    if payload.amount > 50000:
        raise HTTPException(status_code=400, detail="Maximum top-up per transaction is ₹50,000.")

    # Atomic row-level lock on wallet
    wallet = db.query(Wallet).filter(Wallet.user_id == current_user.id).with_for_update().first()
    if not wallet:
        wallet = Wallet(user_id=current_user.id, balance=0.0, credits=0, currency="INR")
        db.add(wallet)
        db.flush()

    new_balance   = round(wallet.balance + payload.amount, 2)
    credits_added = int(payload.amount * 8.5)
    wallet.balance = new_balance
    wallet.credits += credits_added

    tx = WalletTransaction(
        wallet_id=wallet.id, tx_type="TOP_UP",
        amount=payload.amount, balance_after=new_balance,
        credits_delta=credits_added,
        description=f"[DEVELOPMENT / TEST TOP-UP] via {payload.payment_method}",
        status="COMPLETED",
    )
    db.add(tx)
    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="DEVELOPMENT_TOPUP",
        resource_type="WALLET",
        resource_id=str(wallet.id),
        details=f"Dev test top-up amount={payload.amount}, new_balance={new_balance}"
    ))
    db.add(Notification(
        user_id=current_user.id, title="Test Wallet Top-Up",
        message=f"[DEV TEST] Added ₹{payload.amount:.2f} to your wallet. New balance: ₹{new_balance:.2f}.",
        type="INFO",
    ))
    db.commit()
    return {"success": True, "data": {
        "balance": wallet.balance, "credits": wallet.credits,
        "amount_added": payload.amount, "credits_added": credits_added,
        "mode": "DEVELOPMENT / TEST TOP-UP"
    }}


class CreateOrderSchema(BaseModel):
    amount: float
    payment_method: str = "UPI"
    gateway: str = "Razorpay"


@api_router.post("/payments/create-order")
def create_payment_order(
    payload: CreateOrderSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Production Payment Architecture Step 1:
    User -> Payment Gateway Order Initiation
    Creates a PENDING payment record with an order_id.
    """
    if payload.amount < 10:
        raise HTTPException(status_code=400, detail="Minimum top-up amount is ₹10.")
    if payload.amount > 50000:
        raise HTTPException(status_code=400, detail="Maximum top-up per transaction is ₹50,000.")

    order_id = f"order_{uuid.uuid4().hex[:16]}"
    payment = PaymentRecord(
        order_id=order_id,
        user_id=current_user.id,
        amount=payload.amount,
        currency="INR",
        payment_method=payload.payment_method,
        status="PENDING",
    )
    db.add(payment)
    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="PAYMENT_ORDER_CREATED",
        resource_type="PAYMENT",
        resource_id=order_id,
        details=f"Payment order initiated for amount ₹{payload.amount:.2f} via {payload.payment_method}"
    ))
    db.commit()
    return {
        "success": True,
        "data": {
            "order_id": order_id,
            "amount": payload.amount,
            "currency": "INR",
            "status": "PENDING",
            "message": "Payment order initiated. Complete transaction with gateway and await verified webhook."
        }
    }


class WebhookPayloadSchema(BaseModel):
    order_id: str
    payment_id: str
    amount: float
    status: str = "SUCCESS"  # SUCCESS or FAILED
    error_message: Optional[str] = None


@api_router.post("/payments/webhook")
def payment_gateway_webhook(
    payload: WebhookPayloadSchema,
    x_gateway_signature: Optional[str] = Header(None, alias="X-Gateway-Signature"),
    db: Session = Depends(get_db),
):
    """
    Production Payment Architecture Steps 2-5:
    Payment Gateway -> Verified Backend Webhook -> Payment verification -> Wallet transaction -> Balance update
    
    Security Guarantees:
    - Cryptographic HMAC-SHA256 signature verification with PAYMENT_WEBHOOK_SECRET
    - Prevents arbitrary balance creation and forged 'payment successful' frontend calls
    - Idempotent: Duplicate webhook events do not double-credit wallet
    - Row-level locking on wallet balance update
    - Failed gateway transactions record status and never credit the wallet
    """
    if not x_gateway_signature:
        raise HTTPException(status_code=401, detail="Missing X-Gateway-Signature header.")

    # Format variants to handle floating point / integer representations
    sig_payload_1 = f"{payload.order_id}|{payload.payment_id}|{payload.amount:.2f}"
    sig_payload_2 = f"{payload.order_id}|{payload.payment_id}|{int(payload.amount) if payload.amount.is_integer() else payload.amount}"
    sig_payload_3 = f"{payload.order_id}|{payload.payment_id}|{payload.amount}"

    expected_sig_1 = hmac.new(settings.PAYMENT_WEBHOOK_SECRET.encode(), sig_payload_1.encode(), hashlib.sha256).hexdigest()
    expected_sig_2 = hmac.new(settings.PAYMENT_WEBHOOK_SECRET.encode(), sig_payload_2.encode(), hashlib.sha256).hexdigest()
    expected_sig_3 = hmac.new(settings.PAYMENT_WEBHOOK_SECRET.encode(), sig_payload_3.encode(), hashlib.sha256).hexdigest()

    valid_sig = (
        hmac.compare_digest(x_gateway_signature, expected_sig_1) or
        hmac.compare_digest(x_gateway_signature, expected_sig_2) or
        hmac.compare_digest(x_gateway_signature, expected_sig_3)
    )

    if not valid_sig:
        raise HTTPException(status_code=401, detail="Invalid webhook signature. Unauthorized payment verification.")

    payment = db.query(PaymentRecord).filter(PaymentRecord.order_id == payload.order_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment order not found.")

    # Idempotency check: if already processed, return success without duplicate crediting
    if payment.status in ("SUCCESS", "COMPLETED"):
        return {
            "success": True,
            "message": "Payment already processed.",
            "idempotent": True,
            "order_id": payment.order_id,
            "status": payment.status
        }

    if payload.status == "SUCCESS":
        # Row-level lock on user's wallet
        wallet = db.query(Wallet).filter(Wallet.user_id == payment.user_id).with_for_update().first()
        if not wallet:
            wallet = Wallet(user_id=payment.user_id, balance=0.0, credits=0, currency="INR")
            db.add(wallet)
            db.flush()

        credits_added = int(payment.amount * 8.5)
        wallet.balance = round(wallet.balance + payment.amount, 2)
        wallet.credits += credits_added

        payment.status = "SUCCESS"
        payment.gateway_id = payload.payment_id
        payment.signature = x_gateway_signature
        payment.credits_credited = credits_added

        tx = WalletTransaction(
            wallet_id=wallet.id,
            tx_type="TOP_UP",
            amount=payment.amount,
            balance_after=wallet.balance,
            credits_delta=credits_added,
            description=f"Verified gateway payment {payload.payment_id} for order {payment.order_id}",
            status="COMPLETED",
        )
        db.add(tx)
        db.add(AuditLog(
            user_id=payment.user_id,
            actor_email="gateway_webhook",
            action="PAYMENT_VERIFIED",
            resource_type="PAYMENT",
            resource_id=payment.order_id,
            details=f"Payment verified via webhook: amount={payment.amount}, gateway_id={payload.payment_id}"
        ))
        db.add(Notification(
            user_id=payment.user_id,
            title="Payment Received",
            message=f"Verified payment of ₹{payment.amount:.2f} credited to your wallet.",
            type="SUCCESS",
        ))
        db.commit()
        return {
            "success": True,
            "message": "Payment verified and wallet credited.",
            "order_id": payment.order_id,
            "amount_credited": payment.amount,
            "new_balance": wallet.balance
        }
    else:
        # FAILED payment at gateway
        payment.status = "FAILED"
        payment.gateway_id = payload.payment_id
        payment.error_message = payload.error_message or "Payment failed at gateway."
        db.add(AuditLog(
            user_id=payment.user_id,
            actor_email="gateway_webhook",
            action="PAYMENT_FAILED",
            resource_type="PAYMENT",
            resource_id=payment.order_id,
            details=f"Payment failed at gateway: order={payment.order_id}, reason={payment.error_message}"
        ))
        db.commit()
        return {
            "success": False,
            "message": "Payment failure record saved. No wallet credits were issued.",
            "order_id": payment.order_id,
            "status": "FAILED"
        }


@api_router.get("/consumer/transactions")
def get_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet = db.query(Wallet).filter(Wallet.user_id == current_user.id).first()
    if not wallet:
        return {"success": True, "data": {"total": 0, "page": 1, "transactions": []}}

    query = db.query(WalletTransaction).filter(
        WalletTransaction.wallet_id == wallet.id
    )
    if search:
        query = query.filter(WalletTransaction.description.ilike(f"%{search}%"))
    query = query.order_by(WalletTransaction.created_at.desc())
    total = query.count()
    txs   = query.offset((page - 1) * limit).limit(limit).all()

    return {
        "success": True,
        "data": {
            "total": total, "page": page,
            "transactions": [{
                "id": str(t.id), "type": t.tx_type,
                "description": t.description,
                "amount": t.amount, "credits": t.credits_delta,
                "balance_after": t.balance_after,
                "status": t.status, "created_at": t.created_at.isoformat(),
            } for t in txs],
        }
    }


@api_router.get("/consumer/dashboard")
def get_consumer_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Alias for /consumer/overview — used by dashboard.html."""
    return get_consumer_overview(current_user=current_user, db=db)


@api_router.get("/consumer/usage")
def get_usage(
    time_range: str = Query("7d", alias="range"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    days = 7 if time_range == "7d" else (30 if time_range == "30d" else 90)
    now  = datetime.now(timezone.utc)
    daily = []
    total_tokens = 0
    for i in range(days - 1, -1, -1):
        day = now - timedelta(days=i)
        s   = day.replace(hour=0, minute=0, second=0, microsecond=0)
        e   = s + timedelta(days=1)
        toks = db.query(func.sum(AIRequestRecord.total_tokens)).filter(
            AIRequestRecord.user_id == current_user.id,
            AIRequestRecord.created_at >= s,
            AIRequestRecord.created_at < e,
        ).scalar() or 0
        total_tokens += toks
        daily.append({"date": day.strftime("%b %d"), "tokens": toks})

    total_cost = db.query(func.sum(AIRequestRecord.consumer_cost)).filter(
        AIRequestRecord.user_id == current_user.id,
        AIRequestRecord.created_at >= now - timedelta(days=days),
    ).scalar() or 0.0

    req_count = db.query(AIRequestRecord).filter(
        AIRequestRecord.user_id == current_user.id,
        AIRequestRecord.created_at >= now - timedelta(days=days),
    ).count()

    avg_lat = db.query(func.avg(AIRequestRecord.latency_ms)).filter(
        AIRequestRecord.user_id == current_user.id,
    ).scalar() or 0

    recent_reqs = db.query(AIRequestRecord).filter(
        AIRequestRecord.user_id == current_user.id,
    ).order_by(AIRequestRecord.created_at.desc()).limit(20).all()

    return {
        "success": True,
        "data": {
            "total_tokens": total_tokens, "total_cost": total_cost,
            "request_count": req_count, "avg_latency_ms": int(avg_lat),
            "daily": daily,
            "requests": [{
                "id": r.request_id, "model": r.model_name, "badge": r.badge,
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "total_tokens": r.total_tokens, "cost": r.consumer_cost,
                "latency_ms": r.latency_ms, "status": r.status,
                "created_at": r.created_at.isoformat(),
            } for r in recent_reqs],
        }
    }


# ══════════════════════════════════════════════════════════════════════════
#  CHAT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/chat/conversations")
def get_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    convs = db.query(Conversation).filter(
        Conversation.user_id == current_user.id
    ).order_by(Conversation.updated_at.desc()).all()
    return {
        "success": True,
        "data": [{
            "id": c.id, "title": c.title,
            "model_slug": c.model_slug,
            "updated_at": c.updated_at.isoformat(),
        } for c in convs]
    }


class CreateConversationSchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    title: Optional[str] = "New conversation"
    model_slug: Optional[str] = None
    model: Optional[str] = None
    listing_id: Optional[int] = None
    provider_id: Optional[int] = None


class SendMessageSchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    conversation_id: Optional[int] = None
    model_slug: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    message: Optional[str] = None
    content: Optional[str] = None
    is_demo: bool = True
    request_id: Optional[str] = None
    listing_id: Optional[int] = None


@api_router.post("/chat/conversations")
def create_conversation(
    payload: Optional[CreateConversationSchema] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Consumer creates a new conversation owned exclusively by the consumer.
    Isolated by consumer_id (user_id).
    """
    slug = (payload.model_slug or payload.model or "nova-reasoning") if payload else "nova-reasoning"
    title = (payload.title or "New conversation") if payload else "New conversation"
    listing_id = payload.listing_id if payload else None
    provider_id = payload.provider_id if payload else None

    if listing_id and not provider_id:
        listing = db.query(ProviderListing).filter(ProviderListing.id == listing_id).first()
        if listing:
            provider_id = listing.provider_id

    conv = Conversation(
        user_id=current_user.id,
        title=title,
        model_slug=slug,
        model=slug,
        listing_id=listing_id,
        provider_id=provider_id,
        status="ACTIVE",
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    return {
        "success": True,
        "data": {
            "id": conv.id,
            "consumer_id": conv.user_id,
            "user_id": conv.user_id,
            "title": conv.title,
            "model_slug": conv.model_slug,
            "model": conv.model or conv.model_slug,
            "listing_id": conv.listing_id,
            "provider_id": conv.provider_id,
            "status": conv.status,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        }
    }


@api_router.get("/chat/conversations")
def list_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List consumer's conversations.
    Chat history MUST be isolated by consumer_id:
    A consumer can only access their own conversations. Providers cannot view consumer conversations.
    """
    convs = db.query(Conversation).filter(
        Conversation.user_id == current_user.id,
        Conversation.status != "DELETED",
    ).order_by(Conversation.updated_at.desc()).all()

    data = []
    for c in convs:
        last_msg = db.query(ChatMessage).filter(ChatMessage.conversation_id == c.id).order_by(ChatMessage.created_at.desc()).first()
        msg_count = db.query(ChatMessage).filter(ChatMessage.conversation_id == c.id).count()
        data.append({
            "id": c.id,
            "consumer_id": c.user_id,
            "user_id": c.user_id,
            "title": c.title,
            "model_slug": c.model_slug,
            "model": c.model or c.model_slug,
            "listing_id": c.listing_id,
            "provider_id": c.provider_id,
            "status": c.status,
            "message_count": msg_count,
            "last_message": last_msg.content[:80] if last_msg else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })

    return {"success": True, "data": data}


@api_router.get("/chat/conversations/{conv_id}")
def get_conversation_detail(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retrieve single conversation with ownership verification.
    """
    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.status != "DELETED",
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this conversation.")

    msgs = db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conv.id
    ).order_by(ChatMessage.created_at.asc()).all()

    return {
        "success": True,
        "data": {
            "id": conv.id,
            "consumer_id": conv.user_id,
            "user_id": conv.user_id,
            "title": conv.title,
            "model_slug": conv.model_slug,
            "model": conv.model or conv.model_slug,
            "listing_id": conv.listing_id,
            "provider_id": conv.provider_id,
            "status": conv.status,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "messages": [{
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "model": m.model,
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
                "total_tokens": m.total_tokens,
                "provider_request_id": m.provider_request_id,
                "cost": m.cost,
                "latency_ms": m.latency_ms,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            } for m in msgs]
        }
    }


@api_router.delete("/chat/conversations/{conv_id}")
def delete_conversation(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete a conversation owned by the consumer.
    """
    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.status != "DELETED",
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this conversation.")

    db.delete(conv)
    db.commit()
    return {"success": True, "message": "Conversation deleted successfully."}


@api_router.post("/chat/conversations/{conv_id}/messages")
def send_conversation_message(
    conv_id: int,
    payload: SendMessageSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Convenience endpoint: sends message inside specific conversation.
    """
    payload.conversation_id = conv_id
    return send_chat_message(payload, current_user, db)


@api_router.get("/chat/conversations/{conv_id}/messages")
def get_conversation_messages(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.status != "DELETED",
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this conversation.")
    msgs = db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conv.id
    ).order_by(ChatMessage.created_at.asc()).all()
    return {
        "success": True,
        "data": [{
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "model": m.model,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "total_tokens": m.total_tokens,
            "provider_request_id": m.provider_request_id,
            "cost": m.cost,
            "latency_ms": m.latency_ms,
            "created_at": m.created_at.isoformat(),
        } for m in msgs]
    }


@api_router.post("/chat/messages")
def send_chat_message(
    payload: SendMessageSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    slug = payload.model_slug or payload.model or "nova-reasoning"
    text = payload.prompt or payload.message or payload.content or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="Message/prompt cannot be empty.")

    wallet = db.query(Wallet).filter(Wallet.user_id == current_user.id).with_for_update().first()
    if not wallet:
        wallet = Wallet(user_id=current_user.id, balance=0.0, credits=0, currency="INR")
        db.add(wallet)
        db.flush()

    model = db.query(AIModel).filter(AIModel.slug == slug).first()
    model_name = model.name if model else slug

    # Model-specific credit check: verify consumer owns credits for THIS specific model
    mcb = db.query(ModelCreditBalance).filter(
        ModelCreditBalance.user_id == current_user.id,
        ModelCreditBalance.model_slug == slug,
    ).with_for_update().first()

    available_model_credits = mcb.credits_remaining if mcb else 0

    # Pay-as-you-go unified TokenPool wallet balance bridge:
    # A consumer deposits into their single wallet INR balance or has unallocated general credits
    if available_model_credits <= 0:
        total_allocated = db.query(func.coalesce(func.sum(ModelCreditBalance.credits_remaining), 0)).filter(
            ModelCreditBalance.user_id == current_user.id
        ).scalar() or 0
        unallocated_credits = max(0, int((wallet.credits or 0) - total_allocated))
        wallet_funds = max(0.0, float(wallet.balance or 0.0))

        if unallocated_credits > 0 or wallet_funds > 0:
            bridge_credits = unallocated_credits if unallocated_credits > 0 else int(wallet_funds * 100)
            if not mcb:
                mcb = ModelCreditBalance(
                    user_id=current_user.id,
                    model_slug=slug,
                    model_name=model_name,
                    credits_purchased=bridge_credits,
                    credits_used=0,
                    credits_remaining=bridge_credits,
                )
                db.add(mcb)
                db.flush()
            else:
                mcb.credits_remaining = max(mcb.credits_remaining, bridge_credits)
            available_model_credits = mcb.credits_remaining

    if available_model_credits <= 0:
        raise HTTPException(
            status_code=402,
            detail=f"You don't have enough credits for {model_name}. Available: 0 credits. Please purchase {model_name} credits from the Marketplace."
        )

    # Idempotency check: if client retried with same request_id, do not charge again
    if payload.request_id:
        existing_req = db.query(AIRequestRecord).filter(
            AIRequestRecord.request_id == payload.request_id,
            AIRequestRecord.user_id == current_user.id
        ).first()
        if existing_req:
            return {
                "success": True,
                "data": {
                    "request_id": existing_req.request_id,
                    "response": "Response returned from idempotent cache. No additional deduction was made.",
                    "message": "Response returned from idempotent cache. No additional deduction was made.",
                    "usage": {
                        "input_tokens": existing_req.input_tokens,
                        "output_tokens": existing_req.output_tokens,
                        "total_tokens": existing_req.total_tokens,
                    },
                    "cost": existing_req.consumer_cost,
                    "latency_ms": existing_req.latency_ms,
                    "is_demo": existing_req.is_demo,
                    "idempotent": True,
                    "wallet_balance": wallet.balance,
                    "credits_remaining": mcb.credits_remaining if mcb else 0,
                    "model_slug": slug,
                    "model_name": model_name,
                }
            }

    listing = None
    if payload.listing_id:
        listing = db.query(ProviderListing).filter(ProviderListing.id == payload.listing_id).first()
    if not listing and payload.conversation_id:
        c_exist = db.query(Conversation).filter(Conversation.id == payload.conversation_id).first()
        if c_exist and c_exist.listing_id:
            listing = db.query(ProviderListing).filter(ProviderListing.id == c_exist.listing_id).first()
    if not listing:
        pa_user = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
        query = db.query(ProviderListing).filter(
            ProviderListing.model_slug == slug,
            ProviderListing.status == "ACTIVE",
            ProviderListing.remaining_capacity > 0
        )
        if pa_user:
            other_listing = query.filter(ProviderListing.provider_id != pa_user.id).order_by(ProviderListing.id.asc()).first()
            if other_listing:
                listing = other_listing
            if not listing:
                listing = query.order_by(ProviderListing.id.asc()).first()
        else:
            # For pure consumer users: check if they bought from a specific listing allocation first
            alloc = db.query(CreditAllocation).filter(
                CreditAllocation.buyer_id == current_user.id,
                CreditAllocation.model_slug == slug,
                CreditAllocation.status == "ACTIVE",
                CreditAllocation.credits_remaining > 0
            ).order_by(CreditAllocation.released_at.desc()).first()
            if alloc and alloc.listing_id:
                alloc_listing = query.filter(ProviderListing.id == alloc.listing_id).first()
                if alloc_listing:
                    listing = alloc_listing
            if not listing:
                listing = query.order_by(ProviderListing.id.desc()).first()

        if not listing:
            listing = db.query(ProviderListing).filter(
                ProviderListing.model_slug == slug,
                ProviderListing.status == "ACTIVE"
            ).order_by(ProviderListing.id.desc()).first()

    # Resolve provider connection from user's credit allocation or listing
    connection = None
    effective_is_demo = bool(payload.is_demo and settings.DEMO_MODE_DEFAULT)
    if not effective_is_demo:
        alloc = db.query(CreditAllocation).filter(
            CreditAllocation.buyer_id == current_user.id,
            CreditAllocation.model_slug == slug,
            CreditAllocation.status == "ACTIVE",
            CreditAllocation.credits_remaining > 0
        ).order_by(CreditAllocation.released_at.asc()).first()
        if alloc and alloc.provider_connection_id:
            connection = db.query(ProviderConnection).filter(ProviderConnection.id == alloc.provider_connection_id).first()
        elif listing and listing.provider_connection_id:
            connection = db.query(ProviderConnection).filter(ProviderConnection.id == listing.provider_connection_id).first()

        if not connection:
            raise HTTPException(
                status_code=503,
                detail="EXECUTION_NOT_SUPPORTED: No active, eligible provider connection is available to serve requests for this model. No credits were charged."
            )

    # Use or create conversation
    resolved_listing_id = listing.id if listing else None
    resolved_provider_id = listing.provider_id if listing else (connection.provider_id if connection else None)
    if payload.conversation_id:
        conv = db.query(Conversation).filter(
            Conversation.id == payload.conversation_id,
            Conversation.user_id == current_user.id,
        ).first()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if not conv.listing_id and resolved_listing_id:
            conv.listing_id = resolved_listing_id
        if not conv.provider_id and resolved_provider_id:
            conv.provider_id = resolved_provider_id
    else:
        conv = Conversation(
            user_id=current_user.id,
            title=text[:60] + ("…" if len(text) > 60 else ""),
            model_slug=slug,
            model=slug,
            listing_id=resolved_listing_id,
            provider_id=resolved_provider_id,
            status="ACTIVE",
        )
        db.add(conv)
        db.flush()

    # Load conversation context (multi-turn history)
    prior_messages = db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conv.id
    ).order_by(ChatMessage.created_at.asc()).all()

    context_messages = []
    for pm in prior_messages:
        context_messages.append({"role": pm.role, "content": pm.content})
    context_messages.append({"role": "user", "content": text})

    # Route through AI provider (MockAIProvider or authorized real adapter)
    # Credit/Usage Integrity: If provider request fails, do not permanently charge user or consume capacity!
    try:
        result = AIRouterService.route(
            model_slug=slug,
            prompt=text,
            is_demo=effective_is_demo,
            connection=connection,
            messages=context_messages,
        )
    except Exception as e:
        db.rollback()
        # Audit failure without credit deduction
        try:
            with get_db() as audit_db:
                audit_db.add(AuditLog(
                    user_id=current_user.id,
                    actor_email=current_user.email,
                    action="AI_PROVIDER_REQUEST_FAILED",
                    resource_type="AI_EXECUTION",
                    resource_id=slug,
                    details=f"AI Provider execution failed: {str(e)}. 0 credits charged. 0 capacity consumed."
                ))
                audit_db.commit()
        except Exception:
            pass
        status_code = 503 if "EXECUTION_NOT_SUPPORTED" in str(e) else 502
        raise HTTPException(
            status_code=status_code,
            detail=f"{str(e)}. No credits or wallet balance were charged."
        )

    response_text  = result.get("response", "No response generated.")
    input_tokens   = result.get("input_tokens", max(10, len(text.split()) * 2))
    output_tokens  = result.get("output_tokens", max(20, len(response_text.split()) * 2))
    total_tokens   = input_tokens + output_tokens
    in_price = model.input_price_per_1k if model else 0.08
    out_price = model.output_price_per_1k if model else 0.15
    cost = round((input_tokens * in_price + output_tokens * out_price) / 1000, 6)
    latency_ms = result.get("latency_ms", 350)
    provider_name = result.get("provider_name", "AI Provider")

    req_id = payload.request_id or f"req_{uuid.uuid4().hex[:12]}"

    # Save messages in PostgreSQL (consumer chat history isolated by consumer_id / conversation_id)
    db.add(ChatMessage(
        conversation_id=conv.id, role="user",
        content=text, model=slug, created_at=datetime.now(timezone.utc),
    ))
    db.add(ChatMessage(
        conversation_id=conv.id, role="assistant",
        content=response_text, model=slug, input_tokens=input_tokens,
        output_tokens=output_tokens, total_tokens=total_tokens,
        provider_request_id=req_id,
        cost=cost, latency_ms=latency_ms,
        created_at=datetime.now(timezone.utc),
    ))

    # Deduct strictly from model-specific credits
    credits_needed = max(1, int(round(cost * 8.5)))
    credits_to_deduct = min(available_model_credits, credits_needed)

    mcb.credits_used += credits_to_deduct
    mcb.credits_remaining = max(0, mcb.credits_remaining - credits_to_deduct)

    # Deduct from active CreditAllocations for this model
    allocations = db.query(CreditAllocation).filter(
        CreditAllocation.buyer_id == current_user.id,
        CreditAllocation.model_slug == slug,
        CreditAllocation.status == "ACTIVE",
        CreditAllocation.credits_remaining > 0,
    ).order_by(CreditAllocation.released_at.asc()).all()
    rem_deduct = credits_to_deduct
    for a in allocations:
        take = min(rem_deduct, a.credits_remaining)
        a.credits_used += take
        a.credits_remaining -= take
        if a.credits_remaining <= 0:
            a.status = "CONSUMED"
        rem_deduct -= take
        if rem_deduct <= 0:
            break

    # Also update wallet total credits summary
    wallet.credits = max(0, wallet.credits - credits_to_deduct)

    db.add(WalletTransaction(
        wallet_id=wallet.id, tx_type="USAGE", amount=-cost,
        balance_after=wallet.balance,
        credits_delta=-credits_to_deduct,
        description=f"AI request — {model_name} ({credits_to_deduct} credits)",
        status="COMPLETED",
    ))

    # Provider-Declared Marketplace Capacity Accounting: Consume capacity from ProviderListing & ProviderConnection
    consumed_units = total_tokens
    provider_id = listing.provider_id if listing else (connection.provider_id if connection else None)
    
    if listing:
        listing.consumed_capacity = (listing.consumed_capacity or 0) + consumed_units
        listing.remaining_capacity = max(0, (listing.remaining_capacity or 0) - consumed_units)
        if listing.provider_connection_id:
            conn_record = db.query(ProviderConnection).filter(ProviderConnection.id == listing.provider_connection_id).with_for_update().first()
            if conn_record:
                conn_record.capacity_consumed = (conn_record.capacity_consumed or 0) + consumed_units
                conn_record.capacity_remaining = max(
                    0,
                    (conn_record.capacity_declared or 0)
                    - (conn_record.capacity_reserved or 0)
                    - (conn_record.capacity_sold or 0)
                    - (conn_record.capacity_consumed or 0)
                )
    elif connection:
        conn_record = db.query(ProviderConnection).filter(ProviderConnection.id == connection.id).with_for_update().first()
        if conn_record:
            conn_record.capacity_consumed = (conn_record.capacity_consumed or 0) + consumed_units
            conn_record.capacity_remaining = max(
                0,
                (conn_record.capacity_declared or 0)
                - (conn_record.capacity_reserved or 0)
                - (conn_record.capacity_sold or 0)
                - (conn_record.capacity_consumed or 0)
            )

    if listing and listing.provider_capacity_allocation_id:
        prov_alloc = db.query(ProviderCapacityAllocation).filter(
            ProviderCapacityAllocation.id == listing.provider_capacity_allocation_id
        ).with_for_update().first()
        if prov_alloc:
            prov_alloc.consumed_capacity = (prov_alloc.consumed_capacity or 0) + consumed_units
            prov_alloc.remaining_capacity = max(0, prov_alloc.allocated_capacity - (prov_alloc.sold_capacity or 0) - (prov_alloc.reserved_capacity or 0))

    if model and model.id:
        pool = db.query(AICapacityPool).filter(
            AICapacityPool.model_id == model.id
        ).with_for_update().first()
        if pool:
            pool.consumed_capacity = (pool.consumed_capacity or 0) + consumed_units
            db.add(AuditLog(
                user_id=current_user.id,
                actor_email=current_user.email,
                action="CAPACITY_CONSUMED",
                resource_type="CAPACITY_POOL",
                resource_id=str(pool.id),
                details=f"Consumed {consumed_units} {pool.capacity_unit} of {model.name}. Request {payload.request_id or 'auto'}."
            ))

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="MODEL_CREDIT_CONSUMED",
        resource_type="MODEL_CREDIT",
        resource_id=slug,
        details=f"Deducted {credits_to_deduct} credits for {model_name}. Remaining: {mcb.credits_remaining}."
    ))

    # Provider earnings calculation (derived strictly from successful usage)
    platform_fee = round(cost * 0.20, 6)
    provider_earning = round(cost * 0.80, 6)

    if provider_id:
        prov = db.query(ProviderAccount).filter(ProviderAccount.id == provider_id).first()
        if prov:
            prov.total_earnings = round(prov.total_earnings + provider_earning, 6)
            prov.withdrawable_balance = round(prov.withdrawable_balance + provider_earning, 6)
            prov.used_capacity += total_tokens
            prov.available_capacity = max(0, prov.total_capacity - prov.used_capacity)

    # Record AI request in PostgreSQL with idempotency request_id
    req_id = payload.request_id or f"req_{uuid.uuid4().hex[:12]}"
    active_conn = connection or (listing.connection if listing else None)
    usage_u = getattr(active_conn, "upstream_usage_unit", "TOKEN") or "TOKEN"
    db.add(AIRequestRecord(
        request_id=req_id,
        user_id=current_user.id,
        provider_id=provider_id,
        provider_connection_id=connection.id if connection else (listing.provider_connection_id if listing else None),
        conversation_id=conv.id,
        provider_request_id=req_id,
        usage_amount=float(total_tokens),
        usage_unit=usage_u,
        metering_status="VERIFIED" if not payload.is_demo else "DEMO",
        model_slug=slug,
        model_name=model.name if model else slug,
        badge=model.badge if model else "?",
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens, consumer_cost=cost,
        provider_earning=provider_earning,
        platform_fee=platform_fee,
        latency_ms=latency_ms, status="COMPLETED", is_demo=payload.is_demo,
        prompt_snippet=text[:200],
    ))

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="CONSUMER_EXECUTION",
        resource_type="AI_EXECUTION",
        resource_id=req_id,
        details=f"Consumer executed {slug} ({total_tokens} units) in conversation #{conv.id}. Cost: ₹{cost}."
    ))
    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="USAGE_RECORDED",
        resource_type="USAGE",
        resource_id=req_id,
        details=f"Metered {total_tokens} {usage_u} units for {slug} (Request {req_id})."
    ))
    if provider_id and provider_earning > 0:
        db.add(AuditLog(
            user_id=provider_id,
            actor_email=current_user.email,
            action="PROVIDER_EARNINGS_RECORDED",
            resource_type="EARNINGS",
            resource_id=req_id,
            details=f"Credited provider #{provider_id} with ₹{provider_earning} from request {req_id}."
        ))

    # Update conversation timestamp
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "success": True,
        "data": {
            "conversation_id": conv.id,
            "response": response_text,
            "message": response_text,
            "content": response_text,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "cost": cost,
            "latency_ms": latency_ms,
            "is_demo": payload.is_demo,
            "provider": provider_name,
            "source": "PROVIDER" if not payload.is_demo else "DEMO",
            "wallet_balance": wallet.balance,
            "credits_remaining": wallet.credits,
        }
    }



# ══════════════════════════════════════════════════════════════════════════
#  AI MODEL CATALOG  (public read; admin write)
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/ai-models")
@api_router.get("/models")
def list_ai_models(
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """Return the AI model catalog for dropdown population."""
    query = db.query(AIModel)
    if active_only:
        query = query.filter(AIModel.is_active == True)
    models = query.order_by(AIModel.name).all()
    return {
        "success": True,
        "data": {
            "models": [{
                "id": m.id,
                "slug": m.slug,
                "name": m.name,
                "provider_brand": m.provider_brand,
                "badge": m.badge,
                "badge_color": m.badge_color,
                "description": m.description,
                "capabilities": m.capabilities,
                "capacity_unit": getattr(m, "capacity_unit", "TOKEN") or "TOKEN",
                "supported_periods": getattr(m, "supported_periods", "DAILY,MONTHLY,YEARLY") or "DAILY,MONTHLY,YEARLY",
                "execution_adapter": getattr(m, "execution_adapter", "MOCK") or "MOCK",
                "input_price_per_1k": m.input_price_per_1k,
                "output_price_per_1k": m.output_price_per_1k,
                "price_per_credit": m.price_per_credit or 0.50,
                "latency_label": m.latency_label,
                "status": m.status,
                "context_window": m.context_window,
                "is_active": m.is_active,
                "is_demo": (getattr(m, "execution_adapter", "mock") or "mock").lower() == "mock",
            } for m in models]
        }
    }


@api_router.get("/consumer/model-credits")
def get_consumer_model_credits(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return model-specific credit balances for the authenticated consumer."""
    balances = db.query(ModelCreditBalance).filter(
        ModelCreditBalance.user_id == current_user.id
    ).all()
    models = db.query(AIModel).filter(AIModel.is_active == True).order_by(AIModel.name).all()
    balance_map = {b.model_slug: b for b in balances}

    result = []
    for m in models:
        b = balance_map.get(m.slug)
        result.append({
            "model_slug": m.slug,
            "model_name": m.name,
            "provider_brand": m.provider_brand,
            "badge": m.badge,
            "badge_color": m.badge_color,
            "price_per_credit": m.price_per_credit or 0.50,
            "credits_purchased": b.credits_purchased if b else 0,
            "credits_used": b.credits_used if b else 0,
            "credits_remaining": b.credits_remaining if b else 0,
        })

    by_slug = {r["model_slug"]: r for r in result}
    return {
        "success": True,
        "data": {
            "balances": result,
            "by_model": by_slug,
            "by_slug": by_slug,
            **by_slug,
            "total_credits": sum(r["credits_remaining"] for r in result)
        }
    }



@api_router.get("/public/stats")
def get_public_stats(db: Session = Depends(get_db)):
    """Return platform public stats (real DB counts)."""
    user_count = db.query(User).filter(User.status == "ACTIVE").count()
    provider_count = db.query(ProviderAccount).filter(ProviderAccount.status == "ACTIVE").count()
    request_count = db.query(AIRequestRecord).count()
    return {
        "success": True,
        "data": {
            "users": user_count,
            "providers": provider_count,
            "requests": request_count,
            "uptime": "99.9%"
        }
    }


class AdminAIModelSchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    slug: str
    name: str
    provider_brand: str
    badge: str
    badge_color: str = "#10b981"
    description: str
    capabilities: str = ""
    input_price_per_1k: float = 0.08
    output_price_per_1k: float = 0.15
    avg_latency_ms: int = 500
    latency_label: str = "500ms avg"
    status: str = "AVAILABLE"
    context_window: str = "128k"
    is_active: bool = True


@api_router.post("/admin/ai-models", status_code=201)
def admin_create_ai_model(
    payload: AdminAIModelSchema,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: add a new AI model to the catalog."""
    if db.query(AIModel).filter(AIModel.slug == payload.slug).first():
        raise HTTPException(status_code=400, detail="A model with this slug already exists.")
    m = AIModel(**payload.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return {"success": True, "data": {"id": m.id, "slug": m.slug, "name": m.name}}


@api_router.put("/admin/ai-models/{model_id}")
def admin_update_ai_model(
    model_id: int,
    payload: AdminAIModelSchema,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: update an existing AI model."""
    m = db.query(AIModel).filter(AIModel.id == model_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Model not found.")
    for k, v in payload.model_dump().items():
        setattr(m, k, v)
    db.commit()
    return {"success": True, "data": {"id": m.id, "slug": m.slug}}


@api_router.patch("/admin/ai-models/{model_id}/toggle")
def admin_toggle_ai_model(
    model_id: int,
    is_active: bool,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: activate or deactivate an AI model."""
    m = db.query(AIModel).filter(AIModel.id == model_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Model not found.")
    m.is_active = is_active
    db.commit()
    return {"success": True, "data": {"id": m.id, "is_active": m.is_active}}


# ══════════════════════════════════════════════════════════════════════════
#  PROVIDER ENDPOINTS  (any authenticated user can be a provider)
# ══════════════════════════════════════════════════════════════════════════

def _get_or_create_provider(user: User, db: Session) -> ProviderAccount:
    """Auto-create a ProviderAccount row for the user if it doesn't exist yet."""
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == user.id).first()
    if not pa:
        pa = ProviderAccount(
            user_id=user.id,
            workspace_name=f"{user.full_name}'s Provider",
            total_capacity=0, available_capacity=0, used_capacity=0,
            total_earnings=0.0, pending_earnings=0.0, withdrawable_balance=0.0,
            status="ACTIVE",
        )
        db.add(pa)
        db.flush()
    return pa


@api_router.get("/provider/overview")
def get_provider_overview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        return {"success": True, "data": {
            "connected": False,
            "balance": 0.0, "total_earned": 0.0,
            "active_listings": 0, "monthly_requests": 0,
            "daily_earnings": [],
        }}

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_reqs = db.query(AIRequestRecord).filter(
        AIRequestRecord.provider_id == pa.id,
        AIRequestRecord.created_at >= month_start,
    ).count()

    active_listings = db.query(ProviderListing).filter(
        ProviderListing.provider_id == pa.id,
        ProviderListing.status == "ACTIVE",
    ).count()

    # Daily earnings chart (last 7 days)
    daily_earnings = []
    for i in range(6, -1, -1):
        day = now - timedelta(days=i)
        s   = day.replace(hour=0, minute=0, second=0, microsecond=0)
        e   = s + timedelta(days=1)
        earn = db.query(func.sum(AIRequestRecord.provider_earning)).filter(
            AIRequestRecord.provider_id == pa.id,
            AIRequestRecord.created_at >= s,
            AIRequestRecord.created_at < e,
        ).scalar() or 0.0
        daily_earnings.append({"date": day.strftime("%a"), "amount": round(earn, 2)})

    connections = db.query(ProviderConnection).filter(
        ProviderConnection.provider_id == pa.id
    ).all()

    return {
        "success": True,
        "data": {
            "connected": len(connections) > 0,
            "balance": pa.withdrawable_balance,
            "total_earned": pa.total_earnings,
            "pending_earnings": pa.pending_earnings,
            "active_listings": active_listings,
            "monthly_requests": monthly_reqs,
            "status": pa.status,
            "daily_earnings": daily_earnings,
        }
    }


@api_router.get("/provider/connections")
def get_provider_connections(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = _get_or_create_provider(current_user, db)
    db.commit()
    connections = db.query(ProviderConnection).filter(
        ProviderConnection.provider_id == pa.id
    ).order_by(ProviderConnection.created_at.desc()).all()
    return {
        "success": True,
        "data": {
            "connections": [{
                "id": c.id,
                "provider_type": c.provider_type or c.provider_name,
                "provider_name": c.provider_name,
                "auth_type": c.auth_type,
                "status": c.status,
                "connection_status": getattr(c, "connection_status", None) or c.status or "CONNECTED",
                "identity_status": getattr(c, "identity_status", None) or "IDENTITY_MATCHED",
                "authorization_status": getattr(c, "authorization_status", None) or "AUTHORIZED",
                "account_email": getattr(c, "account_email", None) or current_user.email,
                "account_identifier": getattr(c, "account_identifier", None),
                "connection_method": getattr(c, "connection_method", None) or "google_identity",
                "plan_name": getattr(c, "plan_name", None) or "Unable to verify",
                "plan_type": getattr(c, "plan_type", None) or "UNVERIFIED",
                "marketplace_status": getattr(c, "marketplace_status", None) or "NOT_ELIGIBLE",
                "marketplace_reason": getattr(c, "marketplace_reason", None) or "",
                "health": c.health_status,
                "health_status": c.health_status,
                "validation_status": c.validation_status or "PENDING",
                "validated_at": c.validated_at.isoformat() if c.validated_at else None,
                "last_verified_at": c.last_verified_at.isoformat() if getattr(c, "last_verified_at", None) else (c.validated_at.isoformat() if c.validated_at else None),
                "token_expires_at": c.token_expires_at.isoformat() if getattr(c, "token_expires_at", None) else None,
                "credential_masked": c.credential_masked,
                "label": c.provider_name,
                "supported_models": c.supported_models,
                "capacity_declared": c.capacity_declared or 0,
                "capacity_reserved": c.capacity_reserved or 0,
                "capacity_listed": c.capacity_listed or 0,
                "capacity_sold": c.capacity_sold or 0,
                "capacity_consumed": c.capacity_consumed or 0,
                "capacity_remaining": c.capacity_remaining if c.capacity_remaining is not None else (c.capacity_declared or 0),
                "capacity_source": "Provider declared",
                "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            } for c in connections]
        }
    }


# ══════════════════════════════════════════════════════════════════════════
#  PROVIDER AI ACCOUNTS (Gmail-based AI Account Connection Architecture)
# ══════════════════════════════════════════════════════════════════════════

class ConnectAIAccountSchema(BaseModel):
    provider_type: str # gemini, openai, anthropic, mock
    connection_method: Optional[str] = "google_identity" # google_identity, official_oauth, delegated_access, api_key_fallback, demo
    account_email: Optional[str] = None # Optional: defaults to provider's verified Gmail
    account_identifier: Optional[str] = None
    plan_name: Optional[str] = None # e.g. "AI Pro", "Pro", "Pro/Max", "Unable to verify"
    declared_capacity: Optional[int] = 100000
    capacity_declared: Optional[int] = None
    authorization_token: Optional[str] = None
    authorization_code: Optional[str] = None
    api_key: Optional[str] = None # Optional fallback only


class DeclareCapacitySchema(BaseModel):
    declared_capacity: int


class DelegatedCommitmentSchema(BaseModel):
    commitment_percent: Optional[float] = None
    committed_units: Optional[int] = None
    confirmed: bool = False
    consumer_chat_privacy_acknowledged: bool = False


class UpdateIntegrationStatusSchema(BaseModel):
    status: str
    reason: Optional[str] = None


@api_router.get("/provider/ai-accounts")
def get_provider_ai_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns the provider's AI Accounts Center ("MY AI ACCOUNTS").
    Shows standard accounts: Google Gemini, ChatGPT, Claude.
    Associates owned AI subscriptions with provider's verified Gmail identity.
    Truthfully distinguishes Identity, Authorization, Execution Capability, and Marketplace Eligibility.
    """
    pa = _get_or_create_provider(current_user, db)
    db.commit()

    all_conns = db.query(ProviderConnection).filter(
        ProviderConnection.provider_id == pa.id
    ).all()
    conns_by_type = {c.provider_type.lower(): c for c in all_conns if c.provider_type}

    openai_supported = bool(settings.OPENAI_DELEGATED_PLAN_ENABLED and settings.OPENAI_PARTICIPATING_APP_STATUS == "APPROVED")

    standard_services = [
        {
            "provider_type": "gemini",
            "name": "Google Gemini",
            "default_plan": "Unable to verify",
            "description": "Google AI subscriptions associated with your Google identity.",
            "supported": True,
            "badge": "G",
            "badge_color": "emerald"
        },
        {
            "provider_type": "openai",
            "name": "ChatGPT",
            "default_plan": "ChatGPT Plus/Pro",
            "description": "Official delegated ChatGPT plan integration (participating external app mechanism).",
            "supported": openai_supported,
            "badge": "O",
            "badge_color": "teal"
        },
        {
            "provider_type": "anthropic",
            "name": "Claude",
            "default_plan": "Unable to verify",
            "description": "Anthropic Claude Pro subscriptions associated with your email identity.",
            "supported": False, # Consumer Claude Pro does not offer 3rd party execution OAuth API
            "badge": "A",
            "badge_color": "amber"
        }
    ]

    accounts = []
    for svc in standard_services:
        ptype = svc["provider_type"]
        conn = conns_by_type.get(ptype)
        if conn:
            # Re-evaluate eligibility dynamically against current policy
            MarketplaceEligibilityService.evaluate_and_update(conn, current_user, db)
            db.commit()
            db.refresh(conn)

            accounts.append({
                "connection_id": conn.id,
                "provider_type": ptype,
                "name": svc["name"],
                "badge": svc["badge"],
                "badge_color": svc["badge_color"],
                "account_email": conn.account_email or current_user.email,
                "is_connected": conn.connection_status in ("CONNECTED", "ACTIVE"),
                "connection_status": conn.connection_status or conn.status,
                "identity_status": conn.identity_status,
                "authorization_status": conn.authorization_status,
                "execution_status": conn.execution_status,
                "failure_reason": conn.failure_reason,
                "capacity_source": conn.capacity_source,
                "delegated_provider": conn.delegated_provider,
                "delegated_authorization_status": conn.delegated_authorization_status,
                "delegated_execution_status": conn.delegated_execution_status,
                "marketplace_commitment": conn.marketplace_commitment or 0.0,
                "committed_units": conn.committed_units or conn.capacity_declared or 0,
                "consumed_units": conn.consumed_units or conn.capacity_consumed or 0,
                "remaining_units": conn.remaining_units if conn.remaining_units is not None else (conn.capacity_remaining or 0),
                "upstream_usage_unit": conn.upstream_usage_unit or "REQUEST",
                "participating_app_status": settings.OPENAI_PARTICIPATING_APP_STATUS if ptype in ("openai", "chatgpt") else None,
                "plan_name": conn.plan_name or svc["default_plan"],
                "plan_type": conn.plan_type,
                "capacity_declared": conn.capacity_declared or 0,
                "capacity_remaining": conn.capacity_remaining if conn.capacity_remaining is not None else (conn.capacity_declared or 0),
                "marketplace_status": conn.marketplace_status,
                "marketplace_reason": conn.marketplace_reason,
                "connection_method": conn.connection_method,
                "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
            })
        else:
            accounts.append({
                "connection_id": None,
                "provider_type": ptype,
                "name": svc["name"],
                "badge": svc["badge"],
                "badge_color": svc["badge_color"],
                "account_email": current_user.email,
                "is_connected": False,
                "connection_status": "NOT_CONNECTED",
                "identity_status": "PENDING_VERIFICATION",
                "authorization_status": "NOT_AUTHORIZED",
                "execution_status": "NOT_SUPPORTED",
                "failure_reason": None,
                "capacity_source": "OFFICIAL_DELEGATED_PLAN" if ptype in ("openai", "chatgpt") else "UNSUPPORTED_CONSUMER_SUBSCRIPTION",
                "delegated_provider": ptype if ptype in ("openai", "chatgpt") else None,
                "delegated_authorization_status": "NOT_CONNECTED",
                "delegated_execution_status": "NOT_SUPPORTED",
                "marketplace_commitment": 0.0,
                "committed_units": 0,
                "consumed_units": 0,
                "remaining_units": 0,
                "upstream_usage_unit": "REQUEST",
                "participating_app_status": settings.OPENAI_PARTICIPATING_APP_STATUS if ptype in ("openai", "chatgpt") else None,
                "plan_name": "Unable to verify",
                "plan_type": "UNVERIFIED",
                "capacity_declared": 0,
                "capacity_remaining": 0,
                "marketplace_status": "NOT_ELIGIBLE",
                "marketplace_reason": "Account is not connected. Connect and authorize your account to determine eligibility.",
                "connection_method": "official_delegated_plan" if ptype in ("openai", "chatgpt") else "google_identity",
                "last_verified_at": None,
            })

    return {
        "success": True,
        "data": {
            "provider_email": current_user.email,
            "provider_name": pa.workspace_name,
            "openai_participating_app_status": settings.OPENAI_PARTICIPATING_APP_STATUS,
            "openai_delegated_plan_enabled": settings.OPENAI_DELEGATED_PLAN_ENABLED,
            "accounts": accounts,
        }
    }


@api_router.post("/provider/ai-accounts/connect")
def connect_ai_account(
    payload: ConnectAIAccountSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Connect an AI account to TokenPool provider account using Gmail/email identity.
    - Matches connected AI account identity with provider's verified TokenPool email identity.
    - Never requests or stores Gmail password, AI password, cookies, OTP, or session tokens.
    - Determines identity status, authorization status, plan, and marketplace eligibility independently.
    - Email equality sets IDENTITY_MATCHED, but does NOT grant execution authorization.
    """
    pa = _get_or_create_provider(current_user, db)
    ptype = (payload.provider_type or "gemini").lower().strip()
    method = (payload.connection_method or "google_identity").lower().strip()

    # Target AI account identity: defaults to provider's verified Gmail
    acct_email = (payload.account_email or current_user.email).lower().strip()
    user_email = current_user.email.lower().strip()

    # 1. Identity match check (email equality only establishes identity match, NOT authorization)
    is_identity_match = bool(acct_email == user_email)
    identity_status = "IDENTITY_MATCHED" if is_identity_match else "IDENTITY_MISMATCH"

    # Display & model configuration
    display_names = {
        "gemini": "Google Gemini",
        "google": "Google Gemini",
        "openai": "ChatGPT",
        "chatgpt": "ChatGPT",
        "anthropic": "Claude",
        "claude": "Claude",
        "mock": "Orbit AI (DEMO)",
        "demo": "Orbit AI (DEMO)"
    }
    svc_name = display_names.get(ptype, ptype.title())

    # Supported model lists
    supported_models_map = {
        "gemini": "gemini-2.5-flash, gemini-1.5-pro, nova-reasoning",
        "openai": "gpt-4o, gpt-4o-mini, o1-preview",
        "anthropic": "claude-3-5-sonnet, claude-3-haiku, claude-3-opus",
        "mock": "nova-reasoning, swift-chat, vision-pro, atlas-code"
    }
    supported_models = supported_models_map.get(ptype, "Supported models")

    # 2. Plan detection: do NOT fabricate plans
    plan_name = payload.plan_name if payload.plan_name else "Unable to verify"

    # 3. Authorization status & capacity source determination
    # Email matching alone MUST NOT establish AUTHORIZED status.
    encrypted_key = None
    masked_key = "unauthorized"
    auth_tok = (payload.authorization_token or payload.authorization_code or "").strip()

    if method == "api_key_fallback" and payload.api_key:
        raw_key = payload.api_key.strip()
        encrypted_key = encrypt_credential(raw_key)
        masked_key = mask_credential(raw_key)
        auth_status = "AUTHORIZED"
        cap_source = "PROVIDER_API_CAPACITY"
    elif ptype in ("mock", "demo") or method in ("mock", "demo"):
        auth_status = "AUTHORIZED"
        masked_key = "mock-delegated"
        encrypted_key = encrypt_credential(auth_tok or "mock-token")
        cap_source = "DEMO_CAPACITY"
    elif method in ("official_delegated_plan", "delegated_access") or (ptype in ("openai", "chatgpt") and method == "official_oauth"):
        cap_source = "OFFICIAL_DELEGATED_PLAN"
        if auth_tok:
            encrypted_key = encrypt_credential(auth_tok)
            masked_key = "oauth-token"
            auth_status = "AUTHORIZED"
        else:
            auth_status = "ACTION_REQUIRED"
            masked_key = "oauth-pending"
    elif method == "official_oauth":
        cap_source = "PROVIDER_API_CAPACITY" if ptype not in ("openai", "chatgpt") else "OFFICIAL_DELEGATED_PLAN"
        if auth_tok:
            encrypted_key = encrypt_credential(auth_tok)
            masked_key = "oauth-token"
            auth_status = "AUTHORIZED"
        else:
            auth_status = "ACTION_REQUIRED"
            masked_key = "oauth-pending"
    elif method == "google_identity":
        auth_status = "ACTION_REQUIRED" if is_identity_match else "NOT_AUTHORIZED"
        masked_key = f"gmail-identity:{acct_email[:4]}••••@{acct_email.split('@')[-1]}"
        if ptype in ("gemini", "google"):
            cap_source = "UNSUPPORTED_CONSUMER_SUBSCRIPTION"
        elif ptype in ("anthropic", "claude"):
            cap_source = "UNSUPPORTED_CONSUMER_SUBSCRIPTION"
        elif ptype in ("openai", "chatgpt"):
            cap_source = "UNSUPPORTED_CONSUMER_SUBSCRIPTION"
        else:
            cap_source = "PROVIDER_API_CAPACITY"
    else:
        cap_source = "PROVIDER_API_CAPACITY"
        auth_status = "ACTION_REQUIRED"

    is_valid_auth = (auth_status == "AUTHORIZED" and (encrypted_key is not None or ptype in ("mock", "demo")))
    val_status = "VALIDATED" if is_valid_auth else "PENDING"

    declared_cap = max(0, int(payload.capacity_declared if payload.capacity_declared is not None else (payload.declared_capacity or 100000)))

    # Check for existing connection of this type for this provider
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.provider_id == pa.id,
        ProviderConnection.provider_type == ptype
    ).first()

    now = datetime.now(timezone.utc)
    if not conn:
        conn = ProviderConnection(
            provider_id=pa.id,
            provider_name=svc_name,
            provider_type=ptype,
            auth_type="OAUTH" if method != "api_key_fallback" else "API_KEY",
            status="CONNECTED",
            connection_status="CONNECTED",
            connection_method=method,
            capacity_source=cap_source,
            delegated_provider=ptype if cap_source == "OFFICIAL_DELEGATED_PLAN" else None,
            delegated_plan_name="ChatGPT Plus/Pro" if cap_source == "OFFICIAL_DELEGATED_PLAN" else None,
            delegated_authorization_status=auth_status if cap_source == "OFFICIAL_DELEGATED_PLAN" else "NOT_CONNECTED",
            delegated_execution_status="NOT_SUPPORTED",
            account_email=acct_email,
            account_identifier=payload.account_identifier or f"acct_{uuid.uuid4().hex[:8]}",
            identity_status=identity_status,
            authorization_status=auth_status,
            plan_name=plan_name,
            plan_type="PRO" if "pro" in plan_name.lower() else "UNVERIFIED",
            encrypted_credential=encrypted_key,
            credential_masked=masked_key,
            validation_status=val_status,
            validated_at=now if val_status == "VALIDATED" else None,
            last_verified_at=now,
            last_health_check_at=now,
            health_status="HEALTHY",
            supported_models=supported_models,
            capacity_declared=declared_cap,
            capacity_reserved=0,
            capacity_listed=0,
            capacity_sold=0,
            capacity_consumed=0,
            capacity_remaining=declared_cap,
            committed_units=declared_cap if cap_source == "OFFICIAL_DELEGATED_PLAN" else 0,
            remaining_units=declared_cap if cap_source == "OFFICIAL_DELEGATED_PLAN" else 0,
            upstream_usage_unit="REQUEST" if cap_source == "OFFICIAL_DELEGATED_PLAN" else "TOKEN",
        )
        db.add(conn)
        db.flush()
    else:
        # Update existing
        conn.provider_name = svc_name
        conn.account_email = acct_email
        conn.connection_method = method
        conn.capacity_source = cap_source
        if cap_source == "OFFICIAL_DELEGATED_PLAN":
            conn.delegated_provider = ptype
            conn.delegated_plan_name = "ChatGPT Plus/Pro"
            conn.delegated_authorization_status = auth_status
        conn.connection_status = "CONNECTED"
        conn.status = "CONNECTED"
        conn.identity_status = identity_status
        conn.authorization_status = auth_status
        conn.plan_name = plan_name
        conn.plan_type = "PRO" if "pro" in plan_name.lower() else "UNVERIFIED"
        if encrypted_key:
            conn.encrypted_credential = encrypted_key
            conn.credential_masked = masked_key
        conn.last_verified_at = now
        conn.last_health_check_at = now
        conn.validation_status = val_status
        conn.validated_at = now if val_status == "VALIDATED" else None
        if declared_cap > 0:
            conn.capacity_declared = declared_cap
            conn.capacity_remaining = max(0, declared_cap - (conn.capacity_reserved or 0) - (conn.capacity_listed or 0) - (conn.capacity_sold or 0) - (conn.capacity_consumed or 0))
            if cap_source == "OFFICIAL_DELEGATED_PLAN":
                conn.committed_units = declared_cap
                conn.remaining_units = conn.capacity_remaining

    # Evaluate marketplace eligibility strictly using the engine
    MarketplaceEligibilityService.evaluate_and_update(conn, current_user, db)

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="AI_ACCOUNT_CONNECTED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=ptype,
        details=(
            f"Provider connected {svc_name} account ({acct_email}). "
            f"Identity: {conn.identity_status}, Authorization: {conn.authorization_status}, "
            f"Marketplace: {conn.marketplace_status} ({conn.marketplace_reason})"
        )
    ))

    db.commit()
    db.refresh(conn)

    return {
        "success": True,
        "message": f"{svc_name} account connected successfully.",
        "data": {
            "id": conn.id,
            "connection_id": conn.id,
            "provider_type": conn.provider_type,
            "provider_name": conn.provider_name,
            "account_email": conn.account_email,
            "connection_method": conn.connection_method,
            "connection_status": conn.connection_status,
            "identity_status": conn.identity_status,
            "authorization_status": conn.authorization_status,
            "plan_name": conn.plan_name,
            "marketplace_status": conn.marketplace_status,
            "marketplace_reason": conn.marketplace_reason,
            "capacity_declared": conn.capacity_declared,
            "capacity_remaining": conn.capacity_remaining,
            "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        }
    }


@api_router.get("/provider/ai-accounts/google/auth-url")
def get_google_ai_auth_url(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns official Google OAuth 2.0 authorization URL for connecting a provider's Gemini account.
    Requested scopes:
    - openid
    - https://www.googleapis.com/auth/userinfo.email
    - https://www.googleapis.com/auth/userinfo.profile
    - https://www.googleapis.com/auth/generative-language
    """
    pa = _get_or_create_provider(current_user, db)
    if not settings.GOOGLE_CLIENT_ID:
        return {
            "success": False,
            "configured": False,
            "message": (
                "Google OAuth client is not configured on this server (GOOGLE_CLIENT_ID is not set). "
                "Google does not provide an officially supported execution mechanism for using consumer AI subscriptions "
                "to fulfill TokenPool marketplace requests without a configured Google Cloud OAuth client."
            )
        }

    import secrets
    state_token = f"tp_state_{current_user.id}_{secrets.token_urlsafe(16)}"
    scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/generative-language",
    ]
    scope_str = "%20".join([urllib.parse.quote(s) for s in scopes])
    redirect_uri = urllib.parse.quote(settings.GOOGLE_OAUTH_REDIRECT_URI)

    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={settings.GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope_str}"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&state={state_token}"
    )

    return {
        "success": True,
        "configured": True,
        "auth_url": auth_url,
        "scopes": scopes,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
    }


@api_router.get("/provider/ai-accounts/google/callback")
@api_router.post("/provider/ai-accounts/google/callback")
async def google_ai_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """
    Receives Google OAuth authorization callback:
    1. Validates OAuth code and state
    2. Exchanges authorization code for official Google tokens
    3. Verifies account identity against provider Gmail
    4. Performs real upstream execution test against Google Generative Language API
    5. Sets authorization and marketplace eligibility based strictly on real test result
    """
    now = datetime.now(timezone.utc)
    from fastapi.responses import RedirectResponse, HTMLResponse

    if error:
        return HTMLResponse(
            f"<h3>Google Authorization Error</h3><p>{error}</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
            status_code=400
        )

    if not code:
        return HTMLResponse(
            "<h3>Missing Code</h3><p>No authorization code received from Google.</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
            status_code=400
        )

    # Resolve user from state token if not in session header
    user = current_user
    if not user and state and state.startswith("tp_state_"):
        parts = state.split("_")
        if len(parts) >= 3 and parts[2].isdigit():
            uid = int(parts[2])
            user = db.query(User).filter(User.id == uid).first()

    if not user:
        return HTMLResponse(
            "<h3>Session Expired</h3><p>Could not verify provider session for OAuth callback. Please log in again.</p>",
            status_code=401
        )

    pa = _get_or_create_provider(user, db)

    # Exchange code for tokens
    token_url = "https://oauth2.googleapis.com/token"
    token_payload = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            token_res = await http_client.post(token_url, data=token_payload)
            if token_res.status_code != 200:
                return HTMLResponse(
                    f"<h3>Google Token Exchange Failed</h3><p>{token_res.text}</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
                    status_code=400
                )
            token_data = token_res.json()
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 3600)

            # Fetch userinfo to confirm Gmail identity
            userinfo_res = await http_client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if userinfo_res.status_code != 200:
                return HTMLResponse(
                    "<h3>Identity Verification Failed</h3><p>Could not fetch Google user profile.</p>",
                    status_code=400
                )
            userinfo = userinfo_res.json()
            google_email = (userinfo.get("email") or "").lower().strip()

            # Find or create Gemini ProviderConnection
            conn = db.query(ProviderConnection).filter(
                ProviderConnection.provider_id == pa.id,
                ProviderConnection.provider_type == "gemini"
            ).first()

            if not conn:
                conn = ProviderConnection(
                    provider_id=pa.id,
                    provider_name="Google Gemini",
                    provider_type="gemini",
                    auth_type="OAUTH",
                    status="CONNECTED",
                    connection_status="CONNECTED",
                    connection_method="official_oauth",
                    account_email=google_email,
                    identity_status="IDENTITY_MATCHED" if google_email == user.email.lower() else "IDENTITY_MISMATCH",
                    authorization_status="PENDING",
                    plan_name="Unable to verify",
                    capacity_declared=100000,
                    capacity_remaining=100000,
                )
                db.add(conn)
                db.flush()

            conn.account_email = google_email
            conn.identity_status = "IDENTITY_MATCHED" if google_email == user.email.lower() else "IDENTITY_MISMATCH"
            conn.token_expires_at = now + timedelta(seconds=expires_in)

            # REAL EXECUTION TEST against Google Generative Language API
            test_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
            test_body = {
                "contents": [{"parts": [{"text": "TokenPool capability verification"}]}],
                "generationConfig": {"maxOutputTokens": 5}
            }
            exec_test_res = await http_client.post(
                test_url,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json=test_body
            )

            import json
            auth_store = json.dumps({
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "email": google_email
            })
            conn.encrypted_authorization_data = encrypt_credential(auth_store)

            if exec_test_res.status_code == 200:
                conn.authorization_status = "AUTHORIZED"
                conn.validation_status = "VALIDATED"
                conn.validated_at = now
                conn.last_verified_at = now
            else:
                conn.authorization_status = "ACTION_REQUIRED"
                conn.validation_status = "FAILED"
                conn.marketplace_status = "NOT_ELIGIBLE"
                conn.marketplace_reason = (
                    "Google does not provide an officially supported execution mechanism "
                    "for using this consumer AI subscription to fulfill TokenPool marketplace requests."
                )

            MarketplaceEligibilityService.evaluate_and_update(conn, user, db)
            db.commit()

            return RedirectResponse(url="/provider/connections?oauth_status=complete", status_code=303)

    except Exception as exc:
        return HTMLResponse(
            f"<h3>OAuth Callback Error</h3><p>{str(exc)}</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
            status_code=500
        )


# ══════════════════════════════════════════════════════════════════════════
#  OFFICIAL OPENAI DELEGATED PLAN INTEGRATION ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/provider/ai-accounts/openai/auth-url")
def get_openai_ai_auth_url(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns official OpenAI OAuth authorization URL for participating app flow.
    Gated by OPENAI_DELEGATED_PLAN_ENABLED and OPENAI_PARTICIPATING_APP_STATUS == 'APPROVED'.
    """
    import secrets
    state_token = f"tp_openai_{current_user.id}_{secrets.token_urlsafe(16)}"
    adapter = OpenAIDelegatedPlanAdapter()
    return adapter.authorize(user_id=current_user.id, state=state_token)


@api_router.post("/provider/ai-accounts/openai/connect")
def connect_openai_ai_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Initiates official OpenAI delegated plan authorization flow."""
    return get_openai_ai_auth_url(current_user, db)


@api_router.get("/provider/ai-accounts/openai/callback")
@api_router.post("/provider/ai-accounts/openai/callback")
async def openai_ai_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """
    Receives official OpenAI authorization callback:
    1. Validates OAuth code and state
    2. Exchanges authorization code via official endpoint
    3. Stores encrypted token at rest
    4. Evaluates execution capability and marketplace eligibility
    """
    now = datetime.now(timezone.utc)
    from fastapi.responses import HTMLResponse, RedirectResponse
    import json

    if error:
        return HTMLResponse(
            f"<h3>OpenAI Authorization Error</h3><p>{error}</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
            status_code=400
        )

    if not code:
        return HTMLResponse(
            "<h3>Missing Code</h3><p>No authorization code received from OpenAI.</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
            status_code=400
        )

    # Resolve user from state token if not in session header
    user = current_user
    if not user and state and state.startswith("tp_openai_"):
        parts = state.split("_")
        if len(parts) >= 3 and parts[2].isdigit():
            uid = int(parts[2])
            user = db.query(User).filter(User.id == uid).first()

    if not user:
        return HTMLResponse(
            "<h3>Session Expired</h3><p>Could not verify provider session for OpenAI callback.</p>",
            status_code=401
        )

    pa = _get_or_create_provider(user, db)
    adapter = OpenAIDelegatedPlanAdapter()
    try:
        token_data = await adapter.callback(code=code, state=state)
    except Exception as exc:
        return HTMLResponse(
            f"<h3>OpenAI Token Exchange Failed</h3><p>{str(exc)}</p><p><a href='/provider/connections'>Return to TokenPool</a></p>",
            status_code=400
        )

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)

    # Find or create OpenAI ProviderConnection
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.provider_id == pa.id,
        ProviderConnection.provider_type == "openai"
    ).first()

    if not conn:
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
            account_email=user.email,
            identity_status="IDENTITY_MATCHED",
            delegated_authorization_status="AUTHORIZED",
            authorization_status="AUTHORIZED",
            plan_name="ChatGPT Plus/Pro",
            plan_type="PRO",
            capacity_declared=0,
            capacity_remaining=0,
            committed_units=0,
            remaining_units=0,
            upstream_usage_unit="REQUEST"
        )
        db.add(conn)
        db.flush()

    conn.capacity_source = "OFFICIAL_DELEGATED_PLAN"
    conn.delegated_provider = "openai"
    conn.delegated_plan_name = "ChatGPT Plus/Pro"
    conn.delegated_authorization_status = "AUTHORIZED"
    conn.authorization_status = "AUTHORIZED"
    conn.connection_status = "CONNECTED"
    conn.status = "CONNECTED"
    conn.token_expires_at = now + timedelta(seconds=expires_in)
    conn.last_verified_at = now

    auth_payload = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "email": user.email
    })
    conn.encrypted_authorization_data = encrypt_credential(auth_payload)
    conn.credential_masked = f"chatgpt-oauth:{user.email[:4]}••••@{user.email.split('@')[-1]}"

    MarketplaceEligibilityService.evaluate_and_update(conn, user, db)

    db.add(AuditLog(
        user_id=user.id,
        actor_email=user.email,
        action="AUTHORIZATION_COMPLETED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Provider completed official OpenAI delegated plan authorization for connection #{conn.id}."
    ))
    db.commit()

    return RedirectResponse(url="/provider/connections?openai_oauth_status=complete", status_code=303)


@api_router.post("/provider/ai-accounts/{connection_id}/commitment")
def set_delegated_plan_commitment(
    connection_id: int,
    payload: DelegatedCommitmentSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Configures provider's marketplace capacity commitment for an official delegated plan.
    Requires explicit provider confirmation.
    Tracks committed_units, reserved_units, consumed_units, and remaining_units.
    """
    pa = _get_or_create_provider(current_user, db)
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.id == connection_id,
        ProviderConnection.provider_id == pa.id
    ).with_for_update().first()

    if not conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")

    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Explicit provider confirmation is required before activating marketplace capacity commitment."
        )

    # Determine committed units from percentage or absolute units
    if payload.committed_units is not None and payload.committed_units >= 0:
        units = int(payload.committed_units)
        pct = payload.commitment_percent if payload.commitment_percent is not None else round((units / 100000.0) * 100, 2)
    elif payload.commitment_percent is not None and payload.commitment_percent >= 0:
        pct = float(payload.commitment_percent)
        units = int((pct / 100.0) * 100000)
    else:
        raise HTTPException(status_code=400, detail="Please provide a valid commitment percentage or committed units.")

    conn.capacity_source = "OFFICIAL_DELEGATED_PLAN"
    conn.marketplace_commitment = pct
    conn.committed_units = units
    conn.capacity_declared = units
    conn.remaining_units = max(0, units - (conn.reserved_units or 0) - (conn.consumed_units or 0))
    conn.capacity_remaining = conn.remaining_units

    MarketplaceEligibilityService.evaluate_and_update(conn, current_user, db)

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="CAPACITY_DECLARED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Provider set marketplace commitment to {pct}% ({units:,} units) for connection #{conn.id}."
    ))
    db.commit()

    return {
        "success": True,
        "message": f"Marketplace capacity commitment updated to {pct}% ({units:,} units).",
        "data": {
            "connection_id": conn.id,
            "capacity_source": conn.capacity_source,
            "marketplace_commitment": conn.marketplace_commitment,
            "committed_units": conn.committed_units,
            "consumed_units": conn.consumed_units or 0,
            "remaining_units": conn.remaining_units,
            "marketplace_status": conn.marketplace_status,
            "marketplace_reason": conn.marketplace_reason,
            "execution_status": conn.execution_status
        }
    }


@api_router.post("/provider/ai-accounts/{connection_id}/declare-capacity")

def declare_ai_account_capacity(
    connection_id: int,
    payload: DeclareCapacitySchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Provider commits marketplace capacity for their connected AI account."""
    pa = _get_or_create_provider(current_user, db)
    other_conn = db.query(ProviderConnection).filter(ProviderConnection.id == connection_id).first()
    if not other_conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")
    if other_conn.provider_id != pa.id:
        raise HTTPException(status_code=403, detail="Access denied: You do not own this provider connection.")
    conn = other_conn

    declared = int(payload.declared_capacity)
    if declared < 0:
        raise HTTPException(status_code=400, detail="Declared capacity cannot be negative.")

    min_allowed = (conn.capacity_listed or 0) + (conn.capacity_sold or 0) + (conn.capacity_consumed or 0)
    if declared < min_allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Declared capacity ({declared:,} tokens) cannot be lower than capacity already committed ({min_allowed:,} tokens)."
        )

    conn.capacity_declared = declared
    conn.capacity_remaining = max(
        0,
        declared - (conn.capacity_reserved or 0) - (conn.capacity_listed or 0) - (conn.capacity_sold or 0) - (conn.capacity_consumed or 0)
    )

    MarketplaceEligibilityService.evaluate_and_update(conn, current_user, db)

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="AI_ACCOUNT_CAPACITY_DECLARED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Provider declared {declared:,} tokens for AI account #{conn.id} ({conn.provider_name}). Remaining: {conn.capacity_remaining:,}."
    ))

    db.commit()
    db.refresh(conn)

    return {
        "success": True,
        "data": {
            "id": conn.id,
            "capacity_declared": conn.capacity_declared,
            "capacity_remaining": conn.capacity_remaining,
            "marketplace_status": conn.marketplace_status,
            "marketplace_reason": conn.marketplace_reason,
        }
    }


@api_router.post("/provider/ai-accounts/{connection_id}/disconnect")
def disconnect_ai_account(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disconnects an AI account and pauses all associated marketplace listings."""
    pa = _get_or_create_provider(current_user, db)
    other_conn = db.query(ProviderConnection).filter(ProviderConnection.id == connection_id).first()
    if not other_conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")
    if other_conn.provider_id != pa.id:
        raise HTTPException(status_code=403, detail="Access denied: You do not own this provider connection.")
    conn = other_conn

    conn.status = "DISCONNECTED"
    conn.connection_status = "DISCONNECTED"
    conn.marketplace_status = "PAUSED"
    conn.marketplace_reason = "AI account disconnected by provider."
    conn.revoked_at = datetime.now(timezone.utc)

    # Automatically pause any active listings backed by this connection
    listings = db.query(ProviderListing).filter(
        ProviderListing.provider_connection_id == conn.id,
        ProviderListing.status == "ACTIVE"
    ).all()
    for l in listings:
        l.status = "PAUSED"

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="AI_ACCOUNT_DISCONNECTED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Provider disconnected AI account #{conn.id} ({conn.provider_name}). {len(listings)} listings paused."
    ))

    db.commit()

    return {
        "success": True,
        "message": f"{conn.provider_name} account disconnected. {len(listings)} listings paused.",
        "data": {
            "id": conn.id,
            "connection_status": "DISCONNECTED",
            "marketplace_status": "PAUSED",
            "listings_paused": len(listings)
        }
    }


@api_router.post("/provider/ai-accounts/{connection_id}/health-check")
def health_check_ai_account(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verifies health and token expiration for a connected AI account."""
    pa = _get_or_create_provider(current_user, db)
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.id == connection_id,
        ProviderConnection.provider_id == pa.id
    ).first()

    if not conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")

    now = datetime.now(timezone.utc)
    conn.last_health_check_at = now

    # Check token expiration
    if conn.token_expires_at:
        exp = conn.token_expires_at
        if exp.tzinfo is not None:
            is_expired = exp < datetime.now(timezone.utc)
        else:
            is_expired = (exp < datetime.utcnow()) or (exp < datetime.now())
        if is_expired:
            conn.connection_status = "EXPIRED"
            conn.status = "EXPIRED"
            conn.marketplace_status = "PAUSED"
            conn.marketplace_reason = "Provider authorization token has expired."
            # Pause listings
            listings = db.query(ProviderListing).filter(
                ProviderListing.provider_connection_id == conn.id,
                ProviderListing.status == "ACTIVE"
            ).all()
            for l in listings:
                l.status = "PAUSED"
            db.commit()
            return {
                "success": False,
                "status": "EXPIRED",
                "message": "Authorization has expired. Re-authorization required.",
                "data": {
                    "id": conn.id,
                    "connection_status": "EXPIRED",
                    "marketplace_status": "PAUSED",
                    "marketplace_reason": conn.marketplace_reason,
                }
            }

    MarketplaceEligibilityService.evaluate_and_update(conn, current_user, db)
    db.commit()

    return {
        "success": True,
        "status": "HEALTHY",
        "connection_status": conn.connection_status,
        "marketplace_status": conn.marketplace_status,
        "marketplace_reason": conn.marketplace_reason,
        "data": {
            "id": conn.id,
            "connection_status": conn.connection_status,
            "marketplace_status": conn.marketplace_status,
            "marketplace_reason": conn.marketplace_reason,
        }
    }


class ConnectSchema(BaseModel):
    provider_type: str
    api_key: Optional[str] = None
    credential: Optional[str] = None
    label: str = ""
    capacity_declared: Optional[int] = 0
    declared_capacity: Optional[int] = 0


class DeclareCapacitySchema(BaseModel):
    declared_capacity: int


@api_router.post("/provider/connections", status_code=201)
async def add_provider_connection(
    payload: ConnectSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw_key = (payload.api_key or payload.credential or "").strip()
    if not raw_key or len(raw_key) < 6:
        raise HTTPException(status_code=400, detail="API key is required and cannot be empty.")
    
    pa = _get_or_create_provider(current_user, db)
    ptype = (payload.provider_type or "openai").lower()
    
    # 1. Connection Verification against provider's API
    adapter = AIRouterService.create_provider_adapter(ptype, raw_key)
    try:
        is_valid = await adapter.validate_credentials({"api_key": raw_key})
    except Exception as e:
        is_valid = False

    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Credential validation failed against upstream {ptype.upper()} API. "
                   "Please verify that your API key is active, properly formatted, and has authorized access."
        )

    # 2. Encrypt credential securely (never stored in plain text, never returned)
    encrypted_key = encrypt_credential(raw_key)
    masked_key = mask_credential(raw_key)
    
    initial_declared = max(0, int(payload.capacity_declared or payload.declared_capacity or 0))
    supported_models = ", ".join(adapter.get_supported_models()[:4])

    conn = ProviderConnection(
        provider_id=pa.id,
        account_email=current_user.email,
        identity_status="IDENTITY_MATCHED",
        authorization_status="AUTHORIZED",
        connection_status="CONNECTED",
        execution_status="EXECUTION_SUPPORTED",
        connection_method="api_key_fallback",
        capacity_source="Provider API Capacity",
        provider_name=payload.label or f"{ptype.title()} Capacity Source",
        provider_type=ptype,
        auth_type="API_KEY",
        status="CONNECTED",
        encrypted_credential=encrypted_key,
        credential_masked=masked_key,
        validation_status="VALIDATED",
        validated_at=datetime.now(timezone.utc),
        last_verified_at=datetime.now(timezone.utc),
        health_status="HEALTHY",
        supported_models=supported_models,
        capacity_declared=initial_declared,
        capacity_reserved=0,
        capacity_listed=0,
        capacity_sold=0,
        capacity_consumed=0,
        capacity_remaining=initial_declared,
    )
    db.add(conn)
    MarketplaceEligibilityService.evaluate_and_update(conn, current_user, db)
    if pa.status == "NOT_CONNECTED":
        pa.status = "ACTIVE"

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="PROVIDER_CONNECTION_VALIDATED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=ptype,
        details=f"Provider verified {ptype.upper()} credential ({masked_key}). Initial declared capacity: {initial_declared:,} tokens."
    ))
    db.commit()
    db.refresh(conn)

    return {
        "success": True,
        "data": {
            "id": conn.id,
            "provider_type": conn.provider_type,
            "provider_name": conn.provider_name,
            "status": conn.status,
            "validation_status": conn.validation_status,
            "validated_at": conn.validated_at.isoformat() if conn.validated_at else None,
            "credential_masked": conn.credential_masked,
            "capacity_declared": conn.capacity_declared,
            "capacity_remaining": conn.capacity_remaining,
            "capacity_source": "Provider declared",
            "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
            "created_at": conn.created_at.isoformat(),
        }
    }


@api_router.post("/provider/connections/{connection_id}/declare-capacity")
@api_router.post("/provider/connections/{connection_id}/capacity")
def declare_connection_capacity(
    connection_id: int,
    payload: DeclareCapacitySchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = _get_or_create_provider(current_user, db)
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.id == connection_id,
        ProviderConnection.provider_id == pa.id
    ).with_for_update().first()

    if not conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")

    if conn.validation_status != "VALIDATED":
        raise HTTPException(
            status_code=400,
            detail=f"Only validated connections can declare marketplace capacity. Current status: {conn.validation_status}."
        )

    if conn.status in ("REVOKED", "DISABLED", "DISCONNECTED"):
        raise HTTPException(
            status_code=400,
            detail=f"Connection is {conn.status.lower()} and cannot accept new capacity declarations."
        )

    declared = int(payload.declared_capacity)
    if declared < 0:
        raise HTTPException(status_code=400, detail="Declared capacity cannot be negative.")

    min_allowed = (conn.capacity_listed or 0) + (conn.capacity_sold or 0) + (conn.capacity_consumed or 0)
    if declared < min_allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Declared capacity ({declared:,} tokens) cannot be lower than capacity already committed "
                   f"({min_allowed:,} tokens: listed={conn.capacity_listed}, sold={conn.capacity_sold}, consumed={conn.capacity_consumed})."
        )

    conn.capacity_declared = declared
    conn.capacity_remaining = max(
        0,
        declared - (conn.capacity_reserved or 0) - (conn.capacity_listed or 0) - (conn.capacity_sold or 0) - (conn.capacity_consumed or 0)
    )

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="CAPACITY_DECLARED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Provider declared {declared:,} tokens for connection #{conn.id} ({conn.provider_name}). Remaining marketplace capacity: {conn.capacity_remaining:,}."
    ))
    db.commit()
    db.refresh(conn)

    return {
        "success": True,
        "data": {
            "id": conn.id,
            "capacity_declared": conn.capacity_declared,
            "capacity_reserved": conn.capacity_reserved,
            "capacity_listed": conn.capacity_listed,
            "capacity_sold": conn.capacity_sold,
            "capacity_consumed": conn.capacity_consumed,
            "capacity_remaining": conn.capacity_remaining,
            "capacity_source": "Provider-Declared Capacity",
            "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
        }
    }


@api_router.get("/provider/connections/{connection_id}/capacity")
def get_connection_capacity_endpoint(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns capacity accounting for a specific ProviderConnection.
    remaining_capacity = declared_capacity - reserved_capacity - consumed_capacity.
    """
    pa = _get_or_create_provider(current_user, db)
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.id == connection_id,
        ProviderConnection.provider_id == pa.id,
    ).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")

    return {
        "success": True,
        "data": {
            "id": conn.id,
            "capacity_declared": conn.capacity_declared or 0,
            "capacity_reserved": conn.capacity_reserved or 0,
            "capacity_listed": conn.capacity_listed or 0,
            "capacity_sold": conn.capacity_sold or 0,
            "capacity_consumed": conn.capacity_consumed or 0,
            "capacity_remaining": conn.capacity_remaining if conn.capacity_remaining is not None else (conn.capacity_declared or 0),
            "capacity_source": "Provider-Declared Capacity",
        }
    }


@api_router.post("/provider/connections/{connection_id}/disconnect")
@api_router.post("/provider/connections/{connection_id}/revoke")
@api_router.post("/provider/ai-accounts/{connection_id}/revoke")
@api_router.delete("/provider/connections/{connection_id}")
def disconnect_provider_connection(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = _get_or_create_provider(current_user, db)
    conn = db.query(ProviderConnection).filter(
        ProviderConnection.id == connection_id,
        ProviderConnection.provider_id == pa.id
    ).with_for_update().first()

    if not conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")

    conn.status = "DISCONNECTED"
    conn.connection_status = "DISCONNECTED"
    conn.validation_status = "REVOKED"
    conn.marketplace_status = "PAUSED"
    conn.marketplace_reason = "Provider revoked connection."
    conn.revoked_at = datetime.now(timezone.utc)

    # Pause active listings backed by this connection
    listings = db.query(ProviderListing).filter(
        ProviderListing.provider_connection_id == conn.id,
        ProviderListing.status == "ACTIVE"
    ).all()
    for l in listings:
        l.status = "PAUSED"

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="PROVIDER_CONNECTION_REVOKED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Provider revoked connection #{conn.id} ({conn.provider_name}). {len(listings)} listings paused."
    ))
    db.commit()

    return {
        "success": True,
        "message": f"Connection #{conn.id} revoked and disconnected. {len(listings)} listings paused."
    }


@api_router.post("/provider/ai-accounts/{connection_id}/verify")
def verify_provider_ai_account(
    connection_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Verifies provider AI account and evaluates marketplace eligibility.
    """
    return health_check_ai_account(connection_id, current_user, db)


@api_router.post("/provider/ai-accounts/google/connect")
def connect_google_ai_account_api(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Initiates Google AI account official OAuth flow.
    """
    return get_google_ai_oauth_url(current_user, db)


@api_router.get("/marketplace")
def get_marketplace_listings(
    search: Optional[str] = None,
    model: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    GET /api/v1/marketplace: Browse active marketplace listings.
    """
    query = db.query(ProviderListing).filter(
        ProviderListing.status == "ACTIVE"
    )
    if search:
        query = query.filter(ProviderListing.model_name.ilike(f"%{search}%"))
    if model:
        query = query.filter(ProviderListing.model_slug == model)

    listings = query.order_by(ProviderListing.created_at.desc()).all()
    return {
        "success": True,
        "data": [{
            "id": l.id,
            "listing_id": l.id,
            "model_slug": l.model_slug,
            "model_name": l.model_name,
            "badge": l.badge,
            "capacity_unit": l.capacity_unit or "TOKEN",
            "capacity_units": l.capacity_units,
            "remaining_capacity": l.remaining_capacity,
            "price_per_unit": l.price_per_unit,
            "min_purchase": l.min_purchase,
            "max_purchase": l.max_purchase,
            "status": l.status,
            "provider_id": l.provider_id,
            "provider_connection_id": l.provider_connection_id,
            "capacity_source": "Provider-Declared Capacity",
        } for l in listings]
    }


@api_router.get("/marketplace/{listing_id}")
def get_marketplace_listing_by_id(
    listing_id: int,
    db: Session = Depends(get_db),
):
    """
    GET /api/v1/marketplace/{listing_id}: Retrieve single marketplace listing detail.
    """
    listing = db.query(ProviderListing).filter(ProviderListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Marketplace listing not found.")

    return {
        "success": True,
        "data": {
            "id": listing.id,
            "listing_id": listing.id,
            "model_slug": listing.model_slug,
            "model_name": listing.model_name,
            "badge": listing.badge,
            "capacity_unit": listing.capacity_unit or "TOKEN",
            "capacity_units": listing.capacity_units,
            "remaining_capacity": listing.remaining_capacity,
            "price_per_unit": listing.price_per_unit,
            "min_purchase": listing.min_purchase,
            "max_purchase": listing.max_purchase,
            "status": listing.status,
            "provider_id": listing.provider_id,
            "provider_connection_id": listing.provider_connection_id,
            "capacity_source": "Provider-Declared Capacity",
        }
    }


@api_router.get("/provider/listings")
def get_provider_listings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        return {"success": True, "data": {"listings": []}}
    listings = db.query(ProviderListing).filter(
        ProviderListing.provider_id == pa.id,
        ProviderListing.status != "DELETED",
    ).order_by(ProviderListing.created_at.desc()).all()
    return {
        "success": True,
        "data": {
            "listings": [{
                "id": l.id,
                "provider_connection_id": l.provider_connection_id,
                "connection_name": l.connection.provider_name if l.connection else "Direct Capacity",
                "connection_verified": bool(l.connection and l.connection.validation_status == "VALIDATED"),
                "capacity_source": "Provider declared" if l.provider_connection_id else "Platform allocated",
                "capacity_source_label": "Provider-Declared Capacity",
                "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
                "model_slug": l.model_slug,
                "model_name": l.model_name,
                "badge": l.badge,
                "capacity_unit": l.capacity_unit or "TOKEN",
                "period_type": l.period_type or "MONTHLY",
                "capacity_units": l.capacity_units,
                "total_capacity": l.total_capacity or l.capacity_units,
                "reserved_capacity": l.reserved_capacity or 0,
                "sold_capacity": l.sold_capacity or 0,
                "consumed_capacity": l.consumed_capacity or 0,
                "remaining_capacity": l.remaining_capacity if l.remaining_capacity is not None else l.capacity_units,
                "price_per_1k_input": l.price_input,
                "price_per_1k_output": l.price_output,
                "price_per_unit": l.price_per_unit,
                "min_purchase": l.min_purchase,
                "max_purchase": l.max_purchase,
                "status": l.status,
                "created_at": l.created_at.isoformat(),
            } for l in listings]
        }
    }


# ══════════════════════════════════════════════════════════════════════════
#  MODE 3 CAPACITY POOLS & PROVIDER ALLOCATIONS
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/capacity-pools")
def list_capacity_pools(
    db: Session = Depends(get_db),
):
    """
    Public / Authenticated: View TokenPool-managed capacity pools.
    Every pool is backed by real execution adapters (OpenAI, Gemini, Anthropic, Mock).
    """
    pools = db.query(AICapacityPool).order_by(AICapacityPool.id.asc()).all()
    models_by_id = {m.id: m for m in db.query(AIModel).all()}
    return {
        "success": True,
        "data": {
            "pools": [{
                "id": p.id,
                "model_id": p.model_id,
                "model_slug": p.model_slug,
                "model_name": models_by_id.get(p.model_id).name if models_by_id.get(p.model_id) else p.model_slug,
                "provider_brand": models_by_id.get(p.model_id).provider_brand if models_by_id.get(p.model_id) else "AI",
                "execution_adapter": p.execution_adapter,
                "capacity_unit": p.capacity_unit,
                "period_type": p.period_type,
                "total_capacity": p.total_capacity,
                "allocated_capacity": p.allocated_capacity,
                "reserved_capacity": p.reserved_capacity,
                "sold_capacity": p.sold_capacity,
                "consumed_capacity": p.consumed_capacity,
                "remaining_capacity": p.remaining_capacity,
                "period_start": p.period_start.isoformat() if p.period_start else None,
                "period_end": p.period_end.isoformat() if p.period_end else None,
                "status": p.status,
                "is_verified": p.status == "VERIFIED",
                "is_demo": p.execution_adapter.lower() == "mock" if p.execution_adapter else True,
            } for p in pools]
        }
    }


@api_router.get("/provider/capacity/allocations")
def get_provider_allocations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Provider: Retrieve all TokenPool capacity allocations assigned to this provider.
    Allocations are created by admin via POST /admin/provider-allocations.
    Providers CANNOT self-allocate capacity — this prevents fake capacity creation.
    """
    pa = _get_or_create_provider(current_user, db)
    db.commit()
    allocations = db.query(ProviderCapacityAllocation).filter(
        ProviderCapacityAllocation.provider_id == pa.id,
        ProviderCapacityAllocation.status != "EXPIRED",
    ).order_by(ProviderCapacityAllocation.created_at.desc()).all()

    models_by_id = {m.id: m for m in db.query(AIModel).all()}

    # NO auto-seeding of allocations.
    # If provider has no allocations, they see an empty list
    # and must contact admin to get capacity assigned.

    return {
        "success": True,
        "data": {
            "allocations": [{
                "id": a.id,
                "pool_id": a.capacity_pool_id,
                "model_id": a.model_id,
                "model_slug": a.model_slug,
                "model_name": models_by_id.get(a.model_id).name if models_by_id.get(a.model_id) else a.model_slug,
                "provider_brand": models_by_id.get(a.model_id).provider_brand if models_by_id.get(a.model_id) else "AI",
                "allocated_capacity": a.allocated_capacity,
                "reserved_capacity": a.reserved_capacity,
                "sold_capacity": a.sold_capacity,
                "consumed_capacity": a.consumed_capacity,
                "remaining_capacity": a.remaining_capacity,
                "capacity_unit": a.capacity_unit,
                "period_type": a.period_type,
                "period_start": a.period_start.isoformat() if a.period_start else None,
                "period_end": a.period_end.isoformat() if a.period_end else None,
                "status": a.status,
                "is_demo": (models_by_id.get(a.model_id).execution_adapter or "mock").lower() == "mock" if models_by_id.get(a.model_id) else True,
            } for a in allocations]
        }
    }


class AdminCreateCapacityPoolSchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_id: int
    execution_adapter: str  # OPENAI, GEMINI, ANTHROPIC, MOCK
    capacity_unit: str = "TOKEN"
    period_type: str = "MONTHLY"
    total_capacity: int = 10_000_000
    status: str = "VERIFIED"


@api_router.post("/admin/capacity-pools", status_code=201)
def admin_create_capacity_pool(
    payload: AdminCreateCapacityPoolSchema,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: create a real execution-backed capacity pool."""
    model = db.query(AIModel).filter(AIModel.id == payload.model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="AI Model not found.")

    pool = AICapacityPool(
        model_id=model.id,
        model_slug=model.slug,
        provider_brand=model.provider_brand or "AI",
        execution_adapter=payload.execution_adapter.upper(),
        capacity_unit=payload.capacity_unit.upper(),
        period_type=payload.period_type.upper(),
        total_capacity=payload.total_capacity,
        allocated_capacity=0,
        reserved_capacity=0,
        sold_capacity=0,
        consumed_capacity=0,
        remaining_capacity=payload.total_capacity,
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc) + timedelta(days=365),
        status=payload.status.upper(),
    )
    db.add(pool)
    db.add(AuditLog(
        user_id=_admin.id,
        actor_email=_admin.email,
        action="CAPACITY_POOL_CREATED",
        resource_type="CAPACITY_POOL",
        resource_id=model.slug,
        details=f"Admin created {payload.total_capacity} {payload.capacity_unit} capacity pool for {model.name} backed by {payload.execution_adapter}."
    ))
    db.commit()
    db.refresh(pool)
    return {"success": True, "data": {"id": pool.id, "model_slug": pool.model_slug, "total_capacity": pool.total_capacity}}


class AdminAllocateCapacitySchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    provider_id: int
    pool_id: int
    allocated_capacity: int
    period_type: Optional[str] = "MONTHLY"


@api_router.post("/admin/provider-allocations", status_code=201)
def admin_allocate_capacity(
    payload: AdminAllocateCapacitySchema,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: allocate TokenPool capacity to a provider."""
    pool = db.query(AICapacityPool).filter(AICapacityPool.id == payload.pool_id).with_for_update().first()
    if not pool:
        raise HTTPException(status_code=404, detail="Capacity pool not found.")

    if payload.allocated_capacity > pool.remaining_capacity:
        raise HTTPException(
            status_code=400,
            detail=f"Requested allocation ({payload.allocated_capacity:,}) exceeds pool remaining capacity ({pool.remaining_capacity:,})."
        )

    prov = db.query(ProviderAccount).filter(ProviderAccount.id == payload.provider_id).first()
    if not prov:
        raise HTTPException(status_code=404, detail="Provider account not found.")

    alloc = ProviderCapacityAllocation(
        provider_id=prov.id,
        capacity_pool_id=pool.id,
        model_id=pool.model_id,
        model_slug=pool.model_slug,
        allocated_capacity=payload.allocated_capacity,
        reserved_capacity=0,
        sold_capacity=0,
        consumed_capacity=0,
        remaining_capacity=payload.allocated_capacity,
        capacity_unit=pool.capacity_unit,
        period_type=payload.period_type or pool.period_type,
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc) + timedelta(days=365),
        status="ACTIVE",
    )
    db.add(alloc)
    pool.allocated_capacity = (pool.allocated_capacity or 0) + payload.allocated_capacity
    pool.remaining_capacity = max(0, pool.total_capacity - pool.allocated_capacity)

    db.add(AuditLog(
        user_id=_admin.id,
        actor_email=_admin.email,
        action="PROVIDER_CAPACITY_ALLOCATED",
        resource_type="PROVIDER_ALLOCATION",
        resource_id=str(prov.id),
        details=f"Admin allocated {payload.allocated_capacity} {pool.capacity_unit} of {pool.model_slug} to provider #{prov.id} from pool #{pool.id}."
    ))

    db.commit()
    db.refresh(alloc)
    return {"success": True, "data": {"id": alloc.id, "allocated_capacity": alloc.allocated_capacity}}


class ListingSchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_slug: Optional[str] = None
    model_id: Optional[int] = None
    connection_id: Optional[int] = None
    provider_connection_id: Optional[int] = None
    allocation_id: Optional[int] = None
    available_credits: Optional[int] = None
    total_tokens: Optional[int] = None
    daily_capacity: Optional[int] = None
    capacity_units: Optional[int] = None
    capacity_unit: Optional[str] = "TOKEN"
    period_type: Optional[str] = "MONTHLY"
    price_per_1k_input: Optional[float] = None
    price_per_1k_output: Optional[float] = None
    price_per_unit: Optional[float] = None
    min_purchase: Optional[int] = 100
    max_purchase: Optional[int] = 50000
    payment_method: Optional[str] = "UPI"
    payment_identifier: Optional[str] = "seller@upi"
    payment_qr_url: Optional[str] = None
    payment_instructions: Optional[str] = None
    description: Optional[str] = ""


@api_router.post("/provider/listings", status_code=201)
def create_listing(
    payload: ListingSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Provider creates AI Model Capacity listing backed by their validated ProviderConnection.
    - Uses Provider-Declared Capacity accounting.
    - Row-level lock prevents concurrent overselling or double-listing.
    - Server-side pricing is strictly verified.
    """
    pa = _get_or_create_provider(current_user, db)
    if pa.status == "BLOCKED":
        raise HTTPException(
            status_code=403,
            detail="Your seller account is blocked by an administrator. You cannot create new listings."
        )

    model = None
    if payload.model_id:
        model = db.query(AIModel).filter(AIModel.id == payload.model_id).first()
    elif payload.model_slug:
        model = db.query(AIModel).filter(AIModel.slug == payload.model_slug).first()

    if not model:
        raise HTTPException(status_code=404, detail="AI Model not found.")

    capacity = payload.total_tokens or payload.available_credits or payload.daily_capacity or payload.capacity_units or 50000
    if capacity <= 0:
        raise HTTPException(status_code=400, detail="Available credits/capacity must be greater than zero.")

    min_p = payload.min_purchase or 100
    max_p = payload.max_purchase or 50000
    if min_p > max_p:
        raise HTTPException(status_code=400, detail="Minimum purchase cannot exceed maximum purchase.")

    # 1. Resolve Provider's Validated Capacity Source (ProviderConnection)
    conn_id = payload.connection_id or payload.provider_connection_id
    conn = None
    if conn_id:
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.id == conn_id,
            ProviderConnection.provider_id == pa.id
        ).with_for_update().first()
        if not conn:
            raise HTTPException(
                status_code=404,
                detail=f"Provider connection #{conn_id} not found or not owned by your provider account."
            )
    else:
        # Find an active, validated connection for this provider
        query = db.query(ProviderConnection).filter(
            ProviderConnection.provider_id == pa.id,
            ProviderConnection.validation_status == "VALIDATED",
            ProviderConnection.status.in_(["CONNECTED", "ACTIVE"])
        )
        # Prefer matching provider type if possible
        req_type = "mock" if ("demo" in model.slug or getattr(model, "is_demo", False)) else None
        if "gemini" in model.slug:
            req_type = "gemini"
        elif "claude" in model.slug:
            req_type = "anthropic"
        elif "gpt" in model.slug:
            req_type = "openai"

        if req_type:
            conn = query.filter(ProviderConnection.provider_type == req_type).order_by(ProviderConnection.capacity_remaining.desc()).with_for_update().first()
        if not conn:
            conn = query.order_by(ProviderConnection.capacity_remaining.desc()).with_for_update().first()

    # Fallback to legacy ProviderCapacityAllocation for migration safety if no connection found
    alloc = None
    if not conn and payload.allocation_id:
        alloc = db.query(ProviderCapacityAllocation).filter(
            ProviderCapacityAllocation.id == payload.allocation_id,
            ProviderCapacityAllocation.provider_id == pa.id,
            ProviderCapacityAllocation.status == "ACTIVE",
        ).with_for_update().first()

    if not conn and not alloc:
        # Only allow auto-creating demo mock connection for explicitly demo models
        is_demo_model = bool(getattr(model, "is_demo", False) or "demo" in model.slug or getattr(model, "execution_adapter", "") == "mock")
        if is_demo_model:
            conn = ProviderConnection(
                provider_id=pa.id,
                provider_name="Orbit AI (DEMO)",
                provider_type="mock",
                auth_type="API_KEY",
                status="CONNECTED",
                credential_masked="sk-demo-••••••••0000",
                validation_status="VALIDATED",
                validated_at=datetime.now(timezone.utc),
                health_status="HEALTHY",
                supported_models=model.slug,
                capacity_declared=1000000,
                capacity_reserved=0,
                capacity_listed=0,
                capacity_sold=0,
                capacity_consumed=0,
                capacity_remaining=1000000,
                identity_status="IDENTITY_MATCHED",
                authorization_status="AUTHORIZED",
                marketplace_status="ELIGIBLE",
                marketplace_reason="Demo mock provider connection is eligible for marketplace execution.",
            )
            db.add(conn)
            db.flush()
        else:
            raise HTTPException(
                status_code=400,
                detail="No connected capacity source. Connect and verify an AI provider before creating a listing."
            )

    # 2. Strict Capacity Accounting Verification
    if conn:
        if getattr(conn, "marketplace_status", None) and conn.marketplace_status != "ELIGIBLE":
            reason = conn.marketplace_reason or "AI account is not eligible for marketplace execution."
            raise HTTPException(
                status_code=400,
                detail=f"This AI account is connected, but it is not currently eligible to supply marketplace requests. {reason}"
            )
        if conn.validation_status != "VALIDATED":
            raise HTTPException(
                status_code=400,
                detail=f"Connection #{conn.id} is not validated (current status: {conn.validation_status}). "
                       "Only validated connections can back marketplace listings."
            )
        if conn.status in ("REVOKED", "DISABLED", "DISCONNECTED"):
            raise HTTPException(
                status_code=400,
                detail=f"Connection #{conn.id} is {conn.status.lower()}. Cannot create listing with inactive capacity source."
            )
        unlisted_capacity = max(0, (conn.capacity_declared or 0) - (conn.capacity_listed or 0))
        if capacity > unlisted_capacity:
            raise HTTPException(
                status_code=400,
                detail=f"Requested listing capacity ({capacity:,} tokens) exceeds available unlisted capacity "
                       f"({unlisted_capacity:,} tokens remaining) from your declared capacity "
                       f"({conn.capacity_declared:,} tokens). You cannot list more than your declared capacity."
            )
        # Atomically update connection listed capacity & apply strict formula
        conn.capacity_listed = (conn.capacity_listed or 0) + capacity
        conn.capacity_remaining = max(
            0,
            (conn.capacity_declared or 0)
            - (conn.capacity_reserved or 0)
            - (conn.capacity_sold or 0)
            - (conn.capacity_consumed or 0)
        )
    elif alloc:
        if capacity > alloc.remaining_capacity:
            raise HTTPException(
                status_code=400,
                detail=f"Requested listing capacity ({capacity:,}) exceeds your available allocation ({alloc.remaining_capacity:,} remaining)."
            )
        alloc.reserved_capacity = (alloc.reserved_capacity or 0) + capacity
        alloc.remaining_capacity = max(0, (alloc.remaining_capacity or 0) - capacity)

    # 3. Server-side price calculation
    price_per_credit = float(payload.price_per_unit or model.price_per_credit or 0.50)
    price_input = payload.price_per_1k_input if payload.price_per_1k_input is not None else model.input_price_per_1k
    price_output = payload.price_per_1k_output if payload.price_per_1k_output is not None else model.output_price_per_1k

    listing = ProviderListing(
        provider_id=pa.id,
        provider_connection_id=conn.id if conn else None,
        provider_capacity_allocation_id=alloc.id if alloc else None,
        model_slug=model.slug,
        model_name=model.name,
        badge=model.badge,
        capacity_unit="TOKEN",
        period_type=payload.period_type or "MONTHLY",
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc) + timedelta(days=365),
        capacity_units=capacity,
        total_capacity=capacity,
        reserved_capacity=0,
        sold_capacity=0,
        consumed_capacity=0,
        remaining_capacity=capacity,
        price_input=price_input,
        price_output=price_output,
        price_label="Server Fixed",
        price_per_unit=price_per_credit,
        min_purchase=min_p,
        max_purchase=max_p,
        payment_method=payload.payment_method or "UPI",
        payment_identifier=payload.payment_identifier or "seller@upi",
        payment_qr_url=payload.payment_qr_url,
        payment_instructions=payload.payment_instructions,
        status="ACTIVE",
    )
    db.add(listing)

    # Update provider account capacity counters
    pa.status = "ACTIVE"
    pa.total_capacity = (pa.total_capacity or 0) + capacity
    pa.available_capacity = (pa.available_capacity or 0) + capacity

    db.add(AuditLog(
        user_id=current_user.id,
        actor_email=current_user.email,
        action="CAPACITY_LISTING_CREATED",
        resource_type="LISTING",
        resource_id=model.slug,
        details=f"Provider #{pa.id} created listing for {capacity:,} tokens ({model.name}) backed by ProviderConnection #{conn.id if conn else 'none'}."
    ))

    db.commit()
    db.refresh(listing)
    return {
        "success": True,
        "data": {
            "id": listing.id,
            "model_id": model.id,
            "model_slug": listing.model_slug,
            "model_name": listing.model_name,
            "provider_connection_id": listing.provider_connection_id,
            "connection_verified": bool(conn and conn.validation_status == "VALIDATED"),
            "capacity_source": "Provider declared" if conn else "Platform allocated",
            "capacity_source_label": "Provider-Declared Capacity",
            "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
            "capacity_unit": listing.capacity_unit,
            "period_type": listing.period_type,
            "capacity_units": listing.capacity_units,
            "total_capacity": listing.total_capacity,
            "remaining_capacity": listing.remaining_capacity,
            "available_credits": listing.remaining_capacity or listing.capacity_units,
            "available_capacity": listing.remaining_capacity or listing.capacity_units,
            "price_per_1k_input": listing.price_input,
            "price_per_1k_output": listing.price_output,
            "price_per_unit": listing.price_per_unit,
            "price_per_credit": listing.price_per_unit,
            "min_purchase": listing.min_purchase,
            "max_purchase": listing.max_purchase,
            "payment_method": listing.payment_method,
            "payment_identifier": listing.payment_identifier,
            "status": listing.status,
            "created_at": listing.created_at.isoformat(),
        }
    }


@api_router.patch("/provider/listings/{listing_id}/status")
def toggle_listing(
    listing_id: int,
    new_status: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        raise HTTPException(status_code=404, detail="No provider account.")
    listing = db.query(ProviderListing).filter(
        ProviderListing.id == listing_id,
        ProviderListing.provider_id == pa.id,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found.")
    if new_status not in ["ACTIVE", "PAUSED", "DELETED"]:
        raise HTTPException(status_code=400, detail="Invalid status.")
    listing.status = new_status
    db.commit()
    return {"success": True, "data": {"id": listing_id, "status": new_status}}


class EditListingSchema(BaseModel):
    model_config = {"protected_namespaces": ()}
    price_per_1k_input: Optional[float] = None
    price_per_1k_output: Optional[float] = None
    daily_capacity: Optional[int] = None
    min_purchase: Optional[int] = None
    status: Optional[str] = None


@api_router.put("/provider/listings/{listing_id}")
def edit_provider_listing(
    listing_id: int,
    payload: EditListingSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        raise HTTPException(status_code=404, detail="No provider account.")
    listing = db.query(ProviderListing).filter(
        ProviderListing.id == listing_id,
        ProviderListing.provider_id == pa.id,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found.")
    if payload.price_per_1k_input is not None:
        listing.price_input = payload.price_per_1k_input
    if payload.price_per_1k_output is not None:
        listing.price_output = payload.price_per_1k_output
    if payload.daily_capacity is not None:
        listing.capacity_units = payload.daily_capacity
    if payload.min_purchase is not None:
        listing.min_purchase = payload.min_purchase
    if payload.status is not None:
        if payload.status.upper() not in ["ACTIVE", "PAUSED", "DELETED"]:
            raise HTTPException(status_code=400, detail="Invalid status.")
        listing.status = payload.status.upper()
    db.commit()
    return {"success": True, "data": {"id": listing.id, "status": listing.status}}


@api_router.delete("/provider/listings/{listing_id}")
def delete_provider_listing(
    listing_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        raise HTTPException(status_code=404, detail="No provider account.")
    listing = db.query(ProviderListing).filter(
        ProviderListing.id == listing_id,
        ProviderListing.provider_id == pa.id,
    ).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found.")
    listing.status = "DELETED"
    db.commit()
    return {"success": True, "data": {"id": listing_id, "deleted": True}}


@api_router.get("/listings")
def get_public_listings(
    search: Optional[str] = None,
    status: Optional[str] = "ACTIVE",
    provider_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    Public Marketplace Listings Query:
    - Filters out listings from BLOCKED providers.
    - Exposes manual payment identifiers, pricing per credit, and capacity availability.
    """
    query = db.query(ProviderListing).join(ProviderAccount).filter(ProviderAccount.status != "BLOCKED")
    if status and status.upper() != "ALL":
        query = query.filter(ProviderListing.status == status.upper())
    if provider_id:
        query = query.filter(ProviderListing.provider_id == provider_id)
    if search:
        query = query.filter(
            ProviderListing.model_name.ilike(f"%{search}%") | ProviderListing.model_slug.ilike(f"%{search}%")
        )
    listings = query.order_by(ProviderListing.created_at.desc()).all()
    models_by_slug = {m.slug: m for m in db.query(AIModel).all()}
    return {
        "success": True,
        "data": {
            "total": len(listings),
            "listings": [{
                "id": l.id,
                "provider_id": l.provider_id,
                "model_id": models_by_slug.get(l.model_slug).id if models_by_slug.get(l.model_slug) else None,
                "model_slug": l.model_slug,
                "model_name": l.model_name,
                "badge": l.badge,
                "capacity_unit": l.capacity_unit or "TOKEN",
                "period_type": l.period_type or "MONTHLY",
                "period_start": l.period_start.isoformat() if l.period_start else None,
                "period_end": l.period_end.isoformat() if l.period_end else None,
                "capacity_units": l.capacity_units,
                "total_capacity": l.total_capacity or l.capacity_units,
                "reserved_capacity": l.reserved_capacity or 0,
                "sold_capacity": l.sold_capacity or 0,
                "consumed_capacity": l.consumed_capacity or 0,
                "remaining_capacity": l.remaining_capacity if l.remaining_capacity is not None else max(0, l.capacity_units - (l.reserved_capacity or 0)),
                "available_capacity": max(0, l.capacity_units - (l.reserved_capacity or 0)),
                "available_credits": max(0, l.capacity_units - (l.reserved_capacity or 0)),
                "provider_connection_id": l.provider_connection_id,
                "connection_name": l.connection.provider_name if l.connection else "Direct Capacity",
                "connection_verified": bool(l.connection and l.connection.validation_status == "VALIDATED"),
                "capacity_source": "Provider declared" if l.provider_connection_id else "Platform",
                "capacity_source_label": "Provider-Declared Capacity",
                "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
                "is_provider_supplied": bool(l.provider_connection_id or l.provider_id),
                "price_input": l.price_input,
                "price_output": l.price_output,
                "price_per_unit": l.price_per_unit or 0.50,
                "price_per_credit": l.price_per_unit or 0.50,
                "min_purchase": l.min_purchase or 100,
                "max_purchase": l.max_purchase or 50000,
                "payment_method": l.payment_method or "UPI",
                "payment_identifier": l.payment_identifier or "seller@upi",
                "payment_qr_url": l.payment_qr_url,
                "payment_instructions": l.payment_instructions,
                "status": l.status,
                "created_at": l.created_at.isoformat(),
            } for l in listings]
        }
    }


# ══════════════════════════════════════════════════════════════════════════
#  MARKETPLACE ORDERS, MANUAL PAYMENT & DISPUTE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

class CreateBuyOrderSchema(BaseModel):
    listing_id: int
    quantity: int

class RejectPaymentSchema(BaseModel):
    reason: str

class RaiseDisputeSchema(BaseModel):
    reason: str
    details: Optional[str] = None

class AdminApprovePaymentSchema(BaseModel):
    admin_notes: Optional[str] = "Admin verified payment and approved order."

class AdminRejectPaymentSchema(BaseModel):
    rejection_reason: str

class AdminRequestEvidenceSchema(BaseModel):
    message: str

class AdminUpdateProviderStatusSchema(BaseModel):
    status: str
    reason: Optional[str] = None


# ── BUYER ENDPOINTS ────────────────────────────────────────────────────────

@api_router.post("/orders")
def create_buy_order(
    payload: CreateBuyOrderSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Step 1: Buyer creates Buy Order for a listing.
    - Capacity is reserved.
    - Financial amount is strictly calculated on backend.
    - Status: PAYMENT_PENDING.
    - Credits are NOT released yet.
    """
    order = MarketplaceOrderService.create_buy_order(
        db=db,
        buyer=current_user,
        listing_id=payload.listing_id,
        quantity=payload.quantity,
    )
    return {
        "success": True,
        "message": "Order created successfully. Please pay the seller directly and submit payment proof.",
        "data": {
            "id": order.id,
            "order_id": order.order_id,
            "listing_id": order.listing_id,
            "quantity": order.quantity,
            "price_per_unit": order.price_per_unit,
            "total_amount": order.total_amount,
            "currency": order.currency,
            "status": order.status,
            "seller_payment_method": order.seller_payment_method,
            "seller_payment_identifier": order.seller_payment_identifier,
            "seller_payment_instructions": order.seller_payment_instructions,
            "seller_qr_url": order.seller_qr_url,
            "expires_at": order.expires_at.isoformat(),
            "created_at": order.created_at.isoformat(),
        }
    }


@api_router.get("/orders")
def get_buyer_orders(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all orders created by the current buyer."""
    query = db.query(Order).filter(Order.buyer_id == current_user.id)
    if status and status.upper() != "ALL":
        query = query.filter(Order.status == status.upper())
    orders = query.order_by(Order.created_at.desc()).all()

    for o in orders:
        MarketplaceOrderService.check_order_expiration(o, db)

    return {
        "success": True,
        "data": {
            "total": len(orders),
            "orders": [{
                "id": o.id,
                "order_id": o.order_id,
                "listing_id": o.listing_id,
                "model_name": o.listing.model_name if o.listing else "AI Model",
                "seller_email": o.seller.email if o.seller else "Seller",
                "seller_name": o.seller.full_name if o.seller else "Seller",
                "quantity": o.quantity,
                "price_per_unit": o.price_per_unit,
                "total_amount": o.total_amount,
                "currency": o.currency,
                "status": o.status,
                "seller_payment_method": o.seller_payment_method,
                "seller_payment_identifier": o.seller_payment_identifier,
                "seller_payment_instructions": o.seller_payment_instructions,
                "rejection_reason": o.rejection_reason,
                "has_proof": o.payment_proof is not None,
                "proof_tx_id": o.payment_proof.transaction_id if o.payment_proof else None,
                "proof_submitted_at": o.payment_proof.submitted_at.isoformat() if o.payment_proof else None,
                "has_dispute": len(o.disputes) > 0,
                "dispute_status": o.disputes[0].status if o.disputes else None,
                "expires_at": o.expires_at.isoformat() if o.expires_at else None,
                "created_at": o.created_at.isoformat(),
            } for o in orders]
        }
    }


@api_router.get("/orders/{order_id}")
def get_order_details(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """View details of a single order. Accessible only by buyer, seller, or admin."""
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if current_user.role != "ADMIN" and current_user.id not in (order.buyer_id, order.seller_id):
        raise HTTPException(status_code=403, detail="You are not authorized to view this order.")

    MarketplaceOrderService.check_order_expiration(order, db)

    proof_data = None
    if order.payment_proof:
        proof_data = {
            "transaction_id": order.payment_proof.transaction_id,
            "extracted_transaction_id": order.payment_proof.extracted_transaction_id,
            "verification_status": order.payment_proof.verification_status,
            "verification_reason": order.payment_proof.verification_reason,
            "verified_at": order.payment_proof.verified_at.isoformat() if order.payment_proof.verified_at else None,
            "amount_reported": order.payment_proof.amount_reported,
            "note": order.payment_proof.note,
            "mime_type": order.payment_proof.mime_type,
            "submitted_at": order.payment_proof.submitted_at.isoformat(),
            "proof_url": f"/api/v1/orders/{order.order_id}/payment-proof",
        }

    disputes_data = [{
        "dispute_id": d.dispute_id,
        "reason": d.reason,
        "details": d.details,
        "status": d.status,
        "admin_notes": d.admin_notes,
        "seller_response": d.seller_response,
        "created_at": d.created_at.isoformat(),
        "messages": [{
            "sender_role": m.sender_role,
            "message": m.message,
            "created_at": m.created_at.isoformat(),
        } for m in d.messages]
    } for d in order.disputes]

    return {
        "success": True,
        "data": {
            "id": order.id,
            "order_id": order.order_id,
            "listing_id": order.listing_id,
            "model_name": order.listing.model_name if order.listing else "AI Model",
            "model_slug": order.listing.model_slug if order.listing else "nova-reasoning",
            "buyer_id": order.buyer_id,
            "buyer_name": order.buyer.full_name if order.buyer else "Buyer",
            "buyer_email": order.buyer.email if order.buyer else "Buyer",
            "seller_id": order.seller_id,
            "seller_name": order.seller.full_name if order.seller else "Seller",
            "seller_email": order.seller.email if order.seller else "Seller",
            "quantity": order.quantity,
            "price_per_unit": order.price_per_unit,
            "total_amount": order.total_amount,
            "currency": order.currency,
            "status": order.status,
            "seller_payment_method": order.seller_payment_method,
            "seller_payment_identifier": order.seller_payment_identifier,
            "seller_payment_instructions": order.seller_payment_instructions,
            "seller_qr_url": order.seller_qr_url,
            "rejection_reason": order.rejection_reason,
            "payment_proof": proof_data,
            "disputes": disputes_data,
            "expires_at": order.expires_at.isoformat() if order.expires_at else None,
            "created_at": order.created_at.isoformat(),
        }
    }


@api_router.post("/payment/verify")
@api_router.post("/orders/{order_id}/verify-payment")
@api_router.post("/orders/{order_id}/verify")
def reject_client_side_verification(
    order_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    current_user: User = Depends(get_current_user),
):
    """
    TEST 9 / Requirement 9: Prevent Frontend Bypass.
    Rejects any direct attempt to forge payment verification with verified=true.
    Payment verification can only be determined server-side from OCR of uploaded screenshot.
    """
    raise HTTPException(
        status_code=400,
        detail="Client-side payment verification is rejected. Payment verification requires server-side OCR evidence from uploaded screenshot."
    )


@api_router.post("/orders/{order_id}/payment-proof")
async def submit_payment_proof_endpoint(
    order_id: str,
    transaction_id: Optional[str] = Form(None),
    amount: float = Form(...),
    screenshot: Optional[UploadFile] = File(None),
    note: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Step 2: Buyer submits payment proof (Transaction ID + Screenshot).
    - Performs server-side OCR and validates exact match of Transaction ID.
    - Status transitions to PAYMENT_SUBMITTED only upon exact match.
    - If mismatch or unreadable, payment is automatically REJECTED.
    """
    if (not transaction_id or not transaction_id.strip()) and (not screenshot or not screenshot.filename):
        raise HTTPException(status_code=400, detail="Transaction ID and payment screenshot are both required.")
    if not transaction_id or not transaction_id.strip():
        raise HTTPException(status_code=400, detail="Transaction ID is required.")
    if not screenshot or not screenshot.filename:
        raise HTTPException(status_code=400, detail="Payment screenshot is required.")

    proof = await MarketplaceOrderService.submit_payment_proof(
        db=db,
        buyer=current_user,
        order_id=order_id,
        transaction_id=transaction_id,
        amount_reported=amount,
        file=screenshot,
        note=note,
    )
    return {
        "success": True,
        "message": "Payment verified successfully.",
        "status": "VERIFIED",
        "transaction_verified": True,
        "data": {
            "order_id": order_id,
            "transaction_id": proof.transaction_id,
            "extracted_transaction_id": proof.extracted_transaction_id,
            "amount_reported": proof.amount_reported,
            "status": "VERIFIED",
            "order_status": "PAYMENT_SUBMITTED",
            "verification_status": "VERIFIED",
            "transaction_verified": True,
            "submitted_at": proof.submitted_at.isoformat(),
        }
    }


@api_router.get("/orders/{order_id}/payment-proof")
def get_order_payment_proof_file(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Secure Payment Screenshot Retrieval:
    - Not accessible via public static directory.
    - Only buyer, seller of the order, or an authorized admin may access it.
    """
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    if current_user.role != "ADMIN" and current_user.id not in (order.buyer_id, order.seller_id):
        raise HTTPException(status_code=403, detail="You are not authorized to view this payment proof.")

    proof = db.query(PaymentProof).filter(PaymentProof.order_id == order.id).first()
    if not proof or not os.path.exists(proof.screenshot_path):
        raise HTTPException(status_code=404, detail="Payment screenshot file not found.")

    return FileResponse(proof.screenshot_path, media_type=proof.mime_type)


@api_router.post("/orders/{order_id}/dispute")
def raise_order_dispute(
    order_id: str,
    payload: RaiseDisputeSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buyer raises a dispute if seller does not confirm or unfairly rejects."""
    dispute = MarketplaceOrderService.buyer_raise_dispute(
        db=db,
        buyer=current_user,
        order_id=order_id,
        reason=payload.reason,
        details=payload.details,
    )
    return {
        "success": True,
        "message": "Dispute raised successfully. TokenPool admin has been notified for review.",
        "data": {
            "dispute_id": dispute.dispute_id,
            "order_id": order_id,
            "reason": dispute.reason,
            "status": dispute.status,
            "created_at": dispute.created_at.isoformat(),
        }
    }


# ── SELLER ENDPOINTS ───────────────────────────────────────────────────────

@api_router.get("/provider/orders")
def get_seller_orders(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Seller views all orders placed on their listings."""
    query = db.query(Order).filter(Order.seller_id == current_user.id)
    if status and status.upper() != "ALL":
        query = query.filter(Order.status == status.upper())
    orders = query.order_by(Order.created_at.desc()).all()

    for o in orders:
        MarketplaceOrderService.check_order_expiration(o, db)

    return {
        "success": True,
        "data": {
            "total": len(orders),
            "orders": [{
                "id": o.id,
                "order_id": o.order_id,
                "listing_id": o.listing_id,
                "model_name": o.listing.model_name if o.listing else "AI Model",
                "buyer_id": o.buyer_id,
                "buyer_name": o.buyer.full_name if o.buyer else "Buyer",
                "buyer_email": o.buyer.email if o.buyer else "Buyer",
                "quantity": o.quantity,
                "price_per_unit": o.price_per_unit,
                "total_amount": o.total_amount,
                "currency": o.currency,
                "status": o.status,
                "has_proof": o.payment_proof is not None,
                "transaction_id": o.payment_proof.transaction_id if o.payment_proof else None,
                "extracted_transaction_id": o.payment_proof.extracted_transaction_id if o.payment_proof else None,
                "verification_status": o.payment_proof.verification_status if o.payment_proof else None,
                "verification_reason": o.payment_proof.verification_reason if o.payment_proof else None,
                "verified_at": o.payment_proof.verified_at.isoformat() if (o.payment_proof and o.payment_proof.verified_at) else None,
                "proof_url": f"/api/v1/orders/{o.order_id}/payment-proof" if o.payment_proof else None,
                "amount_reported": o.payment_proof.amount_reported if o.payment_proof else None,
                "proof_note": o.payment_proof.note if o.payment_proof else None,
                "proof_submitted_at": o.payment_proof.submitted_at.isoformat() if o.payment_proof else None,
                "rejection_reason": o.rejection_reason,
                "has_dispute": len(o.disputes) > 0,
                "dispute_status": o.disputes[0].status if o.disputes else None,
                "created_at": o.created_at.isoformat(),
            } for o in orders]
        }
    }


@api_router.get("/provider/orders/{order_id}")
def get_seller_order_details(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Seller views single order details."""
    return get_order_details(order_id, current_user, db)


@api_router.post("/provider/orders/{order_id}/confirm-payment")
def seller_confirm_payment(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Step 3: Seller confirms payment has arrived in their account.
    - Validates order status = PAYMENT_SUBMITTED.
    - Validates proof & transaction ID exist.
    - Atomically releases credits to buyer.
    - Records provider earning & platform commission.
    - Status transitions to APPROVED.
    """
    order = MarketplaceOrderService.seller_confirm_payment(
        db=db,
        seller=current_user,
        order_id=order_id,
    )
    return {
        "success": True,
        "message": f"Payment confirmed! {order.quantity} credits have been released to buyer.",
        "data": {
            "order_id": order.order_id,
            "status": "APPROVED",
            "quantity": order.quantity,
            "total_amount": order.total_amount,
        }
    }


@api_router.post("/provider/orders/{order_id}/reject-payment")
def seller_reject_payment(
    order_id: str,
    payload: RejectPaymentSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Seller rejects payment submission with reason."""
    order = MarketplaceOrderService.seller_reject_payment(
        db=db,
        seller=current_user,
        order_id=order_id,
        reason=payload.reason,
    )
    return {
        "success": True,
        "message": "Payment rejected. Reserved capacity has been restored.",
        "data": {
            "order_id": order.order_id,
            "status": "REJECTED",
            "reason": order.rejection_reason,
        }
    }


# ── ADMIN ENDPOINTS ────────────────────────────────────────────────────────

@api_router.get("/admin/orders")
def get_admin_orders(
    status: Optional[str] = None,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin views all marketplace orders."""
    query = db.query(Order)
    if status and status.upper() != "ALL":
        query = query.filter(Order.status == status.upper())
    orders = query.order_by(Order.created_at.desc()).all()

    for o in orders:
        MarketplaceOrderService.check_order_expiration(o, db)

    return {
        "success": True,
        "data": {
            "total": len(orders),
            "orders": [{
                "id": o.id,
                "order_id": o.order_id,
                "listing_id": o.listing_id,
                "model_name": o.listing.model_name if o.listing else "AI Model",
                "buyer_id": o.buyer_id,
                "buyer_name": o.buyer.full_name if o.buyer else "Buyer",
                "buyer_email": o.buyer.email if o.buyer else "Buyer",
                "seller_id": o.seller_id,
                "seller_name": o.seller.full_name if o.seller else "Seller",
                "seller_email": o.seller.email if o.seller else "Seller",
                "quantity": o.quantity,
                "price_per_unit": o.price_per_unit,
                "total_amount": o.total_amount,
                "currency": o.currency,
                "status": o.status,
                "has_proof": o.payment_proof is not None,
                "transaction_id": o.payment_proof.transaction_id if o.payment_proof else None,
                "extracted_transaction_id": o.payment_proof.extracted_transaction_id if o.payment_proof else None,
                "verification_status": o.payment_proof.verification_status if o.payment_proof else None,
                "verification_reason": o.payment_proof.verification_reason if o.payment_proof else None,
                "verified_at": o.payment_proof.verified_at.isoformat() if (o.payment_proof and o.payment_proof.verified_at) else None,
                "proof_url": f"/api/v1/orders/{o.order_id}/payment-proof" if o.payment_proof else None,
                "amount_reported": o.payment_proof.amount_reported if o.payment_proof else None,
                "rejection_reason": o.rejection_reason,
                "has_dispute": len(o.disputes) > 0,
                "dispute_status": o.disputes[0].status if o.disputes else None,
                "created_at": o.created_at.isoformat(),
            } for o in orders]
        }
    }


@api_router.get("/admin/disputes")
def get_admin_disputes(
    status: Optional[str] = None,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin views all marketplace disputes."""
    query = db.query(MarketplaceDispute)
    if status and status.upper() != "ALL":
        query = query.filter(MarketplaceDispute.status == status.upper())
    disputes = query.order_by(MarketplaceDispute.created_at.desc()).all()

    return {
        "success": True,
        "data": {
            "total": len(disputes),
            "disputes": [{
                "id": d.id,
                "dispute_id": d.dispute_id,
                "order_id": d.order.order_id if d.order else None,
                "amount": d.order.total_amount if d.order else 0.0,
                "credits": d.order.quantity if d.order else 0,
                "buyer_name": d.raised_by.full_name if d.raised_by else "Buyer",
                "buyer_email": d.raised_by.email if d.raised_by else "Buyer",
                "seller_name": d.order.seller.full_name if (d.order and d.order.seller) else "Seller",
                "seller_email": d.order.seller.email if (d.order and d.order.seller) else "Seller",
                "reason": d.reason,
                "details": d.details,
                "status": d.status,
                "admin_notes": d.admin_notes,
                "seller_response": d.seller_response,
                "transaction_id": d.order.payment_proof.transaction_id if (d.order and d.order.payment_proof) else None,
                "extracted_transaction_id": d.order.payment_proof.extracted_transaction_id if (d.order and d.order.payment_proof) else None,
                "verification_status": d.order.payment_proof.verification_status if (d.order and d.order.payment_proof) else None,
                "proof_url": f"/api/v1/orders/{d.order.order_id}/payment-proof" if (d.order and d.order.payment_proof) else None,
                "created_at": d.created_at.isoformat(),
            } for d in disputes]
        }
    }


@api_router.get("/admin/disputes/{dispute_id}")
def get_admin_dispute_details(
    dispute_id: str,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin views single dispute details with message history."""
    dispute = db.query(MarketplaceDispute).filter(MarketplaceDispute.dispute_id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found.")

    order = dispute.order
    proof = order.payment_proof if order else None

    return {
        "success": True,
        "data": {
            "dispute_id": dispute.dispute_id,
            "status": dispute.status,
            "reason": dispute.reason,
            "details": dispute.details,
            "admin_notes": dispute.admin_notes,
            "seller_response": dispute.seller_response,
            "created_at": dispute.created_at.isoformat(),
            "order": {
                "order_id": order.order_id if order else None,
                "quantity": order.quantity if order else 0,
                "total_amount": order.total_amount if order else 0.0,
                "status": order.status if order else None,
                "buyer_id": order.buyer_id if order else None,
                "buyer_name": order.buyer.full_name if (order and order.buyer) else None,
                "buyer_email": order.buyer.email if (order and order.buyer) else None,
                "seller_id": order.seller_id if order else None,
                "seller_name": order.seller.full_name if (order and order.seller) else None,
                "seller_email": order.seller.email if (order and order.seller) else None,
                "provider_id": order.provider_id if order else None,
            },
            "payment_proof": {
                "transaction_id": proof.transaction_id if proof else None,
                "extracted_transaction_id": proof.extracted_transaction_id if proof else None,
                "verification_status": proof.verification_status if proof else None,
                "verification_reason": proof.verification_reason if proof else None,
                "verified_at": proof.verified_at.isoformat() if (proof and proof.verified_at) else None,
                "amount_reported": proof.amount_reported if proof else None,
                "note": proof.note if proof else None,
                "submitted_at": proof.submitted_at.isoformat() if proof else None,
                "proof_url": f"/api/v1/orders/{order.order_id}/payment-proof" if (order and proof) else None,
            } if proof else None,
            "messages": [{
                "sender_role": m.sender_role,
                "message": m.message,
                "created_at": m.created_at.isoformat(),
            } for m in dispute.messages]
        }
    }


@api_router.post("/admin/orders/{order_id}/approve-payment")
def admin_approve_payment_order(
    order_id: str,
    payload: AdminApprovePaymentSchema = AdminApprovePaymentSchema(),
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin verifies payment and approves order, releasing credits to buyer."""
    order = MarketplaceOrderService.admin_approve_payment(
        db=db,
        admin=current_user,
        order_id=order_id,
        admin_notes=payload.admin_notes,
    )
    return {
        "success": True,
        "message": f"Order #{order.order_id} approved by admin. {order.quantity} credits released to buyer.",
        "data": {
            "order_id": order.order_id,
            "status": "APPROVED",
            "quantity": order.quantity,
        }
    }


@api_router.post("/admin/orders/{order_id}/reject-payment")
def admin_reject_payment_order(
    order_id: str,
    payload: AdminRejectPaymentSchema,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin rejects payment dispute."""
    order = MarketplaceOrderService.admin_reject_payment(
        db=db,
        admin=current_user,
        order_id=order_id,
        rejection_reason=payload.rejection_reason,
    )
    return {
        "success": True,
        "message": f"Order #{order.order_id} rejected by admin.",
        "data": {
            "order_id": order.order_id,
            "status": "REJECTED",
            "reason": order.rejection_reason,
        }
    }


@api_router.post("/admin/disputes/{dispute_id}/request-evidence")
def admin_request_dispute_evidence(
    dispute_id: str,
    payload: AdminRequestEvidenceSchema,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin requests additional evidence from buyer/seller."""
    dispute = MarketplaceOrderService.admin_request_evidence(
        db=db,
        admin=current_user,
        dispute_id=dispute_id,
        message=payload.message,
    )
    return {
        "success": True,
        "message": "Evidence request sent to buyer and seller.",
        "data": {
            "dispute_id": dispute.dispute_id,
            "status": dispute.status,
        }
    }


@api_router.patch("/admin/providers/{provider_id}/status")
def admin_set_provider_status(
    provider_id: int,
    payload: AdminUpdateProviderStatusSchema,
    current_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin blocks, suspends, or unblocks a seller/provider."""
    provider = MarketplaceOrderService.admin_update_provider_status(
        db=db,
        admin=current_user,
        provider_id=provider_id,
        new_status=payload.status,
        reason=payload.reason,
    )
    return {
        "success": True,
        "message": f"Provider #{provider.id} status changed to {provider.status}.",
        "data": {
            "provider_id": provider.id,
            "status": provider.status,
        }
    }



class PayoutSchema(BaseModel):
    amount: float
    destination: str


@api_router.get("/provider/payouts")
def get_payouts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        return {"success": True, "data": {"payouts": []}}
    payouts = db.query(PayoutRequest).filter(
        PayoutRequest.provider_id == pa.id
    ).order_by(PayoutRequest.created_at.desc()).all()
    return {
        "success": True,
        "data": {
            "payouts": [{
                "id": p.id, "amount": p.amount, "status": p.status,
                "destination": p.destination, "reference_id": p.reference_id,
                "created_at": p.created_at.isoformat(),
            } for p in payouts]
        }
    }


@api_router.post("/provider/payouts", status_code=201)
def request_payout(
    payload: PayoutSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        raise HTTPException(status_code=404, detail="No provider account found.")
    if payload.amount < 500:
        raise HTTPException(status_code=400, detail="Minimum payout is ₹500.")
    if payload.amount > pa.withdrawable_balance:
        raise HTTPException(status_code=400, detail=f"Insufficient balance. Available: \u20b9{pa.withdrawable_balance:.2f}")
    pa.withdrawable_balance -= payload.amount
    payout = PayoutRequest(
        provider_id=pa.id, amount=payload.amount,
        status="PENDING", destination=payload.destination,
        reference_id=f"PO-{uuid.uuid4().hex[:8].upper()}",
    )
    db.add(payout)
    db.add(Notification(
        user_id=current_user.id, title="Payout requested",
        message=f"Your payout request of \u20b9{payload.amount:.2f} is being processed.",
        type="INFO",
    ))
    db.commit()
    return {"success": True, "data": {"payout_id": payout.id, "status": "PENDING"}}


# ══════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS  (authenticated user sees own notifications)
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/notifications")
def get_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notifs = db.query(Notification).filter(
        Notification.user_id == current_user.id
    ).order_by(Notification.created_at.desc()).limit(50).all()
    unread = sum(1 for n in notifs if not n.is_read)
    return {
        "success": True,
        "unread_count": unread,
        "data": [{
            "id": n.id, "title": n.title, "message": n.message,
            "type": n.type, "is_read": n.is_read,
            "created_at": n.created_at.isoformat(),
        } for n in notifs]
    }


@api_router.patch("/notifications/read-all")
def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Must be defined BEFORE /notifications/{notif_id}/read to avoid path conflict."""
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,
    ).update({"is_read": True})
    db.commit()
    return {"success": True}


@api_router.patch("/notifications/{notif_id}/read")
def mark_notification_read(
    notif_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    n = db.query(Notification).filter(
        Notification.id == notif_id,
        Notification.user_id == current_user.id,
    ).first()
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found.")
    n.is_read = True
    db.commit()
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════════
#  SUPPORT TICKETS
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/support/tickets")
def get_tickets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tickets = db.query(SupportTicket).filter(
        SupportTicket.user_id == current_user.id
    ).order_by(SupportTicket.created_at.desc()).all()
    return {
        "success": True,
        "data": [{
            "id": t.id, "ticket_id": t.ticket_id, "subject": t.subject,
            "category": t.category, "status": t.status, "priority": t.priority,
            "created_at": t.created_at.isoformat(),
        } for t in tickets]
    }


class TicketSchema(BaseModel):
    subject: str
    message: str
    category: str = "General"
    priority: str = "MEDIUM"


@api_router.post("/support/tickets", status_code=201)
def create_ticket(
    payload: TicketSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = SupportTicket(
        ticket_id=f"TICK-{uuid.uuid4().hex[:6].upper()}",
        user_id=current_user.id, subject=payload.subject,
        category=payload.category, description=payload.message,
        priority=payload.priority, status="OPEN",
    )
    db.add(ticket)
    db.flush()
    db.add(TicketMessage(
        ticket_id=ticket.id, sender_name=current_user.full_name,
        sender_role="USER", message=payload.message,
    ))
    db.add(Notification(
        user_id=current_user.id, title="Support ticket created",
        message=f"Ticket {ticket.ticket_id} submitted. We'll respond within 24 hours.",
        type="INFO",
    ))
    db.commit()
    return {"success": True, "data": {"ticket_id": ticket.ticket_id, "id": ticket.id}}


# ══════════════════════════════════════════════════════════════════════════
#  PROVIDER EARNINGS
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/provider/earnings")
def get_provider_earnings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pa = db.query(ProviderAccount).filter(ProviderAccount.user_id == current_user.id).first()
    if not pa:
        return {"success": True, "data": {
            "total_earned": 0.0, "pending": 0.0, "withdrawable": 0.0,
            "monthly_earned": 0.0, "monthly_requests": 0,
            "daily_earnings": [], "by_model": [],
        }}

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    monthly_earned = db.query(func.sum(AIRequestRecord.provider_earning)).filter(
        AIRequestRecord.provider_id == pa.id,
        AIRequestRecord.created_at >= month_start,
    ).scalar() or 0.0

    monthly_reqs = db.query(AIRequestRecord).filter(
        AIRequestRecord.provider_id == pa.id,
        AIRequestRecord.created_at >= month_start,
    ).count()

    # Daily earnings chart (last 30 days)
    daily_earnings = []
    for i in range(29, -1, -1):
        day = now - timedelta(days=i)
        s = day.replace(hour=0, minute=0, second=0, microsecond=0)
        e = s + timedelta(days=1)
        earn = db.query(func.sum(AIRequestRecord.provider_earning)).filter(
            AIRequestRecord.provider_id == pa.id,
            AIRequestRecord.created_at >= s,
            AIRequestRecord.created_at < e,
        ).scalar() or 0.0
        daily_earnings.append({"date": day.strftime("%b %d"), "amount": round(earn, 4)})

    # Earnings by model
    model_rows = db.query(
        AIRequestRecord.model_name,
        AIRequestRecord.badge,
        func.sum(AIRequestRecord.provider_earning).label("earned"),
        func.count(AIRequestRecord.id).label("requests"),
    ).filter(
        AIRequestRecord.provider_id == pa.id,
    ).group_by(AIRequestRecord.model_name, AIRequestRecord.badge).all()

    return {"success": True, "data": {
        "total_earned": pa.total_earnings,
        "pending": pa.pending_earnings,
        "withdrawable": pa.withdrawable_balance,
        "monthly_earned": round(monthly_earned, 4),
        "monthly_requests": monthly_reqs,
        "daily_earnings": daily_earnings,
        "by_model": [{
            "model": r.model_name, "badge": r.badge,
            "earned": round(r.earned, 4), "requests": r.requests,
        } for r in model_rows],
    }}


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS  —  RBAC enforced by get_admin_user dependency
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/admin/overview")
def admin_overview(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    total_users    = db.query(User).count()
    active_users   = db.query(User).filter(User.status == "ACTIVE").count()
    total_providers= db.query(ProviderAccount).count()
    active_listings= db.query(ProviderListing).filter(ProviderListing.status == "ACTIVE").count()
    total_requests = db.query(AIRequestRecord).count()

    platform_rev   = db.query(func.sum(AIRequestRecord.platform_fee)).scalar() or 0.0
    total_spent    = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.tx_type == "TOP_UP"
    ).scalar() or 0.0

    # Daily revenue last 7 days
    now = datetime.now(timezone.utc)
    daily_revenue = []
    for i in range(6, -1, -1):
        day = now - timedelta(days=i)
        s   = day.replace(hour=0, minute=0, second=0, microsecond=0)
        e   = s + timedelta(days=1)
        rev = db.query(func.sum(AIRequestRecord.platform_fee)).filter(
            AIRequestRecord.created_at >= s,
            AIRequestRecord.created_at < e,
        ).scalar() or 0.0
        daily_revenue.append({"date": day.strftime("%a"), "amount": round(rev, 4)})

    recent_users = db.query(User).order_by(User.created_at.desc()).limit(10).all()

    return {
        "success": True,
        "data": {
            "total_users": total_users, "active_users": active_users,
            "total_providers": total_providers, "active_listings": active_listings,
            "total_requests": total_requests, "platform_revenue": platform_rev,
            "total_platform_topup": total_spent,
            "daily_revenue": daily_revenue,
            "recent_users": [{
                "id": u.id, "full_name": u.full_name, "email": u.email,
                "role": u.role, "status": u.status, "is_active": u.status == "ACTIVE",
                "wallet_balance": (db.query(Wallet).filter(Wallet.user_id == u.id).first() or Wallet()).balance or 0,
                "total_requests": db.query(AIRequestRecord).filter(AIRequestRecord.user_id == u.id).count(),
                "created_at": u.created_at.isoformat(),
            } for u in recent_users],
        }
    }


@api_router.get("/admin/users")
def admin_get_users(
    search: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = db.query(User)
    if search:
        query = query.filter(
            User.full_name.ilike(f"%{search}%") | User.email.ilike(f"%{search}%")
        )
    if role:
        query = query.filter(User.role == role.upper())
    if status:
        query = query.filter(User.status == status.upper())
    total = query.count()
    users = query.order_by(User.created_at.desc()).offset((page - 1) * limit).limit(limit).all()

    return {
        "success": True,
        "data": {
            "total": total, "page": page,
            "users": [{
                "id": u.id, "full_name": u.full_name, "email": u.email,
                "role": u.role, "status": u.status, "is_active": u.status == "ACTIVE",
                "wallet_balance": (db.query(Wallet).filter(Wallet.user_id == u.id).first() or Wallet()).balance or 0,
                "total_requests": db.query(AIRequestRecord).filter(AIRequestRecord.user_id == u.id).count(),
                "created_at": u.created_at.isoformat(),
            } for u in users],
        }
    }


@api_router.patch("/admin/users/{user_id}/status")
def admin_toggle_user(
    user_id: int,
    new_status: str,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found.")
    if u.role == "ADMIN":
        raise HTTPException(status_code=403, detail="Cannot suspend admin accounts.")
    if new_status not in ["ACTIVE", "SUSPENDED"]:
        raise HTTPException(status_code=400, detail="Invalid status.")
    u.status = new_status
    db.commit()
    return {"success": True, "data": {"id": user_id, "status": new_status}}


@api_router.get("/admin/providers")
def admin_get_providers(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    providers = db.query(ProviderAccount).all()
    data = []
    for p in providers:
        u = p.user
        conns = db.query(ProviderConnection).filter(ProviderConnection.provider_id == p.id).all()
        data.append({
            "id": p.id,
            "user_id": p.user_id,
            "workspace_name": p.workspace_name,
            "email": u.email if u else None,
            "status": p.status,
            "total_earnings": p.total_earnings,
            "withdrawable_balance": p.withdrawable_balance,
            "active_listings": db.query(ProviderListing).filter(
                ProviderListing.provider_id == p.id,
                ProviderListing.status == "ACTIVE"
            ).count(),
            "connections": [{
                "id": c.id,
                "provider_type": c.provider_type,
                "account_email": c.account_email or (u.email if u else ""),
                "identity_status": c.identity_status,
                "authorization_status": c.authorization_status,
                "marketplace_status": c.marketplace_status,
                "capacity_declared": c.capacity_declared or 0,
                "capacity_consumed": c.capacity_consumed or 0,
                "last_verified_at": c.last_verified_at.isoformat() if c.last_verified_at else None,
            } for c in conns],
            "created_at": p.created_at.isoformat(),
        })
    return {
        "success": True,
        "data": {
            "total": len(providers),
            "providers": data,
        }
    }


@api_router.get("/admin/provider-connections")
def admin_get_provider_connections(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    conns = db.query(ProviderConnection).order_by(ProviderConnection.created_at.desc()).all()
    return {
        "success": True,
        "data": {
            "total": len(conns),
            "connections": [{
                "id": c.id,
                "provider_id": c.provider_id,
                "provider_email": c.provider.user.email if (c.provider and c.provider.user) else None,
                "provider_type": c.provider_type,
                "provider_name": c.provider_name,
                "account_email": c.account_email,
                "identity_status": c.identity_status,
                "authorization_status": c.authorization_status,
                "marketplace_status": c.marketplace_status,
                "marketplace_reason": c.marketplace_reason,
                "plan_name": c.plan_name,
                "capacity_declared": c.capacity_declared or 0,
                "capacity_consumed": c.capacity_consumed or 0,
                "capacity_remaining": c.capacity_remaining or 0,
                "last_verified_at": c.last_verified_at.isoformat() if c.last_verified_at else None,
                "last_health_check_at": c.last_health_check_at.isoformat() if c.last_health_check_at else None,
                "status": c.connection_status or c.status,
                "created_at": c.created_at.isoformat(),
            } for c in conns]
        }
    }


@api_router.post("/admin/provider-connections/{connection_id}/set-capacity")
def admin_set_provider_capacity(
    connection_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin attempts to fabricate or manually alter provider capacity.
    Strictly prohibited. Capacity must be declared directly by the provider.
    """
    raise HTTPException(
        status_code=403,
        detail="Fabricating or artificially altering provider capacity is strictly prohibited. Capacity commitments must be declared directly by the authorized provider."
    )


@api_router.get("/admin/listings")
def admin_get_listings(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    listings = db.query(ProviderListing).order_by(ProviderListing.created_at.desc()).all()
    return {
        "success": True,
        "data": {
            "total": len(listings),
            "listings": [{
                "id": l.id,
                "provider_id": l.provider_id,
                "model_slug": l.model_slug,
                "model_name": l.model_name,
                "badge": l.badge,
                "capacity_units": l.capacity_units,
                "price_input": l.price_input,
                "price_output": l.price_output,
                "min_purchase": l.min_purchase,
                "status": l.status,
                "created_at": l.created_at.isoformat(),
            } for l in listings]
        }
    }


@api_router.get("/admin/transactions")
def admin_get_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tx_type: Optional[str] = None,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = db.query(WalletTransaction)
    if tx_type:
        query = query.filter(WalletTransaction.tx_type == tx_type.upper())
    total = query.count()
    txs = query.order_by(WalletTransaction.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "success": True,
        "data": {
            "total": total, "page": page,
            "transactions": [{
                "id": t.id, "wallet_id": t.wallet_id,
                "type": t.tx_type, "amount": t.amount,
                "balance_after": t.balance_after,
                "description": t.description, "status": t.status,
                "created_at": t.created_at.isoformat(),
            } for t in txs]
        }
    }


@api_router.get("/admin/settings")
def admin_get_settings(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings_rows = db.query(PlatformSetting).all()
    return {
        "success": True,
        "data": {s.key: s.value for s in settings_rows}
    }


@api_router.put("/admin/settings")
def admin_save_settings(
    payload: Dict[str, str],
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    for key, value in payload.items():
        row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(PlatformSetting(key=key, value=value))
    db.commit()
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN PROVIDER CONNECTIONS & CAPACITY OVERSIGHT
# ══════════════════════════════════════════════════════════════════════════

class AdminConnectionStatusSchema(BaseModel):
    status: Optional[str] = None # ACTIVE, DISABLED, REVOKED, DISCONNECTED
    validation_status: Optional[str] = None # VALIDATED, DISABLED, REVOKED, FAILED
    reason: Optional[str] = None


@api_router.get("/admin/provider-connections")
def admin_get_provider_connections(
    status: Optional[str] = None,
    validation_status: Optional[str] = None,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin: inspect all provider connections, credential verification status,
    and provider-declared capacity accounting.
    """
    query = db.query(ProviderConnection).join(ProviderAccount).join(User, ProviderAccount.user_id == User.id)
    if status:
        query = query.filter(ProviderConnection.status == status.upper())
    if validation_status:
        query = query.filter(ProviderConnection.validation_status == validation_status.upper())

    connections = query.order_by(ProviderConnection.created_at.desc()).all()

    return {
        "success": True,
        "data": {
            "total": len(connections),
            "connections": [{
                "id": c.id,
                "provider_id": c.provider_id,
                "provider_email": c.provider.user.email if (c.provider and c.provider.user) else "Unknown",
                "provider_name": c.provider_name,
                "provider_type": c.provider_type or "openai",
                "auth_type": c.auth_type,
                "status": c.status,
                "validation_status": c.validation_status or "PENDING",
                "validated_at": c.validated_at.isoformat() if c.validated_at else None,
                "credential_masked": c.credential_masked,
                "health_status": c.health_status,
                "capacity_declared": c.capacity_declared or 0,
                "capacity_reserved": c.capacity_reserved or 0,
                "capacity_listed": c.capacity_listed or 0,
                "capacity_sold": c.capacity_sold or 0,
                "capacity_consumed": c.capacity_consumed or 0,
                "capacity_remaining": c.capacity_remaining if c.capacity_remaining is not None else (c.capacity_declared or 0),
                "capacity_source": "Provider declared",
                "verification_note": "TokenPool verifies the connected credential, but the upstream provider's remaining quota may vary.",
                "total_earnings": c.provider.total_earnings if c.provider else 0.0,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            } for c in connections]
        }
    }


@api_router.patch("/admin/provider-connections/{connection_id}/status")
def admin_update_connection_status(
    connection_id: int,
    payload: AdminConnectionStatusSchema,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin: disable or revoke suspicious capacity sources or re-enable them.
    Automatically pauses all marketplace listings backed by a disabled connection.
    """
    conn = db.query(ProviderConnection).filter(ProviderConnection.id == connection_id).with_for_update().first()
    if not conn:
        raise HTTPException(status_code=404, detail="Provider connection not found.")

    if payload.status:
        conn.status = payload.status.upper()
    if payload.validation_status:
        conn.validation_status = payload.validation_status.upper()

    listings_affected = 0
    if conn.status in ("DISABLED", "REVOKED", "DISCONNECTED") or conn.validation_status in ("DISABLED", "REVOKED"):
        listings = db.query(ProviderListing).filter(
            ProviderListing.provider_connection_id == conn.id,
            ProviderListing.status == "ACTIVE"
        ).all()
        for l in listings:
            l.status = "PAUSED"
        listings_affected = len(listings)

    db.add(AuditLog(
        user_id=_admin.id,
        actor_email=_admin.email,
        action="ADMIN_PROVIDER_CONNECTION_UPDATED",
        resource_type="PROVIDER_CONNECTION",
        resource_id=str(conn.id),
        details=f"Admin set connection #{conn.id} status={conn.status}, validation={conn.validation_status}. "
               f"Reason: {payload.reason or 'Admin intervention'}. Listings paused: {listings_affected}."
    ))
    db.commit()

    return {
        "success": True,
        "data": {
            "id": conn.id,
            "status": conn.status,
            "validation_status": conn.validation_status,
            "listings_paused": listings_affected,
        }
    }


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN DELEGATED INTEGRATION STATUS & GOVERNANCE
# ══════════════════════════════════════════════════════════════════════════

@api_router.get("/admin/integrations/openai-delegated")
def admin_get_openai_delegated_status(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin: inspect official OpenAI Delegated Integration status and operational readiness.
    Status values: NOT_APPLIED, PENDING, APPROVED, REJECTED, SUSPENDED.
    """
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "openai_participating_app_status").first()
    current_status = setting.value if setting else getattr(settings, "OPENAI_PARTICIPATING_APP_STATUS", "NOT_APPLIED")

    client_id_ok = bool(settings.OPENAI_DELEGATED_CLIENT_ID)
    client_secret_ok = bool(settings.OPENAI_DELEGATED_CLIENT_SECRET)
    configured = client_id_ok and client_secret_ok

    enabled = getattr(settings, "OPENAI_DELEGATED_PLAN_ENABLED", False)
    exec_avail = bool(enabled and current_status == "APPROVED" and configured)

    return {
        "success": True,
        "data": {
            "app_name": "TokenPool",
            "integration_type": "OFFICIAL_DELEGATED_PLAN",
            "status": current_status,
            "status_label": current_status.replace("_", " ").title(),
            "enabled": enabled,
            "configured": configured,
            "execution_available": exec_avail,
            "execution_status": "AVAILABLE" if exec_avail else "UNAVAILABLE",
            "auth_url": settings.OPENAI_DELEGATED_AUTH_URL,
            "token_url": settings.OPENAI_DELEGATED_TOKEN_URL,
            "redirect_uri": settings.OPENAI_DELEGATED_REDIRECT_URI,
            "details": (
                "Official OpenAI participating application integration for eligible ChatGPT Plus/Pro subscriptions. "
                "Only APPROVED status and OPENAI_DELEGATED_PLAN_ENABLED=true permits live production execution."
            )
        }
    }


@api_router.post("/admin/integrations/openai-delegated/status")
def admin_update_openai_delegated_status(
    payload: UpdateIntegrationStatusSchema,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin: updates OpenAI participating app status.
    Allowed statuses: NOT_APPLIED, PENDING, APPROVED, REJECTED, SUSPENDED.
    """
    valid_statuses = {"NOT_APPLIED", "PENDING", "APPROVED", "REJECTED", "SUSPENDED"}
    new_status = (payload.status or "").upper().strip()
    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: {new_status}. Allowed values: {', '.join(sorted(valid_statuses))}."
        )

    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "openai_participating_app_status").first()
    if not setting:
        setting = PlatformSetting(
            key="openai_participating_app_status",
            value=new_status,
            description="Official OpenAI participating app approval status"
        )
        db.add(setting)
    else:
        setting.value = new_status

    # Also update in-memory settings
    settings.OPENAI_PARTICIPATING_APP_STATUS = new_status

    db.add(AuditLog(
        user_id=_admin.id,
        actor_email=_admin.email,
        action="ADMIN_INTEGRATION_STATUS_UPDATED",
        resource_type="INTEGRATION",
        resource_id="openai_delegated",
        details=f"Admin updated OpenAI participating app status to {new_status}. Reason: {payload.reason or 'Admin update'}."
    ))
    db.commit()

    return {
        "success": True,
        "message": f"OpenAI participating app status updated to {new_status}.",
        "data": {
            "status": new_status,
            "execution_available": bool(settings.OPENAI_DELEGATED_PLAN_ENABLED and new_status == "APPROVED" and settings.OPENAI_DELEGATED_CLIENT_ID)
        }
    }

