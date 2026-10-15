from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, ErrorEvent

from app.access import AdminAccessFilter
from app.config import Settings
from app.conversation_handlers import router as conversation_router
from app.database import Database
from app.middleware import UserRateLimitMiddleware
from app.user_handlers import router as user_router


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secret:
            rendered = record.getMessage()
            if self.secret in rendered:
                record.msg = rendered.replace(self.secret, "[REDACTED]")
                record.args = ()
        return True


async def _retention_loop(database: Database, settings: Settings) -> None:
    while True:
        try:
            deleted, failed = await database.purge_closed_cases(settings.retention_hours)
            if deleted:
                logging.getLogger(__name__).info("retention removed %s closed cases", deleted)
            if failed:
                logging.getLogger(__name__).warning("retention failed for %s cases", failed)
        except Exception as error:
            logging.getLogger(__name__).error(
                "retention job failed; exception_type=%s", type(error).__name__
            )
        await asyncio.sleep(3600)


async def run() -> None:
    settings = Settings.from_environment()
    settings.prepare_storage()
    database = Database(settings.database_path)
    await database.initialize()
    if settings.database_path != ":memory:":
        os.chmod(settings.database_path, 0o600)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    redaction = SecretRedactionFilter(settings.bot_token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher(database=database, settings=settings)
    admin_filter = AdminAccessFilter(settings.admin_telegram_id)
    conversation_router.message.filter(admin_filter)
    conversation_router.callback_query.filter(admin_filter)
    rate_limit_middleware = UserRateLimitMiddleware(settings, database)
    user_router.message.outer_middleware(rate_limit_middleware)
    user_router.callback_query.outer_middleware(rate_limit_middleware)
    dispatcher.include_router(conversation_router)
    dispatcher.include_router(user_router)

    @dispatcher.errors()
    async def safe_error_handler(event: ErrorEvent) -> bool:
        error = event.exception
        logging.getLogger(__name__).error(
            "update processing failed; exception_type=%s", type(error).__name__
        )
        update_message = event.update.message
        if update_message and update_message.chat.type == "private":
            with suppress(TelegramAPIError):
                await update_message.answer("در انجام عملیات خطایی رخ داد. اطلاعات ثبت‌شده را بررسی کن و دوباره تلاش کن.")
        return True

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="شروع یا نمایش منو"),
        ]
    )
    retention_task = asyncio.create_task(_retention_loop(database, settings))
    logging.getLogger(__name__).info("bot polling started")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("bot stopped by operator")


if __name__ == "__main__":
    main()