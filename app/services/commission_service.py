from decimal import Decimal
from datetime import datetime, timezone

from app.config import get_settings
from app.models import Direction, Order, DriverProfile, CommissionLedger


def order_base_fare(order: Order) -> Decimal:
    d: Direction = order.direction  # type: ignore
    per = Decimal(str(d.price_per_seat)) * order.seats
    fixed = Decimal(str(d.fixed_price))
    return per + fixed


def commission_amount_for_order(order: Order) -> Decimal:
    base = order_base_fare(order)
    pct = Decimal(get_settings().commission_percent) / Decimal(100)
    return (base * pct).quantize(Decimal("0.01"))


def record_commission(order: Order, driver: DriverProfile) -> CommissionLedger:
    amount = commission_amount_for_order(order)
    base = order_base_fare(order)
    now = datetime.now(timezone.utc)
    row = CommissionLedger.create(
        order=order,
        driver=driver,
        amount=amount,
        base_fare=base,
        created_at=now,
    )
    new_balance = Decimal(str(driver.balance)) + amount
    DriverProfile.update(balance=new_balance).where(DriverProfile.id == driver.id).execute()
    return row
