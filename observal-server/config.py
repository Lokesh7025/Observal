from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/observal"
    CLICKHOUSE_URL: str = "clickhouse://localhost:8123/observal"
    REDIS_URL: str = "redis://localhost:6379"
    SECRET_KEY: str = "change-me-in-production"
    API_KEY_LENGTH: int = 32
    EVAL_MODEL_URL: str = ""  # OpenAI-compatible endpoint (e.g., https://bedrock-runtime.us-east-1.amazonaws.com)
    EVAL_MODEL_API_KEY: str = ""  # API key or empty for AWS credential chain
    EVAL_MODEL_NAME: str = ""  # e.g., us.anthropic.claude-3-5-haiku-20241022-v1:0
    EVAL_MODEL_PROVIDER: str = ""  # "bedrock", "openai", or "" for auto-detect
    AWS_REGION: str = "us-east-1"

    # OAuth integrations (GitHub/GitLab)
    # Public URL the browser is redirected back to after the provider finishes its consent flow.
    # Must match the callback URLs registered in the provider apps.
    OAUTH_PUBLIC_BASE_URL: str = "http://localhost:8000"

    # 32-byte urlsafe-base64 Fernet key. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Tokens stored in the oauth_tokens table are encrypted with this key.
    OAUTH_TOKEN_ENCRYPTION_KEY: str = ""

    # GitHub OAuth App / GitHub App (user-to-server auth)
    GITHUB_OAUTH_CLIENT_ID: str = ""
    GITHUB_OAUTH_CLIENT_SECRET: str = ""

    # GitLab OAuth Application
    GITLAB_OAUTH_CLIENT_ID: str = ""
    GITLAB_OAUTH_CLIENT_SECRET: str = ""
    # Override to point at a self-managed GitLab instance, e.g. https://gitlab.acme.internal
    GITLAB_BASE_URL: str = "https://gitlab.com"

    model_config = {"env_file": ".env"}


settings = Settings()
