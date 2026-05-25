from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional

from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.config import get_settings
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
    online_payment_required: bool = False


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
    online_payment_required: bool = False


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
            online_payment_required=getattr(d, "online_payment_required", False),
        ))
    return out


class DirectionUpdate(BaseModel):
    from_label: Optional[str] = None
    to_label: Optional[str] = None
    estimated_time_min: Optional[int] = None
    min_time_percent: Optional[int] = None
    enabled: Optional[bool] = None
    price_per_seat: Optional[Decimal] = None
    fixed_price: Optional[Decimal] = None
    vehicle_capacity_default: Optional[int] = None
    reverse_direction_id: Optional[int] = None
    online_payment_required: Optional[bool] = None


@router.put("/directions/{direction_id}", response_model=DirectionOut)
def update_direction(
    direction_id: int, body: DirectionUpdate, user: User = Depends(require_admin)
) -> Any:
    d = Direction.get_by_id(direction_id)
    data = body.model_dump(exclude_unset=True)
    updates = dict(data)
    if "reverse_direction_id" in data:
        updates["reverse_direction"] = data["reverse_direction_id"]
        updates.pop("reverse_direction_id", None)
    if updates:
        Direction.update(**updates).where(Direction.id == d.id).execute()
    audit_service.log_action(
        "direction_update",
        actor_telegram_id=user.telegram_id,
        entity_type="direction",
        entity_id=str(direction_id),
        payload=updates,
    )
    d = Direction.get_by_id(direction_id)
    return DirectionOut(
        id=d.id, from_label=d.from_label, to_label=d.to_label,
        estimated_time_min=d.estimated_time_min, min_time_percent=d.min_time_percent,
        enabled=d.enabled, price_per_seat=d.price_per_seat, fixed_price=d.fixed_price,
        vehicle_capacity_default=d.vehicle_capacity_default,
        reverse_direction_id=d.reverse_direction_id,
        online_payment_required=getattr(d, "online_payment_required", False),
    )


@router.delete("/directions/{direction_id}")
def delete_direction(direction_id: int, user: User = Depends(require_admin)) -> Any:
    Direction.update(enabled=False).where(Direction.id == direction_id).execute()
    audit_service.log_action(
        "direction_soft_delete",
        actor_telegram_id=user.telegram_id,
        entity_type="direction",
        entity_id=str(direction_id),
    )
    return {"ok": True}


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
        online_payment_required=body.online_payment_required,
    )
    if body.reverse_direction_id:
        Direction.update(reverse_direction=d.id).where(Direction.id == body.reverse_direction_id).execute()
    else:
        from app.services import direction_pairs

        direction_pairs.ensure_reverse_direction(d)
    audit_service.log_action("direction_create", actor_telegram_id=user.telegram_id, entity_type="direction", entity_id=str(d.id))
    d = Direction.get_by_id(d.id)
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
        online_payment_required=getattr(d, "online_payment_required", False),
    )


class OrderPatchIn(BaseModel):
    from_location: Optional[str] = None
    to_location: Optional[str] = None
    seats: Optional[int] = None
    status: Optional[str] = None
    direction_id: Optional[int] = None
    platform_seats: Optional[int] = None
    pickup_location: Optional[str] = None
    pickup_time_text: Optional[str] = None
    pickup_surcharge: Optional[Decimal] = None


