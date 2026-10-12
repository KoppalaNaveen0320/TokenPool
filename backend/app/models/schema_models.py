import enum
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, Enum as SQLEnum
from sqlalchemy.orm import relationship
from backend.app.core.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class UserRole(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"

class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    PENDING = "PENDING"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), default="USER", nullable=False)
    status = Column(String(50), default="ACTIVE", nullable=False)
    avatar_initials = Column(String(10), default="TP")
    organization = Column(String(255), nullable=True)
    bio = Column(Text, nullable=True)
    is_verified = Column(Boolean, default=True)
    google_sub_id = Column(String(255), unique=True, index=True, nullable=True)
    email_verified = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    wallet = relationship("Wallet", back_populates="user", uselist=False)
    provider_account = relationship("ProviderAccount", back_populates="user", uselist=False)
    conversations = relationship("Conversation", back_populates="user")
    requests = relationship("AIRequestRecord", back_populates="user")
    notifications = relationship("Notification", back_populates="user")
    support_tickets = relationship("SupportTicket", back_populates="user")

class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    balance = Column(Float, default=0.0) # Real cash balance (₹)
    credits = Column(Integer, default=0) # AI Credits
    currency = Column(String(10), default="INR")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="wallet")
    transactions = relationship("WalletTransaction", back_populates="wallet", order_by="desc(WalletTransaction.created_at)")

class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id"), nullable=False)
    tx_type = Column(String(50), nullable=False) # USAGE, TOP_UP, REFUND, EARNING, PAYOUT, COMMISSION
    amount = Column(Float, nullable=False) # + or - amount
    balance_after = Column(Float, nullable=False)
    credits_delta = Column(Integer, default=0)
    description = Column(String(255), nullable=False)
    status = Column(String(50), default="COMPLETED") # COMPLETED, PENDING, REFUNDED, FAILED
    meta_info = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    wallet = relationship("Wallet", back_populates="transactions")

class ProviderAccount(Base):
    __tablename__ = "provider_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    workspace_name = Column(String(255), nullable=False, default="My Provider")
    total_capacity = Column(Integer, default=0)
    available_capacity = Column(Integer, default=0)
    used_capacity = Column(Integer, default=0)
    reserved_capacity = Column(Integer, default=0)
    total_earnings = Column(Float, default=0.0)
    pending_earnings = Column(Float, default=0.0)
    withdrawable_balance = Column(Float, default=0.0)
    status = Column(String(50), default="NOT_CONNECTED")
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="provider_account")
    connections = relationship("ProviderConnection", back_populates="provider")
    listings = relationship("ProviderListing", back_populates="provider")
    payouts = relationship("PayoutRequest", back_populates="provider")

