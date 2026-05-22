"""Map API error codes to Russian messages for admin UI and bot."""
from __future__ import annotations

ADMIN_ERROR_LABELS: dict[str, str] = {
    "direction_mismatch": (
        "Водитель привязан к другому направлению маршрута. "
        "Нажмите «Другой» или «Назначить вручную»."
    ),
    "capacity_exceeded": "У водителя недостаточно свободных мест для этого заказа.",
    "driver_offline": "Водитель не в сети. Дождитесь онлайна или назначьте другого.",
    "no_suggestion": "Нет активного предложения системы. Обновите список заказов.",
    "no_suggestion_after_mismatch": (
        "Предложение устарело (сменилось направление водителя). "
        "Назначьте водителя вручную."
    ),
    "direction_required": "Выберите направление при подтверждении водителя.",
}


def admin_error_message(code: str, *, fallback: str | None = None) -> str:
    return ADMIN_ERROR_LABELS.get(code, fallback or code)
