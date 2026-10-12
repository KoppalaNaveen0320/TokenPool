"""
TokenPool — Central Marketplace Eligibility Service
Encapsulates strict business logic to separate:
1. Identity Match (TokenPool Gmail vs AI account email)
2. Authorization Status (Can TokenPool perform permitted AI operations?)
3. Execution Capability (Can TokenPool actually send an AI request through the authorized provider mechanism?)
4. Marketplace Eligibility (Can this AI account be listed and used for consumer requests?)
5. Capacity Commitment (How much marketplace capacity has the provider committed?)

Rules:
- Email identity match alone is NOT proof of execution authorization.
- If upstream provider does not expose a supported 3rd-party execution delegation mechanism,
  marketplace_status must be NOT_ELIGIBLE with clear, truthful explanation.
- OpenAI Delegated Plan requires OPENAI_DELEGATED_PLAN_ENABLED=true AND OPENAI_PARTICIPATING_APP_STATUS='APPROVED'.
- Google AI Pro and Claude Pro remain NOT_ELIGIBLE for consumer subscription execution.
- Never fabricate AI Pro plans or verified token quotas.
"""
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.models.schema_models import ProviderConnection, User, ProviderListing
from backend.app.providers.delegated_plan_adapters import (
    OpenAIDelegatedPlanAdapter,
    GoogleDelegatedPlanAdapter,
    AnthropicDelegatedPlanAdapter
)