class ProviderConnection(Base):
    """
    Real provider capacity source backed by authenticated provider credentials.
    - Connection verification validates credential via provider API.
    - Provider-declared capacity records the provider's token commitment to TokenPool marketplace.
    - Upstream quota is NOT assumed or falsely claimed as verified.
    """
    __tablename__ = "provider_connections"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id"), nullable=False)
    provider_name = Column(String(100), nullable=False) # e.g. Orbit AI, Nexus Cloud, Vector Labs
    provider_type = Column(String(50), default="openai") # openai, gemini, anthropic, mock
    auth_type = Column(String(50), default="API_KEY") # API_KEY, OAUTH
    status = Column(String(50), default="CONNECTED") # CONNECTED, NOT_CONNECTED, REAUTHORIZATION, DISCONNECTED
    encrypted_credential = Column(Text, nullable=True) # AES/Fernet encrypted key
    credential_masked = Column(String(255), default="sk-••••••••9842")
    validation_status = Column(String(50), default="PENDING") # PENDING, VALIDATED, FAILED, REVOKED, DISABLED
    validated_at = Column(DateTime, nullable=True)
    health_status = Column(String(50), default="HEALTHY") # HEALTHY, DEGRADED, OFFLINE
    supported_models = Column(String(255), default="6 supported models")
    
    # Provider-Declared Marketplace Capacity Accounting (Tokens)
    capacity_declared = Column(Integer, default=0) # Total capacity declared by provider
    capacity_reserved = Column(Integer, default=0) # Temporarily reserved for pending orders
    capacity_listed = Column(Integer, default=0) # Committed to active marketplace listings
    capacity_sold = Column(Integer, default=0) # Sold to consumers
    capacity_consumed = Column(Integer, default=0) # Actually consumed by executed AI requests
    capacity_remaining = Column(Integer, default=0) # Remaining available marketplace capacity
    # Gmail-based AI Account Identity & Association Fields
    account_email = Column(String(255), nullable=True, index=True)
    account_identifier = Column(String(255), nullable=True)
    connection_method = Column(String(50), default="google_identity") # google_identity, official_oauth, provider_authorization, delegated_access, api_key_fallback, demo
    identity_status = Column(String(50), default="IDENTITY_MATCHED") # IDENTITY_MATCHED, IDENTITY_MISMATCH, PENDING_VERIFICATION
    authorization_status = Column(String(50), default="AUTHORIZED") # AUTHORIZED, NOT_AUTHORIZED, EXPIRED, REVOKED, PENDING
    connection_status = Column(String(50), default="CONNECTED") # CONNECTED, NOT_CONNECTED, DISCONNECTED, EXPIRED, PENDING
    plan_type = Column(String(50), default="UNVERIFIED") # PRO, FREE, CUSTOM, UNVERIFIED
    plan_name = Column(String(100), default="Unable to verify") # e.g. AI Pro, Pro, Pro/Max, Unable to verify
    capacity_status = Column(String(50), default="DECLARED") # DECLARED, AVAILABLE, EXHAUSTED, UNKNOWN
    marketplace_status = Column(String(50), default="NOT_ELIGIBLE") # ELIGIBLE, NOT_ELIGIBLE, PENDING_REVIEW, PAUSED
    marketplace_reason = Column(Text, nullable=True) # Reason why eligible or not
    execution_status = Column(String(50), default="EXECUTION_NOT_SUPPORTED") # EXECUTION_SUPPORTED, EXECUTION_NOT_SUPPORTED, PENDING
    failure_reason = Column(Text, nullable=True)
    capacity_source = Column(String(100), default="Provider API Capacity")
    # Official Delegated Plan Execution Fields
    delegated_provider = Column(String(50), nullable=True) # openai, google, anthropic
    delegated_plan_name = Column(String(100), nullable=True) # ChatGPT Plus, ChatGPT Pro, Google One AI Pro, Claude Pro
    delegated_authorization_status = Column(String(50), default="NOT_CONNECTED") # NOT_CONNECTED, AUTHORIZATION_REQUIRED, AUTHORIZATION_PENDING, AUTHORIZED, REVOKED
    delegated_execution_status = Column(String(50), default="NOT_SUPPORTED") # NOT_SUPPORTED, PLAN_USAGE_SUPPORTED, EXECUTION_SUPPORTED, EXECUTION_NOT_SUPPORTED, EXECUTION_FAILED
    marketplace_commitment = Column(Float, default=0.0) # percentage or fraction e.g. 20.0 (20%)
    committed_units = Column(Integer, default=0) # Marketplace Capacity Commitment units
    reserved_units = Column(Integer, default=0)
    consumed_units = Column(Integer, default=0)
    remaining_units = Column(Integer, default=0)
    upstream_usage_unit = Column(String(50), default="REQUEST") # REQUEST, PERCENTAGE, TOKEN, CREDIT
    upstream_limit = Column(Integer, nullable=True)
    upstream_reset_at = Column(DateTime, nullable=True)
    encrypted_authorization_data = Column(Text, nullable=True) # Encrypted OAuth token/secret payload
    token_expires_at = Column(DateTime, nullable=True)
    last_verified_at = Column(DateTime, nullable=True)
    last_health_check_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    
    last_health_check = Column(DateTime, default=utcnow)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    # Conceptual Section 7 Compatibility Properties
    @property
    def provider_email(self):
        return self.account_email

    @property
    def ai_provider(self):
        return self.provider_type

    @property
    def declared_capacity(self):
        return self.capacity_declared

    @property
    def reserved_capacity(self):
        return self.capacity_reserved

    @property
    def consumed_capacity(self):
        return self.capacity_consumed

    @property
    def remaining_marketplace_capacity(self):
        return self.capacity_remaining

    @property
    def masked_credential(self):
        return self.credential_masked

    provider = relationship("ProviderAccount", back_populates="connections")
    listings = relationship("ProviderListing", back_populates="connection")

