"""Driver debt thresholds and automatic blocking."""
from __future__ import annotations

import logging
from decimal import Decimal

from app.config import get_settings
from app.models import DriverProfile, DriverStatus
from app.services import order_service

logger = logging.getLogger("taxi_bot.debt")


def apply_debt_block_if_needed(driver: DriverProfile) -> bool:
    """Block driver if balance >= debt_block. Returns True if blocked now."""
    bal = Decimal(str(driver.balance))
    if order_service.debt_level(bal) != "block":
        return False
    if driver.status == DriverStatus.BLOCKED.value:
        return False
    DriverProfile.update(
        status=DriverStatus.BLOCKED.value,
        online=False,
        loading=False,
    ).where(DriverProfile.id == driver.id).execute()
    logger.info("Driver %s auto-blocked: balance %s", driver.id, bal)
    return True
