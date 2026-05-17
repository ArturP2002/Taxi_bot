from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.models import Direction, DriverProfile, ProposedDirection, ProposedStatus, DirectionPioneer
from app.services import audit_service, queue_service, direction_pairs


def normalize_route_label(label: str) -> str:
    return " ".join(label.strip().lower().split())


def approve_proposal(
    proposal: ProposedDirection,
    *,
    actor_telegram_id: int,
    reverse_direction_id: Optional[int] = None,
) -> Direction:
    if proposal.status != ProposedStatus.PENDING.value:
        raise ValueError("not_pending")
    now = datetime.now(timezone.utc)
    price_seat = Decimal(str(getattr(proposal, "price_per_seat", 0) or 0))
    fixed = Decimal(str(getattr(proposal, "fixed_price", 0) or 0))
    max_seats = int(getattr(proposal, "max_seats", 6) or 6)

    d = Direction.create(
        from_label=proposal.from_label,
        to_label=proposal.to_label,
        estimated_time_min=proposal.estimated_time_min,
        min_time_percent=70,
        enabled=True,
        price_per_seat=price_seat,
        fixed_price=fixed,
        vehicle_capacity_default=max_seats,
        reverse_direction_id=reverse_direction_id,
    )
    if reverse_direction_id:
        Direction.update(reverse_direction_id=d.id).where(Direction.id == reverse_direction_id).execute()

    nf = normalize_route_label(proposal.from_label)
    nt = normalize_route_label(proposal.to_label)
    others = list(
        ProposedDirection.select()
        .where(ProposedDirection.status == ProposedStatus.PENDING.value)
        .order_by(ProposedDirection.created_at)
    )
    merged: list[ProposedDirection] = [proposal]
    for o in others:
        if o.id == proposal.id:
            continue
        if normalize_route_label(o.from_label) == nf and normalize_route_label(o.to_label) == nt:
            merged.append(o)

    pioneers: list[DriverProfile] = []
    for p in merged:
        proposer = p.proposer
        DriverProfile.update(
            direction=d,
            is_primary_on_direction=True,
            max_seats=int(getattr(p, "max_seats", 6) or 6),
            own_seats_reserved=int(getattr(p, "own_seats", 0) or 0),
        ).where(DriverProfile.id == proposer.id).execute()
        DirectionPioneer.get_or_create(direction=d, driver=proposer)
        pioneers.append(DriverProfile.get_by_id(proposer.id))
        ProposedDirection.update(
            status=ProposedStatus.APPROVED.value,
            created_direction=d,
            resolved_at=now,
            admin_note="merged_with_same_route" if p.id != proposal.id else None,
        ).where(ProposedDirection.id == p.id).execute()

    queue_service.enqueue_pioneers_in_order(d, pioneers)

    # Пара туда/обратно: создать обратное направление и очередь на нём
    rev, rev_pioneers = direction_pairs.setup_reverse_after_forward_approve(d, pioneers, now=now)
    if rev_pioneers:
        queue_service.enqueue_pioneers_in_order(rev, rev_pioneers)

    audit_service.log_action(
        "proposed_direction_approved",
        actor_telegram_id=actor_telegram_id,
        entity_type="direction",
        entity_id=str(d.id),
        payload={
            "proposal_id": proposal.id,
            "pioneers": [p.id for p in pioneers],
            "reverse_direction_id": rev.id,
            "reverse_pioneers": [p.id for p in rev_pioneers],
        },
    )
    return d


def reject_proposal(proposal: ProposedDirection, *, actor_telegram_id: int, note: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc)
    ProposedDirection.update(
        status=ProposedStatus.REJECTED.value,
        resolved_at=now,
        admin_note=note,
    ).where(ProposedDirection.id == proposal.id).execute()
    audit_service.log_action(
        "proposed_direction_rejected",
        actor_telegram_id=actor_telegram_id,
        entity_type="proposal",
        entity_id=str(proposal.id),
    )
