"""Display and parse trip duration as hours (stored as minutes in DB)."""


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