class AIModel(Base):
    __tablename__ = "ai_models"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(100), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    provider_brand = Column(String(100), nullable=False)
    badge = Column(String(5), nullable=False) # N, S, A, V, E, R
    badge_color = Column(String(50), default="emerald")
    description = Column(String(255), nullable=False)
    capabilities = Column(String(255), default="Reasoning, tools")
    input_price_per_1k = Column(Float, default=0.08)
    output_price_per_1k = Column(Float, default=0.15)
    avg_latency_ms = Column(Integer, default=420)
    latency_label = Column(String(50), default="420ms avg")
    status = Column(String(50), default="AVAILABLE") # AVAILABLE, LIMITED, UNAVAILABLE
    context_window = Column(String(50), default="128k")
    price_per_credit = Column(Float, default=0.50) # INR per AI model credit
    capacity_unit = Column(String(50), default="TOKEN") # TOKEN, REQUEST, CREDIT, IMAGE, COMPUTE, SPEND
    supported_periods = Column(String(100), default="DAILY,MONTHLY,YEARLY")
    execution_adapter = Column(String(50), default="mock") # gemini, openai, anthropic, mock
    is_active = Column(Boolean, default=True)

    capacity_pools = relationship("AICapacityPool", back_populates="model")


class AICapacityPool(Base):
    """
    TokenPool-managed AI Capacity Pool backed by authorized AI provider integrations.
    A PostgreSQL number alone is NEVER marked VERIFIED without an active execution adapter.
    """
    __tablename__ = "ai_capacity_pools"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(Integer, ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False)
    model_slug = Column(String(100), nullable=False, index=True)
    provider_brand = Column(String(100), nullable=False) # Google Gemini, OpenAI, Anthropic, Orbit AI
    execution_adapter = Column(String(50), nullable=False, default="mock") # gemini, openai, anthropic, mock
    capacity_unit = Column(String(50), default="TOKEN", nullable=False) # TOKEN, REQUEST, CREDIT, IMAGE, COMPUTE, SPEND
    period_type = Column(String(50), default="MONTHLY", nullable=False) # DAILY, MONTHLY, YEARLY
    total_capacity = Column(Integer, default=0, nullable=False)
    allocated_capacity = Column(Integer, default=0, nullable=False)
    reserved_capacity = Column(Integer, default=0, nullable=False)
    sold_capacity = Column(Integer, default=0, nullable=False)
    consumed_capacity = Column(Integer, default=0, nullable=False)
    remaining_capacity = Column(Integer, default=0, nullable=False)
    period_start = Column(DateTime, default=utcnow, nullable=False)
    period_end = Column(DateTime, nullable=False)
    status = Column(String(50), default="UNVERIFIED", nullable=False)
    # Statuses: ACTIVE, INACTIVE, VERIFIED, UNVERIFIED, AUTHORIZATION_REQUIRED, SUSPENDED, EXPIRED, EXHAUSTED
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    model = relationship("AIModel", back_populates="capacity_pools")
    allocations = relationship("ProviderCapacityAllocation", back_populates="capacity_pool", cascade="all, delete-orphan")


