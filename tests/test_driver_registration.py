from decimal import Decimal

from app.models import DriverProfile, User
from app.services import driver_registration as reg


def test_draft_route_roundtrip():
    u = User.create(telegram_id=900001, role="driver")
    d = DriverProfile.create(user=u, status="pending")
    reg.save_draft_route_from(d, "Москва")
    d = DriverProfile.get_by_id(d.id)
    reg.save_draft_route_to(d, "Тбилиси")
    d = DriverProfile.get_by_id(d.id)
    reg.save_draft_return_choice(d, "Тбилиси", True)
    d = DriverProfile.get_by_id(d.id)
    fr, to, inc = reg.parse_draft_route(d)
    assert fr == "Москва"
    assert to == "Тбилиси"
    assert inc is True
    assert reg.draft_route_label(d) == "Москва → Тбилиси"


def test_merge_registration_data_from_db():
    u = User.create(telegram_id=900002, role="driver")
    d = DriverProfile.create(
        user=u,
        status="pending",
        full_name="Иван Иванов",
        current_city="Москва",
        tariff_note="Тбилиси|1",
    )
    merged = reg.merge_registration_data(d, {})
    assert merged["route_from"] == "Москва"
    assert merged["route_to"] == "Тбилиси"
    assert merged["include_return"] is True
    assert merged["full_name"] == "Иван Иванов"
