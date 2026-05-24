from datetime import datetime, timezone

from app.util.time_format import format_datetime_display, parse_datetime_display


def test_format_and_parse_display_datetime():
    dt = datetime(2026, 5, 25, 8, 30, tzinfo=timezone.utc)
    assert format_datetime_display(dt) == "25.05.2026 08:30"
    parsed = parse_datetime_display("25.05.2026 08:30")
    assert parsed.year == 2026
    assert parsed.hour == 8
    assert parsed.minute == 30


def test_parse_display_datetime_flexible_digits():
    parsed = parse_datetime_display("5.5.2026 8:05")
    assert parsed == datetime(2026, 5, 5, 8, 5, tzinfo=timezone.utc)
