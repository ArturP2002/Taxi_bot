import logging
from functools import lru_cache
from typing import FrozenSet

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("taxi_bot.config")


def _strip_env_quotes(value: object) -> object:
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].strip()
    return s


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""

    @field_validator("bot_token", "secret_key", "code_pepper", mode="before")
    @classmethod
    def strip_quoted_env(cls, value: object) -> object:
        return _strip_env_quotes(value)
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

    # 54-FZ receipt for YooKassa (required when online cash register is enabled in shop)
    receipt_vat_code: int = 1
    receipt_payment_mode: str = "full_prepayment"
    receipt_payment_subject: str = "service"
    receipt_fallback_email: str = ""

    qr_token_ttl_minutes: int = 120

    debt_warn: int = 5000
    debt_restrict: int = 8000
    debt_block: int = 10000

    commission_percent: int = 10
    auto_assign_enabled: bool = True
    direction_page_size: int = 10

    # Queue ETA: setup before departure, gap between trips, default rest after trip (minutes)
    queue_loading_setup_min: int = 45
    queue_loading_gap_min: int = 30
    queue_default_rest_min: int = 0

    driver_offer_url: str = ""
    passenger_terms_url: str = (
        "https://docs.google.com/document/d/1mjE42bm2P-RD1up6WpTZUQo_k0TbVW2yA3O1uvauIGg/edit?usp=drivesdk"
    )
    passenger_privacy_url: str = (
        "https://docs.google.com/document/d/1zyp-q7MO6NKd69ajtiu89PP2czA_iF8nE3fa5RWxlxQ/edit?usp=drivesdk"
    )
    driver_can_create_trips: bool = False
    scheduled_trip_booking_days_ahead: int = 60

    route_reserve_min_drivers: int = 3
    loading_reminder_minutes_before: int = 30
    queue_underfill_notify_min_orders: int = 1

    # Driver risk (30 days window)
    driver_declines_suspicious_30d: int = 5
    driver_cancels_suspicious_30d: int = 3
    driver_decline_rate_suspicious: float = 0.55

    @property
    def admin_ids(self) -> FrozenSet[int]:
        if not self.admin_telegram_ids.strip():
            return frozenset()
        parts = [p.strip() for p in self.admin_telegram_ids.split(",") if p.strip()]
        ids: list[int] = []
        for part in parts:
            try:
                ids.append(int(part))
            except ValueError:
                logger.warning("Invalid ADMIN_TELEGRAM_IDS entry ignored: %r", part)
        return frozenset(ids)


@lru_cache
def get_settings() -> Settings:
    return Settings()
