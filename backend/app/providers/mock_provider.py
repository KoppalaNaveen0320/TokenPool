import asyncio
import random
import time
from typing import Dict, Any, List, AsyncGenerator
from backend.app.providers.base import AIProviderAdapter, AIRequestPayload, AIResponseResult

class MockAIProvider(AIProviderAdapter):
    """
    Mock AI Provider simulating Orbit AI, Nexus Cloud, and Arc Compute
    for DEMO MODE and authorized integration testing.
    """
    def __init__(self, provider_name: str = "Orbit AI"):
        self.provider_name = provider_name
        self.supported_models = [
            "nova-reasoning",
            "swift-chat",
            "atlas-code",
            "vision-pro",
            "embed-mini",
            "research-long"
        ]

    def get_supported_models(self) -> List[str]:
        return self.supported_models

    async def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        # Mock credentials check: accepts keys starting with sk- or oauth tokens
        key = credentials.get("api_key", "")
        return len(key) >= 10

    async def health_check(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY",
            "latency_ms": random.randint(180, 420),
            "provider": self.provider_name,
            "models_available": len(self.supported_models)
        }

    async def send_request(self, payload: AIRequestPayload) -> AIResponseResult:
        start_time = time.time()
        
        # Calculate realistic token metrics
        total_prompt_text = " ".join([m.get("content", "") for m in payload.messages]) if payload.messages else payload.prompt
        input_tokens = max(10, len(total_prompt_text.split()) * 2 + random.randint(5, 25))
        
        # Simulate realistic model latency
        latency_base = {
            "swift-chat": 0.25,
            "embed-mini": 0.15,
            "atlas-code": 0.45,
            "nova-reasoning": 0.65,
            "vision-pro": 0.55,
            "research-long": 0.90
        }.get(payload.model_slug, 0.40)
        
        await asyncio.sleep(latency_base)
        
        # Generate rich contextual markdown response based on prompt
        prompt_lower = payload.prompt.lower()
        if payload.messages and len(payload.messages) > 1 and ("example" in prompt_lower or "follow" in prompt_lower):
            prev_topic = payload.messages[0].get("content", "the previous concept")[:50]
            response_text = (
                f"**Follow-up Example for Context: \"{prev_topic}\"**:\n\n"
                f"Building on our earlier conversation, here is a practical demonstration:\n\n"
                "```python\n"
                "# Demonstrated execution pipeline maintaining conversation context\n"
                "def execute_pipeline(context, user_query):\n"
                "    return {'status': 'success', 'context_depth': len(context)}\n"
                "```\n\n"
                "Notice how maintaining TokenPool-level conversation history allows context-aware assistance without depending on provider consumer accounts."
            )
        elif "reduce cost" in prompt_lower or "token" in prompt_lower or "pricing" in prompt_lower:
            response_text = (
                "There are several practical ways to reduce token usage while preserving answer quality:\n\n"
                "1. **Chunk by meaning, not size**. Split documents at section boundaries so each request has complete context.\n"
                "2. **Summarize progressively**. Create compact summaries once, then reuse them instead of resending source text.\n"
                "3. **Use the right model**. Reserve advanced reasoning models for tasks that benefit from them.\n\n"
                "```typescript\n"
                "const context = await summarize(document);\n"
                "return askModel({ context, question });\n"
                "```\n\n"
                "By matching prompt design with TokenPool marketplace rates, average savings exceed 40% per workload."
            )
        elif "code" in prompt_lower or "typescript" in prompt_lower or "python" in prompt_lower or "function" in prompt_lower:
            response_text = (
                "Here is an optimized implementation designed for high throughput and predictable memory usage:\n\n"
                "```typescript\n"
                "export async function routeRequest(model: string, payload: unknown) {\n"
                "  const provider = await pool.getHealthyProvider(model);\n"
                "  if (!provider) throw new Error('No capacity available');\n"
                "  return await provider.execute(payload);\n"
                "}\n"
                "```\n\n"
                "- **Connection Pooling**: Reuses keep-alive sockets.\n"
                "- **Failover Guarantee**: Gracefully reroutes if provider latency exceeds SLA."
            )
        else:
            response_text = (
                f"**Analysis and Response from {payload.model_slug.replace('-', ' ').title()}**:\n\n"
                f"I processed your query regarding *\"{payload.prompt[:60]}...\"*.\n\n"
                "1. **Key Takeaway**: The workflow is viable and fits standard operational parameters.\n"
                "2. **Capacity Routing**: The platform verified available authorized provider bandwidth before execution.\n"
                "3. **Settlement**: This usage is recorded with immutable token metrics.\n\n"
                "> [!NOTE]\n"
                "> This response was generated via TokenPool's authorized provider adapter in DEMO MODE."
            )
            
        output_tokens = max(20, len(response_text.split()) * 2)
        total_tokens = input_tokens + output_tokens
        
        # Pricing in INR per 1000 tokens (e.g. ₹0.08 / 1k input, ₹0.15 / 1k output)
        cost = round((input_tokens * 0.00008) + (output_tokens * 0.00015), 4)
        latency_ms = int((time.time() - start_time) * 1000)

        return AIResponseResult(
            content=response_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost=cost,
            latency_ms=latency_ms,
            model_slug=payload.model_slug,
            provider_name=self.provider_name,
            is_demo=payload.is_demo
        )

    async def stream_request(self, payload: AIRequestPayload) -> AsyncGenerator[str, None]:
        res = await self.send_request(payload)
        words = res.content.split(" ")
        for word in words:
            yield word + " "
            await asyncio.sleep(0.03)
