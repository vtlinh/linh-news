from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_EMAIL = "vtlinh87@gmail.com"

# All "today" calculations in the app must use Linh's local time, not UTC,
# so the date picker and calendar lookups match what's on her wall clock.
LOCAL_TZ = ZoneInfo("America/New_York")


def local_today() -> date:
    """Today's date in Linh's local timezone (America/New_York)."""
    return datetime.now(LOCAL_TZ).date()


def local_now() -> datetime:
    """Current time in Linh's local timezone."""
    return datetime.now(LOCAL_TZ)

# Load .env early with override=True so values in .env take precedence over
# any pre-existing (possibly empty) shell variables.
load_dotenv(REPO_ROOT / ".env", override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/linh_news"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-7"

    google_client_id: str = ""
    google_client_secret: str = ""
    public_base_url: str = "http://localhost:8000"

    redis_url: str = ""
    events_refresh_min_seconds: int = 3600  # at most once per hour

    weather_coords: str = "41.0223,-74.0635"
    weather_address: str = "15 Hunter Ridge, Woodcliff Lake, NJ 07677"

    session_secret: str = "dev-only-change-me"
    session_ttl_days: int = 30

    users_file: Path = REPO_ROOT / "users.txt"
    news_pr_path: Path = REPO_ROOT / "news.pr"


@lru_cache
def get_settings() -> Settings:
    return Settings()
