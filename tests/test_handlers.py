import json
from types import SimpleNamespace
from pathlib import Path

import pytest
import pytest_asyncio

from app.admin_handlers import admin_callbacks, admin_messages
from app.conversation_handlers import end_conversation, reply_to_request
from app.database import Database
from app.domain import CaseStatus, INFO_OPTIONS
from app.user_handlers import start_user, user_callbacks, user_messages


@pytest_asyncio.fixture
async def database(tmp_path):
    database = Database(tmp_path / "handlers.sqlite3")
    await database.initialize()
    return database


class FakeBot:
    def __init__(self):
        self.copies = []
        self.sent = []
        self.deleted = []

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)
        return SimpleNamespace(message_id=500 + len(self.copies))

    async def forward_message(self, **kwargs):
        self.copies.append(kwargs)
        return SimpleNamespace(message_id=500 + len(self.copies))

    async def edit_message_reply_markup(self, **kwargs):
        self.sent.append(((), kwargs))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(id=args[0]), message_id=600 + len(self.sent)
        )

    async def get_file(self, file_id):
        return SimpleNamespace(file_path=file_id)

    async def download_file(self, file_path, destination):
        payload = {
            "photo-id": b"\xff\xd8\xffphoto",
            "pdf-id": b"%PDF-1.7\nsynthetic",
        }.get(file_path, b"OggSvoice")
        Path(destination).write_bytes(payload)


class FakeMessage:
    def __init__(
        self,
        user_id,
        message_id,
        text=None,
        photo=None,
        voice=None,
        document=None,
        reply_to_message=None,
    ):
        self.from_user = SimpleNamespace(
            id=user_id, first_name="کاربر آزمایشی", last_name="", username="sample_user"
        )
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.photo = photo
        self.voice = voice
        self.document = document
        self.reply_to_message = reply_to_message
        self.audio = None
        self.answers = []
        self.edits = []
        self.markups = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def edit_reply_markup(self, **kwargs):
        self.markups.append(kwargs.get("reply_markup"))


class FakeCallback:
    def __init__(self, user_id, data):
        self.from_user = SimpleNamespace(
            id=user_id, first_name="کاربر آزمایشی", last_name="", username="sample_user"
        )
        self.message = FakeMessage(user_id, 1)
        self.data = data
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture
def settings():
    return SimpleNamespace(
        admin_telegram_id=900,
        max_upload_bytes=10 * 1024 * 1024,
        welcome_text="پیام خوشامد آزمایشی",
        info_options=INFO_OPTIONS,
        notice_version="test-notice",
    )


def request_data():
    return {
        "have_fields": ["آدرس"],
        "have_values": {"آدرس": "تهران"},
        "need_fields": ["آدرس"],
        "requested_fields": ["آدرس"],
        "requested_values": {},
    }


async def open_conversation(database, case, telegram_id):
    assert await database.start_conversation(case["id"], 900)
    assert await database.activate_conversation(case["id"], 900)
    await database.set_session(telegram_id, case["id"], "conversation")


@pytest.mark.asyncio
async def test_start_registers_user_and_shows_welcome(database, settings):
    message = FakeMessage(90, 1, text="/start")

    await start_user(message, database, settings)

    assert await database.register_user(90) > 0
    assert message.answers == ["سلام! برای ثبت درخواست، دکمهٔ زیر را بزن."]
    callbacks = [
        button.callback_data
        for row in message.markups[-1].inline_keyboard
        for button in row
    ]
    assert callbacks == ["u:new"]


@pytest.mark.asyncio
async def test_ordinary_message_without_active_case_is_handled(database, settings):
    message = FakeMessage(92, 3, text="سلام")

    await user_messages(message, FakeBot(), database, settings)

    assert message.answers == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "text", "photo", "voice", "media_type"),
    [
        (11, "متن آزمایشی", None, None, "text"),
        (12, None, [SimpleNamespace(file_id="photo-id", file_size=100)], None, "photo"),
        (13, None, None, SimpleNamespace(file_id="voice-id", file_size=100), "voice"),
    ],
)
async def test_user_messages_are_bound_to_selected_case(
    database, settings, message_id, text, photo, voice, media_type
):
    case = await database.create_case(101, request_data())
    other = await database.create_case(202, request_data())
    await open_conversation(database, case, 101)
    bot = FakeBot()
    message = FakeMessage(101, message_id, text=text, photo=photo, voice=voice)

    await user_messages(message, bot, database, settings)

    assert bot.copies[0]["chat_id"] == settings.admin_telegram_id
    stored = await database.list_messages(case["id"])
    assert len(stored) == 1
    assert stored[0]["media_type"] == media_type
    assert stored[0]["delivery_state"] == "SENT"
    assert await database.list_messages(other["id"]) == []


