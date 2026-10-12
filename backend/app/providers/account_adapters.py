"""
TokenPool — Provider AI Account Execution Adapters
Implements AIAccountExecutionAdapter with:
- GeminiAccountAdapter
- OpenAIAccountAdapter
- ClaudeAccountAdapter
- MockAccountAdapter

Guarantees:
- Uses ONLY officially supported mechanisms.
- Never scrapes websites, never automates browser sessions, never uses cookies or session tokens.
- If official execution through the connected consumer account is not supported, raises EXECUTION_NOT_SUPPORTED.
- Never fakes an AI response.
"""
import time
from typing import Dict, Any, List, Optional, AsyncGenerator
from backend.app.core.encryption import decrypt_credential
from backend.app.models.schema_models import ProviderConnection
from backend.app.providers.base import AIProviderAdapter, AIRequestPayload, AIResponseResult
from backend.app.providers.gemini_provider import GeminiProviderAdapter
from backend.app.providers.openai_provider import OpenAIProviderAdapter
from backend.app.providers.anthropic_provider import AnthropicProviderAdapter
from backend.app.providers.mock_provider import MockAIProvider


class AIAccountExecutionAdapter(AIProviderAdapter):
    """Base adapter for a connected provider's AI account."""

    def __init__(self, connection: ProviderConnection):
        self.connection = connection
        self.provider_type = (connection.provider_type or "gemini").lower()
        self.account_email = connection.account_email
        self.connection_method = connection.connection_method or "google_identity"

    def _check_execution_support(self):
        """Validates that this connected account is officially eligible for execution."""
        if not self.connection:
            raise ValueError("EXECUTION_NOT_SUPPORTED: No connected AI account provided.")

        if (self.connection.connection_status or self.connection.status) != "CONNECTED":
            raise ValueError(
                f"EXECUTION_NOT_SUPPORTED: Account connection is {self.connection.connection_status or self.connection.status}."
            )

        if self.connection.identity_status != "IDENTITY_MATCHED":
            raise ValueError(
                f"EXECUTION_NOT_SUPPORTED: Account identity is {self.connection.identity_status}."
            )

        if self.connection.authorization_status != "AUTHORIZED":
            raise ValueError(
                f"EXECUTION_NOT_SUPPORTED: AI account authorization status is {self.connection.authorization_status}."
            )

        if self.connection.marketplace_status != "ELIGIBLE":
            reason = self.connection.marketplace_reason or "Account is not eligible for marketplace execution."
            raise ValueError(f"EXECUTION_NOT_SUPPORTED: {reason}")


class GeminiAccountAdapter(AIAccountExecutionAdapter):
    """
    Adapter for Google Gemini accounts associated with Provider's Google identity.
    Enforces real provider authorization — NEVER uses settings.GEMINI_API_KEY for provider listings.
    """

    def __init__(self, connection: ProviderConnection):
        super().__init__(connection)
        self.api_key = None
        self.bearer_token = None

        # Check for encrypted OAuth authorization or encrypted credential
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

        # Parse token: could be JSON with access_token or direct string
        if raw_token:
            if raw_token.startswith("{") and "access_token" in raw_token:
                import json
                try:
                    data = json.loads(raw_token)
                    self.bearer_token = data.get("access_token")
                except Exception:
                    self.bearer_token = raw_token
            elif raw_token.startswith("ya29."):
                self.bearer_token = raw_token
            else:
                self.api_key = raw_token

        self.underlying_adapter = GeminiProviderAdapter(
            api_key=self.api_key,
            bearer_token=self.bearer_token
        )

    def _check_execution_support(self):
        super()._check_execution_support()
        if not self.api_key and not self.bearer_token:
            raise ValueError(
                "EXECUTION_NOT_SUPPORTED: Google does not provide an officially supported execution mechanism "
                "for using this consumer AI subscription to fulfill TokenPool marketplace requests. "
                "No authorized provider credential or OAuth Bearer token found."
            )

    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
        self._check_execution_support()
        if payload.is_demo:
            mock = MockAIProvider(provider_name=f"Google Gemini ({self.account_email}) (DEMO)")
            return await mock.send_request(payload)
        return await self.underlying_adapter.send_request(payload)

    async def stream_request(self, payload: AIRequestPayload) -> AsyncGenerator[str, None]:
        self._check_execution_support()
        async for chunk in self.underlying_adapter.stream_request(payload):
            yield chunk

    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return await self.underlying_adapter.validate_credentials(credentials)

    async def health_check(self) -> Dict[str, Any]:
        return await self.underlying_adapter.health_check()

    def get_supported_models(self) -> List[str]:
        return self.underlying_adapter.get_supported_models()


