import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import (
    AssignmentStatus,
    Direction,
    DriverProfile,
    Order,
    OrderDriverAssignment,
    OrderStatus,
    User,
)
from app.services.boarding_credentials import send_passenger_trip_ticket


def test_trip_ticket_idempotent():
    d = Direction.create(
        from_label="A", to_label="B", estimated_time_min=120, price_per_seat=Decimal("100")
    )
    pu = User.create(telegram_id=920001, role="passenger")
    du = User.create(telegram_id=920002, role="driver")
    drv = DriverProfile.create(
        user=du, direction=d, max_seats=6, status="active", full_name="Иван", car_info="Toyota"
    )
    order = Order.create(
        direction=d,
        passenger=pu,
        from_location="A",
        to_location="B",
        seats=1,
        phone="1",
        status=OrderStatus.ASSIGNED.value,
        confirmation_code_hash="h",
    )
    OrderDriverAssignment.create(
        order=order, driver=drv, status=AssignmentStatus.ACCEPTED.value
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="testbot"))

    async def _run():
        with patch(
            "app.services.photo_service.car_photo_file_ids_for_driver",
            return_value=[],
        ):
            ok1 = await send_passenger_trip_ticket(bot, order, driver=drv, direction=d)
            ok2 = await send_passenger_trip_ticket(
                bot, Order.get_by_id(order.id), driver=drv, direction=d
            )
        return ok1, ok2

    ok1, ok2 = asyncio.run(_run())
    assert ok1 is True
    assert ok2 is False
    assert bot.send_photo.call_count == 1