class ProviderCapacityAllocation(Base):
    """
    Internal TokenPool capacity allocated to a marketplace provider.
    Providers cannot type or create fake arbitrary capacity.
    """
    __tablename__ = "provider_capacity_allocations"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    capacity_pool_id = Column(Integer, ForeignKey("ai_capacity_pools.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id = Column(Integer, ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False)
    model_slug = Column(String(100), nullable=False, index=True)
    allocated_capacity = Column(Integer, default=0, nullable=False)
    reserved_capacity = Column(Integer, default=0, nullable=False)
    sold_capacity = Column(Integer, default=0, nullable=False)
    consumed_capacity = Column(Integer, default=0, nullable=False)
    remaining_capacity = Column(Integer, default=0, nullable=False)
    capacity_unit = Column(String(50), default="TOKEN", nullable=False)
    period_type = Column(String(50), default="MONTHLY", nullable=False)
    period_start = Column(DateTime, default=utcnow, nullable=False)
    period_end = Column(DateTime, nullable=False)
    status = Column(String(50), default="ACTIVE", nullable=False) # ACTIVE, SUSPENDED, EXPIRED, EXHAUSTED
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    provider = relationship("ProviderAccount")
    capacity_pool = relationship("AICapacityPool", back_populates="allocations")
    model = relationship("AIModel")
    listings = relationship("ProviderListing", back_populates="allocation")


class ProviderListing(Base):
    __tablename__ = "provider_listings"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id"), nullable=False)
    provider_connection_id = Column(Integer, ForeignKey("provider_connections.id", ondelete="SET NULL"), nullable=True) # Direct Provider Connection Capacity Source
    provider_capacity_allocation_id = Column(Integer, ForeignKey("provider_capacity_allocations.id", ondelete="SET NULL"), nullable=True) # Deprecated top-down allocation (kept for safe migration)
    model_slug = Column(String(100), nullable=False)
    model_name = Column(String(100), nullable=False)
    badge = Column(String(5), default="N")
    capacity_unit = Column(String(50), default="TOKEN")
    period_type = Column(String(50), default="MONTHLY")
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    capacity_units = Column(Integer, default=18500)
    total_capacity = Column(Integer, default=18500)
    reserved_capacity = Column(Integer, default=0)
    sold_capacity = Column(Integer, default=0)
    consumed_capacity = Column(Integer, default=0)
    remaining_capacity = Column(Integer, default=18500)
    price_input = Column(Float, default=0.08)
    price_output = Column(Float, default=0.15)
    price_label = Column(String(100), default="Backend set")
    price_per_unit = Column(Float, default=0.50) # INR per unit/credit
    min_purchase = Column(Integer, default=100)
    max_purchase = Column(Integer, default=50000)
    payment_method = Column(String(100), default="UPI") # UPI, Bank Transfer, IMPS, etc.
    payment_identifier = Column(String(255), default="seller@upi")
    payment_qr_url = Column(String(255), nullable=True)
    payment_instructions = Column(Text, nullable=True)
    status = Column(String(50), default="ACTIVE") # ACTIVE, PAUSED, DELETED
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    provider = relationship("ProviderAccount", back_populates="listings")
    connection = relationship("ProviderConnection", back_populates="listings")
    allocation = relationship("ProviderCapacityAllocation", back_populates="listings")
    orders = relationship("Order", back_populates="listing")

class AIRequestRecord(Base):
    __tablename__ = "ai_request_records"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(String(100), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider_id = Column(Integer, nullable=True)
    provider_connection_id = Column(Integer, nullable=True)
    model_slug = Column(String(100), nullable=False)
    model_name = Column(String(100), nullable=False)
    badge = Column(String(5), default="N")
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    consumer_cost = Column(Float, default=0.0) # Total deducted
    provider_earning = Column(Float, default=0.0) # Provider share (e.g. 80%)
    platform_fee = Column(Float, default=0.0) # Platform commission (e.g. 20%)
    latency_ms = Column(Integer, default=450)
    status = Column(String(50), default="COMPLETED") # COMPLETED, FAILED
    is_demo = Column(Boolean, default=False)
    usage_amount = Column(Float, default=0.0)
    usage_unit = Column(String(50), default="TOKEN") # TOKEN, REQUEST, PERCENTAGE, CREDIT
    metering_status = Column(String(50), default="VERIFIED") # VERIFIED, USAGE_UNVERIFIED
    conversation_id = Column(Integer, nullable=True)
    provider_request_id = Column(String(100), nullable=True)
    prompt_snippet = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="requests")

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    listing_id = Column(Integer, ForeignKey("provider_listings.id", ondelete="SET NULL"), nullable=True)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id", ondelete="SET NULL"), nullable=True)
    model = Column(String(100), nullable=True)
    model_slug = Column(String(100), default="nova-reasoning")
    title = Column(String(255), default="New conversation")
    status = Column(String(50), default="ACTIVE") # ACTIVE, ARCHIVED, DELETED
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    # Conceptual Section 5 Compatibility Property: Consumer owns the conversation
    @property
    def consumer_id(self):
        return self.user_id

    @consumer_id.setter
    def consumer_id(self, val):
        self.user_id = val

    user = relationship("User", back_populates="conversations")
    listing = relationship("ProviderListing", foreign_keys=[listing_id])
    provider = relationship("ProviderAccount", foreign_keys=[provider_id])
    messages = relationship("ChatMessage", back_populates="conversation", order_by="ChatMessage.created_at", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), nullable=False) # user, assistant, system
    content = Column(Text, nullable=False)
    model = Column(String(100), nullable=True)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    provider_request_id = Column(String(100), nullable=True)
    cost = Column(Float, default=0.0)
    latency_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)

    conversation = relationship("Conversation", back_populates="messages")

