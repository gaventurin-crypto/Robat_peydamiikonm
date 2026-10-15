import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from app.database import Database
from app.domain import CaseStatus, InvalidTransition


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    await db.initialize()
    return db


def sample_data():
    return {
        "have": ["آدرس", "کد ملی"],
        "need": ["کد ملی", "سایر موارد"],
        "other_request": "درخواست آزمایشی",
        "description": "توضیح آزمایشی",
    }


@pytest.mark.asyncio
async def test_case_ids_are_unique_and_owner_scoped(database):
    first, second = await asyncio.gather(
        database.create_case(101, sample_data()),
        database.create_case(202, sample_data()),
    )
    assert first["case_number"] != second["case_number"]
    assert (await database.get_case_by_number(first["case_number"], 101))["id"] == first["id"]
    assert await database.get_case_by_number(first["case_number"], 202) is None


@pytest.mark.asyncio
async def test_case_persists_structured_requested_values_and_telegram_profile(database):
    await database.register_user(203, "نام", "خانوادگی", "sample_user")
    data = {
        **sample_data(),
        "requested_fields": ["شماره تماس", "آدرس"],
        "requested_values": {"شماره تماس": "+989120000000", "آدرس": "تهران"},
    }

    case = await database.create_case(203, data)

    assert json.loads(case["need_options"]) == ["شماره تماس", "آدرس"]
    assert json.loads(case["requested_values"]) == data["requested_values"]
    assert (case["first_name"], case["last_name"], case["username"]) == (
        "نام",
        "خانوادگی",
        "sample_user",
    )
    await database.register_user(203, "نام", "خانوادگی", None)
    assert (await database.get_case(case["id"]))["username"] is None


@pytest.mark.asyncio
async def test_submit_draft_rejects_missing_selected_values(database):
    user_id = await database.register_user(205)
    await database.save_draft(
        user_id,
        "ready",
        {
            "have_fields": ["شماره تماس", "آدرس"],
            "have_values": {"آدرس": "تهران"},
            "need_fields": [],
            "requested_fields": [],
            "requested_values": {},
        },
    )

    assert await database.submit_draft(205) is None
    assert await database.count_cases() == 0
    assert await database.get_draft(205) is not None


@pytest.mark.asyncio
async def test_submit_draft_allows_empty_sections_and_unvalued_search_types(database):
    user_id = await database.register_user(206)
    await database.save_draft(
        user_id,
        "ready",
        {
            "have_fields": [],
            "have_values": {},
            "need_fields": ["شماره تماس", "آدرس"],
            "requested_fields": ["شماره تماس", "آدرس"],
            "requested_values": {},
        },
    )

    case = await database.submit_draft(206)

    assert case is not None
    assert json.loads(case["have_options"]) == []
    assert json.loads(case["have_values"]) == {}
    assert json.loads(case["need_options"]) == ["شماره تماس", "آدرس"]


@pytest.mark.asyncio
async def test_conversation_lifecycle_is_case_scoped_and_persistent(database):
    case = await database.create_case(204, sample_data())
    another_case = await database.create_case(204, sample_data())

    assert await database.start_conversation(case["id"], 900)
    assert (await database.get_case(case["id"]))["conversation_status"] == "STARTING"
    assert not await database.start_conversation(another_case["id"], 900)
    assert await database.activate_conversation(case["id"], 900)
    await database.set_session(204, case["id"], "conversation")
    await database.set_session(900, case["id"], "conversation")
    assert await database.user_has_active_conversation(204)
    assert not await database.user_has_active_conversation(205)

    assert await database.add_message(case["id"], "user", "text", "پاسخ", None, 204, 9)
    await database.update_message_delivery(204, 9, "SENT", 700)
    reply_target = await database.get_case_for_reply("user", 700)
    assert reply_target["id"] == case["id"]

    reopened = Database(database.path)
    assert (await reopened.get_case(case["id"]))["conversation_status"] == "OPEN"
    assert await reopened.end_conversation(case["id"], 900)
    assert (await database.get_case(case["id"]))["conversation_status"] == "CLOSED"
    assert not await database.user_has_active_conversation(204)
    assert (await database.get_session(900))["mode"] == "closed"
    assert (await database.audit_history(case["id"]))[-1]["event_type"] == "CONVERSATION_CLOSED"
    assert not await database.start_conversation(case["id"], 900)


