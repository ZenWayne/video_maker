"""Application configuration using pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Gemini API key (Google AI Studio — set via secrets/gemini_api_key)
    gemini_api_key: str = ""

    # DeepSeek API key (OpenAI-compatible — set via secrets/deepseek_api_key)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Redis
    redis_url: str = "redis://redis:6379"

    # Storage
    storage_root: str = "./storage"

    # Database (3 slashes for relative path)
    database_url: str = "sqlite+aiosqlite:///./metadata.db"

    # LLM Models (from config.yml / config.env)
    gemini_script_model: str = "gemini-3.1-pro-preview"
    gemini_director_model: str = "gemini-3.1-pro-preview"

    # Worker settings (from config.yml / config.env)
    worker_pool_size: int = 4

    # Veo (video generation via Vertex AI)
    veo_api_key: str = ""
    veo_project: str = "tarot-493203"
    veo_location: str = "us-central1"
    veo_model: str = "veo-3.1-fast-generate-001"
    veo_poll_interval_seconds: int = 10
    veo_max_wait_seconds: int = 300

    # CosyVoice VC service
    cosyvoice_url: str = "http://cosyvoice-vc:9880"

    # CORS (from config.yml / config.env)
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
