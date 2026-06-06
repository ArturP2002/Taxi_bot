from app.models import PassengerProfile, User


def test_passenger_consent_gate():
    u = User.create(telegram_id=910001, role="passenger")
    profile, _ = PassengerProfile.get_or_create(user=u)
    assert profile.terms_accepted_at is None
    assert profile.privacy_accepted_at is None

    from app.util.datetimeutil import utcnow

    now = utcnow()
    profile.terms_accepted_at = now
    profile.privacy_accepted_at = now
    profile.save()
    profile = PassengerProfile.get_by_id(profile.id)
    assert profile.terms_accepted_at is not None
    assert profile.privacy_accepted_at is not None