class PaymentRecord(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String(100), unique=True, index=True, nullable=True)
    gateway_id = Column(String(100), unique=True, index=True, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="INR")
    payment_method = Column(String(50), default="UPI / Card")
    signature = Column(String(255), nullable=True)
    credits_credited = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    status = Column(String(50), default="PENDING") # PENDING, SUCCESS, COMPLETED, FAILED
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

class PayoutRequest(Base):
    __tablename__ = "payout_requests"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id"), nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String(50), default="COMPLETED") # PENDING, APPROVED, COMPLETED, REJECTED
    destination = Column(String(100), default="HDFC Bank ···· 4821")
    reference_id = Column(String(100), default="PO-84291-INR")
    created_at = Column(DateTime, default=utcnow)

    provider = relationship("ProviderAccount", back_populates="payouts")

class DisputeRecord(Base):
    __tablename__ = "disputes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    request_record_id = Column(String(100), nullable=True)
    reason = Column(String(255), nullable=False)
    amount = Column(Float, default=0.0)
    status = Column(String(50), default="OPEN") # OPEN, UNDER_REVIEW, RESOLVED, REJECTED
    resolution_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    type = Column(String(50), default="INFO") # INFO, SUCCESS, WARNING, ERROR
    is_read = Column(Boolean, default=False)
    link = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="notifications")

class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(String(50), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject = Column(String(255), nullable=False)
    category = Column(String(100), default="General")
    description = Column(Text, nullable=False)
    priority = Column(String(50), default="MEDIUM") # LOW, MEDIUM, HIGH
    status = Column(String(50), default="OPEN") # OPEN, IN_PROGRESS, RESOLVED, CLOSED
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="support_tickets")
    messages = relationship("TicketMessage", back_populates="ticket", order_by="TicketMessage.created_at")

class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("support_tickets.id"), nullable=False)
    sender_name = Column(String(100), nullable=False)
    sender_role = Column(String(50), nullable=False) # CONSUMER, PROVIDER, ADMIN, SUPPORT
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    ticket = relationship("SupportTicket", back_populates="messages")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    actor_email = Column(String(255), nullable=False)
    action = Column(String(100), nullable=False)
    resource_type = Column(String(100), nullable=False)
    resource_id = Column(String(100), nullable=True)
    details = Column(Text, nullable=True)
    ip_address = Column(String(50), default="127.0.0.1")
    created_at = Column(DateTime, default=utcnow)

