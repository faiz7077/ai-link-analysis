from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    gemini_thinking_level: str = "low"  # minimal | low | medium | high, empty = model default
    llm_temperature: float = 0.3
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    # Website fetching
    fetch_timeout_seconds: float = 12.0
    max_page_bytes: int = 2_000_000
    max_redirects: int = 5
    max_subpages: int = 3
    max_content_chars: int = 14_000
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 LinkAnalysisBot/1.0"
    )

    # Abuse protection: analyses per client IP per minute (0 = disabled)
    rate_limit_per_minute: int = 10

    # Caching
    cache_ttl_seconds: int = 3600
    cache_max_items: int = 256


@lru_cache
def get_settings() -> Settings:
    return Settings()
