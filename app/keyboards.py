from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.domain import CaseStatus


FILTERS: dict[str, tuple[str, str]] = {
    "new": ("درخواست‌های جدید", "PENDING"),
    "conversations": ("گفتگوها", "OPEN"),
    "finished": ("پایان‌یافته‌ها", "CLOSED"),
}


def user_home() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ثبت درخواست", callback_data="u:new")
    builder.adjust(1)
    return builder.as_markup()


def finish_conversation(case_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="تمام شد", callback_data=f"a:end:{case_id}")
    return builder.as_markup()


def open_conversation(case_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="باز کردن گفتگو", callback_data=f"m:open:{case_id}:0")
    return builder.as_markup()


def notice_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="می‌فهمم، ادامه", callback_data="u:notice:accept")
    builder.adjust(1)
    return builder.as_markup()


def form_options(
    step: str, options: tuple[str, ...], selected: list[str]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, option in enumerate(options):
        mark = "✅" if option in selected else "▫️"
        builder.button(text=f"{mark} {option}", callback_data=f"u:toggle:{step}:{index}")
    next_text = "ادامه ⟶" if step == "have" else "ثبت درخواست ✓"
    builder.row(InlineKeyboardButton(text=next_text, callback_data=f"u:next:{step}"))
    builder.adjust(*([1] * len(options)), 1)
    return builder.as_markup()


def user_cases(cases: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for case in cases:
        builder.button(
            text=f"{case['case_number']} · {case['status_label']}",
            callback_data=f"u:case:{case['id']}",
        )
    builder.button(text="بازگشت به منو", callback_data="u:home")
    builder.adjust(1)
    return builder.as_markup()


def admin_menu(counts: dict[str, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (label, _) in FILTERS.items():
        builder.button(text=f"{label} · {counts.get(key, 0)}", callback_data=f"a:list:{key}:0")
    builder.adjust(1)
    return builder.as_markup()


def case_list(
    cases: list[dict], filter_key: str, page: int, total: int, page_size: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for case in cases:
        display_name = " ".join(
            value for value in (case.get("first_name"), case.get("last_name")) if value
        ) or (f"@{case['username']}" if case.get("username") else str(case.get("telegram_id") or "کاربر"))
        preview = (case.get("last_message") or "درخواست ثبت‌شده").replace("\n", " ")
        last_time = (case.get("last_message_at") or case["created_at"]).split("T")[-1][:5]
        conversation_status = {
            "PENDING": "جدید",
            "STARTING": "در حال شروع",
            "OPEN": "باز",
            "CLOSED": "پایان",
        }.get(case.get("conversation_status"), "نامشخص")
        builder.button(
            text=f"{display_name[:12]} · {case['case_number']} · {preview[:10]} · {last_time} · {conversation_status}",
            callback_data=f"a:open:{case['id']}:{filter_key}:{page}",
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="قبلی", callback_data=f"a:list:{filter_key}:{page - 1}"))
    if (page + 1) * page_size < total:
        navigation.append(InlineKeyboardButton(text="بعدی", callback_data=f"a:list:{filter_key}:{page + 1}"))
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="منوی مدیریت", callback_data="a:menu"))
    builder.adjust(*([1] * len(cases)), 2, 1)
    return builder.as_markup()


def case_details(case: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    case_id = case["id"]
    status = CaseStatus(case["status"])
    conversation_status = case.get("conversation_status", "PENDING")
    if conversation_status == "OPEN":
        builder.button(text="پایان گفتگو", callback_data=f"a:end:{case_id}")
        builder.button(text="ادامه گفتگو", callback_data=f"a:continue:{case_id}")
    elif conversation_status == "STARTING":
        builder.button(text="ارسال پیام آغازین", callback_data=f"a:continue:{case_id}")
    elif conversation_status == "PENDING":
        builder.button(text="شروع گفتگو", callback_data=f"a:start:{case_id}")
    builder.button(text="درخواست اطلاعات تکمیلی", callback_data=f"a:send:ask:{case_id}")
    if status in {CaseStatus.NEW, CaseStatus.UNDER_REVIEW, CaseStatus.NEEDS_INFORMATION}:
        if status != CaseStatus.UNDER_REVIEW:
            builder.button(text="شروع بررسی", callback_data=f"a:transition:review:{case_id}")
        builder.button(text="تأیید درخواست", callback_data=f"a:transition:approve:{case_id}")
        builder.button(text="رد درخواست", callback_data=f"a:confirm:reject:{case_id}")
    if status == CaseStatus.APPROVED:
        builder.button(text="ارسال اطلاعات پرداخت", callback_data=f"a:send:payment:{case_id}")
    if status == CaseStatus.RECEIPT_REVIEW:
        builder.button(text="بررسی رسید", callback_data=f"a:receipt:view:{case_id}")
        builder.button(text="یادداشت داخلی رسید", callback_data=f"a:receipt_note:{case_id}")
        builder.button(text="رسید تأیید شد", callback_data=f"a:receipt:yes:{case_id}")
        builder.button(text="رسید رد شد", callback_data=f"a:receipt:no:{case_id}")
        builder.button(text="درخواست رسید یا توضیح مجدد", callback_data=f"a:send:receipt_request:{case_id}")
    if status == CaseStatus.PAYMENT_CONFIRMED:
        builder.button(text="بستن پرونده", callback_data=f"a:confirm:close:{case_id}")
    if status != CaseStatus.EXPIRED:
        operation = "unhold" if case.get("retention_hold") else "hold"
        label = "فعال‌کردن حذف خودکار" if case.get("retention_hold") else "توقف حذف خودکار"
        builder.button(text=label, callback_data=f"a:confirm:{operation}:{case_id}")
    if status not in {CaseStatus.COMPLETED, CaseStatus.CANCELLED, CaseStatus.EXPIRED}:
        builder.button(text="لغو پرونده", callback_data=f"a:confirm:cancel:{case_id}")
    if status in {CaseStatus.REJECTED, CaseStatus.NEEDS_INFORMATION, CaseStatus.APPROVED}:
        builder.button(text="بازگشت به بررسی", callback_data=f"a:transition:review:{case_id}")
    builder.button(text="مشاهدهٔ تاریخچه", callback_data=f"a:history:{case_id}")
    builder.button(text="منوی مدیریت", callback_data="a:menu")
    builder.adjust(1)
    return builder.as_markup()


def confirm_action(action: str, case_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="تأیید نهایی", callback_data=f"a:do:{action}:{case_id}")
    builder.button(text="لغو عملیات", callback_data=f"a:open:{case_id}:new:0")
    builder.adjust(1)
    return builder.as_markup()


def cancel_operation() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="لغو عملیات", callback_data="a:cancel")
    builder.button(text="منوی مدیریت", callback_data="a:menu")
    builder.adjust(1)
    return builder.as_markup()