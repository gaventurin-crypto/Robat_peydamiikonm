from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.database import Database
from app.domain import CaseStatus, STATUS_LABELS, normalize_case_id
from app.keyboards import (
    FILTERS,
    admin_menu,
    cancel_operation,
    case_details,
    case_list,
    confirm_action,
)
from app.media import message_media

logger = logging.getLogger(__name__)
router = Router(name="admin")
PAGE_SIZE = 8
_PAYMENT_NUMBER_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _is_admin(user_id: int, chat_type: str, settings: Settings) -> bool:
    return user_id == settings.admin_telegram_id and chat_type == "private"


def _format_time(value: str | None) -> str:
    if not value:
        return "ثبت نشده"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def _parse_payment_details(value: str) -> tuple[int, str, str] | None:
    parts = value.translate(_PAYMENT_NUMBER_TRANSLATION).replace(",", "").split()
    if len(parts) < 2 or not parts[0].isdigit() or not re.fullmatch(r"[A-Za-z]{3}", parts[1]):
        return None
    amount = int(parts[0])
    if amount <= 0 or amount > 10**12:
        return None
    return amount, parts[1].upper(), " ".join(parts[2:])[:80] or "manual"


async def _show_menu(target: Message | CallbackQuery, database: Database, settings: Settings) -> None:
    counts = {
        key: await database.count_cases(conversation_status=conversation_status)
        for key, (_, conversation_status) in FILTERS.items()
    }
    text = "پنل مدیریت درخواست‌ها\nپرونده را از یکی از بخش‌ها انتخاب کن."
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=admin_menu(counts))
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=admin_menu(counts))
    else:
        await target.answer(text, reply_markup=admin_menu(counts))


def _case_summary(case: dict) -> str:
    status = STATUS_LABELS[CaseStatus(case["status"])]
    name = " ".join(
        value for value in (case.get("first_name"), case.get("last_name")) if value
    ) or "ثبت نشده"
    username = f"@{case['username']}" if case.get("username") else "ندارد"
    user_id = str(case["telegram_id"]) if case.get("telegram_id") else "حذف‌شده طبق مهلت نگهداری"
    fields = json.loads(case.get("need_options") or "[]")
    values = json.loads(case.get("requested_values") or "{}")
    details = "\n".join(f"{field}: {values.get(field, 'ثبت نشده')}" for field in fields)
    conversation = {
        "PENDING": "جدید",
        "STARTING": "در حال شروع",
        "OPEN": "باز",
        "CLOSED": "پایان‌یافته",
    }.get(case.get("conversation_status", "PENDING"), "نامشخص")
    return (
        f"درخواست {case['case_number']} · {status}\n"
        f"👤 {name} · {username}\nشناسه تلگرام: {user_id}\n"
        f"🕒 ثبت: {_format_time(case['created_at'])}\n"
        f"📌 گفتگو: {conversation}\n\n📋 اطلاعات درخواست\n{details or 'موردی انتخاب نشده'}\n\n"
        f"نگهداری: {'توقف حذف خودکار' if case.get('retention_hold') else 'مهلت معمول'}"
    )


async def _show_long_text(message: Message, title: str, value: str | None) -> None:
    if not value:
        return
    chunks = [value[index : index + 3500] for index in range(0, len(value), 3500)]
    for index, chunk in enumerate(chunks, start=1):
        suffix = f" (بخش {index}/{len(chunks)})" if len(chunks) > 1 else ""
        await message.answer(f"{title}{suffix}:\n{chunk}")