@pytest.mark.asyncio
async def test_concurrent_draft_submission_creates_only_one_case(database):
    user_id = await database.register_user(808)
    data = {
        "have_fields": [],
        "have_values": {},
        "need_fields": ["آدرس"],
        "requested_fields": ["آدرس"],
        "requested_values": {},
        "notice_version": "test-notice",
    }
    await database.save_draft(user_id, "ready", data)

    results = await asyncio.gather(
        database.submit_draft(808),
        database.submit_draft(808),
    )

    created = [case for case in results if case is not None]
    assert len(created) == 1
    assert await database.count_cases([CaseStatus.NEW]) == 1
    assert await database.get_draft(808) is None
    async with database.connect() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM consent_records WHERE case_id=?", (created[0]["id"],)
        )
        assert (await cursor.fetchone())["count"] == 1


@pytest.mark.asyncio
async def test_draft_survives_database_reopen(database):
    user_id = await database.register_user(303)
    await database.save_draft(user_id, "need", {"have": ["آدرس"], "need": []})
    reopened = Database(database.path)
    assert await reopened.get_draft(303) == {"step": "need", "data": {"have": ["آدرس"], "need": []}}


@pytest.mark.asyncio
async def test_rate_limit_is_persistent_and_resets_after_window(database):
    assert await database.allow_update(909, 100.0, max_updates=2, min_interval_seconds=0.1)
    assert not await database.allow_update(909, 100.05, max_updates=2, min_interval_seconds=0.1)
    reopened = Database(database.path)
    assert await reopened.allow_update(909, 100.2, max_updates=2, min_interval_seconds=0.1)
    assert not await reopened.allow_update(909, 100.4, max_updates=2, min_interval_seconds=0.1)
    assert await reopened.allow_update(
        909, 161.0, max_updates=2, window_seconds=60.0, min_interval_seconds=0.1
    )


@pytest.mark.asyncio
async def test_receipt_does_not_confirm_payment_and_is_idempotent(database):
    case = await database.create_case(404, sample_data())
    await database.transition_case(case["id"], CaseStatus.UNDER_REVIEW, 900)
    await database.transition_case(case["id"], CaseStatus.APPROVED, 900)
    await database.add_payment_instruction(case["id"], 81, 900)
    accepted = await database.record_receipt(case["id"], 404, 82, "photo", None, "telegram-file")
    duplicate = await database.record_receipt(case["id"], 404, 82, "photo", None, "telegram-file")
    assert accepted and not duplicate
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.RECEIPT_REVIEW.value
    receipt = await database.get_pending_receipt(case["id"])
    assert receipt["source_message_id"] == 82
    assert receipt["file_id"] == "telegram-file"
    await database.review_receipt(case["id"], approved=False, admin_id=900, note="درخواست مجدد")
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.AWAITING_PAYMENT.value
    assert await database.get_pending_receipt(case["id"]) is None
    assert await database.record_receipt(case["id"], 404, 83, "photo", None, "telegram-file-2")
    await database.review_receipt(case["id"], approved=True, admin_id=900)
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.PAYMENT_CONFIRMED.value
    assert await database.get_pending_receipt(case["id"]) is None


