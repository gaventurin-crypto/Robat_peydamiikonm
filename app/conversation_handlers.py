from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.database import Database
from app.keyboards import finish_conversation
from app.media import message_media

logger = logging.getLogger(__name__)
router = Router(name="conversation")


def _is_admin(user_id: int, chat_type: str, settings: Settings) -> bool:
    return user_id == settings.admin_telegram_id and chat_type == "private"


@router.callback_query(F.data.startswith("a:end:"), F.message.chat.type == "private")
async def end_conversation(
    callback: CallbackQuery, bot: Bot, database: Database, settings: Settings
) -> None:
    if not _is_admin(callback.from_user.id, callback.message.chat.type, settings):
        await callback.answer()
        return
    try:
        case_id = int(callback.data.split(":", 2)[2])
    except (ValueError, AttributeError):
        await callback.answer()
        return
    case = await database.get_case(case_id)
    if not case or not await database.end_conversation(case_id, settings.admin_telegram_id):
        await callback.answer("گفتگو قبلاً پایان یافته است.")
        return
    await callback.answer("گفتگو پایان یافت.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        logger.info("could not remove end button for case %s", case_id)
    if case.get("telegram_id"):
        message_ids = await database.user_chat_message_ids(case_id, case["telegram_id"])
        for message_id in message_ids:
            try:
                await bot.delete_message(case["telegram_id"], message_id)
            except TelegramAPIError:
                logger.info("conversation message could not be deleted for case %s", case_id)


@router.message(F.chat.type == "private")
async def reply_to_request(
    message: Message, bot: Bot, database: Database, settings: Settings
) -> None:
    if not _is_admin(message.from_user.id, message.chat.type, settings):
        return
    if message.text and message.text.startswith("/"):
        return
    reply_to = message.reply_to_message
    if not reply_to:
        return

    case = await database.get_case_for_admin_reply(message.chat.id, reply_to.message_id)
    if case is None:
        case = await database.get_case_for_reply("user", reply_to.message_id)
    if not case or not case.get("telegram_id"):
        await message.answer("پیام درخواست پیدا نشد.")
        return

    starting = case["conversation_status"] == "PENDING"
    if starting:
        if not await database.start_conversation(case["id"], settings.admin_telegram_id):
            await message.answer("گفتگوی دیگری برای این کاربر باز است؛ ابتدا آن را با «تمام شد» ببند.")
            return
    elif case["conversation_status"] != "OPEN":
        await message.answer("این گفتگو پایان یافته است.")
        return

    media_type, file_id, text, error = await message_media(
        message, bot, settings.max_upload_bytes
    )
    if error:
        if starting:
            await database.cancel_conversation_start(case["id"])
        await message.answer(error)
        return
    inserted = await database.add_message(
        case["id"],
        "admin",
        media_type,
        text,
        file_id,
        message.chat.id,
        message.message_id,
    )
    if not inserted:
        if starting:
            await database.cancel_conversation_start(case["id"])
        return
    try:
        copied = await bot.copy_message(
            chat_id=case["telegram_id"],
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            protect_content=True,
        )
    except Exception:
        await database.update_message_delivery(message.chat.id, message.message_id, "FAILED")
        if starting:
            await database.cancel_conversation_start(case["id"])
        logger.warning("manager message delivery failed for case %s", case["id"])
        return

    await database.update_message_delivery(
        message.chat.id, message.message_id, "SENT", copied.message_id
    )
    if starting and not await database.activate_conversation(
        case["id"], settings.admin_telegram_id
    ):
        logger.error("manager message delivered but conversation activation failed for case %s", case["id"])
        return
    await database.set_session(case["telegram_id"], case["id"], "conversation")
    if starting:
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=reply_to.message_id,
                reply_markup=finish_conversation(case["id"]),
            )
        except TelegramAPIError:
            logger.warning("could not attach finish button to request for case %s", case["id"])