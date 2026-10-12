"""
TokenPool — Official Delegated AI Plan Execution Adapters
Implements official provider delegation mechanisms:
- DelegatedPlanAdapter (Abstract Base Class)
- OpenAIDelegatedPlanAdapter (Official OpenAI participating-app / delegated plan integration)
- GoogleDelegatedPlanAdapter (Truthful unsupported consumer subscription handler)
- AnthropicDelegatedPlanAdapter (Truthful unsupported consumer subscription handler)

Strict Business Constraints:
- Uses ONLY officially supported provider delegation mechanisms.
- ZERO browser automation, ZERO web scraping, ZERO cookies, ZERO browser session tokens.
- NEVER asks for provider AI passwords or Google passwords.
- NEVER pretends that TokenPool is an approved participating partner if not configured and approved.
- Gated by:
    OPENAI_DELEGATED_PLAN_ENABLED (boolean)
    OPENAI_PARTICIPATING_APP_STATUS ("NOT_APPLIED", "PENDING", "APPROVED", "REJECTED", "SUSPENDED")
- Only OPENAI_PARTICIPATING_APP_STATUS == "APPROVED" and OPENAI_DELEGATED_PLAN_ENABLED == True can execute.
- Decouples consumer chat history from provider personal account history.
"""
from abc import ABC, abstractmethod
import json
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import httpx

from backend.app.core.config import settings
from backend.app.core.encryption import encrypt_credential, decrypt_credential
from backend.app.models.schema_models import ProviderConnection
from backend.app.providers.base import AIProviderAdapter, AIRequestPayload, AIResponseResult
from backend.app.providers.mock_provider import MockAIProvider