@pytest.mark.asyncio
async def test_payment_amount_and_internal_receipt_note_are_persisted(database):
    case = await database.create_case(411, sample_data())
    await database.transition_case(case["id"], CaseStatus.UNDER_REVIEW, 900)
    await database.transition_case(case["id"], CaseStatus.APPROVED, 900)
    await database.add_payment_instruction(
        case["id"], 111, 900, expected_amount=150000, declared_amount=150000,
        currency="IRR", method="bank transfer",
    )
    assert await database.record_receipt(case["id"], 411, 112, "photo", None, "receipt")
    await database.set_receipt_note(case["id"], "تصویر خواناست", 900)

    pending_receipt = await database.get_pending_receipt(case["id"])
    async with database.connect() as connection:
        cursor = await connection.execute(
            "SELECT expected_amount, declared_amount, currency, method FROM payments "
            "WHERE case_id=? AND status='AWAITING'",
            (case["id"],),
        )
        payment = await cursor.fetchone()

    assert payment["expected_amount"] == 150000
    assert payment["declared_amount"] == 150000
    assert payment["currency"] == "IRR"
    assert payment["method"] == "bank transfer"
    assert pending_receipt["admin_note"] == "تصویر خواناست"
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.RECEIPT_REVIEW.value


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected_and_audited(database):
    case = await database.create_case(505, sample_data())
    internal_user_id = await database.register_user(505)
    with pytest.raises(InvalidTransition):
        await database.transition_case(case["id"], CaseStatus.PAYMENT_CONFIRMED, 900)
    assert await database.audit_history(case["id"]) == [
        {
            "actor_id": internal_user_id,
            "actor_kind": "user",
            "event_type": "CASE_CREATED",
            "old_status": None,
            "new_status": CaseStatus.NEW.value,
            "created_at": (await database.get_case(case["id"]))["created_at"],
        }
    ]


@pytest.mark.asyncio
async def test_retention_physically_removes_case_content(database):
    data = sample_data()
    data["description"] = "UNIQUE-SENSITIVE-SENTINEL-9ce17"
    case = await database.create_case(606, data)
    await database.set_session(606, case["id"], "conversation")
    await database.add_message(case["id"], "user", "photo", "caption", "file-secret", 606, 1)
    await database.transition_case(case["id"], CaseStatus.REJECTED, 900)
    async with database.connect() as connection:
        await connection.execute(
            "UPDATE cases SET closed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (case["id"],)
        )
        await connection.commit()
    assert await database.purge_closed_cases(24) == (1, 0)
    purged = await database.get_case(case["id"])
    assert purged["status"] == CaseStatus.EXPIRED.value
    assert purged["user_id"] is None
    assert purged["have_options"] == "[]"
    assert purged["description"] is None
    assert await database.list_messages(case["id"]) == []
    assert (await database.audit_history(case["id"]))[-1]["event_type"] == "DATA_PURGED"
    async with database.connect() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM users WHERE telegram_id=606")
        assert (await cursor.fetchone())["count"] == 0
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM sessions WHERE telegram_id=606")
        assert (await cursor.fetchone())["count"] == 0
    with open(database.path, "rb") as database_file:
        assert b"UNIQUE-SENSITIVE-SENTINEL-9ce17" not in database_file.read()


@pytest.mark.asyncio
async def test_legal_retention_hold_prevents_automatic_purge(database):
    case = await database.create_case(707, sample_data())
    await database.transition_case(case["id"], CaseStatus.REJECTED, 900)
    async with database.connect() as connection:
        await connection.execute(
            "UPDATE cases SET closed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (case["id"],)
        )
        await connection.commit()
    await database.set_retention_hold(case["id"], True, 900)
    assert await database.purge_closed_cases(24) == (0, 0)
    assert (await database.get_case(case["id"]))["description"] == "توضیح آزمایشی"
    await database.set_retention_hold(case["id"], False, 900)
    assert await database.purge_closed_cases(24) == (1, 0)


