from decimal import Decimal
from typing import Any, List, Optional

from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.deps import require_admin
from app.bot import keyboards
from app.models import (
    Direction,
    DriverProfile,
    DriverStatus,
    Order,
    OrderDriverAssignment,
    QueueEntry,
    ProposedDirection,
    ProposedStatus,
    PaymentRecord,
    PaymentStatus,
    User,
)
from app.services import order_service, queue_service, proposed_service, audit_service
from app.services.payment_provider import get_payment_provider

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _bot(request: Request) -> Bot:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="bot_unavailable")
    return bot


class DirectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_label: str
    to_label: str
    estimated_time_min: int
    min_time_percent: int
    enabled: bool
    price_per_seat: Decimal
    fixed_price: Decimal
    vehicle_capacity_default: int
    reverse_direction_id: Optional[int]


class DirectionCreate(BaseModel):
    from_label: str
    to_label: str
    estimated_time_min: int
    min_time_percent: int = 70
    enabled: bool = True
    price_per_seat: Decimal = Decimal("0")
    fixed_price: Decimal = Decimal("0")
    vehicle_capacity_default: int = 6
    reverse_direction_id: Optional[int] = None


@router.get("/directions", response_model=List[DirectionOut])
def list_directions() -> Any:
    out: List[DirectionOut] = []
    for d in Direction.select().order_by(Direction.id):
        out.append(DirectionOut(
            id=d.id, from_label=d.from_label, to_label=d.to_label,
            estimated_time_min=d.estimated_time_min, min_time_percent=d.min_time_percent,
            enabled=d.enabled, price_per_seat=d.price_per_seat, fixed_price=d.fixed_price,
            vehicle_capacity_default=d.vehicle_capacity_default,
            reverse_direction_id=d.reverse_direction_id,
        ))
    return out


@router.post("/directions", response_model=DirectionOut)
def create_direction(body: DirectionCreate, user: User = Depends(require_admin)) -> Any:
    d = Direction.create(
        from_label=body.from_label,
        to_label=body.to_label,
        estimated_time_min=body.estimated_time_min,
        min_time_percent=body.min_time_percent,
        enabled=body.enabled,
        price_per_seat=body.price_per_seat,
        fixed_price=body.fixed_price,
        vehicle_capacity_default=body.vehicle_capacity_default,
        reverse_direction=body.reverse_direction_id,
    )
    if body.reverse_direction_id:
        Direction.update(reverse_direction=d.id).where(Direction.id == body.reverse_direction_id).execute()
    audit_service.log_action("direction_create", actor_telegram_id=user.telegram_id, entity_type="direction", entity_id=str(d.id))
    return DirectionOut(
        id=d.id, from_label=d.from_label, to_label=d.to_label,
        estimated_time_min=d.estimated_time_min, min_time_percent=d.min_time_percent,
        enabled=d.enabled, price_per_seat=d.price_per_seat, fixed_price=d.fixed_price,
        vehicle_capacity_default=d.vehicle_capacity_default,
        reverse_direction_id=d.reverse_direction_id,
    )


class DirectionToggleIn(BaseModel):
    enabled: bool


@router.patch("/directions/{direction_id}", response_model=DirectionOut)
def toggle_direction(direction_id: int, body: DirectionToggleIn, user: User = Depends(require_admin)) -> Any:
    d = Direction.get_by_id(direction_id)
    Direction.update(enabled=body.enabled).where(Direction.id == d.id).execute()
    d = Direction.get_by_id(direction_id)
    audit_service.log_action(
        "direction_toggle",
        actor_telegram_id=user.telegram_id,
        entity_type="direction",
        entity_id=str(direction_id),
        payload={"enabled": body.enabled},
    )
    return DirectionOut(
        id=d.id, from_label=d.from_label, to_label=d.to_label,
        estimated_time_min=d.estimated_time_min, min_time_percent=d.min_time_percent,
        enabled=d.enabled, price_per_seat=d.price_per_seat, fixed_price=d.fixed_price,
        vehicle_capacity_default=d.vehicle_capacity_default,
        reverse_direction_id=d.reverse_direction_id,
    )


class SuggestionOut(BaseModel):
    assignment_id: int
    driver_id: int
    driver_name: Optional[str]
    driver_online: bool


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction_id: int
    status: str
    seats: int
    from_location: str
    to_location: str
    phone: str
    pickup_location: Optional[str]
    pickup_time_text: Optional[str]
    passenger_telegram_id: int
    suggestion: Optional[SuggestionOut] = None


