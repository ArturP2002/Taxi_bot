from app.bot import preview as preview_flow
from app.models import Direction, User


def test_passenger_order_preview_text():
    u = User.create(telegram_id=900100, role="passenger")
    d = Direction.create(from_label="Москва", to_label="Самара", estimated_time_min=60)
    data = {
        "from_location": "Москва",
        "to_location": "Самара",
        "seats": 2,
        "phone": "+79991234567",
        "wants_pickup": True,
        "wants_dropoff": False,
    }
    text = preview_flow.format_passenger_order_preview(data, d)
    assert "Предпросмотр заявки" in text
    assert "Москва → Самара" in text
    assert "2" in text
    assert "Забрать меня" in text


def test_driver_registration_preview_text():
    data = {
        "route_from": "Москва",
        "route_to": "Самара",
        "include_return": True,
        "full_name": "Иван",
        "car_info": "Kia Rio",
        "phone": "+7999",
        "max_seats": 4,
        "price_per_seat": "5000",
        "fixed_price": "1000",
    }
    text = preview_flow.format_driver_registration_preview(data)
    assert "Предпросмотр анкеты" in text
    assert "Москва → Самара" in text
    assert "Иван" in text
    assert "5000" in text
