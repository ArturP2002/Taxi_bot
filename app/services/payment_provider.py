"""YooKassa payment provider — create payment + poll status (no webhooks)."""

import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

YOOKASSA_API = "https://api.yookassa.ru/v3"


def normalize_receipt_phone(phone: str | None) -> str | None:
    """Normalize phone to 7XXXXXXXXXX for YooKassa receipt.customer.phone."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if digits.startswith("7") and len(digits) == 11:
        return digits
    return digits if len(digits) >= 10 else None


def build_receipt(
    *,
    amount: Decimal,
    currency: str,
    description: str,
    customer_phone: str | None = None,
    customer_email: str | None = None,
) -> Dict[str, Any]:
    settings = get_settings()
    customer: Dict[str, str] = {}
    phone = normalize_receipt_phone(customer_phone)
    if phone:
        customer["phone"] = phone
    elif customer_email:
        customer["email"] = customer_email.strip()
    elif settings.receipt_fallback_email.strip():
        customer["email"] = settings.receipt_fallback_email.strip()
    else:
        raise ValueError("receipt_customer_contact_required")

    item_amount = str(amount.quantize(Decimal("0.01")))
    return {
        "customer": customer,
        "items": [
            {
                "description": (description or "Услуга")[:128],
                "quantity": "1.00",
                "amount": {"value": item_amount, "currency": currency},
                "vat_code": settings.receipt_vat_code,
                "payment_mode": settings.receipt_payment_mode,
                "payment_subject": settings.receipt_payment_subject,
            }
        ],
    }


class YooKassaProvider:
    """
    Работа с ЮKассой:
    - create_payment: создаёт платёж, возвращает confirmation_url (ссылка на оплату)
    - check_payment: polling статуса по payment_id (без вебхуков)
    """

    def __init__(self, shop_id: str, secret_key: str):
        self._shop_id = shop_id
        self._secret_key = secret_key

    def _auth(self) -> tuple[str, str]:
        return (self._shop_id, self._secret_key)

    def create_payment(
        self,
        *,
        amount: Decimal,
        currency: str = "RUB",
        description: str = "",
        return_url: str = "https://t.me",
        metadata: Optional[Dict[str, Any]] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a YooKassa payment.
        Returns dict: {"payment_id": str, "confirmation_url": str, "status": str}
        """
        idempotency_key = str(uuid.uuid4())
        body: Dict[str, Any] = {
            "amount": {
                "value": str(amount.quantize(Decimal("0.01"))),
                "currency": currency,
            },
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "capture": True,
            "description": description,
        }
        if metadata:
            body["metadata"] = metadata
        try:
            body["receipt"] = build_receipt(
                amount=amount,
                currency=currency,
                description=description,
                customer_phone=customer_phone,
                customer_email=customer_email,
            )
        except ValueError:
            logger.error("YooKassa create_payment: no customer phone/email for receipt")
            raise

        try:
            resp = httpx.post(
                f"{YOOKASSA_API}/payments",
                json=body,
                auth=self._auth(),
                headers={"Idempotence-Key": idempotency_key},
                timeout=15.0,
            )
            if resp.is_error:
                logger.error(
                    "YooKassa create_payment failed: %s %s",
                    resp.status_code,
                    resp.text[:500],
                )
            resp.raise_for_status()
            data = resp.json()
            return {
                "payment_id": data["id"],
                "confirmation_url": data.get("confirmation", {}).get("confirmation_url", ""),
                "status": data["status"],
                "raw": data,
            }
        except httpx.HTTPError as e:
            logger.error("YooKassa create_payment failed: %s", e)
            raise

    def check_payment(self, payment_id: str) -> Dict[str, Any]:
        """
        Poll YooKassa for the current payment status.
        Returns dict: {"payment_id": str, "status": str, "paid": bool, "amount": str}
        Possible statuses: pending, waiting_for_capture, succeeded, canceled
        """
        try:
            resp = httpx.get(
                f"{YOOKASSA_API}/payments/{payment_id}",
                auth=self._auth(),
                timeout=10.0,
            )
            if resp.is_error:
                logger.error(
                    "YooKassa check_payment failed: %s %s",
                    resp.status_code,
                    resp.text[:500],
                )
            resp.raise_for_status()
            data = resp.json()
            return {
                "payment_id": data["id"],
                "status": data["status"],
                "paid": data.get("paid", False),
                "amount": data.get("amount", {}).get("value", "0"),
                "raw": data,
            }
        except httpx.HTTPError as e:
            logger.error("YooKassa check_payment failed: %s", e)
            raise


class StubPaymentProvider(YooKassaProvider):
    """Fallback for when shop_id is not configured."""

    def __init__(self):
        super().__init__("", "")

    def create_payment(self, **kwargs) -> Dict[str, Any]:
        return {
            "payment_id": f"stub-{uuid.uuid4().hex[:8]}",
            "confirmation_url": "",
            "status": "stub",
            "raw": {},
        }

    def check_payment(self, payment_id: str) -> Dict[str, Any]:
        return {
            "payment_id": payment_id,
            "status": "stub",
            "paid": False,
            "amount": "0",
            "raw": {},
        }


def get_payment_provider() -> YooKassaProvider:
    settings = get_settings()
    if settings.shop_id and settings.shop_secret_key:
        return YooKassaProvider(settings.shop_id, settings.shop_secret_key)
    return StubPaymentProvider()
