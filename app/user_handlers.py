from __future__ import annotations

import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.database import Database
from app.domain import toggle_selection
from app.keyboards import (
    finish_conversation,
    form_options,
    user_home,
)
from app.media import message_media

logger = logging.getLogger(__name__)
router = Router(name="user")
def _is_owner(user_id: int, settings: Settings) -> bool:
    return user_id != settings.admin_telegram_id


def _question_text(step: str, data: dict, settings: Settings) -> str:
    if step == "have":
        return "چه اطلاعاتی داری؟"
    if step == "need":
        return "دنبال چه اطلاعاتی هستی؟"
    if step == "have_value":
        fields = data.get("have_fields", data.get("have", []))
        index = data.get("field_index", 0)
        field = fields[index] if index < len(fields) else "اطلاعات"
        return f"مقدار «{field}» را بنویس."
    return ""


async def _show_draft(message: Message | CallbackQuery, database: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    draft = await database.get_draft(user_id)
    if not draft:
        text, keyboard = "فرم فعالی پیدا نشد. از منو درخواست جدید را شروع کن.", user_home()
    else:
        step = draft["step"]
        data = draft["data"]
        fields = data.get("have_fields", data.get("have", [])) if step == "have" else data.get(
            "need_fields", data.get("need", [])
        )
        if step not in {"have", "need", "have_value"}:
            step = "have"
            data.setdefault("have_fields", [])
            data.setdefault("have_values", {})
            data.setdefault("need_fields", [])
            await database.save_draft(await database.register_user(message.from_user.id), step, data)
        text = _question_text(step, data, settings)
        keyboard = None
        if step in {"have", "need"}:
            keyboard = form_options(step, tuple(settings.info_options), fields)
    if isinstance(message, CallbackQuery):
        await message.answer()
        try:
            await message.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            await message.message.answer(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


async def _register_user(database: Database, user) -> int:
    return await database.register_user(
        user.id,
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
        getattr(user, "username", None),
    )


def _format_new_case(case: dict) -> str:
    name = " ".join(
        value for value in (case.get("first_name"), case.get("last_name")) if value
    ) or "ثبت نشده"
    username = f"@{case['username']}" if case.get("username") else "ندارد"
    have_fields = json.loads(case.get("have_options") or "[]")
    have_values = json.loads(case.get("have_values") or "{}")
    need_fields = json.loads(case.get("need_options") or "[]")
    have_text = "\n".join(
        f"{field}: {have_values.get(field, 'ثبت نشده')}" for field in have_fields
    ) or "انتخابی ندارد"
    need_text = "، ".join(need_fields) or "انتخابی ندارد"
    return (
        "📩 درخواست جدید\n\n"
        "👤 اطلاعات کاربر\n"
        f"نام نمایشی: {name}\nنام کاربری: {username}\n"
        f"شناسه تلگرام: {case.get('telegram_id') or 'در دسترس نیست'}\n\n"
        f"چه اطلاعاتی دارد؟\n{have_text}\n\n"
        f"دنبال چه اطلاعاتی است؟\n{need_text}\n\n"
        f"🕒 زمان ثبت: {case['created_at']}"
    )


async def _show_home(message: Message | CallbackQuery, settings: Settings) -> None:
    welcome = "سلام! برای ثبت درخواست، دکمهٔ زیر را بزن."
    if isinstance(message, CallbackQuery):
        await message.answer()
        try:
            await message.message.edit_text(welcome, reply_markup=user_home())
        except TelegramBadRequest:
            await message.message.answer(welcome, reply_markup=user_home())
    else:
        await message.answer(welcome, reply_markup=user_home())


@router.message(CommandStart(), F.chat.type == "private")
async def start_user(message: Message, database: Database, settings: Settings) -> None:
    if not _is_owner(message.from_user.id, settings):
        return
    await _register_user(database, message.from_user)
    await _show_home(message, settings)


@router.callback_query(F.data.startswith("u:"), F.message.chat.type == "private")
async def user_callbacks(
    callback: CallbackQuery, bot: Bot, database: Database, settings: Settings
) -> None:
    if not _is_owner(callback.from_user.id, settings):
        await callback.answer()
        return
    data = callback.data.split(":")
    action = data[1]
    user_id = await _register_user(database, callback.from_user)
    if action == "new":
        draft = await database.get_draft(callback.from_user.id)
        if not draft:
            await database.save_draft(
                user_id,
                "have",
                {"have_fields": [], "have_values": {}, "need_fields": []},
            )
        await _show_draft(callback, database, settings)
        return
    if action in {"toggle", "next"} and len(data) >= 3:
        step = data[2]
        draft = await database.get_draft(callback.from_user.id)
        if not draft or draft["step"] != step or step not in {"have", "need"}:
            await callback.answer()
            return
        fields_key = "have_fields" if step == "have" else "need_fields"
        options = tuple(settings.info_options)
        if action == "toggle" and len(data) == 4:
            try:
                option = options[int(data[3])]
                draft["data"][fields_key] = toggle_selection(
                    draft["data"].get(fields_key, []), option, options
                )
            except (ValueError, IndexError, TypeError):
                await callback.answer()
                return
            await database.save_draft(user_id, step, draft["data"])
            await _show_draft(callback, database, settings)
            return
        if action != "next":
            await callback.answer()
            return
        if step == "have":
            have_fields = draft["data"].get("have_fields", [])
            if have_fields:
                draft["data"]["field_index"] = 0
                next_step = "have_value"
            else:
                next_step = "need"
            await database.save_draft(user_id, next_step, draft["data"])
            await _show_draft(callback, database, settings)
            return
        draft["data"]["requested_fields"] = draft["data"].get("need_fields", [])
        draft["data"].setdefault("requested_values", {})
        await database.save_draft(user_id, "ready", draft["data"])
        case = await _submit_ready_draft(callback.from_user.id, bot, database, settings)
        if not case:
            await callback.answer()
            return
        await callback.answer()
        await callback.message.edit_text("درخواستت برای مدیر ارسال شد.")
        return
    await callback.answer()


async def _submit_ready_draft(
    user_id: int, bot: Bot, database: Database, settings: Settings
) -> dict | None:
    try:
        case = await database.submit_draft(user_id)
    except Exception:
        logger.warning("case creation failed")
        return None
    if not case:
        return None
    session = await database.get_session(user_id)
    if session and session.get("mode") == "closed":
        await database.set_session(user_id, None, None)
    try:
        sent = await bot.send_message(
            settings.admin_telegram_id,
            _format_new_case(case),
            reply_markup=finish_conversation(case["id"]),
        )
        await database.save_request_reply_target(case["id"], sent.chat.id, sent.message_id)
    except TelegramAPIError:
        logger.warning("new case notification delivery failed")
    return case


def _finish_keyboard(case_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="تمام شد", callback_data=f"a:end:{case_id}")]]
    )


