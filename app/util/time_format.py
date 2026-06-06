"""Display and parse trip duration as hours (stored as minutes in DB)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Union

DATETIME_DISPLAY_FMT = "%d.%m.%Y %H:%M"
DATE_DISPLAY_FMT = "%d.%m.%Y"
DATETIME_DISPLAY_HINT = (
    "ДД.ММ.ГГГГ ЧЧ:ММ (например 25.05.2026 08:00 или 29.05.2026 08.00)"
)
DATE_DISPLAY_HINT = "ДД.ММ.ГГГГ (например 25.05.2026)"

_DISPLAY_RE_COLON = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})$"
)
_DISPLAY_RE_DOT = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2})\.(\d{2})$"
)
_DISPLAY_RE_FLEX = re.compile(
    r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\s+(\d{1,2})[\s:.,-](\d{1,2})$"
)
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


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


def format_date_display(dt: Optional[Union[datetime, str]]) -> str:
    """Format as ДД.ММ.ГГГГ (UTC)."""
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
    return dt.strftime(DATE_DISPLAY_FMT)


def is_midnight_utc(dt: datetime) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0


def format_departure_label(dt: Optional[Union[datetime, str]]) -> str:
    """Date-only requests show date; scheduled trips show date+time."""
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
    if is_midnight_utc(dt):
        return format_date_display(dt)
    return format_datetime_display(dt)


def parse_date_display(text: str) -> datetime:
    """Parse ДД.ММ.ГГГГ to UTC datetime at 00:00."""
    raw = (text or "").strip().replace(",", ".")
    raw = re.sub(r"\s+", " ", raw).strip(" ;,")
    if not raw:
        raise ValueError(DATE_DISPLAY_HINT)
    m = _DATE_RE.match(raw)
    if m:
        day, month, year = (int(x) for x in m.groups())
        return datetime(year, month, day, 0, 0, tzinfo=timezone.utc)
    try:
        dt = datetime.strptime(raw, DATE_DISPLAY_FMT)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(DATE_DISPLAY_HINT) from None


def parse_datetime_display(text: str) -> datetime:
    """Parse ДД.ММ.ГГГГ ЧЧ:ММ / ДД.ММ.ГГГГ ЧЧ.ММ (or ISO) to UTC datetime."""
    raw = (text or "").strip()
    raw = raw.replace("—", "-").replace("–", "-").replace(",", ".")
    raw = re.sub(r"\s+", " ", raw).strip(" ;,")
    if not raw:
        raise ValueError(DATETIME_DISPLAY_HINT)
    m = _DISPLAY_RE_COLON.match(raw) or _DISPLAY_RE_DOT.match(raw)
    if m:
        day, month, year, hour, minute = (int(x) for x in m.groups())
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    m = _DISPLAY_RE_FLEX.match(raw)
    if m:
        day, month, year, hour, minute = (int(x) for x in m.groups())
        if year < 100:
            year += 2000
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    if _DATE_RE.match(raw):
        return parse_date_display(raw)
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
