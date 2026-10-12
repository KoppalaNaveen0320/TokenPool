import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "TokenPool"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = os.getenv("JWT_SECRET", "super-secret-tokenpool-jwt-key-2026-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:ShabhanaaNaveen0320@localhost:5432/tokenpool")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    DEFAULT_COMMISSION_RATE: float = float(os.getenv("COMMISSION_RATE", "0.20"))
    DEMO_MODE_DEFAULT: bool = os.getenv("DEMO_MODE", "true").lower() in ("true", "1")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    REQUIRE_PAYMENT_GATEWAY: bool = os.getenv("REQUIRE_PAYMENT_GATEWAY", "false").lower() in ("true", "1")
    PAYMENT_WEBHOOK_SECRET: str = os.getenv("PAYMENT_WEBHOOK_SECRET", "test-webhook-secret-tokenpool-2026")
    ENCRYPTION_KEY: str = os.getenv("FERNET_KEY", os.getenv("ENCRYPTION_KEY", ""))
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_OAUTH_REDIRECT_URI: str = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/api/v1/provider/ai-accounts/google/callback")
    
    # Official Delegated Plan Execution Configuration
    OPENAI_DELEGATED_PLAN_ENABLED: bool = os.getenv("OPENAI_DELEGATED_PLAN_ENABLED", "false").lower() in ("true", "1")
    OPENAI_PARTICIPATING_APP_STATUS: str = os.getenv("OPENAI_PARTICIPATING_APP_STATUS", "NOT_APPLIED") # NOT_APPLIED, PENDING, APPROVED, REJECTED, SUSPENDED
    OPENAI_DELEGATED_CLIENT_ID: str = os.getenv("OPENAI_DELEGATED_CLIENT_ID", "")
    OPENAI_DELEGATED_CLIENT_SECRET: str = os.getenv("OPENAI_DELEGATED_CLIENT_SECRET", "")
    OPENAI_DELEGATED_AUTH_URL: str = os.getenv("OPENAI_DELEGATED_AUTH_URL", "https://auth.openai.com/authorize")
    OPENAI_DELEGATED_TOKEN_URL: str = os.getenv("OPENAI_DELEGATED_TOKEN_URL", "https://auth.openai.com/oauth/token")
    OPENAI_DELEGATED_REDIRECT_URI: str = os.getenv("OPENAI_DELEGATED_REDIRECT_URI", "http://localhost:8000/api/v1/provider/ai-accounts/openai/callback")
    GOOGLE_DELEGATED_PLAN_ENABLED: bool = os.getenv("GOOGLE_DELEGATED_PLAN_ENABLED", "false").lower() in ("true", "1")
    ANTHROPIC_DELEGATED_PLAN_ENABLED: bool = os.getenv("ANTHROPIC_DELEGATED_PLAN_ENABLED", "false").lower() in ("true", "1")

    class Config:
        env_file = ".env"
        extra = "ignore"
        case_sensitive = True

    def __init__(self, **values):
        super().__init__(**values)
        if self.ENVIRONMENT.lower() == "production":
            raw_jwt = os.getenv("JWT_SECRET", "").strip()
            if not raw_jwt or raw_jwt == "super-secret-tokenpool-jwt-key-2026-production":
                raise ValueError(
                    "FATAL CONFIGURATION ERROR: Production deployment requires a dedicated, cryptographically secure JWT_SECRET. "
                    "Development default secret cannot be used in production."
                )

            raw_fernet = (os.getenv("FERNET_KEY", "") or os.getenv("ENCRYPTION_KEY", "")).strip()
            if not raw_fernet:
                raise ValueError(
                    "FATAL CONFIGURATION ERROR: Production deployment requires FERNET_KEY to be explicitly configured. "
                    "Fernet credential vault cannot use unconfigured or fallback keys in production."
                )

            raw_db = os.getenv("DATABASE_URL", "").strip()
            if not raw_db or "ShabhanaaNaveen0320" in raw_db or "localhost:5432" in raw_db:
                raise ValueError(
                    "FATAL CONFIGURATION ERROR: Production deployment requires an explicit, production-grade DATABASE_URL. "
                    "Development database credentials cannot be used in production."
                )

            if self.REQUIRE_PAYMENT_GATEWAY:
                raw_webhook = os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()
                if not raw_webhook or raw_webhook == "test-webhook-secret-tokenpool-2026":
                    raise ValueError(
                        "FATAL CONFIGURATION ERROR: Production deployment requires a secure, unique PAYMENT_WEBHOOK_SECRET "
                        "when REQUIRE_PAYMENT_GATEWAY is enabled."
                    )


settings = Settings()
