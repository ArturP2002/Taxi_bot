"""Telegram deep links for opening a private chat by user id."""


def user_chat_url(telegram_id: int) -> str:
    return f"tg://user?id={telegram_id}"
