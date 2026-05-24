from aiogram.fsm.state import State, StatesGroup


class DriverOfferConsent(StatesGroup):
    consent = State()


class PassengerOrder(StatesGroup):
    choosing_direction = State()
    choosing_trip_date = State()
    direction_search = State()
    from_location = State()
    to_location = State()
    seats = State()
    phone = State()


class DriverRegister(StatesGroup):
    route_from = State()
    route_to = State()
    return_route = State()
    full_name = State()
    car_info = State()
    photo_front = State()
    photo_back = State()
    photo_left = State()
    photo_right = State()
    photo_salon = State()
    photo_salon_extra = State()
    phone = State()
    max_seats = State()
    price_per_seat = State()
    fixed_price = State()


class ProposeDirection(StatesGroup):
    from_label = State()
    to_label = State()
    return_route = State()
    eta_min = State()
    comment = State()


class DriverOnlineSetup(StatesGroup):
    own_seats = State()


class DriverRest(StatesGroup):
    hours = State()


class DriverCode(StatesGroup):
    waiting_code = State()


class RelayChat(StatesGroup):
    active = State()


class DriverRelayChat(StatesGroup):
    active = State()


class AdminRelayChat(StatesGroup):
    active = State()
    order_id = State()


class DriverLoadingPhoto(StatesGroup):
    waiting = State()


class DriverTransferRequest(StatesGroup):
    note = State()


class DriverCreateTrip(StatesGroup):
    date = State()
    seats = State()