@router.patch("/orders/{order_id}")
async def patch_order(
    order_id: int,
    body: OrderPatchIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    o = Order.get_by_id(order_id)
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if updates:
        Order.update(**updates).where(Order.id == o.id).execute()
    audit_service.log_action(
        "order_patch", actor_telegram_id=user.telegram_id, entity_type="order", entity_id=str(order_id)
    )
    o = Order.get_by_id(order_id)
    if any(k in updates for k in ("pickup_location", "pickup_time_text", "seats")):
        try:
            from app.services.loading_notify import broadcast_loading_update

            bot = _bot(request)
            await broadcast_loading_update(bot, o.direction_id, trigger_order_id=o.id)
        except Exception:
            pass
    return {"ok": True}


class ReassignIn(BaseModel):
    driver_id: int
    pickup_location: Optional[str] = None
    pickup_time_text: Optional[str] = None


@router.post("/orders/{order_id}/unassign")
async def unassign_order_endpoint(
    order_id: int, request: Request, user: User = Depends(require_admin)
) -> Any:
    from app.bot import messages as bot_messages
    from app.services.loading_notify import broadcast_loading_update

    o = Order.get_by_id(order_id)
    old_id = order_service.unassign_order_from_driver(o, actor_telegram_id=user.telegram_id)
    o = Order.get_by_id(order_id)
    bot = _bot(request)
    if old_id:
        old_drv = DriverProfile.get_by_id(old_id)
        try:
            await bot.send_message(
                old_drv.user.telegram_id,
                bot_messages.DRIVER_OVERFLOW_MSG.format(order_id=order_id),
            )
        except Exception:
            pass
    try:
        await bot.send_message(
            o.passenger.telegram_id,
            bot_messages.PASSENGER_OVERFLOW_MSG,
        )
    except Exception:
        pass
    await broadcast_loading_update(bot, o.direction_id)
    return {"ok": True, "previous_driver_id": old_id}


@router.post("/orders/{order_id}/reassign")
async def reassign_order_endpoint(
    order_id: int,
    body: ReassignIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from app.bot import messages as bot_messages
    from app.services.loading_notify import broadcast_loading_update

    o = Order.get_by_id(order_id)
    new_drv = DriverProfile.get_by_id(body.driver_id)
    try:
        ass = order_service.reassign_order(
            o,
            new_drv,
            pickup_location=body.pickup_location,
            pickup_time_text=body.pickup_time_text,
            actor_telegram_id=user.telegram_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    o = Order.get_by_id(order_id)
    d = Direction.get_by_id(o.direction_id)
    bot = _bot(request)
    text = (
        bot_messages.format_order_summary(o, d, extra="Откройте «Мой заказ».")
        + f"\nПодача: {body.pickup_location or '—'} {body.pickup_time_text or ''}"
    )
    try:
        await bot.send_message(
            new_drv.user.telegram_id, text, reply_markup=keyboards.assignment_inline(ass.id)
        )
    except Exception:
        pass
    try:
        await bot.send_message(
            o.passenger.telegram_id,
            bot_messages.format_order_summary(
                o, d, driver_name=new_drv.full_name, extra=bot_messages.PASSENGER_BOARDING_CHECKLIST
            ),
        )
    except Exception:
        pass
    Order.update(transfer_requested_at=None, transfer_note=None).where(Order.id == o.id).execute()
    await broadcast_loading_update(bot, o.direction_id)
    return {"assignment_id": ass.id}


@router.get("/orders/{order_id}/timeline")
def order_timeline(order_id: int) -> Any:
    from app.models import AuditLog, AssignmentStatus

    o = Order.get_by_id(order_id)
    assignments = []
    for a in order_service.list_order_assignments(order_id):
        drv = DriverProfile.get_by_id(a.driver_id)
        assignments.append({
            "id": a.id,
            "status": a.status,
            "driver_id": a.driver_id,
            "driver_name": drv.full_name,
            "assigned_at": str(a.assigned_at) if a.assigned_at else None,
            "responded_at": str(a.responded_at) if a.responded_at else None,
        })
    events = list(
        AuditLog.select()
        .where((AuditLog.entity_type == "order") & (AuditLog.entity_id == str(order_id)))
        .order_by(AuditLog.created_at)
        .limit(50)
    )
    accepted = next(
        (a for a in assignments if a["status"] == AssignmentStatus.ACCEPTED.value), None
    )
    loading_snap = None
    if accepted:
        from app.services import loading_service

        drv = DriverProfile.get_by_id(accepted["driver_id"])
        snap = loading_service.driver_loading_snapshot(drv)
        loading_snap = loading_service.snapshot_to_dict(snap)
    return {
        "order": {
            "id": o.id,
            "status": o.status,
            "passenger_payment_status": o.passenger_payment_status,
            "created_at": str(o.created_at) if o.created_at else None,
            "code_issued_at": str(o.code_issued_at) if o.code_issued_at else None,
            "started_at": str(o.started_at) if o.started_at else None,
            "ended_at": str(o.ended_at) if o.ended_at else None,
            "transfer_requested_at": str(o.transfer_requested_at)
            if getattr(o, "transfer_requested_at", None)
            else None,
            "transfer_note": getattr(o, "transfer_note", None),
        },
        "assignments": assignments,
        "audit": [
            {"action": e.action, "at": str(e.created_at), "payload": e.payload}
            for e in events
        ],
        "loading_snapshot": loading_snap,
    }


@router.post("/orders/{order_id}/cancel")
async def cancel_order_endpoint(
    order_id: int, request: Request, user: User = Depends(require_admin)
) -> Any:
    from app.services import driver_risk_service
    from app.services.admin_notify import notify_driver_suspicious

    o = Order.get_by_id(order_id)
    linked = order_service.cancel_order(o, actor_telegram_id=user.telegram_id)
    if linked:
        d = DriverProfile.get_by_id(linked)
        if d.status == DriverStatus.SUSPICIOUS.value:
            bot = _bot(request)
            stats = driver_risk_service.driver_risk_stats(d.id)
            await notify_driver_suspicious(
                bot, d.full_name or f"ID:{d.id}", d.id, stats
            )
    return {"ok": True}


@router.post("/orders/{order_id}/confirm-passenger-payment")
def confirm_passenger_payment_endpoint(
    order_id: int, user: User = Depends(require_admin)
) -> Any:
    from app.services import passenger_payment_service

    o = Order.get_by_id(order_id)
    passenger_payment_service.confirm_passenger_payment(o, actor_telegram_id=user.telegram_id)
    return {"ok": True}


class SplitOrderIn(BaseModel):
    first_seats: int


@router.post("/orders/{order_id}/split")
def split_order_endpoint(
    order_id: int, body: SplitOrderIn, user: User = Depends(require_admin)
) -> Any:
    """Split overflow order into two (same passenger/route) for manual assign."""
    from datetime import datetime, timezone

    from app.services import code_service
    from app.services.boarding_credentials import boarding_code_for_order

    o = Order.get_by_id(order_id)
    if o.status not in (OrderStatus.NEW.value, OrderStatus.ADMIN_REVIEW.value):
        raise HTTPException(status_code=400, detail="order_not_splittable")
    first = int(body.first_seats)
    if first <= 0 or first >= o.seats:
        raise HTTPException(status_code=400, detail="invalid_split_seats")
    second = o.seats - first
    now = datetime.now(timezone.utc)
    Order.update(
        seats=first,
        platform_seats=first,
        updated_at=now,
    ).where(Order.id == o.id).execute()
    code2 = code_service.generate_six_digit_code()
    o2 = Order.create(
        direction=o.direction,
        passenger=o.passenger,
        from_location=o.from_location,
        to_location=o.to_location,
        seats=second,
        platform_seats=second,
        phone=o.phone,
        status=OrderStatus.ADMIN_REVIEW.value,
        passenger_payment_status=o.passenger_payment_status,
        confirmation_code_hash="tmp",
        boarding_code=code2,
        code_issued_at=now,
        pickup_location=o.pickup_location,
        pickup_time_text=o.pickup_time_text,
        created_at=now,
        updated_at=now,
    )
    code_service.persist_boarding_code(o2.id, code2)
    o = Order.get_by_id(o.id)
    if not boarding_code_for_order(o):
        code1 = code_service.generate_six_digit_code()
        code_service.persist_boarding_code(o.id, code1)
    audit_service.log_action(
        "order_split",
        actor_telegram_id=user.telegram_id,
        entity_type="order",
        entity_id=str(order_id),
        payload={"first_seats": first, "new_order_id": o2.id},
    )
    return {"ok": True, "original_order_id": o.id, "new_order_id": o2.id, "seats": [first, second]}


class SuggestionOut(BaseModel):
    assignment_id: int
    driver_id: int
    driver_name: Optional[str]
    driver_online: bool
    direction_match: bool = True
    driver_direction_label: Optional[str] = None
    order_direction_label: Optional[str] = None


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
    order = Order.get_by_id(order_id)
    order_dir = Direction.get_by_id(order.direction_id)
    order_label = f"{order_dir.from_label} → {order_dir.to_label}"
    driver_label = None
    if drv.direction_id:
        dd = Direction.get_by_id(drv.direction_id)
        driver_label = f"{dd.from_label} → {dd.to_label}"
    match = drv.direction_id == order.direction_id
    return SuggestionOut(
        assignment_id=ass.id,
        driver_id=drv.id,
        driver_name=drv.full_name,
        driver_online=drv.online,
        direction_match=match,
        driver_direction_label=driver_label,
        order_direction_label=order_label,
    )


@router.get("/orders", response_model=List[OrderOut])
def list_orders(status: Optional[str] = None) -> Any:
    q = Order.select()
    if status:
        q = q.where(Order.status == status)
    rows = []
    for o in q.order_by(Order.id.desc()).limit(200):
        pu = User.get_by_id(o.passenger_id)
        suggestion = _build_suggestion(o.id)
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
        from app.services.boarding_credentials import notify_passenger_driver_assigned

        await notify_passenger_driver_assigned(bot, o, drv, d)
    except Exception:
        pass
    try:
        from app.services.loading_notify import broadcast_loading_update

        await broadcast_loading_update(bot, o.direction_id)
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
        from app.services.boarding_credentials import notify_passenger_driver_assigned

        await notify_passenger_driver_assigned(bot, o, drv, d)
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
    car_info: Optional[str] = None
    phone: Optional[str] = None
    status: str
    direction_id: Optional[int]
    max_seats: int
    own_seats_reserved: int = 0
    balance: Decimal
    online: bool
    loading: bool = False
    rest_until: Optional[str] = None
    draft_route: Optional[str] = None
    declines_30d: int = 0
    order_cancellations_30d: int = 0
    trips_completed_30d: int = 0
    risk_label: str = "ok"
    registration_submitted: bool = False


@router.get("/_diag")
def admin_diag() -> Any:
    """Quick diagnostic for "анкета не падает" — DB path + counts."""
    from app.db import get_db
    from app.models import DriverRegistrationPhoto

    db = get_db()
    try:
        path = getattr(db, "database", "?")
    except Exception:
        path = "?"

    sample = []
    for d in DriverProfile.select().limit(5):
        try:
            u = User.get_by_id(d.user_id)
            tg = u.telegram_id
        except Exception:
            tg = None
        sample.append({
            "id": d.id,
            "telegram_id": tg,
            "full_name": d.full_name,
            "status": d.status,
            "phone": d.phone,
            "current_city": d.current_city,
            "tariff_note": d.tariff_note,
        })

    return {
        "db_path": str(path),
        "drivers_total": DriverProfile.select().count(),
        "drivers_pending": DriverProfile.select().where(
            DriverProfile.status == DriverStatus.PENDING.value
        ).count(),
        "drivers_active": DriverProfile.select().where(
            DriverProfile.status == DriverStatus.ACTIVE.value
        ).count(),
        "users_total": User.select().count(),
        "proposals_total": ProposedDirection.select().count(),
        "proposals_pending": ProposedDirection.select().where(
            ProposedDirection.status == ProposedStatus.PENDING.value
        ).count(),
        "proposals_reserved": ProposedDirection.select().where(
            ProposedDirection.status == ProposedStatus.RESERVED.value
        ).count(),
        "registration_photos": DriverRegistrationPhoto.select().count(),
        "sample_drivers": sample,
    }


@router.get("/drivers", response_model=List[DriverOut])
def list_drivers() -> Any:
    from app.services import driver_registration, driver_risk_service

    out: List[DriverOut] = []
    for d in DriverProfile.select():
        u = User.get_by_id(d.user_id)
        stats = driver_risk_service.driver_risk_stats(d.id)
        out.append(
            DriverOut(
                id=d.id,
                telegram_id=u.telegram_id,
                full_name=d.full_name,
                car_info=d.car_info,
                phone=d.phone,
                status=d.status,
                direction_id=d.direction_id,
                max_seats=d.max_seats,
                own_seats_reserved=getattr(d, "own_seats_reserved", 0) or 0,
                balance=d.balance,
                online=d.online,
                loading=getattr(d, "loading", False),
                rest_until=str(d.rest_until) if getattr(d, "rest_until", None) else None,
                draft_route=driver_registration.draft_route_label(d),
                declines_30d=stats["declines"],
                order_cancellations_30d=stats["order_cancellations"],
                trips_completed_30d=stats["trips_completed"],
                risk_label=stats["risk_label"],
                registration_submitted=(
                    d.status == DriverStatus.PENDING.value
                    and driver_registration.registration_is_submitted(d)
                ),
            )
        )
    return out


@router.get("/drivers/{driver_id}/risk")
def driver_risk(driver_id: int) -> Any:
    from app.services import driver_risk_service

    d = DriverProfile.get_by_id(driver_id)
    return driver_risk_service.driver_risk_stats(d.id)


class DriverPatchIn(BaseModel):
    full_name: Optional[str] = None
    car_info: Optional[str] = None
    phone: Optional[str] = None
    max_seats: Optional[int] = None
    own_seats_reserved: Optional[int] = None
    balance: Optional[Decimal] = None
    direction_id: Optional[int] = None
    status: Optional[str] = None
    rest_hours: Optional[int] = None
    clear_rest: Optional[bool] = None


def _driver_in_trip(driver_id: int) -> bool:
    from app.models import AssignmentStatus, OrderStatus

    return (
        Order.select()
        .join(OrderDriverAssignment, on=(OrderDriverAssignment.order_id == Order.id))
        .where(
            (OrderDriverAssignment.driver_id == driver_id)
            & (OrderDriverAssignment.status == AssignmentStatus.ACCEPTED.value)
            & (Order.status == OrderStatus.IN_PROGRESS.value)
        )
        .exists()
    )


@router.patch("/drivers/{driver_id}")
def patch_driver(driver_id: int, body: DriverPatchIn, user: User = Depends(require_admin)) -> Any:
    data = body.model_dump(exclude_unset=True)
    rest_hours = data.pop("rest_hours", None)
    clear_rest = data.pop("clear_rest", None)
    updates = {k: v for k, v in data.items() if v is not None}
    if clear_rest:
        updates["rest_until"] = None
    if rest_hours is not None:
        updates["rest_until"] = datetime.now(timezone.utc) + timedelta(hours=max(1, rest_hours))
    if "direction_id" in updates:
        raise HTTPException(
            status_code=400,
            detail="use_change_direction_endpoint",
        )
    if updates:
        DriverProfile.update(**updates).where(DriverProfile.id == driver_id).execute()
    audit_service.log_action(
        "driver_patch", actor_telegram_id=user.telegram_id, entity_type="driver", entity_id=str(driver_id)
    )
    return {"ok": True}


class ApproveDriverIn(BaseModel):
    direction_id: Optional[int] = None
    max_seats: int = 6


@router.post("/drivers/{driver_id}/approve")
async def approve_driver(driver_id: int, body: ApproveDriverIn, request: Request, user: User = Depends(require_admin)) -> Any:
    if body.direction_id is None:
        raise HTTPException(status_code=400, detail="direction_required")
    d = DriverProfile.get_by_id(driver_id)
    update_fields: dict = {
        "status": DriverStatus.ACTIVE.value,
        "max_seats": body.max_seats,
        "direction_id": body.direction_id,
    }
    DriverProfile.update(**update_fields).where(DriverProfile.id == d.id).execute()
    audit_service.log_action("driver_approve", actor_telegram_id=user.telegram_id, entity_type="driver", entity_id=str(driver_id))
    bot = _bot(request)
    drv_user = User.get_by_id(d.user_id)
    direction = Direction.get_by_id(body.direction_id)
    msg = (
        f"✅ Ваша заявка одобрена!\n\n"
        f"Направление: {direction.from_label} → {direction.to_label}\n"
        f"Макс. мест: {body.max_seats}\n\n"
        "Нажмите «🟢 Онлайн» чтобы встать в очередь."
    )
    from app.services.admin_notify import notify_driver_approved_welcome

    try:
        await bot.send_message(drv_user.telegram_id, msg)
    except Exception:
        pass
    await notify_driver_approved_welcome(bot, drv_user.telegram_id)
    return {"ok": True}


@router.post("/drivers/{driver_id}/clear-suspicious")
async def clear_driver_suspicious(
    driver_id: int, request: Request, user: User = Depends(require_admin)
) -> Any:
    from app.services import driver_risk_service

    d = DriverProfile.get_by_id(driver_id)
    if d.status != DriverStatus.SUSPICIOUS.value:
        raise HTTPException(status_code=400, detail="not_suspicious")
    driver_risk_service.clear_suspicious(driver_id)
    audit_service.log_action(
        "driver_clear_suspicious",
        actor_telegram_id=user.telegram_id,
        entity_type="driver",
        entity_id=str(driver_id),
    )
    bot = _bot(request)
    drv_user = User.get_by_id(d.user_id)
    try:
        await bot.send_message(
            drv_user.telegram_id,
            "✅ Проверка завершена. Можете снова выходить на линию («🟢 Онлайн»).",
        )
    except Exception:
        pass
    return {"ok": True}


@router.post("/drivers/{driver_id}/mark-suspicious")
async def mark_driver_suspicious(
    driver_id: int, request: Request, user: User = Depends(require_admin)
) -> Any:
    from app.services import driver_risk_service
    from app.services.admin_notify import notify_driver_suspicious

    d = DriverProfile.get_by_id(driver_id)
    DriverProfile.update(status=DriverStatus.SUSPICIOUS.value, online=False).where(
        DriverProfile.id == driver_id
    ).execute()
    if d.direction_id:
        queue_service.remove_from_queue(Direction.get_by_id(d.direction_id), d)
    audit_service.log_action(
        "driver_mark_suspicious",
        actor_telegram_id=user.telegram_id,
        entity_type="driver",
        entity_id=str(driver_id),
    )
    stats = driver_risk_service.driver_risk_stats(driver_id)
    bot = _bot(request)
    await notify_driver_suspicious(bot, d.full_name or f"ID:{d.id}", d.id, stats)
    drv_user = User.get_by_id(d.user_id)
    try:
        await bot.send_message(
            drv_user.telegram_id,
            "⚠️ Аккаунт переведён на проверку. Свяжитесь с администратором.",
        )
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
    order_service.decline_suggested_assignments(driver_id=drv.id)

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
    full_name: Optional[str] = None
    car_info: Optional[str] = None
    loading: bool = False
    in_trip: bool = False
    own_seats_reserved: int = 0
    rest_until: Optional[str] = None
    online: bool = True
    loading_eta_at: Optional[str] = None
    minutes_until_loading: Optional[int] = None
    loading_label: Optional[str] = None
    max_seats: int = 6
    occupied_seats: int = 0
    free_seats: int = 0
    loading_status: Optional[str] = None
    passengers: List[dict] = []


@router.get("/directions/{direction_id}/queue", response_model=List[QueueOut])
def get_queue(direction_id: int) -> Any:
    from app.services import queue_eta_service, loading_service

    eta_map = {s.driver_id: s for s in queue_eta_service.compute_queue_schedule(direction_id)}
    rows = (
        QueueEntry.select()
        .where(QueueEntry.direction_id == direction_id)
        .order_by(QueueEntry.position, QueueEntry.enqueued_at)
    )
    out: List[QueueOut] = []
    for r in rows:
        d = DriverProfile.get_by_id(r.driver_id)
        u = User.get_by_id(d.user_id)
        slot = eta_map.get(r.driver_id)
        snap = loading_service.driver_loading_snapshot(
            d, in_trip=_driver_in_trip(d.id)
        )
        pax = [
            {
                "order_id": p.order_id,
                "seats": p.seats,
                "from_location": p.from_location,
                "to_location": p.to_location,
            }
            for p in snap.passengers
        ]
        out.append(
            QueueOut(
                driver_id=r.driver_id,
                position=r.position,
                telegram_id=u.telegram_id,
                full_name=d.full_name,
                car_info=d.car_info,
                loading=bool(getattr(d, "loading", False)),
                in_trip=_driver_in_trip(d.id),
                own_seats_reserved=int(getattr(d, "own_seats_reserved", 0) or 0),
                rest_until=str(d.rest_until) if getattr(d, "rest_until", None) else None,
                online=bool(d.online),
                loading_eta_at=slot.loading_at.isoformat() if slot else None,
                minutes_until_loading=slot.minutes_until if slot else None,
                loading_label=slot.label if slot else None,
                max_seats=snap.max_seats,
                occupied_seats=snap.occupied_seats,
                free_seats=snap.free_seats,
                loading_status=snap.status_label,
                passengers=pax,
            )
        )
    return out


@router.get("/directions/{direction_id}/board")
def direction_board(direction_id: int) -> Any:
    from app.services import loading_service, overflow_service

    d = Direction.get_by_id(direction_id)
    waiting = loading_service.direction_waiting_pool(direction_id)
    cap = overflow_service.direction_capacity_info(direction_id)
    loading_cars = [
        loading_service.snapshot_to_dict(s)
        for s in loading_service.drivers_loading_on_direction(direction_id)
    ]
    queue = get_queue(direction_id)
    return {
        "direction_id": direction_id,
        "label": f"{d.from_label} → {d.to_label}",
        "waiting": waiting,
        "loading_cars": loading_cars,
        "queue": queue,
        "max_single_car_seats": cap.max_single_car_seats,
        "total_available_seats": cap.total_available_seats,
    }


@router.get("/directions/grouped")
def list_directions_grouped() -> Any:
    from app.services.direction_pairs import build_direction_groups

    groups = build_direction_groups(list(Direction.select().order_by(Direction.id)))
    out = []
    for g in groups:
        fwd = g.forward
        rev = g.reverse
        out.append(
            {
                "label": f"{fwd.from_label} ↔ {fwd.to_label}",
                "forward": {
                    "id": fwd.id,
                    "from_label": fwd.from_label,
                    "to_label": fwd.to_label,
                    "estimated_time_min": fwd.estimated_time_min,
                },
                "reverse": (
                    {
                        "id": rev.id,
                        "from_label": rev.from_label,
                        "to_label": rev.to_label,
                        "estimated_time_min": rev.estimated_time_min,
                    }
                    if rev
                    else None
                ),
            }
        )
    return out


class QueueReorderIn(BaseModel):
    driver_ids: List[int]


@router.post("/directions/{direction_id}/queue/reorder")
def reorder(direction_id: int, body: QueueReorderIn, user: User = Depends(require_admin)) -> Any:
    try:
        queue_service.reorder_queue(direction_id, body.driver_ids, actor_telegram_id=user.telegram_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    proposer_name: Optional[str] = None
    max_seats: int = 6
    own_seats: int = 0
    price_per_seat: Decimal = Decimal("0")
    fixed_price: Decimal = Decimal("0")
    created_at: Optional[str] = None


@router.get("/proposals", response_model=List[ProposalOut])
def list_proposals(status: Optional[str] = "pending,reserved") -> Any:
    q = ProposedDirection.select().order_by(ProposedDirection.created_at).limit(100)
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            q = q.where(ProposedDirection.status == statuses[0])
        elif statuses:
            q = q.where(ProposedDirection.status.in_(statuses))
    out: List[ProposalOut] = []
    for p in q:
        drv = DriverProfile.get_by_id(p.proposer_id)
        out.append(ProposalOut(
            id=p.id, from_label=p.from_label, to_label=p.to_label,
            estimated_time_min=p.estimated_time_min, comment=p.comment,
            status=p.status, proposer_id=p.proposer_id,
            proposer_name=drv.full_name,
            max_seats=getattr(p, "max_seats", 6) or 6,
            own_seats=getattr(p, "own_seats", 0) or 0,
            price_per_seat=getattr(p, "price_per_seat", 0) or 0,
            fixed_price=getattr(p, "fixed_price", 0) or 0,
            created_at=str(p.created_at) if p.created_at else None,
        ))
    return out


def _proposal_pair_key(from_label: str, to_label: str) -> str:
    from app.services.route_labels import normalize_route_label

    a, b = sorted([normalize_route_label(from_label), normalize_route_label(to_label)])
    return f"{a}|{b}"


@router.get("/proposals/grouped")
def list_proposals_grouped(status: Optional[str] = "pending,reserved") -> Any:
    q = ProposedDirection.select().order_by(ProposedDirection.created_at).limit(200)
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            q = q.where(ProposedDirection.status == statuses[0])
        elif statuses:
            q = q.where(ProposedDirection.status.in_(statuses))
    groups: dict[str, list] = {}
    for p in q:
        drv = DriverProfile.get_by_id(p.proposer_id)
        item = {
            "id": p.id,
            "from_label": p.from_label,
            "to_label": p.to_label,
            "estimated_time_min": p.estimated_time_min,
            "comment": p.comment,
            "status": p.status,
            "proposer_id": p.proposer_id,
            "proposer_name": drv.full_name,
            "max_seats": getattr(p, "max_seats", 6) or 6,
            "own_seats": getattr(p, "own_seats", 0) or 0,
            "price_per_seat": str(getattr(p, "price_per_seat", 0) or 0),
            "fixed_price": str(getattr(p, "fixed_price", 0) or 0),
            "created_at": str(p.created_at) if p.created_at else None,
        }
        key = _proposal_pair_key(p.from_label, p.to_label)
        groups.setdefault(key, []).append(item)
    out = []
    for key, plist in groups.items():
        p0 = plist[0]
        out.append({
            "route_key": key,
            "label": f"{p0['from_label']} ↔ {p0['to_label']}",
            "proposals": plist,
        })
    return out


@router.get("/drivers/{driver_id}/photos")
def driver_registration_photos(driver_id: int) -> Any:
    from app.services.photo_service import list_registration_photos

    rows = list_registration_photos(driver_id)
    from urllib.parse import quote

    return [
        {
            "kind": p.kind,
            "file_id": p.file_id,
            "url": f"/api/admin/telegram-file?file_id={quote(p.file_id, safe='')}",
        }
        for p in rows
    ]


@router.get("/telegram-file")
async def telegram_file_proxy_query(file_id: str = Query(...)) -> Any:
    return await _telegram_file_response(file_id)


@router.get("/telegram-file/{path_file_id}")
async def telegram_file_proxy_path(path_file_id: str) -> Any:
    return await _telegram_file_response(path_file_id)


async def _telegram_file_response(file_id: str) -> Any:
    import httpx
    from fastapi.responses import Response

    from app.config import get_settings as gs

    fid = file_id
    token = gs().bot_token
    if not token:
        raise HTTPException(status_code=503, detail="no_bot_token")
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": fid},
        )
        data = r.json()
        if not data.get("ok"):
            raise HTTPException(status_code=404, detail="file_not_found")
        path = data["result"]["file_path"]
        fr = await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
    return Response(content=fr.content, media_type=fr.headers.get("content-type", "image/jpeg"))


@router.get("/reserve-groups")
def list_reserve_groups() -> Any:
    from app.models import RouteReserveGroup, ReserveGroupStatus
    from app.services import reserve_service

    out = []
    for g in RouteReserveGroup.select().where(
        RouteReserveGroup.status == ReserveGroupStatus.COLLECTING.value
    ):
        drivers = reserve_service.unique_proposers_in_group(g.id)
        driver_items = [
            {
                "id": drv.id,
                "full_name": drv.full_name or f"ID:{drv.id}",
                "phone": drv.phone,
            }
            for drv in drivers
        ]
        out.append({
            "id": g.id,
            "route_key": g.route_key,
            "from_label": g.from_label,
            "to_label": g.to_label,
            "drivers_count": len(drivers),
            "needed": get_settings().route_reserve_min_drivers,
            "drivers": driver_items,
        })
    return out


@router.post("/backup")
async def create_db_backup(
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from aiogram.types import FSInputFile

    from app.services import backup_service

    try:
        path = backup_service.create_backup()
    except ValueError:
        raise HTTPException(status_code=400, detail="backup_sqlite_only")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="database_not_found")

    bot = _bot(request)
    try:
        await bot.send_document(
            user.telegram_id,
            FSInputFile(path),
            caption=f"💾 Бэкап БД\n{path.name}",
        )
    except Exception:
        raise HTTPException(status_code=503, detail="telegram_send_failed")

    audit_service.log_action(
        "database_backup",
        actor_telegram_id=user.telegram_id,
        entity_type="system",
        entity_id="db",
        payload={"filename": path.name, "sent_to_telegram": user.telegram_id},
    )
    return {"ok": True, "filename": path.name}


class ApproveProposalIn(BaseModel):
    reverse_direction_id: Optional[int] = None


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: int,
    body: ApproveProposalIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from app.models import QueueEntry
    from app.services import direction_pairs
    from app.services.admin_notify import notify_proposal_decision

    p = ProposedDirection.get_by_id(proposal_id)
    d = proposed_service.approve_proposal(
        p,
        actor_telegram_id=user.telegram_id,
        reverse_direction_id=body.reverse_direction_id,
    )
    bot = _bot(request)
    route = f"{d.from_label} → {d.to_label}"
    merged = ProposedDirection.select().where(
        (ProposedDirection.created_direction_id == d.id)
        & (ProposedDirection.status == ProposedStatus.APPROVED.value)
    )
    rev = direction_pairs.get_reverse_direction(d)
    rev_route = f"{rev.from_label} → {rev.to_label}" if rev else None

    for prop in merged:
        drv = DriverProfile.get_by_id(prop.proposer_id)
        qe = QueueEntry.get_or_none(
            (QueueEntry.direction_id == d.id) & (QueueEntry.driver_id == drv.id)
        )
        pos = qe.position if qe else None
        await notify_proposal_decision(
            bot, drv.user.telegram_id, approved=True, route=route, queue_position=pos
        )
        if rev:
            qe_rev = QueueEntry.get_or_none(
                (QueueEntry.direction_id == rev.id) & (QueueEntry.driver_id == drv.id)
            )
            pos_rev = qe_rev.position if qe_rev else None
            await notify_proposal_decision(
                bot,
                drv.user.telegram_id,
                approved=True,
                route=rev_route or "",
                queue_position=pos_rev,
            )
    return {"direction_id": d.id, "reverse_direction_id": rev.id if rev else None}


class RejectProposalIn(BaseModel):
    note: Optional[str] = None


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: int,
    body: RejectProposalIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from app.services.admin_notify import notify_proposal_decision

    p = ProposedDirection.get_by_id(proposal_id)
    route = f"{p.from_label} → {p.to_label}"
    drv = DriverProfile.get_by_id(p.proposer_id)
    proposed_service.reject_proposal(p, actor_telegram_id=user.telegram_id, note=body.note)
    bot = _bot(request)
    await notify_proposal_decision(bot, drv.user.telegram_id, approved=False, route=route)
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
    from app.models import PaymentPayerType

    PaymentRecord.create(
        driver=drv,
        payer_type=PaymentPayerType.DRIVER.value,
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
        from app.services import passenger_payment_service
        from app.models import PaymentPayerType

        if pr.payer_type == PaymentPayerType.PASSENGER.value and pr.order_id:
            order = Order.get_by_id(pr.order_id)
            passenger_payment_service.confirm_passenger_payment(order, actor_telegram_id=user.telegram_id)
            PaymentRecord.update(status=PaymentStatus.CONFIRMED.value).where(PaymentRecord.id == pr.id).execute()
            return {"status": "confirmed", "type": "passenger"}
        if not pr.driver_id:
            raise HTTPException(400, "no_driver")
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
async def confirm_payment_manual(
    payment_id: int,
    body: ManualConfirmIn,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    """Admin manually confirms a payment (e.g. cash/transfer outside YooKassa)."""
    pr = PaymentRecord.get_by_id(payment_id)
    if pr.status == PaymentStatus.CONFIRMED.value:
        return {"status": "already_confirmed"}
    from app.models import PaymentPayerType
    from app.services import passenger_payment_service

    if pr.payer_type == PaymentPayerType.PASSENGER.value and pr.order_id:
        order = Order.get_by_id(pr.order_id)
        passenger_payment_service.confirm_passenger_payment(order, actor_telegram_id=user.telegram_id)
        PaymentRecord.update(status=PaymentStatus.CONFIRMED.value).where(PaymentRecord.id == pr.id).execute()
        return {"ok": True, "type": "passenger"}
    if not pr.driver_id:
        raise HTTPException(400, "no_driver")
    drv = DriverProfile.get_by_id(pr.driver_id)
    amount = body.amount if body.amount > 0 else Decimal(str(pr.amount))
    new_bal = Decimal(str(drv.balance)) - amount
    if new_bal < 0:
        new_bal = Decimal("0")
    DriverProfile.update(balance=new_bal).where(DriverProfile.id == drv.id).execute()
    PaymentRecord.update(status=PaymentStatus.CONFIRMED.value, amount=amount).where(PaymentRecord.id == pr.id).execute()
    drv = DriverProfile.get_by_id(drv.id)
    from app.services.debt_service import apply_debt_block_if_needed
    from app.services.admin_notify import notify_debt_auto_blocked, notify_driver_debt_blocked

    blocked = apply_debt_block_if_needed(drv)
    if blocked:
        bot = _bot(request)
        await notify_debt_auto_blocked(
            bot, drv.full_name or str(drv.id), drv.balance, drv.id
        )
        await notify_driver_debt_blocked(bot, drv.user.telegram_id, drv.balance)
    audit_service.log_action(
        "payment_confirmed_manual",
        actor_telegram_id=user.telegram_id,
        entity_type="payment",
        entity_id=str(payment_id),
        payload={"amount": str(amount), "new_balance": str(new_bal)},
    )
    return {"ok": True, "amount": str(amount), "new_balance": str(new_bal), "blocked": blocked}


@router.get("/audit")
def list_audit(limit: int = 50) -> Any:
    from app.models import AuditLog

    rows = AuditLog.select().order_by(AuditLog.id.desc()).limit(limit)
    return [
        {
            "id": r.id,
            "action": r.action,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "actor_telegram_id": r.actor_telegram_id,
            "payload": r.payload,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]


class ScheduledTripOut(BaseModel):
    id: int
    direction_id: int
    from_label: str
    to_label: str
    departure_at: str
    seats_total: int
    seats_booked: int
    status: str
    driver_id: Optional[int] = None
    driver_name: Optional[str] = None
    created_by: str
    note: Optional[str] = None


class ScheduledTripCreate(BaseModel):
    direction_id: int
    departure_at: str
    seats_total: int
    driver_id: Optional[int] = None
    status: str = "open"
    note: Optional[str] = None
    fulfill_order_id: Optional[int] = None


class TripDepartureRequestOut(BaseModel):
    order_id: int
    telegram_id: int
    username: Optional[str] = None
    display_name: str
    direction_id: int
    from_label: str
    to_label: str
    requested_departure_at: str
    from_location: str
    to_location: str
    seats: int
    phone: str
    created_at: str


class AdminUserOut(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    display_name: str
    created_at: str
    orders_total: int = 0
    orders_completed: int = 0
    orders_awaiting_trip: int = 0
    last_order_at: Optional[str] = None
    is_blocked: bool = False
    status: str = "ok"


class AdminUserDetailOut(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    display_name: str
    created_at: str
    last_seen_at: Optional[str] = None
    is_blocked: bool = False
    orders_total: int = 0
    orders_completed: int = 0
    orders_awaiting_trip: int = 0
    orders: List[dict] = []
    pending_trip_requests: List[dict] = []


class AdminUsersPageOut(BaseModel):
    items: List[AdminUserOut]
    page: int
    page_size: int
    total: int
    total_pages: int


class ScheduledTripPatch(BaseModel):
    departure_at: Optional[str] = None
    seats_total: Optional[int] = None
    status: Optional[str] = None
    driver_id: Optional[int] = None
    note: Optional[str] = None


def _trip_out(t) -> ScheduledTripOut:
    d = Direction.get_by_id(t.direction_id)
    drv_name = None
    if t.driver_id:
        try:
            drv = DriverProfile.get_by_id(t.driver_id)
            drv_name = drv.full_name
        except Exception:
            pass
    from app.util.time_format import format_datetime_display

    dep = t.departure_at
    return ScheduledTripOut(
        id=t.id,
        direction_id=t.direction_id,
        from_label=d.from_label,
        to_label=d.to_label,
        departure_at=format_datetime_display(dep),
        seats_total=int(t.seats_total),
        seats_booked=int(t.seats_booked or 0),
        status=t.status,
        driver_id=t.driver_id,
        driver_name=drv_name,
        created_by=t.created_by,
        note=t.note,
    )


@router.get("/scheduled-trips", response_model=List[ScheduledTripOut])
def list_scheduled_trips(
    direction_id: Optional[int] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Any:
    from app.models.scheduled_trip import ScheduledTrip

    q = ScheduledTrip.select().order_by(ScheduledTrip.departure_at)
    if direction_id:
        q = q.where(ScheduledTrip.direction_id == direction_id)
    if status:
        q = q.where(ScheduledTrip.status == status)
    rows = list(q)
    if date_from:
        df = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        rows = [r for r in rows if r.departure_at >= df]
    if date_to:
        dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
        rows = [r for r in rows if r.departure_at <= dt]
    return [_trip_out(t) for t in rows]


def _trip_request_out(order: Order) -> TripDepartureRequestOut:
    from app.services import trip_request_service
    from app.util.time_format import format_datetime_display

    d = Direction.get_by_id(order.direction_id)
    passenger = User.get_by_id(order.passenger_id)
    return TripDepartureRequestOut(
        order_id=order.id,
        telegram_id=passenger.telegram_id,
        username=passenger.username,
        display_name=trip_request_service.user_display_name(passenger),
        direction_id=order.direction_id,
        from_label=d.from_label,
        to_label=d.to_label,
        requested_departure_at=format_datetime_display(order.requested_departure_at),
        from_location=order.from_location,
        to_location=order.to_location,
        seats=order.seats,
        phone=order.phone,
        created_at=str(order.created_at),
    )


def _passenger_user_ids() -> set[int]:
    """Users who used the bot as passengers (not only role=passenger)."""
    from app.models.passenger import PassengerProfile
    from app.models.user import UserRole

    ids: set[int] = set()
    for u in User.select(User.id).where(User.role == UserRole.PASSENGER.value):
        ids.add(u.id)
    for row in PassengerProfile.select(PassengerProfile.user_id):
        ids.add(int(row.user_id))
    for row in Order.select(Order.passenger_id).distinct():
        if row.passenger_id:
            ids.add(int(row.passenger_id))
    return ids


def _user_order_stats(user_id: int) -> dict:
    from app.models.order import OrderStatus

    orders = list(Order.select().where(Order.passenger_id == user_id))
    total = len(orders)
    completed = sum(1 for o in orders if o.status == OrderStatus.COMPLETED.value)
    awaiting = sum(
        1 for o in orders if o.status == OrderStatus.AWAITING_SCHEDULED_TRIP.value
    )
    last_at = None
    if orders:
        last = max(orders, key=lambda o: o.id)
        last_at = str(last.updated_at or last.created_at)
    return {
        "orders_total": total,
        "orders_completed": completed,
        "orders_awaiting_trip": awaiting,
        "last_order_at": last_at,
    }


@router.get("/trip-departure-requests", response_model=List[TripDepartureRequestOut])
def list_trip_departure_requests(status: str = "pending") -> Any:
    from app.models.order import OrderStatus
    from app.services import trip_request_service

    if status != "pending":
        raise HTTPException(400, "unsupported_status")
    return [_trip_request_out(o) for o in trip_request_service.list_pending_requests()]


@router.get("/users", response_model=AdminUsersPageOut)
def list_admin_users(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
) -> Any:
    from app.services import trip_request_service

    passenger_ids = _passenger_user_ids()
    if not passenger_ids:
        return AdminUsersPageOut(
            items=[],
            page=page,
            page_size=page_size,
            total=0,
            total_pages=1,
        )
    query = User.select().where(User.id.in_(list(passenger_ids)))
    raw_q = (q or "").strip()
    if raw_q:
        if raw_q.isdigit():
            query = query.where(User.telegram_id == int(raw_q))
        else:
            like = f"%{raw_q.lstrip('@')}%"
            query = query.where(
                (User.username ** like)
                | (User.first_name ** like)
                | (User.last_name ** like)
            )
    all_users = list(query.order_by(User.id.desc()))
    total = len(all_users)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    chunk = all_users[start : start + page_size]
    items: List[AdminUserOut] = []
    for u in chunk:
        stats = _user_order_stats(u.id)
        blocked = bool(getattr(u, "is_blocked", False))
        items.append(
            AdminUserOut(
                telegram_id=u.telegram_id,
                username=u.username,
                display_name=trip_request_service.user_display_name(u),
                created_at=str(u.created_at)[:10],
                is_blocked=blocked,
                status="blocked" if blocked else "ok",
                **stats,
            )
        )
    return AdminUsersPageOut(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


@router.get("/users/{telegram_id}", response_model=AdminUserDetailOut)
def get_admin_user(telegram_id: int) -> Any:
    from app.models.order import OrderStatus
    from app.services import trip_request_service
    from app.util.time_format import format_datetime_display

    try:
        u = User.get(User.telegram_id == telegram_id)
    except Exception:
        raise HTTPException(404, "user_not_found") from None
    stats = _user_order_stats(u.id)
    orders_rows = (
        Order.select()
        .where(Order.passenger_id == u.id)
        .order_by(Order.id.desc())
        .limit(20)
    )
    orders = []
    for o in orders_rows:
        d = Direction.get_by_id(o.direction_id)
        trip_label = None
        if o.scheduled_trip_id:
            from app.models.scheduled_trip import ScheduledTrip

            try:
                trip = ScheduledTrip.get_by_id(o.scheduled_trip_id)
                trip_label = format_datetime_display(trip.departure_at)
            except Exception:
                pass
        orders.append(
            {
                "id": o.id,
                "status": o.status,
                "from_label": d.from_label,
                "to_label": d.to_label,
                "seats": o.seats,
                "trip_departure": trip_label,
                "requested_departure": format_datetime_display(o.requested_departure_at)
                if o.requested_departure_at
                else None,
                "created_at": str(o.created_at)[:19],
            }
        )
    pending = [
        {
            "order_id": o.id,
            "requested_departure": format_datetime_display(o.requested_departure_at),
            "from_location": o.from_location,
            "to_location": o.to_location,
            "seats": o.seats,
        }
        for o in Order.select()
        .where(
            (Order.passenger_id == u.id)
            & (Order.status == OrderStatus.AWAITING_SCHEDULED_TRIP.value)
        )
        .order_by(Order.id.desc())
    ]
    return AdminUserDetailOut(
        telegram_id=u.telegram_id,
        username=u.username,
        first_name=getattr(u, "first_name", None),
        last_name=getattr(u, "last_name", None),
        display_name=trip_request_service.user_display_name(u),
        created_at=str(u.created_at),
        last_seen_at=str(u.last_seen_at) if getattr(u, "last_seen_at", None) else None,
        is_blocked=bool(getattr(u, "is_blocked", False)),
        orders=orders,
        pending_trip_requests=pending,
        **stats,
    )


@router.post("/users/{telegram_id}/block")
def block_admin_user(telegram_id: int, user: User = Depends(require_admin)) -> Any:
    n = User.update(is_blocked=True).where(User.telegram_id == telegram_id).execute()
    if not n:
        raise HTTPException(404, "user_not_found")
    audit_service.log_action(
        "user_blocked",
        actor_telegram_id=user.telegram_id,
        entity_type="user",
        entity_id=str(telegram_id),
    )
    return {"ok": True}


@router.post("/users/{telegram_id}/unblock")
def unblock_admin_user(telegram_id: int, user: User = Depends(require_admin)) -> Any:
    n = User.update(is_blocked=False).where(User.telegram_id == telegram_id).execute()
    if not n:
        raise HTTPException(404, "user_not_found")
    audit_service.log_action(
        "user_unblocked",
        actor_telegram_id=user.telegram_id,
        entity_type="user",
        entity_id=str(telegram_id),
    )
    return {"ok": True}


@router.post("/orders/{order_id}/attach-scheduled-trip")
async def attach_order_scheduled_trip(
    order_id: int,
    request: Request,
    trip_id: int = Query(...),
    user: User = Depends(require_admin),
) -> Any:
    from app.models.scheduled_trip import ScheduledTrip
    from app.services import trip_request_service

    trip = ScheduledTrip.get_by_id(trip_id)
    bot = _bot(request)
    try:
        await trip_request_service.fulfill_and_notify(
            bot, order_id, trip, actor_telegram_id=user.telegram_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "order_id": order_id, "trip_id": trip_id}


@router.post("/scheduled-trips", response_model=ScheduledTripOut)
async def create_scheduled_trip(
    body: ScheduledTripCreate,
    request: Request,
    user: User = Depends(require_admin),
) -> Any:
    from app.services import scheduled_trip_service, trip_request_service
    from app.models.scheduled_trip import ScheduledTripCreatedBy

    from app.util.time_format import parse_datetime_display

    try:
        dep = parse_datetime_display(body.departure_at)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.fulfill_order_id:
        order = Order.get_by_id(body.fulfill_order_id)
        if order.direction_id != body.direction_id:
            raise HTTPException(400, "direction_mismatch")
        if int(body.seats_total) < int(order.seats):
            raise HTTPException(400, "not_enough_seats")
    t = scheduled_trip_service.create_trip(
        direction_id=body.direction_id,
        departure_at=dep,
        seats_total=body.seats_total,
        driver_id=body.driver_id,
        created_by=ScheduledTripCreatedBy.ADMIN.value,
        note=body.note,
        status=body.status,
    )
    audit_service.log_action(
        "scheduled_trip_created",
        actor_telegram_id=user.telegram_id,
        entity_type="scheduled_trip",
        entity_id=str(t.id),
    )
    if body.fulfill_order_id:
        bot = _bot(request)
        try:
            await trip_request_service.fulfill_and_notify(
                bot, body.fulfill_order_id, t, actor_telegram_id=user.telegram_id
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return _trip_out(t)


@router.patch("/scheduled-trips/{trip_id}", response_model=ScheduledTripOut)
def patch_scheduled_trip(
    trip_id: int, body: ScheduledTripPatch, user: User = Depends(require_admin)
) -> Any:
    from app.models.scheduled_trip import ScheduledTrip

    t = ScheduledTrip.get_by_id(trip_id)
    updates = body.model_dump(exclude_unset=True)
    if "departure_at" in updates and updates["departure_at"]:
        from app.util.time_format import parse_datetime_display

        try:
            updates["departure_at"] = parse_datetime_display(updates["departure_at"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        ScheduledTrip.update(**updates).where(ScheduledTrip.id == trip_id).execute()
    audit_service.log_action(
        "scheduled_trip_patch",
        actor_telegram_id=user.telegram_id,
        entity_type="scheduled_trip",
        entity_id=str(trip_id),
        payload=updates,
    )
    return _trip_out(ScheduledTrip.get_by_id(trip_id))


@router.delete("/scheduled-trips/{trip_id}")
def delete_scheduled_trip(trip_id: int, user: User = Depends(require_admin)) -> Any:
    from app.models.scheduled_trip import ScheduledTrip, ScheduledTripStatus

    ScheduledTrip.update(
        status=ScheduledTripStatus.CANCELLED.value,
        updated_at=datetime.now(timezone.utc),
    ).where(ScheduledTrip.id == trip_id).execute()
    audit_service.log_action(
        "scheduled_trip_cancelled",
        actor_telegram_id=user.telegram_id,
        entity_type="scheduled_trip",
        entity_id=str(trip_id),
    )
    return {"ok": True}


@router.post("/scheduled-trips/{trip_id}/assign-driver")
def assign_trip_driver(
    trip_id: int, driver_id: int = Query(...), user: User = Depends(require_admin)
) -> Any:
    from app.models.scheduled_trip import ScheduledTrip

    DriverProfile.get_by_id(driver_id)
    ScheduledTrip.update(driver_id=driver_id, updated_at=datetime.now(timezone.utc)).where(
        ScheduledTrip.id == trip_id
    ).execute()
    audit_service.log_action(
        "scheduled_trip_assign_driver",
        actor_telegram_id=user.telegram_id,
        entity_type="scheduled_trip",
        entity_id=str(trip_id),
        payload={"driver_id": driver_id},
    )
    return _trip_out(ScheduledTrip.get_by_id(trip_id))


@router.get("/commissions")
def list_commissions(limit: int = 50) -> Any:
    from app.models import CommissionLedger

    rows = CommissionLedger.select().order_by(CommissionLedger.id.desc()).limit(limit)
    return [
        {
            "id": c.id,
            "order_id": c.order_id,
            "driver_id": c.driver_id,
            "amount": str(c.amount),
            "base_fare": str(c.base_fare),
            "charged_on_start": getattr(c, "charged_on_start", True),
        }
        for c in rows
    ]