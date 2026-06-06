import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from app.models import Direction, Order, OrderStatus, User
from app.services.boarding_credentials import send_passenger_trip_ticket


def test_trip_ticket_without_driver_not_sent():
    d = Direction.create(
        from_label="A", to_label="B", estimated_time_min=120, price_per_seat=Decimal("100")
    )
    pu = User.create(telegram_id=920010, role="passenger")
    order = Order.create(
        direction=d,
        passenger=pu,
        from_location="A",
        to_location="B",
        seats=1,
        phone="1",
        status=OrderStatus.NEW.value,
        confirmation_code_hash="h",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    ok = asyncio.run(send_passenger_trip_ticket(bot, order, direction=d))
    assert ok is False
    bot.send_message.assert_not_called()
    bot.send_photo.assert_not_called()