class OpenAIAccountAdapter(AIAccountExecutionAdapter):
    """
    Adapter for OpenAI / ChatGPT accounts.
    If connected as consumer ChatGPT Pro without API authorization, execution is NOT_SUPPORTED.
    """

    def __init__(self, connection: ProviderConnection):
        super().__init__(connection)
        token = ""
        if connection.encrypted_credential:
            token = decrypt_credential(connection.encrypted_credential)
        self.underlying_adapter = OpenAIProviderAdapter(api_key=token or None)

    @property
    def api_key(self):
        return getattr(self.underlying_adapter, "api_key", None)

    def _check_execution_support(self):
        super()._check_execution_support()
        if not self.underlying_adapter.api_key:
            raise ValueError(
                "EXECUTION_NOT_SUPPORTED: OpenAI consumer accounts cannot be used as marketplace execution capacity "
                "without an authorized API credential."
            )

    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
        self._check_execution_support()
        if payload.is_demo:
            mock = MockAIProvider(provider_name=f"ChatGPT ({self.account_email})")
            return await mock.send_request(payload)
        return await self.underlying_adapter.send_request(payload)

    async def stream_request(self, payload: AIRequestPayload) -> AsyncGenerator[str, None]:
        self._check_execution_support()
        async for chunk in self.underlying_adapter.stream_request(payload):
            yield chunk

    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return await self.underlying_adapter.validate_credentials(credentials)

    async def health_check(self) -> Dict[str, Any]:
        return await self.underlying_adapter.health_check()

    def get_supported_models(self) -> List[str]:
        return self.underlying_adapter.get_supported_models()


class ClaudeAccountAdapter(AIAccountExecutionAdapter):
    """
    Adapter for Anthropic / Claude accounts.
    If connected as consumer Claude Pro without API authorization, execution is NOT_SUPPORTED.
    """

    def __init__(self, connection: ProviderConnection):
        super().__init__(connection)
        token = ""
        if connection.encrypted_credential:
            token = decrypt_credential(connection.encrypted_credential)
        self.underlying_adapter = AnthropicProviderAdapter(api_key=token or None)

    @property
    def api_key(self):
        return getattr(self.underlying_adapter, "api_key", None)

    def _check_execution_support(self):
        super()._check_execution_support()
        if not self.underlying_adapter.api_key:
            raise ValueError(
                "EXECUTION_NOT_SUPPORTED: Anthropic consumer accounts cannot be used as marketplace execution capacity "
                "without an authorized API credential."
            )

    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
        self._check_execution_support()
        if payload.is_demo:
            mock = MockAIProvider(provider_name=f"Claude ({self.account_email})")
            return await mock.send_request(payload)
        return await self.underlying_adapter.send_request(payload)

    async def stream_request(self, payload: AIRequestPayload) -> AsyncGenerator[str, None]:
        self._check_execution_support()
        async for chunk in self.underlying_adapter.stream_request(payload):
            yield chunk

    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return await self.underlying_adapter.validate_credentials(credentials)

    async def health_check(self) -> Dict[str, Any]:
        return await self.underlying_adapter.health_check()

    def get_supported_models(self) -> List[str]:
        return self.underlying_adapter.get_supported_models()


class MockAccountAdapter(AIAccountExecutionAdapter):
    """Adapter for demo and mock testing connections."""

    def __init__(self, connection: ProviderConnection):
        super().__init__(connection)
        self.mock = MockAIProvider(provider_name=connection.provider_name or "Orbit AI (DEMO)")

    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
        self._check_execution_support()
        return await self.mock.send_request(payload)

    async def stream_request(self, payload: AIRequestPayload) -> AsyncGenerator[str, None]:
        self._check_execution_support()
        async for chunk in self.mock.stream_request(payload):
            yield chunk

    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        return True

    async def health_check(self) -> Dict[str, Any]:
        return {"status": "HEALTHY", "provider": "mock"}

    def get_supported_models(self) -> List[str]:
        return self.mock.get_supported_models()
