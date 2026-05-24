"""Display and parse trip duration as hours (stored as minutes in DB)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Union

DATETIME_DISPLAY_FMT = "%d.%m.%Y %H:%M"
DATETIME_DISPLAY_HINT = "ДД.ММ.ГГГГ ЧЧ:ММ (например 25.05.2026 08:00)"

_DISPLAY_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})$"
)


def format_datetime_display(dt: Optional[Union[datetime, str]]) -> str:
    """Format as ДД.ММ.ГГГГ ЧЧ:ММ (UTC)."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        raw = dt.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return dt
        dt = parsed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime(DATETIME_DISPLAY_FMT)


def parse_datetime_display(text: str) -> datetime:
    """Parse ДД.ММ.ГГГГ ЧЧ:ММ (или ISO) → UTC datetime."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError(DATETIME_DISPLAY_HINT)
    m = _DISPLAY_RE.match(raw)
    if m:
        day, month, year, hour, minute = (int(x) for x in m.groups())
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    try:
        dt = datetime.strptime(raw, DATETIME_DISPLAY_FMT)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(DATETIME_DISPLAY_HINT) from None


def minutes_to_hours_label(minutes: int) -> str:
    if minutes <= 0:
        return "0 ч"
    h = minutes / 60
    if minutes % 60 == 0:
        return f"{int(h)} ч"
    return f"{h:.1f} ч".replace(".0 ч", " ч")


def parse_hours_input(text: str) -> int:
    """Parse user hours (int or decimal) → minutes for DB."""
    raw = text.strip().replace(",", ".")
    if not raw:
        raise ValueError("empty")
    value = float(raw)
    if value <= 0 or value > 72:
        raise ValueError("range")
    return max(30, int(round(value * 60)))
