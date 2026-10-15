from __future__ import annotations

import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Settings
from app.database import Database
from app.keyboards import finish_conversation
from app.media import message_media

logger = logging.getLogger(__name__)
router = Router(name="conversation")
INBOX_PAGE_SIZE = 8


def _chat_name(case: dict) -> str:
    full_name = " ".join(
        value for value in (case.get("first_name"), case.get("last_name")) if value
    )
    return full_name or (f"@{case['username']}" if case.get("username") else str(case.get("telegram_id") or "کاربر"))


def _inbox_keyboard(cases: list[dict], page: int, total: int):
    builder = InlineKeyboardBuilder()
    for case in cases:
        preview = " ".join((case.get("last_message") or "درخواست ثبت‌شده").split())
        state = {
            "PENDING": "جدید",
            "STARTING": "در حال شروع",
            "OPEN": "باز",
            "CLOSED": "پایان‌یافته",
        }.get(case.get("conversation_status"), "نامشخص")
        builder.button(
            text=f"{_chat_name(case)[:20]} · {preview[:20]} · {state}",
            callback_data=f"m:open:{case['id']}:{page}",
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="قبلی", callback_data=f"m:list:{page - 1}"))
    if (page + 1) * INBOX_PAGE_SIZE < total:
        navigation.append(InlineKeyboardButton(text="بعدی", callback_data=f"m:list:{page + 1}"))
    if navigation:
        builder.row(*navigation)
    builder.adjust(*([1] * len(cases)), 2)
    return builder.as_markup()


def _chat_keyboard(case: dict, page: int):
    builder = InlineKeyboardBuilder()
    if case["conversation_status"] in {"PENDING", "OPEN"}:
        builder.button(text="پاسخ", callback_data=f"m:reply:{case['id']}:{page}")
    if case["conversation_status"] in {"STARTING", "OPEN"}:
        builder.button(text="تمام شد", callback_data=f"a:end:{case['id']}")
    builder.button(text="فهرست گفتگوها", callback_data=f"m:list:{page}")
    builder.adjust(1)
    return builder.as_markup()


def _chat_transcript(case: dict, messages: list[dict]) -> str:
    have = json.loads(case.get("have_options") or "[]")
    have_values = json.loads(case.get("have_values") or "{}")
    need = json.loads(case.get("need_options") or "[]")
    state = {
        "PENDING": "درخواست جدید",
        "STARTING": "در حال شروع",
        "OPEN": "گفتگو باز",
        "CLOSED": "گفتگو پایان‌یافته",
    }.get(case.get("conversation_status"), "نامشخص")
    user_line = _chat_name(case)
    if case.get("username"):
        user_line += f" · @{case['username']}"
    have_text = "\n".join(
        f"  • {key}: {have_values.get(key, '')}" for key in have
    ) or "  • موردی ثبت نشده"
    need_text = "، ".join(need) or "موردی انتخاب نشده"
    lines = [
        f"💬 گفتگوی {user_line}",
        f"🆔 {case.get('telegram_id') or 'شناسه در دسترس نیست'}    📌 {state}",
        "",
        "📋 اطلاعات درخواست",
        "چه اطلاعاتی دارد؟",
        have_text,
        f"دنبال چه اطلاعاتی است؟\n  • {need_text}",
        "",
        "━━━━━━━━━━━━━━",
        "🗨 پیام‌ها",
    ]
    for item in messages[-15:]:
        body = (item.get("text") or f"[{item['media_type']}]").replace("\n", " ")[:180]
        sender = _chat_name(case) if item["sender_kind"] == "user" else "مدیر"
        message_time = (item["created_at"] or "").replace("T", " ")[:16]
        lines.append(f"\n{sender} · {message_time}\n{body}")
    text = "\n".join(lines)
    return text[-3800:]


