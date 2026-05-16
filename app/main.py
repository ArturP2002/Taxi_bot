import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot
from aiogram.types import BotCommand, Update
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.admin_routes import router as admin_router
from app.api.auth_routes import router as auth_router
from app.bot.dispatcher_setup import build_dispatcher
from app.config import get_settings
from app.db import init_db, ensure_connection, close_connection
from app.rate_limit import limiter

logger = logging.getLogger("taxi_bot.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _logger = logging.getLogger("taxi_bot")

    init_db()
    settings = get_settings()
    bot = Bot(settings.bot_token) if settings.bot_token else None
    dp = build_dispatcher(bot) if bot else None
    app.state.bot = bot
    app.state.dp = dp

    if bot:
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="🏠 Главное меню"),
                BotCommand(command="order", description="🚕 Заказать поездку"),
                BotCommand(command="contact", description="📞 Связь"),
                BotCommand(command="driver", description="🧑‍✈️ Я водитель"),
            ])
        except Exception as e:
            _logger.error("Failed to set bot commands: %s", e)

        if settings.base_url and settings.webhook_path:
            webhook_url = f"{settings.base_url.rstrip('/')}{settings.webhook_path}"
            try:
                await bot.set_webhook(
                    url=webhook_url,
                    secret_token=settings.webhook_secret or None,
                    drop_pending_updates=True,
                )
                _logger.info("Webhook set: %s", webhook_url)
            except Exception as e:
                _logger.error("Failed to set webhook: %s", e)

    yield

    if bot:
        try:
            await bot.delete_webhook()
        except Exception:
            pass
        await bot.session.close()


class DBConnectionMiddleware(BaseHTTPMiddleware):
    """Open DB connection at request start, close at end (critical for PostgreSQL)."""

    async def dispatch(self, request: Request, call_next):
        ensure_connection()
        try:
            response = await call_next(request)
        finally:
            close_connection()
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception("Unhandled error: %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"detail": "internal_server_error"},
            )
        elapsed = (time.time() - start) * 1000
        logger.info(
            "%s %s -> %d (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(lifespan=lifespan, title="Taxi Bot API", version="1.0.0")
    app.state.limiter = limiter
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(DBConnectionMiddleware)
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", "ngrok-skip-browser-warning"],
        expose_headers=["*"],
    )
    app.include_router(auth_router)
    app.include_router(admin_router)

    static_dir = Path(__file__).resolve().parent / "admin_static"
    if static_dir.is_dir():
        from starlette.responses import FileResponse

        no_cache_headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }

        @app.get("/admin")
        @app.get("/admin/")
        @app.get("/admin/{path:path}")
        async def serve_admin(path: str = ""):
            """Serve admin SPA with no-cache headers to avoid Telegram WebView caching."""
            file_path = static_dir / (path or "index.html")
            if not file_path.is_file() or not str(file_path.resolve()).startswith(str(static_dir.resolve())):
                file_path = static_dir / "index.html"
            return FileResponse(file_path, headers=no_cache_headers)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post(settings.webhook_path)
    async def telegram_webhook(request: Request) -> Response:
        bot: Bot | None = getattr(request.app.state, "bot", None)
        dp = getattr(request.app.state, "dp", None)
        if not bot or not dp:
            return Response(status_code=503)
        if settings.webhook_secret:
            secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if secret != settings.webhook_secret:
                return Response(status_code=403)
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return Response()

    return app


app = create_app()
