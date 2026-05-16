from datetime import datetime, timezone
from typing import Optional

from app.models import Direction, DriverProfile, ProposedDirection, ProposedStatus, DirectionPioneer
from app.services import audit_service


def approve_proposal(
    proposal: ProposedDirection,
    *,
    actor_telegram_id: int,
    reverse_direction_id: Optional[int] = None,
) -> Direction:
    if proposal.status != ProposedStatus.PENDING.value:
        raise ValueError("not_pending")
    now = datetime.now(timezone.utc)
    d = Direction.create(
        from_label=proposal.from_label,
        to_label=proposal.to_label,
        estimated_time_min=proposal.estimated_time_min,
        min_time_percent=70,
        enabled=True,
        price_per_seat=0,
        fixed_price=0,
        vehicle_capacity_default=6,
        reverse_direction_id=reverse_direction_id,
    )
    if reverse_direction_id:
        Direction.update(reverse_direction_id=d.id).where(Direction.id == reverse_direction_id).execute()
    proposer = proposal.proposer
    DriverProfile.update(direction=d, is_primary_on_direction=True).where(DriverProfile.id == proposer.id).execute()
    DirectionPioneer.get_or_create(direction=d, driver=proposer)
    ProposedDirection.update(
        status=ProposedStatus.APPROVED.value,
        created_direction=d,
        resolved_at=now,
    ).where(ProposedDirection.id == proposal.id).execute()

    # Other pending proposals same route -> pioneers
    others = ProposedDirection.select().where(
        (ProposedDirection.from_label == proposal.from_label)
        & (ProposedDirection.to_label == proposal.to_label)
        & (ProposedDirection.status == ProposedStatus.PENDING.value)
        & (ProposedDirection.id != proposal.id)
    )
    for o in others:
        DriverProfile.update(direction=d, is_primary_on_direction=True).where(
            DriverProfile.id == o.proposer_id
        ).execute()
        DirectionPioneer.get_or_create(direction=d, driver=o.proposer)
        ProposedDirection.update(
            status=ProposedStatus.APPROVED.value,
            created_direction=d,
            resolved_at=now,
            admin_note="merged_with_same_route",
        ).where(ProposedDirection.id == o.id).execute()

    audit_service.log_action(
        "proposed_direction_approved",
        actor_telegram_id=actor_telegram_id,
        entity_type="direction",
        entity_id=str(d.id),
        payload={"proposal_id": proposal.id},
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