async def _show_inbox(target: Message | CallbackQuery, database: Database, page: int = 0) -> None:
    page = max(page, 0)
    total = await database.count_admin_chats()
    cases = await database.list_admin_chats(INBOX_PAGE_SIZE, page * INBOX_PAGE_SIZE)
    text = f"گفتگوها · {total}"
    markup = _inbox_keyboard(cases, page, total)
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _show_chat(target: CallbackQuery | Message, database: Database, case_id: int, page: int) -> None:
    case = await database.get_case(case_id)
    if not case:
        if isinstance(target, CallbackQuery):
            await target.answer("این گفتگو پیدا نشد.")
        else:
            await target.answer("این گفتگو پیدا نشد.")
        return
    messages = await database.list_messages(case_id, limit=30)
    text = _chat_transcript(case, messages)
    markup = _chat_keyboard(case, page)
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(CommandStart(), F.chat.type == "private")
async def manager_start(message: Message, database: Database, settings: Settings) -> None:
    if _is_admin(message.from_user.id, message.chat.type, settings):
        await database.set_session(settings.admin_telegram_id, None, None)
        await _show_inbox(message, database)


@router.callback_query(F.data.startswith("m:"), F.message.chat.type == "private")
async def manager_navigation(
    callback: CallbackQuery, database: Database, settings: Settings
) -> None:
    if not _is_admin(callback.from_user.id, callback.message.chat.type, settings):
        await callback.answer()
        return
    parts = callback.data.split(":")
    try:
        if parts[1] == "list" and len(parts) == 3:
            await _show_inbox(callback, database, int(parts[2]))
            return
        if parts[1] in {"open", "reply"} and len(parts) == 4:
            case_id, page = int(parts[2]), int(parts[3])
            case = await database.get_case(case_id)
            if not case:
                await callback.answer("این گفتگو پیدا نشد.")
                return
            if parts[1] == "reply":
                if case["conversation_status"] not in {"PENDING", "OPEN"}:
                    await callback.answer("این گفتگو پایان یافته است.")
                    return
                await database.set_session(
                    settings.admin_telegram_id,
                    case_id,
                    "inbox_reply",
                    {"view_message_id": callback.message.message_id, "page": page},
                )
                await callback.answer("پیام بعدی برای همین گفتگو ارسال می‌شود.")
                return
            await _show_chat(callback, database, case_id, page)
            return
    except ValueError:
        pass
    await callback.answer()


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
    total = await database.count_admin_chats()
    inbox_cases = await database.list_admin_chats(INBOX_PAGE_SIZE, 0)
    try:
        await callback.message.edit_text(
            f"💬 گفتگوها · {total}",
            reply_markup=_inbox_keyboard(inbox_cases, 0, total),
        )
    except TelegramAPIError:
        logger.info("manager Inbox could not be refreshed after case %s", case_id)


@router.message(F.chat.type == "private")
async def reply_to_request(
    message: Message, bot: Bot, database: Database, settings: Settings
) -> None:
    if not _is_admin(message.from_user.id, message.chat.type, settings):
        return
    if message.text and message.text.startswith("/"):
        return
    reply_to = message.reply_to_message
    manager_session = await database.get_session(settings.admin_telegram_id)
    if not reply_to:
        if not manager_session or manager_session.get("mode") != "inbox_reply":
            return
        case_id = manager_session.get("case_id")
        case = await database.get_case(case_id) if case_id else None
    else:
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
        context = manager_session.get("context", {}) if manager_session else {}
        control_message_id = reply_to.message_id if reply_to else context.get("view_message_id")
        if control_message_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=control_message_id,
                    reply_markup=finish_conversation(case["id"]),
                )
            except TelegramAPIError:
                logger.warning("could not attach finish button to case %s", case["id"])
    if manager_session and manager_session.get("mode") == "inbox_reply":
        await database.set_session(
            settings.admin_telegram_id,
            case["id"],
            "inbox_reply",
            manager_session.get("context", {}),
        )
        context = manager_session.get("context", {})
        view_message_id = context.get("view_message_id")
        if view_message_id:
            current_case = await database.get_case(case["id"])
            current_messages = await database.list_messages(case["id"], limit=30)
            if current_case:
                try:
                    await bot.edit_message_text(
                        _chat_transcript(current_case, current_messages),
                        chat_id=settings.admin_telegram_id,
                        message_id=view_message_id,
                        reply_markup=_chat_keyboard(current_case, int(context.get("page", 0))),
                    )
                except TelegramAPIError:
                    logger.warning("manager conversation view refresh failed for case %s", case["id"])