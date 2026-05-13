from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_webhook_url: str
    telegram_chat_id: str
    admin_password: str = "ener2026"

    anthropic_api_key: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""
    github_token: str = ""
    xai_api_key: str = ""
    deepseek_api_key: str = ""
    moonshot_api_key: str = ""
    openai_api_key: str = ""

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    database_path: str = "./data/ener.db"

    # Optional: container name for `docker stats` in resource diagnostics (empty = skip docker).
    # Set DOCKER_STATS_CONTAINER=ener-ai (or your service name) in `.env` to enable.
    docker_stats_container: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
