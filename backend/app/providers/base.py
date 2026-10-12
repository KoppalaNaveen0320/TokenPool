from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, AsyncGenerator
from pydantic import BaseModel

class AIRequestPayload(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_slug: str
    prompt: str
    messages: Optional[List[Dict[str, str]]] = None
    temperature: float = 0.7
    max_tokens: int = 1000
    is_demo: bool = False

class AIResponseResult(BaseModel):
    model_config = {"protected_namespaces": ()}
    content: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float
    latency_ms: int
    model_slug: str
    provider_name: str
    is_demo: bool

class AIProviderAdapter(ABC):
    @abstractmethod
    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
        pass

    @abstractmethod
    async def stream_request(self, payload: AIRequestPayload) -> AsyncGenerator[str, None]:
        pass

    @abstractmethod
    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        pass

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def get_supported_models(self) -> List[str]:
        pass
