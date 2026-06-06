from app.services import driver_registration as reg


def test_registration_intro_without_step_number():
    text = reg.prompt_registration_intro()
    assert "шаг 1 из" not in text
    assert "РЕГИСТРАЦИЯ ВОДИТЕЛЯ" in text
    assert "время в пути" in text
    assert "<blockquote>" in text
    assert "Маршрут → ФИО и авто" in text


def test_route_from_prompt_has_step_number():
    text = reg.prompt_route_from(step=1)
    assert "шаг 1 из" in text
    assert "город" in text.lower()
