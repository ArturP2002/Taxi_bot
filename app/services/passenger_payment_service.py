from decimal import Decimal
from datetime import datetime, timezone

from app.config import get_settings
from app.models import Order, OrderStatus, PassengerPaymentStatus, PaymentRecord, PaymentStatus, PaymentPayerType
from app.services.payment_provider import get_payment_provider
from app.services import commission_service, order_service, audit_service


def passenger_fare_amount(order: Order) -> Decimal:
    return commission_service.order_base_fare(order)


def init_passenger_payment(order: Order) -> dict:
    amount = passenger_fare_amount(order)
    prov = get_payment_provider()
    settings = get_settings()
    if not settings.shop_id or not settings.shop_secret_key:
        pr = PaymentRecord.create(
            order=order,
            driver=None,
            payer_type=PaymentPayerType.PASSENGER.value,
            amount=amount,
            status=PaymentStatus.AWAITING_ADMIN.value,
            provider="manual",
        )
        Order.update(
            passenger_payment_status=PassengerPaymentStatus.AWAITING.value,
            status=OrderStatus.AWAITING_PAYMENT.value,
        ).where(Order.id == order.id).execute()
        return {"payment_id": pr.id, "confirmation_url": None, "awaiting_admin": True}

    result = prov.create_payment(
        amount=amount,
        description=f"Заказ #{order.id}",
        metadata={"order_id": order.id, "payer": "passenger"},
    )
    PaymentRecord.create(
        order=order,
        driver=None,
        payer_type=PaymentPayerType.PASSENGER.value,
        amount=amount,
        status=PaymentStatus.PENDING.value,
        provider="yookassa",
        provider_ref=result["payment_id"],
        raw_payload=str(result.get("raw", "")),
    )
    Order.update(
        passenger_payment_status=PassengerPaymentStatus.AWAITING.value,
        status=OrderStatus.AWAITING_PAYMENT.value,
    ).where(Order.id == order.id).execute()
    return result


def confirm_passenger_payment(order: Order, *, actor_telegram_id: int | None = None) -> None:
    now = datetime.now(timezone.utc)
    Order.update(
        passenger_payment_status=PassengerPaymentStatus.PAID.value,
        status=OrderStatus.NEW.value,
        updated_at=now,
    ).where(Order.id == order.id).execute()
    order = Order.get_by_id(order.id)
    order_service.suggest_driver_for_order(order)
    audit_service.log_action(
        "passenger_payment_confirmed",
        actor_telegram_id=actor_telegram_id,
        entity_type="order",
        entity_id=str(order.id),
    )


def check_passenger_payment(order: Order) -> str:
    pr = (
        PaymentRecord.select()
        .where(
            (PaymentRecord.order_id == order.id)
            & (PaymentRecord.payer_type == PaymentPayerType.PASSENGER.value)
        )
        .order_by(PaymentRecord.id.desc())
        .first()
    )
    if not pr:
        return "no_payment"
    if pr.status == PaymentStatus.CONFIRMED.value:
        return "paid"
    if pr.status == PaymentStatus.AWAITING_ADMIN.value:
        return "awaiting_admin"
    if not pr.provider_ref:
        return "pending"
    prov = get_payment_provider()
    info = prov.check_payment(pr.provider_ref)
    if info["status"] == "succeeded" and info["paid"]:
        PaymentRecord.update(status=PaymentStatus.CONFIRMED.value).where(PaymentRecord.id == pr.id).execute()
        confirm_passenger_payment(order)
        return "paid"
    if info["status"] == "canceled":
        PaymentRecord.update(status=PaymentStatus.FAILED.value).where(PaymentRecord.id == pr.id).execute()
        Order.update(passenger_payment_status=PassengerPaymentStatus.FAILED.value).where(Order.id == order.id).execute()
        return "failed"
    return "pending"
