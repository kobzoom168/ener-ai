from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_webhook_url: str
    telegram_chat_id: str

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    database_path: str = "./data/ener.db"

    class Config:
        env_file = ".env"


settings = Settings()
