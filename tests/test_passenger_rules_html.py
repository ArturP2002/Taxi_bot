from app.bot.messages import passenger_rules_html


def test_passenger_rules_use_blockquotes():
    text = passenger_rules_html()
    assert text.count("<blockquote>") == 2
    assert "Правила пассажира" in text
    assert "QR-код" in text
    assert "❗" in text
