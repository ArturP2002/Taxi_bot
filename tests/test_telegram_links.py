from app.util.telegram_links import telegram_open_url, user_chat_url


def test_user_chat_url_by_id():
    assert user_chat_url(1039942647) == "tg://user?id=1039942647"


def test_telegram_open_url_prefers_username():
    assert telegram_open_url(telegram_id=1, username="ArturP2002") == "https://t.me/ArturP2002"
    assert telegram_open_url(telegram_id=1, username="@ArturP2002") == "https://t.me/ArturP2002"


def test_telegram_open_url_falls_back_to_id():
    assert telegram_open_url(telegram_id=1039942647, username=None) == "tg://user?id=1039942647"
    assert telegram_open_url(telegram_id=1039942647, username="") == "tg://user?id=1039942647"
