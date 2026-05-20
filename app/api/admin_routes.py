from datetime import datetime, timedelta, timezone
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
        from app.bot import messages as bot_messages

        await bot.send_message(
            o.passenger.telegram_id,
            bot_messages.format_order_summary(
                o,
                d,
                driver_name=drv.full_name,
                extra=bot_messages.PASSENGER_BOARDING_CHECKLIST,
            ),
        )
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
    from app.services import loading_service

    d = Direction.get_by_id(direction_id)
    waiting = loading_service.direction_waiting_pool(direction_id)
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
def list_proposals(status: Optional[str] = ProposedStatus.PENDING.value) -> Any:
    q = ProposedDirection.select().order_by(ProposedDirection.created_at).limit(100)
    if status:
        q = q.where(ProposedDirection.status == status)
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
def list_proposals_grouped(status: Optional[str] = ProposedStatus.PENDING.value) -> Any:
    q = ProposedDirection.select().order_by(ProposedDirection.created_at).limit(200)
    if status:
        q = q.where(ProposedDirection.status == status)
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
    return [
        {"kind": p.kind, "file_id": p.file_id, "url": f"/api/admin/telegram-file/{p.file_id}"}
        for p in rows
    ]


@router.get("/telegram-file/{file_id}")
async def telegram_file_proxy(file_id: str) -> Any:
    import httpx
    from fastapi.responses import Response

    from app.config import get_settings as gs

    token = gs().bot_token
    if not token:
        raise HTTPException(status_code=503, detail="no_bot_token")
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
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
        out.append({
            "id": g.id,
            "route_key": g.route_key,
            "from_label": g.from_label,
            "to_label": g.to_label,
            "drivers_count": len(drivers),
            "needed": get_settings().route_reserve_min_drivers,
            "drivers": [{"id": d.id, "name": d.full_name} for d in drivers],
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