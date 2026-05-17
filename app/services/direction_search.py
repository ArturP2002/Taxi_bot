"""Search and paginate enabled directions."""
from __future__ import annotations

from typing import List, Tuple

from app.models import Direction
from app.services.direction_pairs import (
    DirectionGroup,
    list_enabled_grouped,
    paginate_groups,
    search_direction_groups,
)


def normalize_query(q: str) -> str:
    return " ".join(q.strip().lower().split())


def score_direction(d: Direction, query: str) -> int:
    if not query:
        return 0
    from_l = d.from_label.lower()
    to_l = d.to_label.lower()
    route = f"{from_l} {to_l}"
    score = 0
    if query in route:
        score += 10
    if query in from_l or query in to_l:
        score += 5
    for part in query.split():
        if part in from_l or part in to_l:
            score += 2
    return score


def list_enabled_directions() -> List[Direction]:
    return list(Direction.select().where(Direction.enabled == True).order_by(Direction.id))  # noqa: E712


def search_directions(query: str, limit: int = 50) -> List[Direction]:
    q = normalize_query(query)
    if not q:
        return list_enabled_directions()[:limit]
    scored: List[Tuple[int, Direction]] = []
    for d in list_enabled_directions():
        s = score_direction(d, q)
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    return [d for _, d in scored[:limit]]


def paginate_directions(
    directions: List[Direction], page: int, page_size: int
) -> Tuple[List[Direction], int, int]:
    total = len(directions)
    if total == 0:
        return [], 0, 0
    pages = (total + page_size - 1) // page_size
    page = max(0, min(page, pages - 1))
    start = page * page_size
    return directions[start : start + page_size], page, pages


def get_groups_for_browse(
    *, search_results: List[Direction] | None = None, page: int, page_size: int
) -> Tuple[List[DirectionGroup], int, int]:
    if search_results is not None:
        from app.services.direction_pairs import build_direction_groups

        groups = build_direction_groups(search_results)
    else:
        groups = list_enabled_grouped()
    return paginate_groups(groups, page, page_size)


def search_groups(query: str, limit: int = 50) -> List[DirectionGroup]:
    return search_direction_groups(query, limit=limit)