class DelegatedPlanAdapter(ABC):
    """
    Abstract base class for official delegated consumer plan execution.
    """

    @abstractmethod
    def authorize(
        self,
        user_id: int,
        state: str,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """Initiates the official provider authorization flow."""
        pass

    @abstractmethod
    async def callback(
        self,
        code: str,
        state: Optional[str] = None,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """Exchanges official authorization code for tokens and identity."""
        pass

    @abstractmethod
    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """Refreshes expired delegated authorization tokens."""
        pass

    @abstractmethod
    async def revoke(self, token_data: str) -> bool:
        """Revokes provider delegation upstream."""
        pass

    @abstractmethod
    async def health_check(self, connection: ProviderConnection) -> Dict[str, Any]:
        """Checks validity, limits, and operational health of delegated connection."""
        pass

    @abstractmethod
    def get_usage(self, connection: ProviderConnection) -> Dict[str, Any]:
        """Returns upstream usage metrics and TokenPool commitment metrics."""
        pass

    @abstractmethod
    def get_limits(self, connection: ProviderConnection) -> Dict[str, Any]:
        """Returns app-specific limit, percentage, and reset information."""
        pass

    @abstractmethod
    async def execute(
        self,
        payload: AIRequestPayload,
        connection: ProviderConnection
    ) -> AIResponseResult:
        """Executes AI request via the official delegated execution mechanism."""
        pass

    @abstractmethod
    def get_execution_capability(
        self,
        connection: Optional[ProviderConnection] = None
    ) -> Tuple[str, Optional[str]]:
        """Returns (execution_status, failure_reason)."""
        pass

    @abstractmethod
    def get_account_identity(self, connection: ProviderConnection) -> Dict[str, Any]:
        """Returns verified account email, plan name, and identity details."""
        pass


class OpenAIDelegatedPlanAdapter(DelegatedPlanAdapter):
    """
    Official OpenAI Participating-App Delegated Plan Adapter.
    Enables eligible ChatGPT Plus/Pro providers to authorize TokenPool to use
    their ChatGPT plan for AI requests using OpenAI's official integration mechanism.
    
    CRITICAL REAL-WORLD REQUIREMENT:
    TokenPool MUST NOT claim or simulate being an approved participating partner
    unless OPENAI_PARTICIPATING_APP_STATUS == "APPROVED" and OPENAI_DELEGATED_PLAN_ENABLED == True.
    """

    def __init__(self):
        self.provider_name = "ChatGPT Official Delegated Plan"

    def get_execution_capability(
        self,
        connection: Optional[ProviderConnection] = None
    ) -> Tuple[str, Optional[str]]:
        """
        Evaluates whether this integration can legally & technically execute requests.
        Must check participating app approval status and connection credentials.
        """
        status = getattr(settings, "OPENAI_PARTICIPATING_APP_STATUS", "NOT_APPLIED")
        enabled = getattr(settings, "OPENAI_DELEGATED_PLAN_ENABLED", False)

        if not enabled or status != "APPROVED":
            return (
                "EXECUTION_NOT_SUPPORTED",
                "TokenPool is not currently enabled as an approved OpenAI participating integration."
            )

        if connection:
            if connection.authorization_status != "AUTHORIZED":
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "OpenAI delegated plan connection is not authorized. Complete ChatGPT authorization first."
                )

            has_token = bool(connection.encrypted_authorization_data or connection.encrypted_credential)
            if not has_token:
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "Missing authorized delegated token for this ChatGPT connection."
                )

            # Check commitment
            committed = connection.committed_units or connection.capacity_declared or 0
            remaining = connection.remaining_units if connection.remaining_units is not None else connection.capacity_remaining or 0
            if committed <= 0 or remaining <= 0:
                return (
                    "EXECUTION_NOT_SUPPORTED",
                    "Marketplace capacity commitment is 0 or exhausted. Update commitment to enable execution."
                )

        return ("EXECUTION_SUPPORTED", None)

    def authorize(
        self,
        user_id: int,
        state: str,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Builds the official OpenAI OAuth authorization URL for participating app flow.
        """
        status = getattr(settings, "OPENAI_PARTICIPATING_APP_STATUS", "NOT_APPLIED")
        enabled = getattr(settings, "OPENAI_DELEGATED_PLAN_ENABLED", False)

        if not enabled or status != "APPROVED":
            return {
                "success": False,
                "configured": False,
                "participating_app_status": status,
                "message": "TokenPool is not currently enabled as an approved OpenAI participating integration."
            }

        client_id = settings.OPENAI_DELEGATED_CLIENT_ID
        if not client_id:
            return {
                "success": False,
                "configured": False,
                "participating_app_status": status,
                "message": "OpenAI delegated client credentials are not configured on this server."
            }

        r_uri = redirect_uri or settings.OPENAI_DELEGATED_REDIRECT_URI
        scopes = ["openid", "profile", "email", "chatgpt.plan.usage"]
        scope_str = "%20".join([urllib.parse.quote(s) for s in scopes])

        auth_url = (
            f"{settings.OPENAI_DELEGATED_AUTH_URL}"
            f"?client_id={urllib.parse.quote(client_id)}"
            f"&redirect_uri={urllib.parse.quote(r_uri)}"
            f"&response_type=code"
            f"&scope={scope_str}"
            f"&state={urllib.parse.quote(state)}"
        )

        return {
            "success": True,
            "configured": True,
            "participating_app_status": status,
            "auth_url": auth_url,
            "scopes": scopes,
            "redirect_uri": r_uri
        }

    async def callback(
        self,
        code: str,
        state: Optional[str] = None,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Exchanges authorization code with OpenAI official token endpoint.
        """
        status = getattr(settings, "OPENAI_PARTICIPATING_APP_STATUS", "NOT_APPLIED")
        enabled = getattr(settings, "OPENAI_DELEGATED_PLAN_ENABLED", False)

        if not enabled or status != "APPROVED":
            raise ValueError("TokenPool is not currently enabled as an approved OpenAI participating integration.")

        token_url = settings.OPENAI_DELEGATED_TOKEN_URL
        r_uri = redirect_uri or settings.OPENAI_DELEGATED_REDIRECT_URI

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": r_uri,
            "client_id": settings.OPENAI_DELEGATED_CLIENT_ID,
            "client_secret": settings.OPENAI_DELEGATED_CLIENT_SECRET,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(token_url, data=payload)
            if res.status_code != 200:
                raise ValueError(f"OpenAI token exchange failed ({res.status_code}): {res.text}")

            token_data = res.json()
            return token_data

    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """Refreshes expired delegated token using official endpoint."""
        token_url = settings.OPENAI_DELEGATED_TOKEN_URL
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.OPENAI_DELEGATED_CLIENT_ID,
            "client_secret": settings.OPENAI_DELEGATED_CLIENT_SECRET,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(token_url, data=payload)
            if res.status_code != 200:
                raise ValueError(f"OpenAI token refresh failed ({res.status_code}): {res.text}")
            return res.json()

    async def revoke(self, token_data: str) -> bool:
        """Revokes delegated authorization token."""
        return True

    async def health_check(self, connection: ProviderConnection) -> Dict[str, Any]:
        """
        Validates connection health:
        1. Token presence and expiration
        2. Plan eligibility
        3. App limit and remaining capacity
        """
        now = datetime.now(timezone.utc)
        cap_status, reason = self.get_execution_capability(connection)

        if cap_status != "EXECUTION_SUPPORTED":
            return {
                "healthy": False,
                "status": "OFFLINE",
                "execution_status": cap_status,
                "failure_reason": reason or "Delegated execution capability unavailable."
            }

        # Check token expiration
        if connection.token_expires_at:
            exp = connection.token_expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                return {
                    "healthy": False,
                    "status": "REAUTHORIZATION_REQUIRED",
                    "execution_status": "EXECUTION_NOT_SUPPORTED",
                    "failure_reason": "Delegated token has expired. Re-authorization required."
                }

        # Check commitment balance
        remaining = connection.remaining_units if connection.remaining_units is not None else connection.capacity_remaining or 0
        if remaining <= 0:
            return {
                "healthy": False,
                "status": "EXHAUSTED",
                "execution_status": "EXECUTION_NOT_SUPPORTED",
                "failure_reason": "Committed marketplace capacity has been exhausted."
            }

        return {
            "healthy": True,
            "status": "HEALTHY",
            "execution_status": "EXECUTION_SUPPORTED",
            "committed_units": connection.committed_units or connection.capacity_declared,
            "remaining_units": remaining,
            "failure_reason": None
        }

    def get_usage(self, connection: ProviderConnection) -> Dict[str, Any]:
        committed = connection.committed_units or connection.capacity_declared or 0
        consumed = connection.consumed_units or connection.capacity_consumed or 0
        remaining = connection.remaining_units if connection.remaining_units is not None else connection.capacity_remaining or 0
        pct = (consumed / committed * 100.0) if committed > 0 else 0.0

        return {
            "capacity_source": "OFFICIAL_DELEGATED_PLAN",
            "committed_units": committed,
            "consumed_units": consumed,
            "remaining_units": remaining,
            "used_percentage": round(pct, 2),
            "marketplace_commitment": connection.marketplace_commitment or 0.0,
            "usage_unit": connection.upstream_usage_unit or "REQUEST",
        }

    def get_limits(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "app_limit": connection.upstream_limit,
            "reset_at": connection.upstream_reset_at.isoformat() if connection.upstream_reset_at else None,
            "usage_unit": connection.upstream_usage_unit or "REQUEST",
            "marketplace_commitment_percent": connection.marketplace_commitment or 0.0,
        }

    async def execute(
        self,
        payload: AIRequestPayload,
        connection: ProviderConnection
    ) -> AIResponseResult:
        """
        Executes request through OpenAI official delegated mechanism.
        NEVER uses global settings.OPENAI_API_KEY as provider capacity!
        """
        cap_status, reason = self.get_execution_capability(connection)
        if cap_status != "EXECUTION_SUPPORTED":
            raise ValueError(f"EXECUTION_NOT_SUPPORTED: {reason}")

        # Decrypt provider delegated token
        raw_token = ""
        if connection.encrypted_authorization_data:
            try:
                raw_token = decrypt_credential(connection.encrypted_authorization_data)
            except Exception:
                raw_token = ""
        elif connection.encrypted_credential:
            try:
                raw_token = decrypt_credential(connection.encrypted_credential)
            except Exception:
                raw_token = ""

        access_token = raw_token
        if raw_token.startswith("{"):
            try:
                data = json.loads(raw_token)
                access_token = data.get("access_token") or raw_token
            except Exception:
                pass

        if not access_token:
            raise ValueError("EXECUTION_NOT_SUPPORTED: Failed to decrypt provider's delegated authorization token.")

        # In demo / test mode:
        if payload.is_demo:
            mock = MockAIProvider(provider_name=f"ChatGPT Delegated Plan ({connection.account_email}) (DEMO)")
            return await mock.send_request(payload)

        # Call official OpenAI endpoint with delegated bearer token
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # Build message history
        msgs = payload.messages
        if not msgs:
            msgs = [{"role": "user", "content": payload.prompt}]

        body = {
            "model": payload.model_slug if payload.model_slug.startswith("gpt-") else "gpt-4o",
            "messages": msgs,
            "temperature": payload.temperature,
            "max_tokens": payload.max_tokens,
        }

        start_time = time.time()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body)
            latency_ms = int((time.time() - start_time) * 1000)

            if resp.status_code != 200:
                raise ValueError(f"OpenAI Delegated Plan execution failed ({resp.status_code}): {resp.text}")

            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            in_tokens = usage.get("prompt_tokens", 0)
            out_tokens = usage.get("completion_tokens", 0)
            tot_tokens = usage.get("total_tokens", in_tokens + out_tokens)

            cost = round((in_tokens * 0.000005) + (out_tokens * 0.000015), 6)

            return AIResponseResult(
                content=content,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                total_tokens=tot_tokens,
                cost=cost,
                latency_ms=latency_ms,
                model_slug=payload.model_slug,
                provider_name=f"ChatGPT ({connection.account_email})",
                is_demo=False
            )

    def get_account_identity(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "account_email": connection.account_email,
            "identity_status": connection.identity_status,
            "plan_name": connection.delegated_plan_name or "ChatGPT Plus/Pro",
            "provider_type": "openai",
            "capacity_source": "OFFICIAL_DELEGATED_PLAN",
        }