def _build_suggestion(order_id: int) -> Optional[SuggestionOut]:
    from app.models import AssignmentStatus
    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == order_id)
            & (OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )
    if not ass:
        return None
    drv = DriverProfile.get_by_id(ass.driver_id)
    return SuggestionOut(
        assignment_id=ass.id,
        driver_id=drv.id,
        driver_name=drv.full_name,
        driver_online=drv.online,
    )


@router.get("/orders", response_model=List[OrderOut])
def list_orders(status: Optional[str] = None) -> Any:
    q = Order.select()
    if status:
        q = q.where(Order.status == status)
    rows = []
    for o in q.order_by(Order.id.desc()).limit(200):
        pu = User.get_by_id(o.passenger_id)
        suggestion = _build_suggestion(o.id) if o.status in ("new", "admin_review") else None
        rows.append(
            OrderOut(
                id=o.id,
                direction_id=o.direction_id,
                status=o.status,
                seats=o.seats,
                from_location=o.from_location,
                to_location=o.to_location,
                phone=o.phone,
                pickup_location=o.pickup_location,
                pickup_time_text=o.pickup_time_text,
                passenger_telegram_id=pu.telegram_id,
                suggestion=suggestion,
            )
        )
    return rows


class AssignIn(BaseModel):
    driver_id: int
    pickup_location: Optional[str] = None
    pickup_time_text: Optional[str] = None


