import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, TelegramObject, Update

from app.bot.handlers import common, driver, passenger
from app.db import close_connection, ensure_connection

logger = logging.getLogger("taxi_bot.dispatcher")


class BotDBMiddleware(BaseMiddleware):
    """Open/close DB connection per Telegram update (PostgreSQL-safe)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            ensure_connection()
        except Exception:
            logger.exception("Bot DB connection failed")
            bot: Bot | None = data.get("bot")
            if bot and isinstance(event, Update):
                chat_id = _chat_id_from_update(event)
                if chat_id:
                    try:
                        await bot.send_message(
                            chat_id,
                            "Сервис временно недоступен. Попробуйте через минуту.",
                        )
                    except Exception:
                        pass
            return None
        try:
            return await handler(event, data)
        finally:
            try:
                close_connection()
            except Exception as e:
                logger.warning("Bot DB close failed: %s", e)


def _chat_id_from_update(update: Update) -> int | None:
    if update.message:
        return update.message.chat.id
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message.chat.id
    if update.edited_message:
        return update.edited_message.chat.id
    return None


async def _global_error_handler(event: ErrorEvent) -> bool:
    logger.exception(
        "Unhandled handler error: update=%s",
        getattr(event.update, "update_id", None),
        exc_info=event.exception,
    )
    bot = event.bot
    chat_id = _chat_id_from_update(event.update) if event.update else None
    if chat_id:
        try:
            await bot.send_message(
                chat_id,
                "Произошла ошибка. Попробуйте ещё раз или нажмите /start.",
            )
        except Exception:
            pass
    state: FSMContext | None = event.data.get("state") if event.data else None
    if state:
        try:
            await state.clear()
        except Exception:
            pass
    return True


def build_dispatcher(bot: Bot) -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.update.outer_middleware(BotDBMiddleware())
    dp.errors.register(_global_error_handler)
    dp.include_router(common.router)
    dp.include_router(passenger.router)
    dp.include_router(driver.router)
    return dp
