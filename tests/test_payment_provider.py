from decimal import Decimal

import pytest

from app.services.payment_provider import (
    build_receipt,
    normalize_receipt_phone,
    StubPaymentProvider,
)


def test_normalize_receipt_phone():
    assert normalize_receipt_phone("+7 (900) 123-45-67") == "79001234567"
    assert normalize_receipt_phone("89001234567") == "79001234567"
    assert normalize_receipt_phone("9001234567") == "79001234567"
    assert normalize_receipt_phone("") is None
    assert normalize_receipt_phone("123") is None


def test_build_receipt_uses_phone(monkeypatch):
    monkeypatch.setenv("RECEIPT_VAT_CODE", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    receipt = build_receipt(
        amount=Decimal("1500.00"),
        currency="RUB",
        description="Заказ #42",
        customer_phone="+7 900 111-22-33",
    )
    assert receipt["customer"]["phone"] == "79001112233"
    assert receipt["items"][0]["amount"]["value"] == "1500.00"
    assert receipt["items"][0]["payment_subject"] == "service"
    get_settings.cache_clear()


def test_build_receipt_fallback_email(monkeypatch):
    monkeypatch.setenv("RECEIPT_FALLBACK_EMAIL", "receipts@example.com")
    from app.config import get_settings

    get_settings.cache_clear()
    receipt = build_receipt(
        amount=Decimal("100.00"),
        currency="RUB",
        description="Комиссия",
    )
    assert receipt["customer"]["email"] == "receipts@example.com"
    get_settings.cache_clear()


def test_build_receipt_requires_contact(monkeypatch):
    monkeypatch.setenv("RECEIPT_FALLBACK_EMAIL", "")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(ValueError, match="receipt_customer_contact_required"):
        build_receipt(
            amount=Decimal("100.00"),
            currency="RUB",
            description="Test",
        )
    get_settings.cache_clear()


def test_stub_provider_without_yookassa_credentials(monkeypatch):
    monkeypatch.setenv("SHOP_ID", "")
    monkeypatch.setenv("SHOP_SECRET_KEY", "")
    from app.config import get_settings
    from app.services.payment_provider import get_payment_provider

    get_settings.cache_clear()
    prov = get_payment_provider()
    assert isinstance(prov, StubPaymentProvider)
    result = prov.create_payment(amount=Decimal("100"))
    assert result["status"] == "stub"
    assert result["payment_id"].startswith("stub-")
    get_settings.cache_clear()