@router.message(F.chat.type == "private")
async def user_messages(message: Message, bot: Bot, database: Database, settings: Settings) -> None:
    if not _is_owner(message.from_user.id, settings):
        return
    if message.text and message.text.startswith("/"):
        return

    draft = await database.get_draft(message.from_user.id)
    if draft:
        user_id = await _register_user(database, message.from_user)
        if draft["step"] != "have_value" or message.text is None:
            await message.answer("مقدار را به‌صورت متن بفرست.")
            return
        answer = message.text.strip()
        fields = draft["data"].get("have_fields", [])
        index = draft["data"].get("field_index", 0)
        if not answer:
            await message.answer("مقدار را خالی نگذار.")
            return
        if len(answer) > 500 or not fields or not isinstance(index, int) or not 0 <= index < len(fields):
            await message.answer("مقدار معتبر نیست؛ دوباره بفرست.")
            return
        values = draft["data"].setdefault("have_values", {})
        values[fields[index]] = answer
        draft["data"]["field_index"] = index + 1
        if index + 1 < len(fields):
            await database.save_draft(user_id, "have_value", draft["data"])
            await _show_draft(message, database, settings)
        else:
            await database.save_draft(user_id, "need", draft["data"])
            await _show_draft(message, database, settings)
        return

    session = await database.get_session(message.from_user.id)
    if session and session.get("mode") == "closed":
        await message.answer("این گفت‌وگو تموم شده. برای ثبت درخواست جدید، /start رو بزن.")
        return
    if not session or session.get("mode") != "conversation" or session.get("case_id") is None:
        return
    case = await database.get_user_case(session["case_id"], message.from_user.id)
    if not case:
        return
    if case.get("conversation_status") != "OPEN":
        await message.answer("این گفت‌وگو تموم شده. برای ثبت درخواست جدید، /start رو بزن.")
        return
    media_type, file_id, text, error = await message_media(message, bot, settings.max_upload_bytes)
    if error:
        await message.answer(error)
        return
    inserted = await database.add_message(
        case["id"], "user", media_type, text, file_id, message.chat.id, message.message_id
    )
    if not inserted:
        return
    try:
        copied = await bot.copy_message(
            chat_id=settings.admin_telegram_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=finish_conversation(case["id"]),
        )
    except Exception:
        await database.update_message_delivery(message.chat.id, message.message_id, "FAILED")
        logger.warning("user message delivery failed for case %s", case["id"])
        return
    await database.update_message_delivery(message.chat.id, message.message_id, "SENT", copied.message_id)