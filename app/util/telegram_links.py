"""Telegram deep links for opening a private chat by user id or username."""


def user_chat_url(telegram_id: int) -> str:
    return f"tg://user?id={telegram_id}"


def telegram_open_url(*, telegram_id: int, username: str | None = None) -> str:
    """Prefer @username link when available; numeric id otherwise."""
    if username:
        uname = str(username).strip().lstrip("@")
        if uname:
            return f"https://t.me/{uname}"
    return f"tg://user?id={telegram_id}"
