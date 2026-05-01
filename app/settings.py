from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_EMAIL = "vtlinh87@gmail.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/linh_news"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-7"

    google_client_id: str = ""
    google_client_secret: str = ""
    public_base_url: str = "http://localhost:8000"

    weather_coords: str = "41.0223,-74.0635"
    weather_address: str = "15 Hunter Ridge, Woodcliff Lake, NJ 07677"

    session_secret: str = "dev-only-change-me"
    session_ttl_days: int = 30

    users_file: Path = REPO_ROOT / "users.txt"
    news_pr_path: Path = REPO_ROOT / "news.pr"


@lru_cache
def get_settings() -> Settings:
    return Settings()