@pytest.mark.asyncio
async def test_user_document_is_validated_and_forwarded(database, settings):
    case = await database.create_case(111, request_data())
    await open_conversation(database, case, 111)
    message = FakeMessage(
        111,
        14,
        document=SimpleNamespace(
            file_id="pdf-id",
            file_size=17,
            mime_type="application/pdf",
            file_name="request.pdf",
        ),
    )
    bot = FakeBot()

    await user_messages(message, bot, database, settings)

    assert bot.copies[0]["chat_id"] == settings.admin_telegram_id
    assert (await database.list_messages(case["id"]))[0]["media_type"] == "document"


@pytest.mark.asyncio
async def test_user_delivery_failure_is_recorded_for_retry(database, settings):
    case = await database.create_case(112, request_data())
    await open_conversation(database, case, 112)
    bot = FakeBot()

    async def fail_copy(**kwargs):
        raise RuntimeError("synthetic Telegram API failure")

    bot.forward_message = fail_copy
    message = FakeMessage(112, 16, text="پیام آزمایشی")

    await user_messages(message, bot, database, settings)

    assert (await database.list_messages(case["id"]))[0]["delivery_state"] == "FAILED"
    assert message.answers == []


@pytest.mark.asyncio
async def test_admin_reply_goes_only_to_active_cases_owner(database, settings):
    case = await database.create_case(303, request_data())
    other = await database.create_case(404, request_data())
    await database.set_session(settings.admin_telegram_id, case["id"], "reply")
    bot = FakeBot()
    message = FakeMessage(settings.admin_telegram_id, 77, text="پاسخ دستی مدیر")

    await admin_messages(message, bot, database, settings)

    assert bot.copies[0]["chat_id"] == 303
    assert bot.copies[0]["message_id"] == 77
    assert len(await database.list_messages(case["id"])) == 1
    assert await database.list_messages(other["id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "photo", "voice", "document", "media_type"),
    [
        (81, [SimpleNamespace(file_id="photo-id", file_size=100)], None, None, "photo"),
        (82, None, SimpleNamespace(file_id="voice-id", file_size=100), None, "voice"),
        (
            83,
            None,
            None,
            SimpleNamespace(
                file_id="pdf-id",
                file_size=17,
                mime_type="application/pdf",
                file_name="reply.pdf",
            ),
            "document",
        ),
    ],
)
async def test_admin_media_replies_are_validated_and_sent_to_owner(
    database, settings, message_id, photo, voice, document, media_type
):
    case = await database.create_case(304, request_data())
    await database.set_session(settings.admin_telegram_id, case["id"], "reply")
    bot = FakeBot()
    message = FakeMessage(
        settings.admin_telegram_id,
        message_id,
        photo=photo,
        voice=voice,
        document=document,
    )

    await admin_messages(message, bot, database, settings)

    assert bot.copies[0]["chat_id"] == 304
    stored = await database.list_messages(case["id"])
    assert stored[0]["media_type"] == media_type
    assert stored[0]["delivery_state"] == "SENT"


@pytest.mark.asyncio
async def test_admin_reply_failure_is_not_marked_as_sent(database, settings):
    case = await database.create_case(505, request_data())
    await database.set_session(settings.admin_telegram_id, case["id"], "reply")
    bot = FakeBot()

    async def fail_copy(**kwargs):
        raise RuntimeError("simulated transport failure")

    bot.copy_message = fail_copy
    message = FakeMessage(settings.admin_telegram_id, 78, text="پاسخ ناموفق")

    await admin_messages(message, bot, database, settings)

    stored = await database.list_messages(case["id"])
    assert stored[0]["delivery_state"] == "FAILED"
    assert stored[0]["delivery_state"] != "SENT"
    assert any("ناموفق" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_admin_reply_handles_telegram_delivery_error(database, settings):
    case = await database.create_case(506, request_data())
    await database.set_session(settings.admin_telegram_id, case["id"], "reply")
    bot = FakeBot()

    async def fail_copy(**kwargs):
        raise RuntimeError("simulated Telegram API failure")

    bot.copy_message = fail_copy
    message = FakeMessage(settings.admin_telegram_id, 79, text="پاسخ آزمایشی")

    await admin_messages(message, bot, database, settings)

    assert (await database.list_messages(case["id"]))[0]["delivery_state"] == "FAILED"
    assert "ثبت نشد" in message.answers[-1]


@pytest.mark.asyncio
async def test_receipt_request_is_delivered_before_case_returns_to_awaiting_payment(
    database, settings
):
    case = await database.create_case(606, request_data())
    await database.transition_case(case["id"], CaseStatus.UNDER_REVIEW, settings.admin_telegram_id)
    await database.transition_case(case["id"], CaseStatus.APPROVED, settings.admin_telegram_id)
    await database.add_payment_instruction(case["id"], 80, settings.admin_telegram_id)
    assert await database.record_receipt(case["id"], 606, 81, "photo", None, "receipt-file")
    await database.set_session(settings.admin_telegram_id, case["id"], "receipt_request")
    bot = FakeBot()
    message = FakeMessage(settings.admin_telegram_id, 82, text="لطفاً رسید خواناتری بفرست.")

    await admin_messages(message, bot, database, settings)

    assert bot.copies[0]["chat_id"] == 606
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.AWAITING_PAYMENT.value
    sent = await database.list_messages(case["id"])
    assert sent[-1]["delivery_state"] == "SENT"


@pytest.mark.asyncio
async def test_successful_delivery_is_not_marked_failed_when_case_update_fails(
    database, settings, monkeypatch
):
    case = await database.create_case(707, request_data())
    await database.transition_case(case["id"], CaseStatus.UNDER_REVIEW, settings.admin_telegram_id)
    await database.transition_case(case["id"], CaseStatus.APPROVED, settings.admin_telegram_id)
    await database.add_payment_instruction(case["id"], 90, settings.admin_telegram_id)
    assert await database.record_receipt(case["id"], 707, 91, "photo", None, "receipt-file")
    await database.set_session(settings.admin_telegram_id, case["id"], "receipt_request")

    async def fail_review(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(database, "review_receipt", fail_review)
    message = FakeMessage(settings.admin_telegram_id, 92, text="لطفاً توضیح تکمیلی بفرست.")

    await admin_messages(message, FakeBot(), database, settings)

    stored = await database.list_messages(case["id"])
    assert stored[-1]["delivery_state"] == "SENT"
    assert any("ارسال شد" in answer and "ثبت تغییر وضعیت" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_admin_payment_flow_stores_declared_amount_and_currency(database, settings):
    case = await database.create_case(808, request_data())
    await database.transition_case(case["id"], CaseStatus.UNDER_REVIEW, settings.admin_telegram_id)
    await database.transition_case(case["id"], CaseStatus.APPROVED, settings.admin_telegram_id)
    await database.set_session(settings.admin_telegram_id, case["id"], "payment_amount")
    bot = FakeBot()

    amount_message = FakeMessage(settings.admin_telegram_id, 101, text="۱۵۰۰۰۰ IRR انتقال بانکی")
    await admin_messages(amount_message, bot, database, settings)
    instruction_message = FakeMessage(settings.admin_telegram_id, 102, text="روش پرداخت مجاز و توضیحات")
    await admin_messages(instruction_message, bot, database, settings)

    async with database.connect() as connection:
        cursor = await connection.execute(
            "SELECT expected_amount, declared_amount, currency, method FROM payments WHERE case_id=?",
            (case["id"],),
        )
        payment = await cursor.fetchone()
    assert payment["expected_amount"] == 150000
    assert payment["declared_amount"] == 150000
    assert payment["currency"] == "IRR"
    assert payment["method"] == "انتقال بانکی"
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.AWAITING_PAYMENT.value
    assert bot.copies[0]["chat_id"] == 808


@pytest.mark.asyncio
async def test_forged_user_case_callback_cannot_open_another_users_case(
    database, settings
):
    case = await database.create_case(1001, request_data())
    callback = FakeCallback(2002, f"u:case:{case['id']}")

    await user_callbacks(callback, FakeBot(), database, settings)

    assert callback.answers[-1][1] == {}
    assert await database.get_session(2002) is None


@pytest.mark.asyncio
async def test_user_cannot_send_message_through_another_users_case_session(
    database, settings
):
    case = await database.create_case(2101, request_data())
    await open_conversation(database, case, 2101)
    await database.set_session(2102, case["id"], "conversation")
    message = FakeMessage(2102, 15, text="نباید به پرونده راه پیدا کند")
    bot = FakeBot()

    await user_messages(message, bot, database, settings)

    assert bot.copies == []
    assert await database.list_messages(case["id"]) == []
    assert message.answers == []


@pytest.mark.asyncio
async def test_non_admin_cannot_use_forged_admin_callback(database, settings):
    case = await database.create_case(1002, request_data())
    callback = FakeCallback(2003, f"a:confirm:close:{case['id']}")

    await admin_callbacks(callback, FakeBot(), database, settings)

    assert callback.answers[-1][1]["show_alert"] is True
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.NEW.value


@pytest.mark.asyncio
async def test_admin_open_callback_sets_explicit_active_case(database, settings):
    case = await database.create_case(1003, request_data())
    callback = FakeCallback(settings.admin_telegram_id, f"a:open:{case['id']}:new:0")

    await admin_callbacks(callback, FakeBot(), database, settings)

    assert (await database.get_session(settings.admin_telegram_id))["case_id"] == case["id"]
    assert any(
        answer and case["case_number"] in answer for answer, _ in callback.answers
    )


@pytest.mark.asyncio
async def test_authorized_admin_can_open_case(database, settings):
    case = await database.create_case(1004, request_data())
    callback = FakeCallback(settings.admin_telegram_id, f"a:open:{case['id']}:new:0")

    await admin_callbacks(callback, FakeBot(), database, settings)

    assert (await database.get_session(settings.admin_telegram_id))["case_id"] == case["id"]
    assert any(case["case_number"] in (text or "") for text, _ in callback.answers)


@pytest.mark.asyncio
async def test_admin_status_change_is_audited(database, settings):
    case = await database.create_case(1007, request_data())
    callback = FakeCallback(
        settings.admin_telegram_id, f"a:transition:approve:{case['id']}"
    )

    await admin_callbacks(callback, FakeBot(), database, settings)

    updated = await database.get_case(case["id"])
    history = await database.audit_history(case["id"])
    assert updated["status"] == CaseStatus.APPROVED.value
    assert history[-1]["event_type"] == "STATUS_CHANGED"
    assert history[-1]["old_status"] == CaseStatus.NEW.value
    assert history[-1]["new_status"] == CaseStatus.APPROVED.value


@pytest.mark.asyncio
async def test_submit_registers_request_once_and_notifies_admin(database, settings):
    user_id = await database.register_user(1005)
    await database.save_draft(user_id, "need", request_data())
    callback = FakeCallback(1005, "u:next:need")
    bot = FakeBot()

    await user_callbacks(callback, bot, database, settings)
    await user_callbacks(callback, bot, database, settings)

    assert await database.count_cases() == 1
    assert await database.get_draft(1005) is None
    assert await database.get_session(1005) is None
    assert bot.sent[0][0][0] == settings.admin_telegram_id
    assert "@sample_user" in bot.sent[0][0][1]
    assert "تهران" in bot.sent[0][0][1]
    anchor = await database.get_case_for_admin_reply(settings.admin_telegram_id, 601)
    assert anchor["id"] == 1
    assert (await database.get_case(1))["conversation_status"] == "PENDING"


@pytest.mark.asyncio
async def test_submit_database_failure_is_reported_without_notification(
    database, settings, monkeypatch
):
    user_id = await database.register_user(1006)
    await database.save_draft(user_id, "need", request_data())

    async def fail_submit(_telegram_id):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(database, "submit_draft", fail_submit)
    callback = FakeCallback(1006, "u:next:need")
    bot = FakeBot()

    await user_callbacks(callback, bot, database, settings)

    assert await database.count_cases() == 0
    assert bot.sent == []
    assert callback.answers[-1][0] is None


@pytest.mark.asyncio
async def test_user_form_collects_independent_values_for_selected_fields(database, settings):
    user_id = 1100
    bot = FakeBot()
    await user_callbacks(FakeCallback(user_id, "u:new"), bot, database, settings)
    await user_callbacks(
        FakeCallback(user_id, f"u:toggle:have:{INFO_OPTIONS.index('شماره تماس')}"),
        bot,
        database,
        settings,
    )
    await user_callbacks(
        FakeCallback(user_id, f"u:toggle:have:{INFO_OPTIONS.index('شهر محل زندگی')}"),
        bot,
        database,
        settings,
    )
    await user_callbacks(FakeCallback(user_id, "u:next:have"), bot, database, settings)

    await user_messages(FakeMessage(user_id, 30, text="   "), bot, database, settings)
    assert (await database.get_draft(user_id))["data"]["field_index"] == 0
    await user_messages(FakeMessage(user_id, 31, text="+989120000000"), bot, database, settings)
    await user_messages(FakeMessage(user_id, 32, text="تهران، خیابان نمونه"), bot, database, settings)
    assert (await database.get_draft(user_id))["step"] == "need"
    await user_callbacks(
        FakeCallback(user_id, f"u:toggle:need:{INFO_OPTIONS.index('کد ملی')}"),
        bot,
        database,
        settings,
    )
    await user_callbacks(FakeCallback(user_id, "u:next:need"), bot, database, settings)
    draft = await database.get_draft(user_id)
    assert draft is None

    case = await database.get_case_by_number("R-000001", user_id)
    assert json.loads(case["have_options"]) == ["شماره تماس", "شهر محل زندگی"]
    assert json.loads(case["have_values"]) == {
        "شماره تماس": "+989120000000",
        "شهر محل زندگی": "تهران، خیابان نمونه",
    }
    assert json.loads(case["need_options"]) == ["کد ملی"]
    assert any("شناسه تلگرام: 1100" in item[0][1] for item in bot.sent)


@pytest.mark.asyncio
async def test_user_can_submit_with_both_sections_empty(database, settings):
    user_id = 1101
    bot = FakeBot()
    await user_callbacks(FakeCallback(user_id, "u:new"), bot, database, settings)
    await user_callbacks(FakeCallback(user_id, "u:next:have"), bot, database, settings)
    await user_callbacks(FakeCallback(user_id, "u:next:need"), bot, database, settings)

    case = await database.get_case_by_number("R-000001", user_id)
    assert json.loads(case["have_options"]) == []
    assert json.loads(case["need_options"]) == []


@pytest.mark.asyncio
async def test_active_conversation_replies_route_and_stop_with_the_same_case(database, settings):
    case = await database.create_case(1200, request_data())
    bot = FakeBot()
    await database.save_request_reply_target(case["id"], settings.admin_telegram_id, 90)
    await reply_to_request(
        FakeMessage(
            settings.admin_telegram_id,
            41,
            text="سلام، پیام اول مدیر",
            reply_to_message=SimpleNamespace(message_id=90),
        ),
        bot,
        database,
        settings,
    )
    assert (await database.get_case(case["id"]))["conversation_status"] == "OPEN"
    assert (await database.get_session(1200))["mode"] == "conversation"
    finish_markup = bot.sent[-1][1]["reply_markup"]
    finish_callbacks = [
        button.callback_data
        for row in finish_markup.inline_keyboard
        for button in row
    ]
    assert finish_callbacks == [f"a:end:{case['id']}"]

    await user_messages(FakeMessage(1200, 42, text="پاسخ کاربر"), bot, database, settings)
    assert "reply_markup" not in bot.copies[-1]
    forwarded_user_message_id = 500 + len(bot.copies)
    await reply_to_request(
        FakeMessage(
            settings.admin_telegram_id,
            43,
            text="پاسخ مدیر با Reply",
            reply_to_message=SimpleNamespace(message_id=forwarded_user_message_id),
        ),
        bot,
        database,
        settings,
    )

    assert len(await database.list_messages(case["id"])) == 3
    assert bot.copies[0]["chat_id"] == 1200
    assert bot.copies[1]["chat_id"] == settings.admin_telegram_id
    assert bot.copies[2]["chat_id"] == 1200
    assert all(copy["protect_content"] is True for copy in bot.copies)
    reopened = Database(database.path)
    assert len(await reopened.list_messages(case["id"])) == 3

    finish_callback = FakeCallback(settings.admin_telegram_id, f"a:end:{case['id']}")
    await end_conversation(finish_callback, bot, database, settings)
    assert finish_callback.message.markups[-1] is None
    assert (1200, 42) in bot.deleted
    assert (1200, 503) in bot.deleted
    assert len(await database.list_messages(case["id"])) == 3
    assert (await database.get_case(case["id"]))["conversation_status"] == "CLOSED"
    assert (await database.get_session(1200))["mode"] == "closed"
    copies_before_closed_message = len(bot.copies)
    closed_message = FakeMessage(1200, 44, text="پیام پس از پایان")
    await user_messages(closed_message, bot, database, settings)
    assert len(bot.copies) == copies_before_closed_message
    assert closed_message.answers[-1] == "این گفت‌وگو تموم شده. برای ثبت درخواست جدید، /start رو بزن."

    await start_user(FakeMessage(1200, 45, text="/start"), database, settings)
    await user_callbacks(FakeCallback(1200, "u:new"), bot, database, settings)
    await user_callbacks(FakeCallback(1200, "u:next:have"), bot, database, settings)
    await user_callbacks(FakeCallback(1200, "u:next:need"), bot, database, settings)
    new_case = await database.get_case_by_number("R-000002", 1200)
    assert new_case is not None
    assert new_case["id"] != case["id"]
    assert new_case["conversation_status"] == "PENDING"
    assert (await reopened.get_case(case["id"]))["conversation_status"] == "CLOSED"


@pytest.mark.asyncio
async def test_manager_reply_keeps_two_users_separate(database, settings):
    first_case = await database.create_case(1501, request_data())
    second_case = await database.create_case(1502, request_data())
    await database.save_request_reply_target(first_case["id"], settings.admin_telegram_id, 101)
    await database.save_request_reply_target(second_case["id"], settings.admin_telegram_id, 102)
    bot = FakeBot()

    await reply_to_request(
        FakeMessage(
            settings.admin_telegram_id,
            51,
            text="پیام برای نفر اول",
            reply_to_message=SimpleNamespace(message_id=101),
        ),
        bot,
        database,
        settings,
    )
    await reply_to_request(
        FakeMessage(
            settings.admin_telegram_id,
            52,
            text="پیام برای نفر دوم",
            reply_to_message=SimpleNamespace(message_id=102),
        ),
        bot,
        database,
        settings,
    )

    assert [copy["chat_id"] for copy in bot.copies] == [1501, 1502]
    assert len(await database.list_messages(first_case["id"])) == 1
    assert len(await database.list_messages(second_case["id"])) == 1


@pytest.mark.asyncio
async def test_same_user_new_request_can_start_after_previous_chat_is_closed(database, settings):
    old_case = await database.create_case(1601, request_data())
    new_case = await database.create_case(1601, request_data())
    assert await database.start_conversation(old_case["id"], settings.admin_telegram_id)
    assert await database.activate_conversation(old_case["id"], settings.admin_telegram_id)
    await database.set_session(1601, old_case["id"], "conversation")
    await database.save_request_reply_target(
        new_case["id"], settings.admin_telegram_id, 160
    )
    bot = FakeBot()
    reply = FakeMessage(
        settings.admin_telegram_id,
        61,
        text="پاسخ درخواست جدید",
        reply_to_message=SimpleNamespace(message_id=160),
    )

    await reply_to_request(reply, bot, database, settings)
    assert bot.copies == []
    assert "گفتگوی دیگری" in reply.answers[-1]

    assert await database.end_conversation(old_case["id"], settings.admin_telegram_id)
    await reply_to_request(reply, bot, database, settings)
    assert [copy["chat_id"] for copy in bot.copies] == [1601]
    assert (await database.get_case(new_case["id"]))["conversation_status"] == "OPEN"