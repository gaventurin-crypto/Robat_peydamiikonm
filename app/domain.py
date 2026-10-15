from __future__ import annotations

from enum import StrEnum


class CaseStatus(StrEnum):
    NEW = "NEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    RECEIPT_REVIEW = "RECEIPT_REVIEW"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


STATUS_LABELS = {
    CaseStatus.NEW: "جدید",
    CaseStatus.UNDER_REVIEW: "در حال بررسی",
    CaseStatus.NEEDS_INFORMATION: "نیازمند اطلاعات تکمیلی",
    CaseStatus.APPROVED: "تأییدشده",
    CaseStatus.REJECTED: "ردشده",
    CaseStatus.AWAITING_PAYMENT: "در انتظار پرداخت",
    CaseStatus.RECEIPT_REVIEW: "رسید در انتظار بررسی",
    CaseStatus.PAYMENT_CONFIRMED: "پرداخت تأییدشده",
    CaseStatus.COMPLETED: "تکمیل‌شده",
    CaseStatus.CANCELLED: "لغوشده",
    CaseStatus.EXPIRED: "منقضی‌شده",
}

INFO_OPTIONS = (
    "شماره تماس",
    "شماره کارت",
    "نام و نام خانوادگی",
    "تاریخ تولد یا حدود سن",
    "جنسیت",
    "استان محل زندگی",
    "شهر محل زندگی",
    "کد ملی",
    "توضیحات تکمیلی",
)
LEGACY_INFO_OPTIONS = (
    "نام و نام خانوادگی",
    "آدرس",
    "شماره تماس",
    "کد ملی",
    "تاریخ تولد",
    "شماره حساب یا کارت بانکی",
)
REQUEST_OPTIONS = INFO_OPTIONS

ALLOWED_TRANSITIONS = {
    CaseStatus.NEW: {CaseStatus.UNDER_REVIEW, CaseStatus.NEEDS_INFORMATION, CaseStatus.APPROVED, CaseStatus.REJECTED, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.UNDER_REVIEW: {CaseStatus.NEEDS_INFORMATION, CaseStatus.APPROVED, CaseStatus.REJECTED, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.NEEDS_INFORMATION: {CaseStatus.UNDER_REVIEW, CaseStatus.APPROVED, CaseStatus.REJECTED, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.APPROVED: {CaseStatus.UNDER_REVIEW, CaseStatus.NEEDS_INFORMATION, CaseStatus.AWAITING_PAYMENT, CaseStatus.COMPLETED, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.REJECTED: {CaseStatus.UNDER_REVIEW, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.AWAITING_PAYMENT: {CaseStatus.RECEIPT_REVIEW, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.RECEIPT_REVIEW: {CaseStatus.AWAITING_PAYMENT, CaseStatus.PAYMENT_CONFIRMED, CaseStatus.CANCELLED, CaseStatus.EXPIRED},
    CaseStatus.PAYMENT_CONFIRMED: {CaseStatus.COMPLETED, CaseStatus.CANCELLED},
    CaseStatus.COMPLETED: {CaseStatus.EXPIRED},
    CaseStatus.CANCELLED: {CaseStatus.EXPIRED},
    CaseStatus.EXPIRED: set(),
}


class InvalidTransition(ValueError):
    pass


def validate_transition(current: CaseStatus, target: CaseStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"انتقال از {current.value} به {target.value} مجاز نیست")


def normalize_case_id(value: str) -> str:
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    normalized = value.strip().translate(translation).upper()
    if normalized.startswith("R-") and normalized[2:].isdigit():
        return f"R-{int(normalized[2:]):06d}"
    return normalized


def toggle_selection(current: list[str], option: str, allowed: tuple[str, ...]) -> list[str]:
    if option not in allowed:
        raise ValueError("گزینه نامعتبر است")
    selected = list(current)
    if option in selected:
        selected.remove(option)
    else:
        selected.append(option)
    return selected