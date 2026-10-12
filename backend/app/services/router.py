import uuid
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.core.encryption import decrypt_credential
from backend.app.models.schema_models import (
    User, Wallet, WalletTransaction, ProviderAccount, ProviderConnection,
    ProviderListing, CreditAllocation, ModelCreditBalance,
    AIRequestRecord, PlatformSetting, AuditLog
)
from backend.app.providers.base import AIProviderAdapter, AIRequestPayload, AIResponseResult
from backend.app.providers.mock_provider import MockAIProvider
from backend.app.providers.openai_provider import OpenAIProviderAdapter
from backend.app.providers.gemini_provider import GeminiProviderAdapter
from backend.app.providers.anthropic_provider import AnthropicProviderAdapter
from backend.app.providers.account_adapters import (
    AIAccountExecutionAdapter,
    GeminiAccountAdapter,
    OpenAIAccountAdapter,
    ClaudeAccountAdapter,
    MockAccountAdapter
)
from backend.app.providers.delegated_plan_adapters import (
    DelegatedPlanAdapter,
    OpenAIDelegatedPlanAdapter,
    GoogleDelegatedPlanAdapter,
    AnthropicDelegatedPlanAdapter
)

# TokenPool-owned default fallback adapters (Used ONLY for TokenPool-owned capacity, demo mode, or internal testing)
default_mock_provider = MockAIProvider(provider_name="Orbit AI (DEMO)")
mock_provider = default_mock_provider
openai_provider = OpenAIProviderAdapter(api_key=settings.OPENAI_API_KEY if settings.OPENAI_API_KEY else None)
gemini_provider = GeminiProviderAdapter(api_key=settings.GEMINI_API_KEY if settings.GEMINI_API_KEY else None)
anthropic_provider = AnthropicProviderAdapter(api_key=settings.ANTHROPIC_API_KEY if settings.ANTHROPIC_API_KEY else None)


