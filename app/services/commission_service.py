from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings
from app.models import Direction, Order, DriverProfile, CommissionLedger


def platform_seats_for_order(order: Order, driver: Optional[DriverProfile] = None) -> int:
    if order.platform_seats is not None:
        return max(0, int(order.platform_seats))
    return order.seats


def order_base_fare(order: Order, driver: Optional[DriverProfile] = None) -> Decimal:
    d: Direction = order.direction  # type: ignore
    seats = platform_seats_for_order(order, driver)
    per = Decimal(str(d.price_per_seat)) * seats
    fixed = Decimal(str(d.fixed_price))
    return per + fixed


def commission_amount_for_order(order: Order, driver: Optional[DriverProfile] = None) -> Decimal:
    base = order_base_fare(order, driver)
    pct = Decimal(get_settings().commission_percent) / Decimal(100)
    return (base * pct).quantize(Decimal("0.01"))


def commission_explanation(order: Order, driver: Optional[DriverProfile] = None) -> str:
    s = get_settings()
    d: Direction = order.direction  # type: ignore
    seats = platform_seats_for_order(order, driver)
    base = order_base_fare(order, driver)
    comm = commission_amount_for_order(order, driver)
    return (
        f"Мест (платформа): {seats} × {d.price_per_seat} ₽ + фикс {d.fixed_price} ₽ = {base} ₽\n"
        f"Комиссия {s.commission_percent}% = {comm} ₽"
    )


def record_commission(order: Order, driver: DriverProfile, *, on_start: bool = True) -> Optional[CommissionLedger]:
    existing = CommissionLedger.select().where(CommissionLedger.order_id == order.id).first()
    if existing:
        return existing
    amount = commission_amount_for_order(order, driver)
    base = order_base_fare(order, driver)
    now = datetime.now(timezone.utc)
    row = CommissionLedger.create(
        order=order,
        driver=driver,
        amount=amount,
        base_fare=base,
        charged_on_start=on_start,
        created_at=now,
    )
    new_balance = Decimal(str(driver.balance)) + amount
    DriverProfile.update(balance=new_balance).where(DriverProfile.id == driver.id).execute()
    driver = DriverProfile.get_by_id(driver.id)
    from app.services.debt_service import apply_debt_block_if_needed

    if apply_debt_block_if_needed(driver):
        driver = DriverProfile.get_by_id(driver.id)
    return row
