from __future__ import annotations

from typing import NotRequired, TypedDict


class DraftData(TypedDict):
    have_fields: NotRequired[list[str]]
    have_values: NotRequired[dict[str, str]]
    requested_fields: NotRequired[list[str]]
    requested_values: NotRequired[dict[str, str]]
    chat_message_ids: NotRequired[list[int]]
    field_index: NotRequired[int]


class DraftRecord(TypedDict):
    step: str
    data: DraftData


class CaseSummary(TypedDict):
    id: int
    case_number: str
    status: str
    created_at: str
    conversation_status: NotRequired[str]
    first_name: NotRequired[str | None]
    last_name: NotRequired[str | None]
    username: NotRequired[str | None]
    telegram_id: NotRequired[int | None]
    last_message_at: NotRequired[str | None]
    last_message: NotRequired[str | None]


class CaseRecord(CaseSummary):
    user_id: int | None
    updated_at: str
    closed_at: str | None
    retention_hold: int
    have_options: str
    need_options: str
    have_values: str
    requested_values: str
    conversation_status: str
    other_request: str | None
    description: str | None
    telegram_id: int | None
    first_name: str | None
    last_name: str | None
    username: str | None


class SessionRecord(TypedDict):
    telegram_id: int
    case_id: int | None
    mode: str | None
    context: dict[str, int | str]
    updated_at: str


class MessageRecord(TypedDict):
    id: int
    case_id: int
    sender_kind: str
    media_type: str
    text: str | None
    file_id: str | None
    source_chat_id: int
    source_message_id: int
    destination_message_id: int | None
    delivery_state: str
    created_at: str


class PendingReceiptRecord(MessageRecord):
    admin_note: str | None


class PaymentRecord(TypedDict):
    id: int
    case_id: int
    amount: int | None
    expected_amount: int | None
    declared_amount: int | None
    currency: str | None
    method: str | None
    instructions_message_id: int | None
    receipt_message_id: int | None
    status: str
    reviewed_by: int | None
    reviewed_at: str | None
    result_note: str | None
    admin_note: str | None
    created_at: str


class AuditEventRecord(TypedDict):
    actor_id: int
    actor_kind: str
    event_type: str
    old_status: str | None
    new_status: str | None
    created_at: str