@pytest.mark.asyncio
async def test_retention_hold_added_after_candidate_scan_prevents_purge(database):
    case = await database.create_case(909, sample_data())
    await database.transition_case(case["id"], CaseStatus.REJECTED, 900)
    async with database.connect() as connection:
        await connection.execute(
            "UPDATE cases SET closed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (case["id"],)
        )
        await connection.commit()

    candidate_scanned = asyncio.Event()
    continue_purge = asyncio.Event()
    original_connect = database.connect
    first_connection = True

    @asynccontextmanager
    async def gated_connect():
        nonlocal first_connection
        async with original_connect() as connection:
            yield connection
        if first_connection:
            first_connection = False
            candidate_scanned.set()
            await continue_purge.wait()

    database.connect = gated_connect
    purge_task = asyncio.create_task(database.purge_closed_cases(24))
    await candidate_scanned.wait()
    await database.set_retention_hold(case["id"], True, 900)
    continue_purge.set()

    assert await purge_task == (0, 0)
    assert (await database.get_case(case["id"]))["description"] == sample_data()["description"]


@pytest.mark.asyncio
async def test_retention_delete_failure_rolls_back_and_is_reported(database):
    case = await database.create_case(910, sample_data())
    await database.add_message(case["id"], "user", "text", "keep", None, 910, 12)
    await database.transition_case(case["id"], CaseStatus.REJECTED, 900)
    async with database.connect() as connection:
        await connection.execute(
            "UPDATE cases SET closed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (case["id"],)
        )
        await connection.execute(
            "CREATE TRIGGER block_message_purge BEFORE DELETE ON messages "
            "BEGIN SELECT RAISE(ABORT, 'simulated purge failure'); END"
        )
        await connection.commit()

    assert await database.purge_closed_cases(24) == (0, 1)
    assert (await database.get_case(case["id"]))["status"] == CaseStatus.REJECTED.value
    assert (await database.list_messages(case["id"]))[0]["text"] == "keep"


@pytest.mark.asyncio
async def test_initialize_upgrades_existing_sqlite_schema_without_data_loss(tmp_path):
    legacy = Database(tmp_path / "legacy.sqlite3")
    async with legacy.connect() as connection:
        await connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                first_seen_at TEXT NOT NULL
            );
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_number TEXT UNIQUE,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                status TEXT NOT NULL,
                have_options TEXT NOT NULL,
                need_options TEXT NOT NULL,
                other_request TEXT,
                description TEXT
            );
            CREATE TABLE sessions (
                telegram_id INTEGER PRIMARY KEY,
                case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
                mode TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                amount INTEGER,
                currency TEXT,
                method TEXT,
                instructions_message_id INTEGER,
                receipt_message_id INTEGER,
                status TEXT NOT NULL,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                result_note TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO users VALUES (1, 991, '2026-01-01T00:00:00+00:00');
            INSERT INTO cases VALUES (
                1, 'R-000001', 1, '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', NULL, 'APPROVED', '[]', '[]', NULL, 'preserve-me'
            );
            INSERT INTO sessions VALUES (991, 1, 'reply', '2026-01-01T00:00:00+00:00');
            INSERT INTO payments VALUES (1, 1, 1200, 'IRR', 'manual', 10, NULL, 'AWAITING', NULL, NULL, NULL, '2026-01-01T00:00:00+00:00');
            """
        )
        await connection.commit()

    await legacy.initialize()
    await legacy.initialize()
    case = await legacy.get_case(1)
    session = await legacy.get_session(991)
    async with legacy.connect() as connection:
        cursor = await connection.execute("PRAGMA table_info(payments)")
        payment_columns = {row["name"] for row in await cursor.fetchall()}

    assert case["description"] == "preserve-me"
    assert case["retention_hold"] == 0
    assert case["requested_values"] == "{}"
    assert case["conversation_status"] == "PENDING"
    assert case["first_name"] is None
    assert case["username"] is None
    assert session["context"] == {}
    assert {"expected_amount", "declared_amount", "admin_note"} <= payment_columns