class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, index=True, nullable=False)
    value = Column(String(255), nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String(100), unique=True, index=True, nullable=False)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id"), nullable=False)
    listing_id = Column(Integer, ForeignKey("provider_listings.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_per_unit = Column(Float, nullable=False)
    total_amount = Column(Float, nullable=False)
    currency = Column(String(10), default="INR")
    status = Column(String(50), default="PAYMENT_PENDING", index=True)
    # States: PAYMENT_PENDING, PAYMENT_SUBMITTED, SELLER_REVIEW, SELLER_CONFIRMED, ADMIN_REVIEW, APPROVED, REJECTED, CANCELLED, DISPUTED, EXPIRED
    seller_payment_method = Column(String(100), default="UPI")
    seller_payment_identifier = Column(String(255), default="seller@upi")
    seller_payment_instructions = Column(Text, nullable=True)
    seller_qr_url = Column(String(255), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    buyer = relationship("User", foreign_keys=[buyer_id])
    seller = relationship("User", foreign_keys=[seller_id])
    provider = relationship("ProviderAccount", foreign_keys=[provider_id])
    listing = relationship("ProviderListing", back_populates="orders")
    payment_proof = relationship("PaymentProof", back_populates="order", uselist=False, cascade="all, delete-orphan")
    disputes = relationship("MarketplaceDispute", back_populates="order", cascade="all, delete-orphan")
    credit_allocation = relationship("CreditAllocation", back_populates="order", uselist=False)


class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True, nullable=False)
    transaction_id = Column(String(100), nullable=False, index=True)
    screenshot_path = Column(String(255), nullable=False)
    screenshot_filename = Column(String(255), nullable=False)
    original_filename = Column(String(255), nullable=True)
    file_size = Column(Integer, default=0)
    mime_type = Column(String(100), default="image/png")
    amount_reported = Column(Float, nullable=False)
    note = Column(Text, nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)
    extracted_transaction_id = Column(String(100), nullable=True)
    ocr_status = Column(String(50), default="PENDING")
    verification_status = Column(String(50), default="PENDING")
    verification_reason = Column(Text, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, default=utcnow)

    order = relationship("Order", back_populates="payment_proof")


class MarketplaceDispute(Base):
    __tablename__ = "marketplace_disputes"

    id = Column(Integer, primary_key=True, index=True)
    dispute_id = Column(String(100), unique=True, index=True, nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    raised_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(String(255), nullable=False)
    details = Column(Text, nullable=True)
    status = Column(String(50), default="OPEN", index=True) # OPEN, ADMIN_REVIEW, RESOLVED_APPROVED, RESOLVED_REJECTED
    admin_notes = Column(Text, nullable=True)
    seller_response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    order = relationship("Order", back_populates="disputes")
    raised_by = relationship("User", foreign_keys=[raised_by_id])
    messages = relationship("DisputeMessage", back_populates="dispute", cascade="all, delete-orphan", order_by="DisputeMessage.created_at")


class DisputeMessage(Base):
    __tablename__ = "dispute_messages"

    id = Column(Integer, primary_key=True, index=True)
    dispute_id = Column(Integer, ForeignKey("marketplace_disputes.id"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_role = Column(String(50), nullable=False) # BUYER, SELLER, ADMIN
    message = Column(Text, nullable=False)
    evidence_path = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    dispute = relationship("MarketplaceDispute", back_populates="messages")
    sender = relationship("User", foreign_keys=[sender_id])


class CreditAllocation(Base):
    __tablename__ = "credit_allocations"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True, nullable=False)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider_connection_id = Column(Integer, ForeignKey("provider_connections.id", ondelete="SET NULL"), nullable=True)
    listing_id = Column(Integer, ForeignKey("provider_listings.id", ondelete="SET NULL"), nullable=True)
    model_slug = Column(String(100), nullable=True) # e.g. "nova-reasoning"
    model_name = Column(String(100), nullable=True)
    credits = Column(Integer, nullable=False)
    credits_used = Column(Integer, default=0)
    credits_remaining = Column(Integer, default=0)
    status = Column(String(50), default="ACTIVE") # ACTIVE, CONSUMED, REVOKED
    released_at = Column(DateTime, default=utcnow)

    order = relationship("Order", back_populates="credit_allocation")
    buyer = relationship("User", foreign_keys=[buyer_id])
    seller = relationship("User", foreign_keys=[seller_id])
    connection = relationship("ProviderConnection", foreign_keys=[provider_connection_id])
    listing = relationship("ProviderListing", foreign_keys=[listing_id])


class ModelCreditBalance(Base):
    __tablename__ = "model_credit_balances"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    model_slug = Column(String(100), nullable=False, index=True)
    model_name = Column(String(100), nullable=False)
    credits_purchased = Column(Integer, default=0)
    credits_used = Column(Integer, default=0)
    credits_remaining = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", foreign_keys=[user_id])



class ProviderEarningRecord(Base):
    __tablename__ = "provider_earning_records"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id"), nullable=False)
    gross_amount = Column(Float, nullable=False)
    platform_fee = Column(Float, nullable=False)
    net_earning = Column(Float, nullable=False)
    status = Column(String(50), default="CREDITED")
    created_at = Column(DateTime, default=utcnow)


class SellerStatusHistory(Base):
    __tablename__ = "seller_status_history"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("provider_accounts.id"), nullable=False)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    previous_status = Column(String(50), nullable=False)
    new_status = Column(String(50), nullable=False) # ACTIVE, SUSPENDED, BLOCKED
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    provider = relationship("ProviderAccount", foreign_keys=[provider_id])
    admin = relationship("User", foreign_keys=[admin_id])



