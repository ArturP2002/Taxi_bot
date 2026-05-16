from aiogram.fsm.state import State, StatesGroup


class PassengerOrder(StatesGroup):
    choosing_direction = State()
    from_location = State()
    to_location = State()
    seats = State()
    phone = State()


class DriverRegister(StatesGroup):
    full_name = State()
    car_info = State()
    phone = State()


class ProposeDirection(StatesGroup):
    from_label = State()
    to_label = State()
    eta_min = State()
    comment = State()


class DriverCode(StatesGroup):
    waiting_code = State()


class RelayChat(StatesGroup):
    active = State()


class DriverRelayChat(StatesGroup):
    active = State()
