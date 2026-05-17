from decimal import Decimal

from app.models import Direction, User, DriverProfile, ProposedDirection, ProposedStatus
from app.services import direction_pairs, proposed_service


def test_ensure_reverse_and_group():
    a = Direction.create(
        from_label="Москва",
        to_label="Тбилиси",
        estimated_time_min=120,
        price_per_seat=Decimal("100"),
    )
    rev = direction_pairs.ensure_reverse_direction(a)
    assert rev.from_label == "Тбилиси"
    assert rev.to_label == "Москва"
    a = Direction.get_by_id(a.id)
    assert a.reverse_direction_id == rev.id

    groups = direction_pairs.build_direction_groups([a, rev])
    assert len(groups) == 1
    assert groups[0].reverse is not None


def test_paired_proposals_and_approve():
    u = User.create(telegram_id=99, role="driver")
    dprof = DriverProfile.create(user=u, max_seats=6, status="active")
    props = direction_pairs.create_paired_proposals(
        dprof, "Москва", "Тбилиси", include_return=True
    )
    assert len(props) == 2
    d = proposed_service.approve_proposal(props[0], actor_telegram_id=1)
    rev = direction_pairs.get_reverse_direction(d)
    assert rev is not None
    pending = ProposedDirection.get_by_id(props[1].id)
    assert pending.status == ProposedStatus.APPROVED.value
