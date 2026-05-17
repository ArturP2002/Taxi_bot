"""Paired directions (e.g. Москва→Тбилиси and Тбилиси→Москва)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from app.models import Direction, DriverProfile, ProposedDirection, ProposedStatus, DirectionPioneer
from app.services.route_labels import normalize_route_label


@dataclass
class DirectionGroup:
    """Forward leg and optional reverse leg shown together in UI."""

    forward: Direction
    reverse: Optional[Direction] = None


def find_direction_by_labels(from_label: str, to_label: str, *, enabled_only: bool = True) -> Optional[Direction]:
    nf, nt = normalize_route_label(from_label), normalize_route_label(to_label)
    for d in Direction.select():
        if enabled_only and not d.enabled:
            continue
        if normalize_route_label(d.from_label) == nf and normalize_route_label(d.to_label) == nt:
            return d
    return None


def get_reverse_direction(direction: Direction) -> Optional[Direction]:
    if direction.reverse_direction_id:
        try:
            rev = Direction.get_by_id(direction.reverse_direction_id)
            return rev if rev.enabled else None
        except Direction.DoesNotExist:
            pass
    back = (
        Direction.select()
        .where(
            (Direction.reverse_direction_id == direction.id)
            & (Direction.enabled == True)  # noqa: E712
        )
        .first()
    )
    return back


def link_direction_pair(a: Direction, b: Direction) -> None:
    """Bidirectional link between two directions."""
    if a.reverse_direction_id != b.id:
        Direction.update(reverse_direction_id=b.id).where(Direction.id == a.id).execute()
    if b.reverse_direction_id != a.id:
        Direction.update(reverse_direction_id=a.id).where(Direction.id == b.id).execute()


def ensure_reverse_direction(
    forward: Direction,
    *,
    estimated_time_min: Optional[int] = None,
) -> Direction:
    """Create or return the reverse leg for *forward*."""
    existing = get_reverse_direction(forward)
    if existing:
        link_direction_pair(forward, existing)
        return existing

    by_labels = find_direction_by_labels(forward.to_label, forward.from_label, enabled_only=False)
    if by_labels:
        if not by_labels.enabled:
            Direction.update(enabled=True).where(Direction.id == by_labels.id).execute()
            by_labels = Direction.get_by_id(by_labels.id)
        link_direction_pair(forward, by_labels)
        return by_labels

    eta = estimated_time_min if estimated_time_min is not None else forward.estimated_time_min
    rev = Direction.create(
        from_label=forward.to_label,
        to_label=forward.from_label,
        estimated_time_min=eta,
        min_time_percent=forward.min_time_percent,
        enabled=True,
        price_per_seat=forward.price_per_seat,
        fixed_price=forward.fixed_price,
        vehicle_capacity_default=forward.vehicle_capacity_default,
        online_payment_required=getattr(forward, "online_payment_required", False),
        reverse_direction_id=forward.id,
    )
    Direction.update(reverse_direction_id=rev.id).where(Direction.id == forward.id).execute()
    return rev


def pending_reverse_proposals(for_label: str, to_label: str) -> List[ProposedDirection]:
    """Pending proposals for the opposite leg (to→from)."""
    nf, nt = normalize_route_label(to_label), normalize_route_label(for_label)
    out: List[ProposedDirection] = []
    for p in ProposedDirection.select().where(ProposedDirection.status == ProposedStatus.PENDING.value):
        if normalize_route_label(p.from_label) == nf and normalize_route_label(p.to_label) == nt:
            out.append(p)
    out.sort(key=lambda x: x.created_at or datetime.min)
    return out


def build_reverse_pioneer_order(
    forward_pioneers: List[DriverProfile],
    reverse_proposals: List[ProposedDirection],
) -> List[DriverProfile]:
    """Reverse queue: те же водители что туда (в том же порядке) + кто подал только обратно."""
    forward_ids = {d.id for d in forward_pioneers}
    ordered: List[DriverProfile] = list(forward_pioneers)
    for p in sorted(reverse_proposals, key=lambda x: x.created_at or datetime.min):
        if p.proposer_id not in forward_ids:
            ordered.append(DriverProfile.get_by_id(p.proposer_id))
            forward_ids.add(p.proposer_id)
    return ordered


def setup_reverse_after_forward_approve(
    forward: Direction,
    forward_pioneers: List[DriverProfile],
    *,
    now,
) -> Tuple[Direction, List[DriverProfile]]:
    """Create/link reverse direction, merge reverse proposals, return reverse + pioneers."""
    rev = ensure_reverse_direction(forward)
    rev_props = pending_reverse_proposals(forward.from_label, forward.to_label)
    rev_pioneers = build_reverse_pioneer_order(forward_pioneers, rev_props)

    for p in rev_props:
        proposer = p.proposer
        DriverProfile.update(
            direction=rev,
            is_primary_on_direction=True,
            max_seats=int(getattr(p, "max_seats", 6) or 6),
            own_seats_reserved=int(getattr(p, "own_seats", 0) or 0),
        ).where(DriverProfile.id == proposer.id).execute()
        DirectionPioneer.get_or_create(direction=rev, driver=proposer)
        ProposedDirection.update(
            status=ProposedStatus.APPROVED.value,
            created_direction=rev,
            resolved_at=now,
            admin_note="merged_with_reverse_pair",
        ).where(ProposedDirection.id == p.id).execute()

    for d in forward_pioneers:
        DirectionPioneer.get_or_create(direction=rev, driver=d)

    return rev, rev_pioneers


def build_direction_groups(directions: List[Direction]) -> List[DirectionGroup]:
    """Group enabled directions so туда/обратно appear as one block."""
    by_id = {d.id: d for d in directions}
    seen: set[int] = set()
    groups: List[DirectionGroup] = []

    for d in sorted(directions, key=lambda x: x.id):
        if d.id in seen:
            continue
        rev = get_reverse_direction(d) if d.reverse_direction_id else None
        if rev and rev.id in by_id:
            seen.add(d.id)
            seen.add(rev.id)
            groups.append(DirectionGroup(forward=d, reverse=rev))
            continue
        # Orphan: skip if another direction already claims us as its reverse
        claimed = any(
            other.reverse_direction_id == d.id
            for other in directions
            if other.id != d.id and other.id not in seen
        )
        if claimed:
            continue
        seen.add(d.id)
        groups.append(DirectionGroup(forward=d, reverse=None))

    return groups


def flatten_groups_for_search(groups: List[DirectionGroup]) -> List[Direction]:
    out: List[Direction] = []
    for g in groups:
        out.append(g.forward)
        if g.reverse:
            out.append(g.reverse)
    return out


def search_direction_groups(query: str, limit: int = 50) -> List[DirectionGroup]:
    from app.services.direction_search import search_directions

    matched = search_directions(query, limit=limit * 2)
    all_enabled = list_enabled_grouped()
    matched_ids = {d.id for d in matched}
    return [g for g in all_enabled if g.forward.id in matched_ids or (g.reverse and g.reverse.id in matched_ids)][:limit]


def list_enabled_grouped() -> List[DirectionGroup]:
    enabled = list(Direction.select().where(Direction.enabled == True).order_by(Direction.id))  # noqa: E712
    return build_direction_groups(enabled)


def paginate_groups(
    groups: List[DirectionGroup], page: int, page_size: int
) -> Tuple[List[DirectionGroup], int, int]:
    total = len(groups)
    if total == 0:
        return [], 0, 0
    pages = (total + page_size - 1) // page_size
    page = max(0, min(page, pages - 1))
    start = page * page_size
    return groups[start : start + page_size], page, pages


def create_paired_proposals(
    proposer: DriverProfile,
    from_label: str,
    to_label: str,
    *,
    estimated_time_min: int = 120,
    max_seats: int = 6,
    own_seats: int = 0,
    price_per_seat: Decimal = Decimal("0"),
    fixed_price: Decimal = Decimal("0"),
    comment: Optional[str] = None,
    include_return: bool = True,
) -> List[ProposedDirection]:
    """Create forward proposal and optionally matching return proposal."""
    created: List[ProposedDirection] = []
    p1 = ProposedDirection.create(
        proposer=proposer,
        from_label=from_label,
        to_label=to_label,
        estimated_time_min=estimated_time_min,
        max_seats=max_seats,
        own_seats=own_seats,
        price_per_seat=price_per_seat,
        fixed_price=fixed_price,
        comment=comment,
        status=ProposedStatus.PENDING.value,
    )
    created.append(p1)
    if include_return:
        p2 = ProposedDirection.create(
            proposer=proposer,
            from_label=to_label,
            to_label=from_label,
            estimated_time_min=estimated_time_min,
            max_seats=max_seats,
            own_seats=own_seats,
            price_per_seat=price_per_seat,
            fixed_price=fixed_price,
            comment=(comment or "") + " [обратный рейс]" if comment else "Обратный рейс",
            status=ProposedStatus.PENDING.value,
        )
        created.append(p2)
    return created
