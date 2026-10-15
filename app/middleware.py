from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import Settings
from app.database import Database


class UserRateLimitMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        chat = getattr(event, "chat", None) or getattr(message, "chat", None)
        if (
            user
            and chat
            and chat.type == "private"
            and user.id != self.settings.admin_telegram_id
        ):
            active_conversation = isinstance(event, Message) and await self.database.user_has_active_conversation(
                user.id
            )
            if active_conversation:
                allowed = True
            else:
                allowed = await self.database.allow_update(user.id, time.time())
            if not allowed:
                if isinstance(event, CallbackQuery):
                    await event.answer("کمی آهسته‌تر اقدام کن.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("کمی آهسته‌تر پیام بفرست.")
                return None
        return await handler(event, data)