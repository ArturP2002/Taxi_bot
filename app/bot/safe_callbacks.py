"""Safe parsing of Telegram callback_data payloads."""
from __future__ import annotations

from typing import Optional


def parse_callback_int(data: str | None, index: int = 1) -> Optional[int]:
    """Return int at split index or None if payload is malformed."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except (TypeError, ValueError):
        return None