async def _show_case(
    target: Message | CallbackQuery, database: Database, case: dict
) -> None:
    text = _case_summary(case)
    keyboard = case_details(case)
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=keyboard)
        message = target.message
    else:
        await target.answer(text, reply_markup=keyboard)
        message = target
    recent_messages = await database.list_messages(case["id"], 8)
    if recent_messages:
        lines = []
        for item in recent_messages:
            body = (item["text"] or f"[{item['media_type']}]").replace("\n", " ")[:120]
            lines.append(
                f"{item['sender_kind']} · {_format_time(item['created_at'])}: {body}"
            )
        await _show_long_text(message, "پیام‌های اخیر", "\n".join(lines))
    await _show_long_text(message, "سایر موارد درخواستی", case["other_request"])
    await _show_long_text(message, "توضیحات کاربر", case["description"])
    if CaseStatus(case["status"]) == CaseStatus.RECEIPT_REVIEW:
        receipt = await database.get_pending_receipt(case["id"])
        if receipt and receipt.get("admin_note"):
            await message.answer(f"یادداشت داخلی رسید:\n{receipt['admin_note']}")


async def _show_cases(
    callback: CallbackQuery, database: Database, filter_key: str, page: int
) -> None:
    if filter_key not in FILTERS:
        await callback.answer("بخش پرونده نامعتبر است.", show_alert=True)
        return
    label, conversation_status = FILTERS[filter_key]
    total = await database.count_cases(conversation_status=conversation_status)
    page = max(page, 0)
    cases = await database.list_cases(
        None, PAGE_SIZE, page * PAGE_SIZE, conversation_status=conversation_status
    )
    decorated = [dict(case, status_label=STATUS_LABELS[CaseStatus(case["status"])]) for case in cases]
    await callback.answer()
    text = f"{label}\nتعداد: {total} · صفحهٔ {page + 1}"
    try:
        await callback.message.edit_text(
            text,
            reply_markup=case_list(decorated, filter_key, page, total, PAGE_SIZE),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text,
            reply_markup=case_list(decorated, filter_key, page, total, PAGE_SIZE),
        )


