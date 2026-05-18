from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import common, passenger, driver, menu


def build_dispatcher(bot: Bot) -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(menu.router)
    dp.include_router(common.router)
    dp.include_router(passenger.router)
    dp.include_router(driver.router)
    return dp
