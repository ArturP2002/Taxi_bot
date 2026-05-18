from app.models import DriverProfile, User, DriverStatus, DriverEventType
from app.services import driver_risk_service as risk


def test_mark_suspicious_on_declines():
    u = User.create(telegram_id=880001, role="driver")
    d = DriverProfile.create(user=u, status=DriverStatus.ACTIVE.value)
    for _ in range(5):
        risk.record_event(d.id, DriverEventType.DECLINE.value)
    risk.evaluate_driver(DriverProfile.get_by_id(d.id))
    d = DriverProfile.get_by_id(d.id)
    assert d.status == DriverStatus.SUSPICIOUS.value
    stats = risk.driver_risk_stats(d.id)
    assert stats["declines"] == 5