async def _record_and_deliver(
    message: Message,
    bot: Bot,
    database: Database,
    case: dict,
    media_type: str,
    file_id: str | None,
    text: str | None,
    settings: Settings,
    action: str,
) -> bool:
    destination = case.get("telegram_id")
    if not destination:
        await message.answer("شناسهٔ مقصد موجود نیست؛ اطلاعات این پرونده حذف شده است.")
        return False
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
        await message.answer("این پیام قبلاً پردازش شده است و دوباره ارسال نمی‌شود.")
        return False
    try:
        copied = await bot.copy_message(
            chat_id=destination,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        await database.update_message_delivery(message.chat.id, message.message_id, "FAILED")
        logger.warning("admin message delivery failed for case %s", case["id"])
        await message.answer("ارسال ناموفق بود؛ پیام ارسال‌شده ثبت نشد. مقصد را بررسی و دوباره اقدام کن.")
        return False

    await database.update_message_delivery(message.chat.id, message.message_id, "SENT", copied.message_id)
    try:
        if action == "payment":
            payment_details = case.get("payment_details", {})
            await database.add_payment_instruction(
                case["id"],
                message.message_id,
                settings.admin_telegram_id,
                expected_amount=payment_details.get("amount"),
                declared_amount=payment_details.get("amount"),
                currency=payment_details.get("currency"),
                method=payment_details.get("method"),
            )
        elif action == "ask" and CaseStatus(case["status"]) != CaseStatus.NEEDS_INFORMATION:
            await database.transition_case(case["id"], CaseStatus.NEEDS_INFORMATION, settings.admin_telegram_id)
        elif action == "receipt_request":
            await database.review_receipt(
                case["id"], approved=False, admin_id=settings.admin_telegram_id
            )
    except Exception as error:
        logger.error(
            "admin message delivered but case update failed; case_id=%s action=%s exception_type=%s",
            case["id"],
            action,
            type(error).__name__,
        )
        await message.answer(
            f"پیام به پروندهٔ {case['case_number']} ارسال شد، اما ثبت تغییر وضعیت انجام نشد. "
            "وضعیت پرونده را بررسی کن و از ارسال تکراری خودداری کن."
        )
        return False
    if action in {"conversation_start", "conversation", "ask"}:
        if action == "conversation_start" or case.get("conversation_status") == "STARTING":
            if not await database.activate_conversation(case["id"], settings.admin_telegram_id):
                await message.answer("پیام ارسال شد، اما شروع گفتگو ثبت نشد.")
                return False
        await database.set_session(settings.admin_telegram_id, case["id"], "conversation")
        await database.set_session(case["telegram_id"], case["id"], "conversation")
    elif action in {"payment", "receipt_request"}:
        current_case = await database.get_case(case["id"])
        if current_case and current_case["status"] == CaseStatus.AWAITING_PAYMENT.value:
            user_mode = "conversation" if current_case["conversation_status"] == "OPEN" else "receipt"
            await database.set_session(case["telegram_id"], case["id"], user_mode)
    if action not in {"conversation_start", "conversation"}:
        await message.answer(f"پیام پروندهٔ {case['case_number']} با موفقیت ارسال شد.")
    return True


@router.message(CommandStart(), F.chat.type == "private")
@router.message(Command("admin"), F.chat.type == "private")
async def admin_start(message: Message, database: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, message.chat.type, settings):
        return
    await database.set_session(settings.admin_telegram_id, None, None)
    await _show_menu(message, database, settings)


@router.callback_query(F.data.startswith("a:"), F.message.chat.type == "private")
async def admin_callbacks(
    callback: CallbackQuery, bot: Bot, database: Database, settings: Settings
) -> None:
    if not _is_admin(callback.from_user.id, callback.message.chat.type, settings):
        await callback.answer("دسترسی مجاز نیست.", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1]

    if action == "menu":
        await database.set_session(settings.admin_telegram_id, None, None)
        await _show_menu(callback, database, settings)
        return
    if action == "list" and len(parts) == 4:
        try:
            page = int(parts[3])
        except ValueError:
            await callback.answer("شمارهٔ صفحه نامعتبر است.", show_alert=True)
            return
        await _show_cases(callback, database, parts[2], page)
        return
    if action == "open" and len(parts) == 5:
        try:
            case_id = int(parts[2])
        except ValueError:
            await callback.answer("شناسهٔ پرونده نامعتبر است.", show_alert=True)
            return
        case = await database.get_case(case_id)
        if not case:
            await callback.answer("پرونده پیدا نشد.", show_alert=True)
            return
        await database.set_session(settings.admin_telegram_id, case_id, None)
        await _show_case(callback, database, case)
        return
    if action == "search":
        await database.set_session(settings.admin_telegram_id, None, "search")
        await callback.answer()
        await callback.message.edit_text("شمارهٔ پرونده مانند R-000123 را بفرست.", reply_markup=cancel_operation())
        return
    if action == "status":
        await database.set_session(settings.admin_telegram_id, None, None)
        current_counts = "\n".join(
            f"{name}: {await database.count_cases(conversation_status=conversation_status)}"
            for name, conversation_status in FILTERS.values()
        )
        text = (
            "وضعیت سرویس: ربات در حال پاسخ‌گویی است.\n"
            f"ذخیره‌سازی: SQLite · {settings.database_path}\n"
            f"حذف پروندهٔ بسته‌شده: پس از {settings.retention_hours} ساعت\n"
            f"حداکثر فایل: {settings.max_upload_bytes // (1024 * 1024)} مگابایت\n\n"
            f"شمار پرونده‌ها:\n{current_counts}"
        )
        await callback.answer()
        await callback.message.edit_text(text, reply_markup=cancel_operation())
        return
    if action == "start" and len(parts) == 3:
        try:
            case_id = int(parts[2])
        except ValueError:
            await callback.answer("شناسهٔ پرونده نامعتبر است.", show_alert=True)
            return
        case = await database.get_case(case_id)
        if not case or not case.get("telegram_id"):
            await callback.answer("پرونده یا کاربر در دسترس نیست.", show_alert=True)
            return
        if not await database.start_conversation(case_id, settings.admin_telegram_id):
            await callback.answer("برای این کاربر گفتگوی دیگری باز است یا پرونده تغییر کرده.", show_alert=True)
            return
        await database.set_session(settings.admin_telegram_id, case_id, "conversation_start")
        await callback.answer()
        await callback.message.answer(
            f"پیام اول برای {case['case_number']} را بنویس یا بفرست.",
            reply_markup=cancel_operation(),
        )
        return
    if action == "continue" and len(parts) == 3:
        try:
            case_id = int(parts[2])
            case = await database.get_case(case_id)
        except ValueError:
            case = None
        if not case or case.get("conversation_status") not in {"STARTING", "OPEN"}:
            await callback.answer("گفتگو در دسترس نیست.", show_alert=True)
            return
        mode = "conversation_start" if case["conversation_status"] == "STARTING" else "conversation"
        await database.set_session(settings.admin_telegram_id, case_id, mode)
        await callback.answer()
        await callback.message.answer(
            f"{case['case_number']} · پیام را بفرست.",
            reply_markup=cancel_operation(),
        )
        return
    if action == "end" and len(parts) == 3:
        try:
            case_id = int(parts[2])
        except ValueError:
            await callback.answer("شناسهٔ پرونده نامعتبر است.", show_alert=True)
            return
        case = await database.get_case(case_id)
        if not case or not await database.end_conversation(case_id, settings.admin_telegram_id):
            await callback.answer("گفتگو قبلاً پایان یافته است.", show_alert=True)
            return
        await database.set_session(settings.admin_telegram_id, None, None)
        try:
            if case.get("telegram_id"):
                await bot.send_message(case["telegram_id"], "گفتگو پایان یافت.")
        except TelegramAPIError:
            logger.warning("conversation close notice delivery failed for case %s", case_id)
        await callback.answer("گفتگو پایان یافت.")
        await _show_case(callback, database, await database.get_case(case_id))
        return
    if action == "cancel":
        session = await database.get_session(settings.admin_telegram_id)
        if session and session.get("case_id"):
            await database.set_session(settings.admin_telegram_id, session["case_id"], None)
            case = await database.get_case(session["case_id"])
            if case:
                await _show_case(callback, database, case)
                return
        await database.set_session(settings.admin_telegram_id, None, None)
        await _show_menu(callback, database, settings)
        return
    if action == "send" and len(parts) == 4:
        mode = parts[2]
        try:
            case_id = int(parts[3])
        except ValueError:
            await callback.answer("شناسهٔ پرونده نامعتبر است.", show_alert=True)
            return
        case = await database.get_case(case_id)
        if not case or not case.get("telegram_id"):
            await callback.answer("پرونده یا مقصد در دسترس نیست.", show_alert=True)
            return
        status = CaseStatus(case["status"])
        if mode == "payment" and status != CaseStatus.APPROVED:
            await callback.answer("فقط پروندهٔ تأییدشده می‌تواند اطلاعات پرداخت دریافت کند.", show_alert=True)
            return
        if mode == "receipt_request" and status != CaseStatus.RECEIPT_REVIEW:
            await callback.answer("این پرونده رسیدی در انتظار بررسی ندارد.", show_alert=True)
            return
        if mode == "ask" and status not in {
            CaseStatus.NEW,
            CaseStatus.UNDER_REVIEW,
            CaseStatus.NEEDS_INFORMATION,
            CaseStatus.APPROVED,
        }:
            await callback.answer("در این وضعیت نمی‌توان اطلاعات تکمیلی خواست.", show_alert=True)
            return
        if mode not in {"reply", "ask", "payment", "receipt_request"}:
            await callback.answer("عملیات ناشناخته است.", show_alert=True)
            return
        if mode in {"reply", "ask"} and case.get("conversation_status") in {"PENDING", "CLOSED"}:
            if not await database.start_conversation(case_id, settings.admin_telegram_id):
                await callback.answer("برای این کاربر گفتگوی دیگری باز است.", show_alert=True)
                return
            if mode == "reply":
                mode = "conversation_start"
        elif mode == "reply":
            mode = "conversation"
        if mode == "payment":
            mode = "payment_amount"
        await database.set_session(settings.admin_telegram_id, case_id, mode)
        await callback.answer()
        prompt = {
            "reply": "پیام موردنظر را برای متقاضی پروندهٔ ",
            "conversation_start": "پیام اول گفتگو را برای پروندهٔ ",
            "conversation": "پیام گفتگو را برای پروندهٔ ",
            "ask": "متن درخواست اطلاعات تکمیلی را خودت بنویس؛ برای پروندهٔ ",
            "payment_amount": "مبلغ، ارز و روش مجاز را بنویس؛ مثال: 150000 IRR کارت به کارت. سپس پیام کامل پرداخت را می‌فرستی. پروندهٔ ",
            "receipt_request": "متن درخواست رسید یا توضیح مجدد را خودت بنویس؛ برای پروندهٔ ",
        }[mode]
        await callback.message.answer(
            f"{prompt}{case['case_number']}"
            + ("" if mode == "payment_amount" else "\nپیام متنی، عکس، ویس یا فایل مجاز بفرست."),
            reply_markup=cancel_operation(),
        )
        return
    if action == "receipt_note" and len(parts) == 3:
        try:
            case_id = int(parts[2])
            case = await database.get_case(case_id)
            if not case or CaseStatus(case["status"]) != CaseStatus.RECEIPT_REVIEW:
                raise LookupError
        except (ValueError, LookupError):
            await callback.answer("رسید در انتظار بررسی پیدا نشد.", show_alert=True)
            return
        await database.set_session(settings.admin_telegram_id, case_id, "receipt_note")
        await callback.answer()
        await callback.message.answer(
            f"یادداشت داخلی برای رسید پروندهٔ {case['case_number']} را بنویس.",
            reply_markup=cancel_operation(),
        )
        return
    if action == "transition" and len(parts) == 4:
        try:
            case_id = int(parts[3])
            case = await database.get_case(case_id)
            target = CaseStatus.APPROVED if parts[2] == "approve" else CaseStatus.UNDER_REVIEW
            if not case or parts[2] not in {"approve", "review"}:
                raise ValueError
            await database.transition_case(case_id, target, settings.admin_telegram_id)
            await callback.answer(f"وضعیت: {STATUS_LABELS[target]}")
            await _show_case(callback, database, await database.get_case(case_id))
        except (ValueError, LookupError, KeyError):
            await callback.answer("تغییر وضعیت مجاز نیست.", show_alert=True)
        return
    if action == "confirm" and len(parts) == 4:
        operation = parts[2]
        try:
            case_id = int(parts[3])
        except ValueError:
            await callback.answer("شناسهٔ پرونده نامعتبر است.", show_alert=True)
            return
        labels = {
            "reject": "رد درخواست",
            "cancel": "لغو پرونده",
            "close": "تکمیل و بستن پرونده",
            "hold": "توقف حذف خودکار به علت الزام نگهداری",
            "unhold": "فعال‌کردن حذف خودکار طبق مهلت مصوب",
        }
        if operation not in labels or not await database.get_case(case_id):
            await callback.answer("عملیات معتبر نیست.", show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            f"آیا برای «{labels[operation]}» پرونده تأیید نهایی می‌کنی؟",
            reply_markup=confirm_action(operation, case_id),
        )
        return
    if action == "do" and len(parts) == 4:
        operation = parts[2]
        try:
            case_id = int(parts[3])
            if operation in {"hold", "unhold"}:
                case = await database.set_retention_hold(
                    case_id, enabled=operation == "hold", admin_id=settings.admin_telegram_id
                )
                await callback.answer("سیاست نگهداری به‌روزرسانی شد.")
                await _show_case(callback, database, case)
                return
            target = {
                "reject": CaseStatus.REJECTED,
                "cancel": CaseStatus.CANCELLED,
                "close": CaseStatus.COMPLETED,
            }[operation]
            case = await database.transition_case(case_id, target, settings.admin_telegram_id)
            await callback.answer("تغییر ثبت شد.")
            await _show_case(callback, database, case)
        except (ValueError, LookupError, KeyError):
            await callback.answer("پرونده تغییر کرده یا این انتقال مجاز نیست.", show_alert=True)
        return
    if action == "receipt" and len(parts) == 4:
        operation = parts[2]
        try:
            case_id = int(parts[3])
            case = await database.get_case(case_id)
            if not case or CaseStatus(case["status"]) != CaseStatus.RECEIPT_REVIEW:
                raise LookupError
            if operation == "view":
                receipt = await database.get_pending_receipt(case_id)
                if not receipt:
                    raise LookupError
                await callback.answer("رسید پرونده")
                if receipt["file_id"]:
                    await bot.copy_message(
                        chat_id=settings.admin_telegram_id,
                        from_chat_id=receipt["source_chat_id"],
                        message_id=receipt["source_message_id"],
                    )
                else:
                    await callback.message.answer(receipt["text"] or "رسید متنی خالی است.")
                return
            if operation not in {"yes", "no"}:
                raise ValueError
            updated = await database.review_receipt(
                case_id, approved=operation == "yes", admin_id=settings.admin_telegram_id
            )
            await callback.answer("نتیجهٔ بررسی ثبت شد.")
            await _show_case(callback, database, updated)
        except (ValueError, LookupError, TelegramAPIError):
            await callback.answer("رسید قابل بررسی نیست یا عملیات انجام نشد.", show_alert=True)
        return
    if action == "history" and len(parts) == 3:
        try:
            case_id = int(parts[2])
            case = await database.get_case(case_id)
            if not case:
                raise LookupError
            events = await database.audit_history(case_id)
            messages = await database.list_messages(case_id, 15)
            await callback.answer()
            lines = [f"تاریخچهٔ {case['case_number']}: "]
            for event in events:
                old = STATUS_LABELS.get(CaseStatus(event["old_status"])) if event["old_status"] else ""
                new = STATUS_LABELS.get(CaseStatus(event["new_status"])) if event["new_status"] else ""
                transition = f": {old} → {new}" if old or new else ""
                lines.append(f"{_format_time(event['created_at'])} · {event['event_type']}{transition}")
            lines.append("\nپیام‌های اخیر:")
            for item in messages:
                body = (item["text"] or "[رسانه]").replace("\n", " ")[:250]
                lines.append(
                    f"{_format_time(item['created_at'])} · {item['sender_kind']} · "
                    f"{item['media_type']} · {item['delivery_state']} · {body}"
                )
            await callback.message.answer("\n".join(lines)[:3900])
            for item in messages:
                if item["text"] and item["media_type"] == "text":
                    await _show_long_text(callback.message, f"پیام {item['sender_kind']}", item["text"])
                if item["file_id"]:
                    try:
                        await bot.copy_message(
                            chat_id=settings.admin_telegram_id,
                            from_chat_id=item["source_chat_id"],
                            message_id=item["source_message_id"],
                        )
                    except TelegramAPIError:
                        await callback.message.answer("نمایش یکی از رسانه‌های تاریخچه ممکن نشد.")
            return
        except (ValueError, LookupError):
            await callback.answer("تاریخچه در دسترس نیست.", show_alert=True)
        return
    await callback.answer("این عملیات شناخته نشد.", show_alert=True)


@router.message(F.chat.type == "private")
async def admin_messages(message: Message, bot: Bot, database: Database, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, message.chat.type, settings):
        return
    if message.text and message.text.startswith("/"):
        return
    reply_to = getattr(message, "reply_to_message", None)
    session = await database.get_session(settings.admin_telegram_id)
    if reply_to:
        reply_case = await database.get_case_for_reply("user", reply_to.message_id)
        if not reply_case or reply_case.get("conversation_status") != "OPEN":
            await message.answer("گفتگوی مربوط به این پیام فعال نیست.")
            return
        await database.set_session(settings.admin_telegram_id, reply_case["id"], "conversation")
        session = await database.get_session(settings.admin_telegram_id)
    if not session:
        await _show_menu(message, database, settings)
        return
    if session["mode"] == "search":
        if not message.text:
            await message.answer("برای جست‌وجو شمارهٔ پرونده را به‌صورت متن بفرست.")
            return
        query = normalize_case_id(message.text)
        cases = await database.search_cases(query)
        decorated = [dict(case, status_label=STATUS_LABELS[CaseStatus(case["status"])]) for case in cases]
        await database.set_session(settings.admin_telegram_id, None, None)
        if not cases:
            await message.answer("پرونده‌ای با این شناسه پیدا نشد.", reply_markup=cancel_operation())
            return
        await message.answer(
            f"نتیجه‌های جست‌وجو: {len(cases)}",
            reply_markup=case_list(decorated, "search", 0, len(cases), PAGE_SIZE),
        )
        return

    mode = session.get("mode")
    case_id = session.get("case_id")
    if mode == "payment_amount":
        if not message.text:
            await message.answer("مبلغ، ارز و روش را به شکل متنی بفرست؛ مثال: 150000 IRR انتقال بانکی.")
            return
        payment_details = _parse_payment_details(message.text)
        if not payment_details:
            await message.answer("مبلغ مثبت و ارز سه‌حرفی معتبر بفرست؛ مثال: 150000 IRR انتقال بانکی.")
            return
        amount, currency, method = payment_details
        await database.set_session(
            settings.admin_telegram_id,
            case_id,
            "payment",
            {"amount": amount, "currency": currency, "method": method},
        )
        await message.answer("حالا متن، روش یا رسانهٔ پرداخت را برای متقاضی بفرست.")
        return
    if mode == "receipt_note":
        if not message.text or len(message.text) > 2000:
            await message.answer("یادداشت داخلی باید متنی و حداکثر ۲۰۰۰ نویسه باشد.")
            return
        try:
            await database.set_receipt_note(case_id, message.text.strip(), settings.admin_telegram_id)
        except LookupError:
            await message.answer("رسید دیگر در انتظار بررسی نیست.")
            return
        await database.set_session(settings.admin_telegram_id, case_id, None)
        await message.answer("یادداشت داخلی ثبت شد و برای کاربر ارسال نمی‌شود.")
        return
    if mode not in {
        "reply",
        "ask",
        "payment",
        "receipt_request",
        "conversation",
        "conversation_start",
    } or case_id is None:
        await message.answer("ابتدا پرونده را باز و عملیات را انتخاب کن.")
        return
    case = await database.get_case(case_id)
    if not case or not case.get("telegram_id"):
        await message.answer("پرونده یا مقصد در دسترس نیست.")
        return
    if mode == "conversation_start" and case.get("conversation_status") != "STARTING":
        await message.answer("شروع گفتگو در دسترس نیست.")
        return
    if mode == "conversation" and case.get("conversation_status") != "OPEN":
        await message.answer("این گفتگو پایان یافته است.")
        return
    if mode == "payment" and (
        CaseStatus(case["status"]) != CaseStatus.APPROVED
        or not message.text
        or not session.get("context", {}).get("amount")
    ):
        await message.answer("برای پروندهٔ تأییدشده، مبلغ ثبت‌شده و پیام متنیِ پرداخت لازم است.")
        return
    if mode == "receipt_request" and CaseStatus(case["status"]) != CaseStatus.RECEIPT_REVIEW:
        await message.answer("این پرونده دیگر رسیدی در انتظار بررسی ندارد؛ وضعیت را دوباره بررسی کن.")
        return
    media_type, file_id, text, error = await message_media(message, bot, settings.max_upload_bytes)
    if error:
        await message.answer(error)
        return
    if mode == "payment":
        payment_details = session["context"]
        case["payment_details"] = payment_details
    delivered = await _record_and_deliver(
        message, bot, database, case, media_type, file_id, text, settings, mode
    )
    if delivered:
        current_case = await database.get_case(case_id)
        if current_case and current_case["conversation_status"] == "OPEN":
            await database.set_session(settings.admin_telegram_id, case_id, "conversation")
        elif mode not in {"conversation", "conversation_start"}:
            await database.set_session(settings.admin_telegram_id, case_id, None)