class AIRouterService:
    @classmethod
    def create_provider_adapter(
        cls,
        provider_type: str,
        api_key: Optional[str] = None
    ) -> AIProviderAdapter:
        """
        Dynamically constructs an isolated AI provider adapter using a specific provider's credential.
        No global singleton keys are shared across providers.
        """
        ptype = (provider_type or "openai").lower()
        if ptype in ("mock", "demo"):
            return MockAIProvider(provider_name="Orbit AI (DEMO)")
        elif ptype in ("gemini", "google"):
            return GeminiProviderAdapter(api_key=api_key)
        elif ptype in ("anthropic", "claude"):
            return AnthropicProviderAdapter(api_key=api_key)
        elif ptype in ("openai", "gpt"):
            return OpenAIProviderAdapter(api_key=api_key)
        else:
            return OpenAIProviderAdapter(api_key=api_key)

    @classmethod
    def get_connection_adapter(cls, connection: ProviderConnection) -> AIProviderAdapter:
        """
        Builds a dedicated AIProviderAdapter for an authorized ProviderConnection.
        Enforces:
        - capacity_source: PROVIDER_API_CAPACITY, OFFICIAL_DELEGATED_PLAN, DEMO_CAPACITY, UNSUPPORTED_CONSUMER_SUBSCRIPTION
        - marketplace eligibility and officially supported execution mechanisms.
        """
        if not connection:
            raise ValueError("No provider connection provided.")

        ptype = (connection.provider_type or "openai").lower()

        # Check marketplace eligibility status
        if getattr(connection, "marketplace_status", None) and connection.marketplace_status != "ELIGIBLE":
            reason = getattr(connection, "marketplace_reason", None) or "Account is not eligible for marketplace execution."
            raise ValueError(f"EXECUTION_NOT_SUPPORTED: {reason}")

        if connection.status in ("REVOKED", "DISABLED", "DISCONNECTED"):
            raise ValueError(
                f"EXECUTION_NOT_SUPPORTED: Provider connection #{connection.id} is {connection.status.lower()} and cannot be used for execution."
            )

        source = (connection.capacity_source or "PROVIDER_API_CAPACITY").upper().strip()

        # 1. Official Delegated Plan Execution
        if source == "OFFICIAL_DELEGATED_PLAN":
            if ptype in ("openai", "chatgpt"):
                plan_adapter = OpenAIDelegatedPlanAdapter()
                cap_status, reason = plan_adapter.get_execution_capability(connection)
                if cap_status != "EXECUTION_SUPPORTED":
                    raise ValueError(f"EXECUTION_NOT_SUPPORTED: {reason}")

                class DelegatedPlanProviderWrapper(AIProviderAdapter):
                    def __init__(self, plan_ad: OpenAIDelegatedPlanAdapter, conn: ProviderConnection):
                        self.plan_ad = plan_ad
                        self.conn = conn

                    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
                        return await self.plan_ad.execute(payload, self.conn)

                    async def stream_request(self, payload: AIRequestPayload):
                        res = await self.send_request(payload)
                        yield res.content

                    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
                        return True

                    async def health_check(self) -> Dict[str, Any]:
                        return await self.plan_ad.health_check(self.conn)

                    def get_supported_models(self) -> List[str]:
                        return ["gpt-4o", "gpt-4o-mini", "o1-preview", "nova-reasoning"]

                return DelegatedPlanProviderWrapper(plan_adapter, connection)

            elif ptype in ("gemini", "google"):
                raise ValueError(
                    "EXECUTION_NOT_SUPPORTED: Google consumer AI subscription cannot currently be used as TokenPool marketplace API execution capacity through a supported delegated execution mechanism."
                )
            elif ptype in ("anthropic", "claude"):
                raise ValueError(
                    "EXECUTION_NOT_SUPPORTED: Claude consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing."
                )
            else:
                raise ValueError(f"EXECUTION_NOT_SUPPORTED: Delegated plan is not supported for {ptype}.")

        # 2. Demo Capacity Source
        elif source == "DEMO_CAPACITY" or ptype in ("mock", "demo"):
            return MockAccountAdapter(connection)

        # 3. Unsupported Consumer Subscription
        elif source == "UNSUPPORTED_CONSUMER_SUBSCRIPTION":
            reason = connection.failure_reason or "Consumer AI subscription cannot be used for marketplace execution."
            raise ValueError(f"EXECUTION_NOT_SUPPORTED: {reason}")

        # 4. Provider API Capacity (Default)
        elif source in ("PROVIDER_API_CAPACITY", "PROVIDER API CAPACITY"):
            if ptype in ("gemini", "google"):
                return GeminiAccountAdapter(connection)
            elif ptype in ("anthropic", "claude"):
                return ClaudeAccountAdapter(connection)
            elif ptype in ("openai", "gpt"):
                return OpenAIAccountAdapter(connection)
            elif ptype in ("mock", "demo"):
                return MockAccountAdapter(connection)
            else:
                return MockAccountAdapter(connection)

        else:
            raise ValueError(f"EXECUTION_NOT_SUPPORTED: Unknown capacity source {connection.capacity_source}.")

    @classmethod
    def get_adapter(cls, model_slug: str, is_demo: bool = True):
        slug = (model_slug or "").lower()
        if is_demo:
            return default_mock_provider

        if "gemini" in slug or slug in gemini_provider.get_supported_models():
            if not settings.GEMINI_API_KEY:
                raise ValueError(
                    "Google Gemini API key not configured. Real AI provider requests require authorized credentials. "
                    "Configure GEMINI_API_KEY in .env or use MockAIProvider for testing."
                )
            return gemini_provider

        if "claude" in slug or slug in anthropic_provider.get_supported_models():
            if not settings.ANTHROPIC_API_KEY:
                raise ValueError(
                    "Anthropic API key not configured. Real AI provider requests require authorized credentials. "
                    "Configure ANTHROPIC_API_KEY in .env or use MockAIProvider for testing."
                )
            return anthropic_provider

        if slug in openai_provider.get_supported_models() or slug.startswith("gpt-"):
            if not settings.OPENAI_API_KEY:
                raise ValueError(
                    "OpenAI API key not configured. Real AI provider requests require authorized credentials. "
                    "Configure OPENAI_API_KEY in .env or use MockAIProvider for testing."
                )
            return openai_provider

        return default_mock_provider

    @classmethod
    def get_provider_status(cls, adapter_type: str) -> str:
        """
        Verification status helper for legacy views.
        """
        adapter_type = (adapter_type or "").lower()
        if adapter_type == "mock":
            return "VERIFIED" if settings.DEMO_MODE_DEFAULT else "INACTIVE"
        elif adapter_type in ("gemini", "google"):
            return "VERIFIED" if bool(settings.GEMINI_API_KEY and len(settings.GEMINI_API_KEY) > 10) else "AUTHORIZATION_REQUIRED"
        elif adapter_type == "openai":
            return "VERIFIED" if bool(settings.OPENAI_API_KEY and len(settings.OPENAI_API_KEY) > 10) else "AUTHORIZATION_REQUIRED"
        elif adapter_type in ("anthropic", "claude"):
            return "VERIFIED" if bool(settings.ANTHROPIC_API_KEY and len(settings.ANTHROPIC_API_KEY) > 10) else "AUTHORIZATION_REQUIRED"
        return "UNVERIFIED"

    @classmethod
    def resolve_execution_adapter(
        cls,
        db: Session,
        model_slug: str,
        is_demo: bool = True,
        listing_id: Optional[int] = None,
        user_id: Optional[int] = None
    ) -> Tuple[AIProviderAdapter, Optional[ProviderListing], Optional[ProviderConnection]]:
        """
        Resolves the exact provider adapter, listing, and connection for an execution request.
        Priority:
        1. If is_demo is True -> MockAIProvider (DEMO MODE)
        2. If active CreditAllocation exists for user and model -> Use linked ProviderConnection
        3. If active ProviderListing exists for model -> Use linked ProviderConnection
        4. Fallback to TokenPool-owned key ONLY if it is an explicitly configured TokenPool model / test
        """
        if is_demo:
            if not settings.DEMO_MODE_DEFAULT:
                raise ValueError("EXECUTION_NOT_SUPPORTED: MockAIProvider is strictly disabled in production (DEMO_MODE=false).")
            return default_mock_provider, None, None

        slug = (model_slug or "").lower()
        listing: Optional[ProviderListing] = None
        connection: Optional[ProviderConnection] = None

        # 1. Check if user purchased through a specific listing or allocation
        if user_id:
            alloc = db.query(CreditAllocation).filter(
                CreditAllocation.buyer_id == user_id,
                CreditAllocation.model_slug == slug,
                CreditAllocation.status == "ACTIVE",
                CreditAllocation.credits_remaining > 0
            ).order_by(CreditAllocation.released_at.asc()).first()
            if alloc and alloc.provider_connection_id:
                connection = db.query(ProviderConnection).filter(
                    ProviderConnection.id == alloc.provider_connection_id
                ).first()
                if alloc.listing_id:
                    listing = db.query(ProviderListing).filter(
                        ProviderListing.id == alloc.listing_id
                    ).first()

        # 2. If not found via allocation, check active listing
        if not connection:
            if listing_id:
                listing = db.query(ProviderListing).filter(
                    ProviderListing.id == listing_id,
                    ProviderListing.status == "ACTIVE"
                ).first()
            else:
                listing = db.query(ProviderListing).filter(
                    ProviderListing.model_slug == slug,
                    ProviderListing.status == "ACTIVE"
                ).first()

            if listing and listing.provider_connection_id:
                connection = db.query(ProviderConnection).filter(
                    ProviderConnection.id == listing.provider_connection_id
                ).first()

        # 3. If ProviderConnection is resolved, create adapter with provider's credential
        if connection:
            adapter = cls.get_connection_adapter(connection)
            return adapter, listing, connection

        # 4. If this is a real request but belongs to a provider listing that lacks a validated connection:
        if listing and listing.provider_id:
            raise ValueError(
                f"The marketplace listing for {listing.model_name} is not linked to an active, validated provider capacity source. "
                "The provider must connect and verify their API credential before this listing can serve AI requests."
            )

        # 5. TokenPool-owned fallback for internal testing / explicitly configured TokenPool models
        if "gemini" in slug:
            if not settings.GEMINI_API_KEY:
                raise ValueError(
                    "Google Gemini API key not configured for TokenPool capacity. "
                    "Provider listings require an authorized provider connection."
                )
            return GeminiProviderAdapter(api_key=settings.GEMINI_API_KEY), listing, None
        elif "claude" in slug:
            if not settings.ANTHROPIC_API_KEY:
                raise ValueError(
                    "Anthropic API key not configured for TokenPool capacity. "
                    "Provider listings require an authorized provider connection."
                )
            return AnthropicProviderAdapter(api_key=settings.ANTHROPIC_API_KEY), listing, None
        elif slug.startswith("gpt-") or "openai" in slug:
            if not settings.OPENAI_API_KEY:
                raise ValueError(
                    "OpenAI API key not configured for TokenPool capacity. "
                    "Provider listings require an authorized provider connection."
                )
            return OpenAIProviderAdapter(api_key=settings.OPENAI_API_KEY), listing, None

        # Default fallback to demo provider if model is mock
        return default_mock_provider, listing, None

    @staticmethod
    def get_commission_rate(db: Session) -> float:
        setting = db.query(PlatformSetting).filter(PlatformSetting.key == "commission_rate").first()
        if setting:
            try:
                return float(setting.value)
            except ValueError:
                pass
        return 0.20  # 20% platform commission default

    @classmethod
    async def execute_request(
        cls,
        db: Session,
        user: User,
        model_slug: str,
        prompt: str,
        is_demo: bool = True,
        listing_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        req_id = f"req_{uuid.uuid4().hex[:12]}"
        
        # 1. Lock user wallet
        wallet = db.query(Wallet).filter(Wallet.user_id == user.id).with_for_update().first()
        if not wallet:
            wallet = Wallet(user_id=user.id, balance=0.0, credits=0, currency="INR")
            db.add(wallet)
            db.flush()

        # Check balance/credits
        if wallet.balance <= 0.0 and wallet.credits <= 0:
            raise ValueError("Insufficient balance or credits. Please top up your wallet or purchase marketplace capacity.")

        # 2. Resolve adapter, listing, connection
        adapter, listing, connection = cls.resolve_execution_adapter(
            db=db,
            model_slug=model_slug,
            is_demo=is_demo,
            listing_id=listing_id,
            user_id=user.id
        )

        model_name = listing.model_name if listing else model_slug.replace("-", " ").title()
        is_demo_execution = is_demo or (connection is None and not settings.OPENAI_API_KEY and not settings.GEMINI_API_KEY)

        # 3. Send request to AI provider
        payload = AIRequestPayload(
            model_slug=model_slug,
            prompt=prompt,
            is_demo=is_demo_execution
        )

        try:
            result: AIResponseResult = await adapter.send_request(payload)
        except Exception as e:
            # CRITICAL FAILURE BEHAVIOR:
            # If provider's API returns insufficient quota, rate limit, invalid credential, timeout, 4xx, 5xx:
            # Zero deduction for consumer, zero capacity consumed, zero provider earnings, zero commission!
            db.rollback()
            try:
                db.add(AuditLog(
                    user_id=user.id,
                    actor_email=user.email,
                    action="AI_PROVIDER_REQUEST_FAILED",
                    resource_type="AI_EXECUTION",
                    resource_id=model_slug,
                    details=f"Provider execution failed: {str(e)}. 0 credits charged. 0 capacity consumed."
                ))
                db.commit()
            except Exception:
                pass
            raise ValueError(
                f"AI Provider execution failed: {str(e)}. "
                "No credits or wallet balance have been deducted."
            )

        # 4. Usage metering & financial settlement (ONLY after successful execution)
        total_cost = result.cost
        commission_rate = cls.get_commission_rate(db)
        platform_fee = round(total_cost * commission_rate, 4)
        provider_earning = round(total_cost - platform_fee, 4)

        # Deduct wallet credits / balance
        credits_needed = max(1, int(total_cost * 10))
        if wallet.credits >= credits_needed:
            wallet.credits -= credits_needed
            credits_delta = -credits_needed
        elif wallet.balance >= total_cost:
            wallet.balance = max(0.0, round(wallet.balance - total_cost, 4))
            wallet.credits = max(0, wallet.credits - credits_needed)
            credits_delta = -credits_needed
        else:
            wallet.credits = 0
            wallet.balance = max(0.0, round(wallet.balance - total_cost, 4))
            credits_delta = -credits_needed

        mode_label = "DEMO" if is_demo_execution else "LIVE"

        # Add transaction record
        tx = WalletTransaction(
            wallet_id=wallet.id,
            tx_type="USAGE",
            amount=-total_cost,
            balance_after=wallet.balance,
            credits_delta=credits_delta,
            description=f"{model_name} usage ({mode_label})",
            status="COMPLETED",
            meta_info=f"Request ID: {req_id}"
        )
        db.add(tx)

        # Consume capacity from ProviderListing & ProviderConnection
        consumed_tokens = result.total_tokens
        provider_id = listing.provider_id if listing else (connection.provider_id if connection else None)

        if listing:
            listing.consumed_capacity = (listing.consumed_capacity or 0) + consumed_tokens
            listing.remaining_capacity = max(0, (listing.remaining_capacity or 0) - consumed_tokens)

        if connection:
            connection.capacity_consumed = (connection.capacity_consumed or 0) + consumed_tokens
            # Formula: remaining = declared - reserved - sold - consumed
            connection.capacity_remaining = max(
                0,
                (connection.capacity_declared or 0)
                - (connection.capacity_reserved or 0)
                - (connection.capacity_sold or 0)
                - (connection.capacity_consumed or 0)
            )
            # Update dedicated delegated plan capacity accounting if applicable
            if connection.capacity_source == "OFFICIAL_DELEGATED_PLAN":
                connection.consumed_units = (connection.consumed_units or 0) + consumed_tokens
                connection.remaining_units = max(
                    0,
                    (connection.committed_units or 0)
                    - (connection.reserved_units or 0)
                    - (connection.consumed_units or 0)
                )

        # Credit provider account if applicable
        if provider_id:
            prov = db.query(ProviderAccount).filter(ProviderAccount.id == provider_id).first()
            if prov:
                prov.total_earnings = round(prov.total_earnings + provider_earning, 4)
                prov.withdrawable_balance = round(prov.withdrawable_balance + provider_earning, 4)
                prov.used_capacity += int(result.total_tokens)

        # Record AI request audit record
        req_record = AIRequestRecord(
            request_id=req_id,
            user_id=user.id,
            provider_id=provider_id,
            provider_connection_id=connection.id if connection else None,
            conversation_id=conversation_id,
            provider_request_id=getattr(result, "provider_request_id", None) or f"upstream_{uuid.uuid4().hex[:10]}",
            usage_amount=float(result.total_tokens),
            usage_unit=getattr(connection, "upstream_usage_unit", "TOKEN") or "TOKEN",
            metering_status="VERIFIED" if not is_demo_execution else "DEMO",
            model_slug=model_slug,
            model_name=model_name,
            badge=listing.badge if listing else "N",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            consumer_cost=total_cost,
            provider_earning=provider_earning,
            platform_fee=platform_fee,
            latency_ms=result.latency_ms,
            status="COMPLETED",
            is_demo=is_demo_execution,
            prompt_snippet=prompt[:120]
        )
        db.add(req_record)

        # Audit events for lifecycle tracking
        db.add(AuditLog(
            user_id=user.id,
            actor_email=user.email,
            action="CONSUMER_EXECUTION",
            resource_type="AI_EXECUTION",
            resource_id=req_id,
            details=f"Consumer executed {model_slug} ({result.total_tokens} tokens). Cost: ₹{total_cost}."
        ))
        db.add(AuditLog(
            user_id=user.id,
            actor_email=user.email,
            action="USAGE_RECORDED",
            resource_type="USAGE",
            resource_id=req_id,
            details=f"Metered usage: {result.total_tokens} units for {model_slug}."
        ))
        if provider_id and provider_earning > 0:
            db.add(AuditLog(
                user_id=provider_id,
                actor_email=user.email,
                action="PROVIDER_EARNINGS_RECORDED",
                resource_type="EARNINGS",
                resource_id=req_id,
                details=f"Credited provider #{provider_id} with ₹{provider_earning}."
            ))

        db.commit()

        provider_display = result.provider_name
        if is_demo_execution and "DEMO" not in provider_display:
            provider_display += " (DEMO)"

        return {
            "request_id": req_id,
            "content": result.content,
            "response": result.content,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "cost": total_cost,
            "latency_ms": result.latency_ms,
            "model_slug": model_slug,
            "model_name": model_name,
            "provider_name": provider_display,
            "is_demo": is_demo_execution,
            "execution_mode": mode_label,
            "connection_id": connection.id if connection else None,
        }

    @classmethod
    def route(
        cls,
        model_slug: str,
        prompt: str,
        is_demo: bool = True,
        connection: Optional[ProviderConnection] = None,
        messages: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Synchronous convenience wrapper for routing an AI request."""
        import asyncio
        import concurrent.futures

        async def _do_route():
            if is_demo:
                if not settings.DEMO_MODE_DEFAULT:
                    raise ValueError(
                        "EXECUTION_NOT_SUPPORTED: MockAIProvider is strictly disabled in production (DEMO_MODE=false). "
                        "An authorized real provider connection is required."
                    )
                adapter = default_mock_provider
            else:
                if not connection:
                    raise ValueError(
                        "EXECUTION_NOT_SUPPORTED: Marketplace execution requires an authorized, eligible provider connection. "
                        "No provider connection was resolved for this request."
                    )
                adapter = cls.get_connection_adapter(connection)

            payload = AIRequestPayload(
                model_slug=model_slug,
                prompt=prompt,
                messages=messages,
                is_demo=is_demo
            )
            result = await adapter.send_request(payload)
            pname = result.provider_name + (" (DEMO)" if is_demo and "DEMO" not in result.provider_name else "")
            return {
                "response": result.content,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.total_tokens,
                "cost": result.cost,
                "latency_ms": result.latency_ms,
                "model_slug": model_slug,
                "provider_name": pname,
                "is_demo": is_demo
            }

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _do_route()).result()
        return asyncio.run(_do_route())