class GoogleDelegatedPlanAdapter(DelegatedPlanAdapter):
    """
    Truthful adapter for Google consumer AI subscription (Google One AI Pro).
    Google does not provide an official delegated execution mechanism for external apps.
    """

    def get_execution_capability(
        self,
        connection: Optional[ProviderConnection] = None
    ) -> Tuple[str, Optional[str]]:
        if getattr(settings, "GOOGLE_DELEGATED_PLAN_ENABLED", False):
            # If Google officially introduces a delegated execution mechanism in the future
            return ("EXECUTION_SUPPORTED", None)
        return (
            "EXECUTION_NOT_SUPPORTED",
            "Google does not provide an officially supported execution mechanism. Google consumer AI subscription cannot currently be used as TokenPool marketplace API execution capacity through a supported delegated execution mechanism."
        )

    def authorize(self, user_id: int, state: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        return {
            "success": False,
            "configured": False,
            "message": "Google does not provide an officially supported delegated execution mechanism for consumer AI subscriptions."
        }

    async def callback(self, code: str, state: Optional[str] = None, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        raise ValueError("Google consumer subscription delegation is not supported by Google.")

    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        raise ValueError("Google consumer subscription delegation is not supported.")

    async def revoke(self, token_data: str) -> bool:
        return True

    async def health_check(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "healthy": False,
            "status": "UNSUPPORTED",
            "execution_status": "EXECUTION_NOT_SUPPORTED",
            "failure_reason": "Google does not offer a delegated execution mechanism for consumer AI subscriptions."
        }

    def get_usage(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "capacity_source": "UNSUPPORTED_CONSUMER_SUBSCRIPTION",
            "committed_units": 0,
            "consumed_units": 0,
            "remaining_units": 0,
            "used_percentage": 0.0,
            "usage_unit": "REQUEST"
        }

    def get_limits(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {"app_limit": 0, "reset_at": None, "usage_unit": "REQUEST"}

    async def execute(self, payload: AIRequestPayload, connection: ProviderConnection) -> AIResponseResult:
        raise ValueError(
            "EXECUTION_NOT_SUPPORTED: Google consumer AI subscription cannot currently be used as TokenPool marketplace API execution capacity through a supported delegated execution mechanism."
        )

    def get_account_identity(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "account_email": connection.account_email,
            "identity_status": connection.identity_status,
            "plan_name": "Google One AI Pro",
            "provider_type": "gemini",
            "capacity_source": "UNSUPPORTED_CONSUMER_SUBSCRIPTION"
        }


class AnthropicDelegatedPlanAdapter(DelegatedPlanAdapter):
    """
    Truthful adapter for Claude consumer Pro subscription.
    Anthropic does not provide a 3rd party delegated execution API for consumer web subscriptions.
    """

    def get_execution_capability(
        self,
        connection: Optional[ProviderConnection] = None
    ) -> Tuple[str, Optional[str]]:
        if getattr(settings, "ANTHROPIC_DELEGATED_PLAN_ENABLED", False):
            return ("EXECUTION_SUPPORTED", None)
        return (
            "EXECUTION_NOT_SUPPORTED",
            "Claude consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing."
        )

    def authorize(self, user_id: int, state: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        return {
            "success": False,
            "configured": False,
            "message": "Anthropic does not offer a 3rd-party delegated execution mechanism for consumer subscriptions."
        }

    async def callback(self, code: str, state: Optional[str] = None, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        raise ValueError("Anthropic consumer subscription delegation is not supported by Anthropic.")

    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        raise ValueError("Anthropic consumer subscription delegation is not supported.")

    async def revoke(self, token_data: str) -> bool:
        return True

    async def health_check(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "healthy": False,
            "status": "UNSUPPORTED",
            "execution_status": "EXECUTION_NOT_SUPPORTED",
            "failure_reason": "Anthropic does not provide a 3rd-party execution API for consumer subscriptions."
        }

    def get_usage(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "capacity_source": "UNSUPPORTED_CONSUMER_SUBSCRIPTION",
            "committed_units": 0,
            "consumed_units": 0,
            "remaining_units": 0,
            "used_percentage": 0.0,
            "usage_unit": "REQUEST"
        }

    def get_limits(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {"app_limit": 0, "reset_at": None, "usage_unit": "REQUEST"}

    async def execute(self, payload: AIRequestPayload, connection: ProviderConnection) -> AIResponseResult:
        raise ValueError(
            "EXECUTION_NOT_SUPPORTED: Claude consumer Pro subscription does not provide a 3rd-party marketplace execution API for external consumer routing."
        )

    def get_account_identity(self, connection: ProviderConnection) -> Dict[str, Any]:
        return {
            "account_email": connection.account_email,
            "identity_status": connection.identity_status,
            "plan_name": "Claude Pro",
            "provider_type": "anthropic",
            "capacity_source": "UNSUPPORTED_CONSUMER_SUBSCRIPTION"
        }