@router.post("/orders/{order_id}/assign")
async def assign_order(
    order_id: int,
    body: AssignIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    o = Order.get_by_id(order_id)
    drv = DriverProfile.get_by_id(body.driver_id)
    try:
        ass = order_service.assign_order_to_driver(
            o,
            drv,
            pickup_location=body.pickup_location,
            pickup_time_text=body.pickup_time_text,
            actor_telegram_id=user.telegram_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    bot = _bot(request)
    d = Direction.get_by_id(o.direction_id)
    text = (
        f"Вам назначен заказ #{o.id}\n"
        f"{d.from_label} → {d.to_label}\n"
        f"Откуда: {o.from_location}\n"
        f"Куда: {o.to_location}\n"
        f"Мест: {o.seats}\n"
        f"Подача: {body.pickup_location or '—'} {body.pickup_time_text or ''}\n"
        "Откройте «Мой заказ»."
    )
    try:
        await bot.send_message(drv.user.telegram_id, text, reply_markup=keyboards.assignment_inline(ass.id))
    except Exception:
        pass
    try:
        await bot.send_message(
            o.passenger.telegram_id,
            f"Водитель назначен по заказу #{o.id}.\n"
            f"Подача: {body.pickup_location or '—'} {body.pickup_time_text or ''}",
        )
    except Exception:
        pass
    return {"assignment_id": ass.id}


class ConfirmSuggestionIn(BaseModel):
    pickup_location: Optional[str] = None
    pickup_time_text: Optional[str] = None


@router.post("/orders/{order_id}/confirm-suggestion")
async def confirm_suggestion_endpoint(
    order_id: int,
    body: ConfirmSuggestionIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from app.models import AssignmentStatus

    o = Order.get_by_id(order_id)
    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == o.id)
            & (OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )
    if not ass:
        raise HTTPException(status_code=400, detail="no_suggestion")

    try:
        confirmed = order_service.confirm_suggestion(
            ass,
            pickup_location=body.pickup_location,
            pickup_time_text=body.pickup_time_text,
            actor_telegram_id=user.telegram_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    bot = _bot(request)
    drv = DriverProfile.get_by_id(ass.driver_id)
    d = Direction.get_by_id(o.direction_id)
    text = (
        f"Вам назначен заказ #{o.id}\n"
        f"{d.from_label} → {d.to_label}\n"
        f"Откуда: {o.from_location}\n"
        f"Куда: {o.to_location}\n"
        f"Мест: {o.seats}\n"
        f"Подача: {body.pickup_location or '—'} {body.pickup_time_text or ''}\n"
        "Откройте «Мой заказ»."
    )
    try:
        await bot.send_message(drv.user.telegram_id, text, reply_markup=keyboards.assignment_inline(confirmed.id))
    except Exception:
        pass
    try:
        await bot.send_message(
            o.passenger.telegram_id,
            f"Водитель назначен по заказу #{o.id}.\n"
            f"Подача: {body.pickup_location or '—'} {body.pickup_time_text or ''}",
        )
    except Exception:
        pass
    return {"assignment_id": confirmed.id}


@router.post("/orders/{order_id}/reject-suggestion")
async def reject_suggestion_endpoint(
    order_id: int,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from app.models import AssignmentStatus

    o = Order.get_by_id(order_id)
    ass = (
        OrderDriverAssignment.select()
        .where(
            (OrderDriverAssignment.order_id == o.id)
            & (OrderDriverAssignment.status == AssignmentStatus.SUGGESTED.value)
        )
        .order_by(OrderDriverAssignment.assigned_at.desc())
        .first()
    )
    if not ass:
        raise HTTPException(status_code=400, detail="no_suggestion")

    next_ass = order_service.reject_suggestion(ass, actor_telegram_id=user.telegram_id)

    result: dict = {"rejected_driver_id": ass.driver_id}
    if next_ass:
        drv = DriverProfile.get_by_id(next_ass.driver_id)
        result["next_suggestion"] = {
            "assignment_id": next_ass.id,
            "driver_id": drv.id,
            "driver_name": drv.full_name,
            "driver_online": drv.online,
        }
    else:
        result["next_suggestion"] = None
    return result


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    full_name: Optional[str]
    status: str
    direction_id: Optional[int]
    max_seats: int
    balance: Decimal
    online: bool


@router.get("/drivers", response_model=List[DriverOut])
def list_drivers() -> Any:
    out: List[DriverOut] = []
    for d in DriverProfile.select():
        u = User.get_by_id(d.user_id)
        out.append(
            DriverOut(
                id=d.id,
                telegram_id=u.telegram_id,
                full_name=d.full_name,
                status=d.status,
                direction_id=d.direction_id,
                max_seats=d.max_seats,
                balance=d.balance,
                online=d.online,
            )
        )
    return out


class ApproveDriverIn(BaseModel):
    direction_id: Optional[int] = None
    max_seats: int = 6


@router.post("/drivers/{driver_id}/approve")
async def approve_driver(driver_id: int, body: ApproveDriverIn, request: Request, user: User = Depends(require_admin)) -> Any:
    d = DriverProfile.get_by_id(driver_id)
    update_fields: dict = {
        "status": DriverStatus.ACTIVE.value,
        "max_seats": body.max_seats,
    }
    if body.direction_id is not None:
        update_fields["direction_id"] = body.direction_id
    DriverProfile.update(**update_fields).where(DriverProfile.id == d.id).execute()
    audit_service.log_action("driver_approve", actor_telegram_id=user.telegram_id, entity_type="driver", entity_id=str(driver_id))
    bot = _bot(request)
    drv_user = User.get_by_id(d.user_id)
    if body.direction_id is not None:
        direction = Direction.get_by_id(body.direction_id)
        msg = (
            f"✅ Ваша заявка одобрена!\n\n"
            f"Направление: {direction.from_label} → {direction.to_label}\n"
            f"Макс. мест: {body.max_seats}\n\n"
            "Нажмите «🟢 Онлайн» чтобы встать в очередь."
        )
    else:
        msg = (
            f"✅ Ваша заявка одобрена!\n\n"
            f"Макс. мест: {body.max_seats}\n\n"
            "Направление будет назначено позже. Ожидайте."
        )
    try:
        await bot.send_message(drv_user.telegram_id, msg)
    except Exception:
        pass
    return {"ok": True}


@router.post("/drivers/{driver_id}/block")
async def block_driver(driver_id: int, request: Request, user: User = Depends(require_admin)) -> Any:
    d = DriverProfile.get_by_id(driver_id)
    DriverProfile.update(status=DriverStatus.BLOCKED.value, online=False).where(DriverProfile.id == driver_id).execute()
    audit_service.log_action("driver_block", actor_telegram_id=user.telegram_id, entity_type="driver", entity_id=str(driver_id))
    bot = _bot(request)
    drv_user = User.get_by_id(d.user_id)
    try:
        await bot.send_message(drv_user.telegram_id, "🚫 Ваш аккаунт заблокирован. Обратитесь к администратору.")
    except Exception:
        pass
    return {"ok": True}


class ChangeDirectionIn(BaseModel):
    direction_id: int


@router.post("/drivers/{driver_id}/direction")
async def change_driver_direction(
    driver_id: int,
    body: ChangeDirectionIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    drv = DriverProfile.get_by_id(driver_id)
    new_dir = Direction.get_by_id(body.direction_id)

    old_direction_id = drv.direction_id
    if old_direction_id:
        old_dir = Direction.get_by_id(old_direction_id)
        queue_service.remove_from_queue(old_dir, drv)

    DriverProfile.update(direction_id=new_dir.id).where(DriverProfile.id == drv.id).execute()

    if drv.online:
        queue_service.enqueue_driver_end(new_dir, drv)

    audit_service.log_action(
        "admin_change_driver_direction",
        actor_telegram_id=user.telegram_id,
        entity_type="driver",
        entity_id=str(driver_id),
        payload={"old_direction_id": old_direction_id, "new_direction_id": new_dir.id},
    )

    bot = _bot(request)
    drv_user = User.get_by_id(drv.user_id)
    try:
        await bot.send_message(
            drv_user.telegram_id,
            f"🔄 Ваше направление изменено администратором.\n"
            f"Новое направление: {new_dir.from_label} → {new_dir.to_label}",
        )
    except Exception:
        pass

    return {"ok": True, "direction_id": new_dir.id}


class QueueOut(BaseModel):
    driver_id: int
    position: int
    telegram_id: int


@router.get("/directions/{direction_id}/queue", response_model=List[QueueOut])
def get_queue(direction_id: int) -> Any:
    rows = (
        QueueEntry.select()
        .where(QueueEntry.direction_id == direction_id)
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    )
    out: List[QueueOut] = []
    for r in rows:
        d = DriverProfile.get_by_id(r.driver_id)
        u = User.get_by_id(d.user_id)
        out.append(QueueOut(driver_id=r.driver_id, position=r.position, telegram_id=u.telegram_id))
    return out


class QueueReorderIn(BaseModel):
    driver_ids: List[int]


@router.post("/directions/{direction_id}/queue/reorder")
def reorder(direction_id: int, body: QueueReorderIn, user: User = Depends(require_admin)) -> Any:
    queue_service.reorder_queue(direction_id, body.driver_ids, actor_telegram_id=user.telegram_id)
    return {"ok": True}


@router.get("/drivers/{driver_id}/capacity")
def driver_capacity(driver_id: int, order_id: Optional[int] = None) -> Any:
    drv = DriverProfile.get_by_id(driver_id)
    occ = order_service.occupied_seats_for_driver(drv)
    extra = 0
    if order_id:
        extra = Order.get_by_id(order_id).seats
    return {
        "occupied": occ,
        "max_seats": drv.max_seats,
        "remaining": drv.max_seats - occ,
        "would_remain": drv.max_seats - occ - extra,
    }


class ProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_label: str
    to_label: str
    estimated_time_min: int
    comment: Optional[str]
    status: str
    proposer_id: int


@router.get("/proposals", response_model=List[ProposalOut])
def list_proposals(status: Optional[str] = ProposedStatus.PENDING.value) -> Any:
    q = ProposedDirection.select().order_by(ProposedDirection.id.desc()).limit(100)
    if status:
        q = q.where(ProposedDirection.status == status)
    out: List[ProposalOut] = []
    for p in q:
        out.append(ProposalOut(
            id=p.id, from_label=p.from_label, to_label=p.to_label,
            estimated_time_min=p.estimated_time_min, comment=p.comment,
            status=p.status, proposer_id=p.proposer_id,
        ))
    return out


class ApproveProposalIn(BaseModel):
    reverse_direction_id: Optional[int] = None


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int,
    body: ApproveProposalIn,
    user: User = Depends(require_admin),
) -> Any:
    p = ProposedDirection.get_by_id(proposal_id)
    d = proposed_service.approve_proposal(
        p,
        actor_telegram_id=user.telegram_id,
        reverse_direction_id=body.reverse_direction_id,
    )
    return {"direction_id": d.id}


class RejectProposalIn(BaseModel):
    note: Optional[str] = None


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    body: RejectProposalIn,
    user: User = Depends(require_admin),
) -> Any:
    p = ProposedDirection.get_by_id(proposal_id)
    proposed_service.reject_proposal(p, actor_telegram_id=user.telegram_id, note=body.note)
    return {"ok": True}


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    driver_id: int
    amount: Decimal
    status: str
    provider: Optional[str]


@router.get("/payments", response_model=List[PaymentOut])
def list_payments() -> Any:
    out: List[PaymentOut] = []
    for p in PaymentRecord.select().order_by(PaymentRecord.id.desc()).limit(100):
        out.append(PaymentOut(
            id=p.id, driver_id=p.driver_id, amount=p.amount,
            status=p.status, provider=p.provider,
        ))
    return out


class CreatePaymentIn(BaseModel):
    amount: Decimal
    description: str = ""


@router.post("/drivers/{driver_id}/payments")
def create_payment_intent(driver_id: int, body: CreatePaymentIn) -> Any:
    drv = DriverProfile.get_by_id(driver_id)
    prov = get_payment_provider()
    result = prov.create_payment(
        amount=body.amount,
        description=body.description or f"Комиссия водителя #{drv.id}",
        metadata={"driver_id": drv.id},
    )
    PaymentRecord.create(
        driver=drv,
        amount=body.amount,
        status=PaymentStatus.PENDING.value,
        provider="yookassa",
        provider_ref=result["payment_id"],
        raw_payload=str(result.get("raw", "")),
    )
    return result


@router.post("/payments/{payment_id}/check")
def check_payment_status(payment_id: int, user: User = Depends(require_admin)) -> Any:
    """Poll YooKassa for payment status (no webhooks)."""
    pr = PaymentRecord.get_by_id(payment_id)
    if pr.status == PaymentStatus.CONFIRMED.value:
        return {"status": "already_confirmed", "payment_id": payment_id}
    if not pr.provider_ref:
        raise HTTPException(400, "no_provider_ref")
    prov = get_payment_provider()
    info = prov.check_payment(pr.provider_ref)
    if info["status"] == "succeeded" and info["paid"]:
        drv = DriverProfile.get_by_id(pr.driver_id)
        amount = Decimal(str(pr.amount))
        new_bal = Decimal(str(drv.balance)) - amount
        if new_bal < 0:
            new_bal = Decimal("0")
        DriverProfile.update(balance=new_bal).where(DriverProfile.id == drv.id).execute()
        PaymentRecord.update(status=PaymentStatus.CONFIRMED.value).where(PaymentRecord.id == pr.id).execute()
        audit_service.log_action(
            "payment_confirmed_via_check",
            actor_telegram_id=user.telegram_id,
            entity_type="payment",
            entity_id=str(payment_id),
            payload={"amount": str(amount), "new_balance": str(new_bal)},
        )
        return {"status": "confirmed", "amount": str(amount), "new_balance": str(new_bal)}
    elif info["status"] == "canceled":
        PaymentRecord.update(status=PaymentStatus.FAILED.value).where(PaymentRecord.id == pr.id).execute()
        return {"status": "canceled"}
    return {"status": info["status"], "paid": info["paid"]}


class ManualConfirmIn(BaseModel):
    amount: Decimal


@router.post("/payments/{payment_id}/confirm")
def confirm_payment_manual(payment_id: int, body: ManualConfirmIn, user: User = Depends(require_admin)) -> Any:
    """Admin manually confirms a payment (e.g. cash/transfer outside YooKassa)."""
    pr = PaymentRecord.get_by_id(payment_id)
    if pr.status == PaymentStatus.CONFIRMED.value:
        return {"status": "already_confirmed"}
    drv = DriverProfile.get_by_id(pr.driver_id)
    amount = body.amount if body.amount > 0 else Decimal(str(pr.amount))
    new_bal = Decimal(str(drv.balance)) - amount
    if new_bal < 0:
        new_bal = Decimal("0")
    DriverProfile.update(balance=new_bal).where(DriverProfile.id == drv.id).execute()
    PaymentRecord.update(status=PaymentStatus.CONFIRMED.value, amount=amount).where(PaymentRecord.id == pr.id).execute()
    audit_service.log_action(
        "payment_confirmed_manual",
        actor_telegram_id=user.telegram_id,
        entity_type="payment",
        entity_id=str(payment_id),
        payload={"amount": str(amount), "new_balance": str(new_bal)},
    )
    return {"ok": True, "amount": str(amount), "new_balance": str(new_bal)}