class MarketplaceEligibilityService:

    @classmethod
    def evaluate_execution_capability(cls, connection: ProviderConnection) -> Tuple[str, Optional[str]]:
        """
        Evaluates execution capability independently of identity or declared quota.
        Returns (execution_status, failure_reason).
        execution_status: EXECUTION_SUPPORTED, EXECUTION_NOT_SUPPORTED
        """
        ptype = (connection.provider_type or "").lower()
        method = (connection.connection_method or "google_identity").lower()
        source = (connection.capacity_source or "").upper().strip()
        has_credential = bool(connection.encrypted_credential or connection.encrypted_authorization_data)
        is_validated = getattr(connection, "validation_status", "") == "VALIDATED"

        # 1. Official Delegated Plan Execution Source
        if source == "OFFICIAL_DELEGATED_PLAN":
            if ptype in ("openai", "chatgpt"):
                adapter = OpenAIDelegatedPlanAdapter()
                return adapter.get_execution_capability(connection)
            elif ptype in ("gemini", "google"):
                adapter = GoogleDelegatedPlanAdapter()
                return adapter.get_execution_capability(connection)
            elif ptype in ("anthropic", "claude"):
                adapter = AnthropicDelegatedPlanAdapter()
                return adapter.get_execution_capability(connection)
            return "EXECUTION_NOT_SUPPORTED", f"Delegated plan execution is not supported for {ptype}."

        # 2. Unsupported Consumer Subscription
        if source == "UNSUPPORTED_CONSUMER_SUBSCRIPTION":
            if ptype in ("gemini", "google"):
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "Google does not provide an officially supported execution mechanism. Google consumer AI subscription cannot currently be used as TokenPool marketplace API execution capacity through a supported delegated execution mechanism."
                )
            elif ptype in ("openai", "chatgpt"):
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "ChatGPT consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing. Connect an authorized API integration or eligible enterprise account."
                )
            elif ptype in ("anthropic", "claude"):
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "Claude consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing. Connect an authorized API integration or eligible enterprise account."
                )
            return "EXECUTION_NOT_SUPPORTED", f"Consumer subscription is not supported for {ptype}."

        # 3. Demo / Mock Capacity Source
        if source == "DEMO_CAPACITY" or ptype in ("mock", "demo"):
            return "EXECUTION_SUPPORTED", None

        # 4. Provider API Capacity (Developer API keys or Cloud service credentials)
        if ptype in ("gemini", "google"):
            if has_credential and is_validated:
                return "EXECUTION_SUPPORTED", None
            return (
                "EXECUTION_NOT_SUPPORTED",
                "Google does not provide an officially supported execution mechanism. Google consumer AI subscription cannot currently be used as TokenPool marketplace API execution capacity through a supported delegated execution mechanism."
            )

        elif ptype in ("openai", "chatgpt"):
            if method in ("google_identity", "official_oauth", "delegated_access") and not has_credential:
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "ChatGPT consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing. Connect an authorized API integration or eligible enterprise account."
                )
            if has_credential and is_validated:
                return "EXECUTION_SUPPORTED", None
            return (
                "EXECUTION_NOT_SUPPORTED",
                "OpenAI connection requires an authorized API credential to support marketplace execution."
            )

        elif ptype in ("anthropic", "claude"):
            if method in ("google_identity", "official_oauth", "delegated_access") and not has_credential:
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "Claude consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing. Connect an authorized API integration or eligible enterprise account."
                )
            if has_credential and is_validated:
                return "EXECUTION_SUPPORTED", None
            return (
                "EXECUTION_NOT_SUPPORTED",
                "Anthropic connection requires an authorized API credential to support marketplace execution."
            )

        return "EXECUTION_NOT_SUPPORTED", f"Provider type {ptype} does not have a supported execution mechanism."

    @classmethod
    def evaluate(
        cls,
        connection: ProviderConnection,
        user: User,
    ) -> Tuple[str, str]:
        """
        Evaluates connection and returns (marketplace_status, marketplace_reason).
        Statuses: ELIGIBLE, NOT_ELIGIBLE, PENDING_REVIEW, PAUSED.
        """
        now = datetime.now(timezone.utc)

        # 1. Connection lifecycle checks
        conn_status = (connection.connection_status or connection.status or "CONNECTED").upper()
        if conn_status == "DISCONNECTED":
            return "PAUSED", "AI account has been disconnected by the provider."
        if conn_status == "REVOKED":
            return "NOT_ELIGIBLE", "AI account connection has been revoked."
        if conn_status == "EXPIRED":
            return "NOT_ELIGIBLE", "Provider authorization has expired. Re-authorization required."
        if conn_status not in ("CONNECTED", "ACTIVE"):
            return "NOT_ELIGIBLE", f"Connection is in inactive state: {conn_status}."

        # 2. Token expiration check
        if connection.token_expires_at:
            exp = connection.token_expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                return "NOT_ELIGIBLE", "Provider authorization has expired. Please re-authorize your AI account."

        # 3. Identity verification check (Gmail identity matching)
        acct_email = (connection.account_email or "").strip().lower()
        user_email = (user.email or "").strip().lower()

        if not acct_email and user_email:
            acct_email = user_email
            connection.account_email = user_email

        if not acct_email:
            return "NOT_ELIGIBLE", "Identity verification pending: No account email associated."
        
        if acct_email != user_email:
            return "NOT_ELIGIBLE", (
                f"Account identity mismatch: Connected AI account ({acct_email}) does not match "
                f"your verified TokenPool provider Gmail identity ({user_email})."
            )

        # 4. Upstream execution capability check
        exec_status, fail_reason = cls.evaluate_execution_capability(connection)
        if exec_status != "EXECUTION_SUPPORTED":
            return "NOT_ELIGIBLE", (fail_reason or "Execution capability is not supported for this provider source.")

        # 5. Authorization check
        auth_status = (connection.authorization_status or "NOT_AUTHORIZED").upper()
        if auth_status in ("NOT_AUTHORIZED", "UNAUTHORIZED", "ACTION_REQUIRED", "PENDING_AUTHORIZATION"):
            return "NOT_ELIGIBLE", "AI account identity matched, but external execution is not authorized or not supported for this consumer subscription."
        if auth_status == "PENDING":
            return "PENDING_REVIEW", "Provider authorization is pending verification."
        if auth_status in ("REVOKED", "DISABLED"):
            return "NOT_ELIGIBLE", "Provider authorization has been revoked."
        if auth_status != "AUTHORIZED":
            return "NOT_ELIGIBLE", f"AI account authorization is in status: {auth_status}."

        # 6. Capacity commitment check
        source = (connection.capacity_source or "").upper().strip()
        if source == "OFFICIAL_DELEGATED_PLAN":
            committed = connection.committed_units or connection.capacity_declared or 0
            remaining = connection.remaining_units if connection.remaining_units is not None else (connection.capacity_remaining or declared)
            if committed <= 0 or remaining <= 0:
                return "NOT_ELIGIBLE", "No marketplace capacity commitment declared (0 remaining units). Please set commitment."
        else:
            declared = connection.capacity_declared or 0
            remaining = connection.capacity_remaining if connection.capacity_remaining is not None else declared
            if declared <= 0 or remaining <= 0:
                return "NOT_ELIGIBLE", "No marketplace capacity commitment declared (0 remaining tokens). Please declare capacity."

        return "ELIGIBLE", "AI account is authorized and eligible for marketplace execution."

    @classmethod
    def evaluate_and_update(
        cls,
        connection: ProviderConnection,
        user: User,
        db: Session,
    ) -> Dict[str, Any]:
        """
        Runs evaluation, updates the connection record and linked listings in DB.
        """
        # Ensure identity status is updated
        acct_email = (connection.account_email or "").strip().lower()
        user_email = (user.email or "").strip().lower()

        if acct_email == user_email:
            connection.identity_status = "IDENTITY_MATCHED"
        elif acct_email:
            connection.identity_status = "IDENTITY_MISMATCH"
        else:
            connection.identity_status = "PENDING_VERIFICATION"

        # Update execution capability status
        exec_status, fail_reason = cls.evaluate_execution_capability(connection)
        connection.execution_status = exec_status
        connection.failure_reason = fail_reason
        if connection.capacity_source == "OFFICIAL_DELEGATED_PLAN":
            connection.delegated_execution_status = exec_status

        status, reason = cls.evaluate(connection, user)
        connection.marketplace_status = status
        connection.marketplace_reason = reason

        # If connection becomes not eligible or paused, pause any active listings
        if status != "ELIGIBLE":
            listings = db.query(ProviderListing).filter(
                ProviderListing.provider_connection_id == connection.id,
                ProviderListing.status == "ACTIVE"
            ).all()
            for l in listings:
                l.status = "PAUSED"

        return {
            "identity_status": connection.identity_status,
            "authorization_status": connection.authorization_status,
            "execution_status": connection.execution_status,
            "failure_reason": connection.failure_reason,
            "connection_status": connection.connection_status,
            "marketplace_status": connection.marketplace_status,
            "marketplace_reason": connection.marketplace_reason,
            "capacity_declared": connection.capacity_declared,
            "capacity_remaining": connection.capacity_remaining,
            "capacity_source": connection.capacity_source,
            "committed_units": connection.committed_units,
            "remaining_units": connection.remaining_units,
        }
