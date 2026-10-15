from app.domain import CaseStatus, INFO_OPTIONS, REQUEST_OPTIONS
from app.keyboards import admin_menu, case_details, case_list, form_options, user_home


def test_option_keyboard_callbacks_match_user_handler_action():
    keyboard = form_options("need", REQUEST_OPTIONS, [])
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    option_callbacks = callbacks[: len(REQUEST_OPTIONS)]
    assert option_callbacks == [f"u:toggle:need:{index}" for index in range(len(REQUEST_OPTIONS))]


def test_admin_case_actions_cover_review_and_receipt_retry():
    case = {"id": 12, "status": CaseStatus.NEW.value, "retention_hold": 0}
    callbacks = [button.callback_data for row in case_details(case).inline_keyboard for button in row]
    assert "a:transition:review:12" in callbacks

    case["status"] = CaseStatus.UNDER_REVIEW.value
    callbacks = [button.callback_data for row in case_details(case).inline_keyboard for button in row]
    assert "a:transition:review:12" not in callbacks

    case["status"] = CaseStatus.RECEIPT_REVIEW.value
    callbacks = [button.callback_data for row in case_details(case).inline_keyboard for button in row]
    assert "a:send:receipt_request:12" in callbacks


def test_entry_menus_are_limited_to_primary_actions():
    user_callbacks = [
        button.callback_data
        for row in user_home().inline_keyboard
        for button in row
    ]
    admin_callbacks = [
        button.callback_data
        for row in admin_menu({}).inline_keyboard
        for button in row
    ]

    assert user_callbacks == ["u:new"]
    assert admin_callbacks == ["a:list:new:0", "a:list:conversations:0", "a:list:finished:0"]


def test_case_keyboard_exposes_explicit_conversation_lifecycle():
    case = {"id": 13, "status": CaseStatus.NEW.value, "conversation_status": "PENDING"}
    callbacks = [button.callback_data for row in case_details(case).inline_keyboard for button in row]
    assert "a:start:13" in callbacks

    case["conversation_status"] = "OPEN"
    callbacks = [button.callback_data for row in case_details(case).inline_keyboard for button in row]
    assert "a:end:13" in callbacks

    case["conversation_status"] = "CLOSED"
    callbacks = [button.callback_data for row in case_details(case).inline_keyboard for button in row]
    assert "a:start:13" not in callbacks


def test_manager_list_row_includes_person_summary_time_and_chat_state():
    case = {
        "id": 14,
        "case_number": "R-000014",
        "status_label": "جدید",
        "created_at": "2026-10-15T12:30:00+00:00",
        "last_message_at": "2026-10-15T12:45:00+00:00",
        "last_message": "سلام، درخواست دارم",
        "conversation_status": "OPEN",
        "first_name": "کاربر",
        "last_name": "نمونه",
    }
    keyboard = case_list([case], "conversations", 0, 1, 8)
    text = keyboard.inline_keyboard[0][0].text

    assert "کاربر نمونه"[:12] in text
    assert "سلام" in text
    assert "12:45" in text
    assert "باز" in text
    assert len(text) <= 64