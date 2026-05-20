"""Route proposals reserve groups (3+ unique drivers)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Tuple

from aiogram import Bot

from app.config import get_settings
from app.db import get_db
from app.models import (
    DriverProfile,
    ProposedDirection,
    ProposedStatus,
    RouteReserveGroup,
    ReserveGroupStatus,
)
from app.services.route_labels import normalize_route_label

logger = logging.getLogger("taxi_bot.reserve")


def route_key(from_label: str, to_label: str) -> str:
    return f"{normalize_route_label(from_label)}|{normalize_route_label(to_label)}"


def reserve_schema_ok() -> bool:
    """True if reserve tables/columns exist (migration v5 applied)."""
    try:
        db = get_db()
        db.execute_sql("SELECT 1 FROM route_reserve_groups LIMIT 1")
        cur = db.execute_sql("PRAGMA table_info(proposed_directions)")
        cols = {row[1] for row in cur.fetchall()}
        return "reserve_group_id" in cols
    except Exception:
        return False


def get_or_create_group(from_label: str, to_label: str) -> RouteReserveGroup:
    key = route_key(from_label, to_label)
    grp = (
        RouteReserveGroup.select()
        .where(
            (RouteReserveGroup.route_key == key)
            & (RouteReserveGroup.status == ReserveGroupStatus.COLLECTING.value)
        )
        .first()
    )
    if grp:
        return grp
    return RouteReserveGroup.create(
        route_key=key,
        from_label=from_label.strip(),
        to_label=to_label.strip(),
        status=ReserveGroupStatus.COLLECTING.value,
    )


def unique_proposers_in_group(group_id: int) -> List[DriverProfile]:
    seen: set[int] = set()
    drivers: List[DriverProfile] = []
    rows = (
        ProposedDirection.select()
        .where(
            (ProposedDirection.reserve_group_id == group_id)
            & (
                ProposedDirection.status.in_(
                    [
                        ProposedStatus.PENDING.value,
                        ProposedStatus.RESERVED.value,
                    ]
                )
            )
        )
        .order_by(ProposedDirection.created_at)
    )
    for p in rows:
        if p.proposer_id in seen:
            continue
        seen.add(p.proposer_id)
        drivers.append(DriverProfile.get_by_id(p.proposer_id))
    return drivers


def proposer_position_in_group(group_id: int, driver_id: int) -> int:
    drivers = unique_proposers_in_group(group_id)
    for i, d in enumerate(drivers, start=1):
        if d.id == driver_id:
            return i
    return len(drivers) + 1


def add_proposal_to_reserve(
    proposal: ProposedDirection,
) -> Tuple[RouteReserveGroup, int, bool]:
    """Attach proposal to reserve group. Returns (group, position, activated)."""
    grp = get_or_create_group(proposal.from_label, proposal.to_label)
    ProposedDirection.update(
        reserve_group=grp,
        status=ProposedStatus.RESERVED.value,
    ).where(ProposedDirection.id == proposal.id).execute()

    drivers = unique_proposers_in_group(grp.id)
    pos = proposer_position_in_group(grp.id, proposal.proposer_id)
    settings = get_settings()
    activated = False
    if len(drivers) >= settings.route_reserve_min_drivers:
        activated = try_activate_group(grp.id)
    return grp, pos, activated


def try_activate_group(group_id: int) -> bool:
    grp = RouteReserveGroup.get_by_id(group_id)
    if grp.status != ReserveGroupStatus.COLLECTING.value:
        return False
    drivers = unique_proposers_in_group(group_id)
    settings = get_settings()
    if len(drivers) < settings.route_reserve_min_drivers:
        return False

    lead = (
        ProposedDirection.select()
        .where(
            (ProposedDirection.reserve_group_id == group_id)
            & (
                ProposedDirection.status.in_(
                    [
                        ProposedStatus.PENDING.value,
                        ProposedStatus.RESERVED.value,
                    ]
                )
            )
        )
        .order_by(ProposedDirection.created_at)
        .first()
    )
    if not lead:
        return False

    from app.services import proposed_service

    try:
        proposed_service.approve_proposal(lead, actor_telegram_id=0)
    except ValueError as e:
        logger.warning("auto activate reserve failed: %s", e)
        return False

    now = datetime.now(timezone.utc)
    RouteReserveGroup.update(
        status=ReserveGroupStatus.ACTIVATED.value,
        activated_at=now,
    ).where(RouteReserveGroup.id == group_id).execute()
    return True


async def notify_reserve_status(
    bot: Bot,
    proposal: ProposedDirection,
    *,
    position: int,
    total: int,
    activated: bool,
) -> None:
    from app.services.admin_notify import notify_proposal_decision, notify_proposal_reserved
    from app.services import queue_service

    route = f"{proposal.from_label} → {proposal.to_label}"
    drv = proposal.proposer
    tid = drv.user.telegram_id
    settings = get_settings()

    if activated:
        d = proposal.created_direction
        if d:
            qpos = None
            from app.models import QueueEntry

            qe = (
                QueueEntry.select()
                .where(
                    (QueueEntry.direction_id == d.id)
                    & (QueueEntry.driver_id == drv.id)
                )
                .first()
            )
            if qe:
                qpos = qe.position
            await notify_proposal_decision(
                bot, tid, approved=True, route=route, queue_position=qpos
            )
    else:
        await notify_proposal_reserved(
            bot,
            tid,
            route=route,
            position=position,
            total_drivers=total,
            needed=settings.route_reserve_min_drivers,
        )


def create_paired_proposals_pending(
    proposer: DriverProfile,
    from_label: str,
    to_label: str,
    **kwargs,
) -> Tuple[List[ProposedDirection], int, bool]:
    """Create paired proposals without reserve (visible in admin as pending)."""
    from app.services.direction_pairs import create_paired_proposals

    created = create_paired_proposals(proposer, from_label, to_label, **kwargs)
    return created, 1, False


def create_reserved_paired_proposals(
    proposer: DriverProfile,
    from_label: str,
    to_label: str,
    **kwargs,
) -> Tuple[List[ProposedDirection], int, bool]:
    """Like create_paired_proposals but through reserve flow; falls back to pending on error."""
    if not reserve_schema_ok():
        logger.warning("reserve schema missing — creating pending proposals only")
        return create_paired_proposals_pending(proposer, from_label, to_label, **kwargs)

    from app.services.direction_pairs import create_paired_proposals

    existing = (
        ProposedDirection.select()
        .where(
            (ProposedDirection.proposer_id == proposer.id)
            & (ProposedDirection.status.in_(
                [ProposedStatus.PENDING.value, ProposedStatus.RESERVED.value]
            ))
        )
    )
    nf, nt = normalize_route_label(from_label), normalize_route_label(to_label)
    for p in existing:
        if (
            normalize_route_label(p.from_label) == nf
            and normalize_route_label(p.to_label) == nt
        ):
            ProposedDirection.update(
                estimated_time_min=kwargs.get("estimated_time_min", p.estimated_time_min),
                comment=kwargs.get("comment", p.comment),
            ).where(ProposedDirection.id == p.id).execute()
            try:
                grp, pos, activated = add_proposal_to_reserve(
                    ProposedDirection.get_by_id(p.id)
                )
                return [ProposedDirection.get_by_id(p.id)], pos, activated
            except Exception as e:
                logger.exception("add_proposal_to_reserve failed: %s", e)
                ProposedDirection.update(
                    status=ProposedStatus.PENDING.value,
                    reserve_group=None,
                ).where(ProposedDirection.id == p.id).execute()
                return [ProposedDirection.get_by_id(p.id)], 1, False

    try:
        created = create_paired_proposals(proposer, from_label, to_label, **kwargs)
        out: List[ProposedDirection] = []
        pos, activated = 1, False
        for p in created:
            if (
                normalize_route_label(p.from_label) == nf
                and normalize_route_label(p.to_label) == nt
            ):
                try:
                    _, pos, activated = add_proposal_to_reserve(
                        ProposedDirection.get_by_id(p.id)
                    )
                    out.append(ProposedDirection.get_by_id(p.id))
                except Exception as e:
                    logger.exception("reserve attach failed for proposal %s: %s", p.id, e)
                    out.append(p)
            else:
                out.append(p)
        return out, pos, activated
    except Exception as e:
        logger.exception("create_reserved_paired_proposals failed: %s", e)
        return create_paired_proposals_pending(proposer, from_label, to_label, **kwargs)
