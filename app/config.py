"""Typed configuration loaded from .env.

Only the Telegram credentials are hard-required. The Sheets mirror and the LLM
fallback parser are optional layers: when their keys are absent the app still
runs, with those layers disabled and a warning logged at startup.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- required ---
    telegram_bot_token: str
    # Comma-separated in .env: TELEGRAM_OWNER_ID=111,222. All listed IDs may
    # log expenses and use commands, and all receive the daily recap. Kept as
    # a raw string field -- pydantic-settings JSON-decodes list-typed env vars
    # before any validator runs, which rejects a plain comma list.
    telegram_owner_id: str

    # --- storage ---
    db_path: Path = Path("data/expenses.db")
    google_sheet_id: str = ""
    google_credentials_path: Path | None = None
    sheet_worksheet: str = "Expenses"

    # --- LLM fallback parser: primary Qwen (OpenRouter), fallback Gemini ---
    llm_enabled: bool = True
    llm_primary_base_url: str = "https://openrouter.ai/api/v1"
    llm_primary_model: str = "qwen/qwen3-235b-a22b:free"
    llm_primary_api_key: str = ""
    llm_fallback_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    llm_fallback_model: str = "gemini-2.5-flash-lite"
    llm_fallback_api_key: str = ""
    llm_timeout_seconds: float = 20.0

    # --- behaviour ---
    timezone: str = "Asia/Jakarta"
    recap_time: str = "21:00"
    default_currency: str = "IDR"
    sync_interval_minutes: int = 5
    confidence_threshold: float = Field(0.7, ge=0.0, le=1.0)

    @property
    def owner_ids(self) -> set[int]:
        return {
            int(part.strip())
            for part in self.telegram_owner_id.split(",")
            if part.strip()
        }

    @property
    def sheets_enabled(self) -> bool:
        return bool(
            self.google_sheet_id
            and self.google_credentials_path
            and self.google_credentials_path.is_file()
        )

    @property
    def llm_primary_enabled(self) -> bool:
        return self.llm_enabled and bool(self.llm_primary_api_key)

    @property
    def llm_fallback_enabled(self) -> bool:
        return self.llm_enabled and bool(self.llm_fallback_api_key)

    @property
    def any_llm_enabled(self) -> bool:
        return self.llm_primary_enabled or self.llm_fallback_enabled

    @property
    def recap_hour_minute(self) -> tuple[int, int]:
        hour, _, minute = self.recap_time.partition(":")
        return int(hour), int(minute or 0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
