from aiogram.filters import Filter


class AdminAccessFilter(Filter):
    def __init__(self, admin_telegram_id: int) -> None:
        self.admin_telegram_id = admin_telegram_id

    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = getattr(message, "chat", None)
        return bool(
            user
            and chat
            and user.id == self.admin_telegram_id
            and chat.type == "private"
        )