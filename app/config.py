from functools import lru_cache
from typing import FrozenSet

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    webhook_secret: str = ""
    webhook_path: str = "/webhook/telegram"
    base_url: str = "http://127.0.0.1:8000"

    database_url: str = "sqlite:///taxi_bot.db"

    secret_key: str = "dev-secret-change-in-production"
    code_pepper: str = "dev-pepper-change-in-production"
    admin_telegram_ids: str = ""

    mini_app_url: str = ""

    shop_id: str = ""
    shop_secret_key: str = ""

    qr_token_ttl_minutes: int = 120

    debt_warn: int = 100
    debt_restrict: int = 150
    debt_block: int = 200

    commission_percent: int = 10

    @property
    def admin_ids(self) -> FrozenSet[int]:
        if not self.admin_telegram_ids.strip():
            return frozenset()
        parts = [p.strip() for p in self.admin_telegram_ids.split(",") if p.strip()]
        return frozenset(int(x) for x in parts)


@lru_cache
def get_settings() -> Settings:
    return Settings()
