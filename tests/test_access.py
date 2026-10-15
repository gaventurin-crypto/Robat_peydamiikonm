from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from datetime import UTC, datetime
from aiogram.types import CallbackQuery, Chat, Message, User

from app.access import AdminAccessFilter
from app.config import Settings
from app.database import Database
from app.middleware import UserRateLimitMiddleware


@pytest.mark.asyncio
async def test_admin_filter_requires_exact_id_and_private_chat():
    access = AdminAccessFilter(12345)
    assert await access(SimpleNamespace(from_user=SimpleNamespace(id=12345), chat=SimpleNamespace(type="private")))
    assert await access(
        SimpleNamespace(
            from_user=SimpleNamespace(id=12345),
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        )
    )
    assert not await access(SimpleNamespace(from_user=SimpleNamespace(id=12346), chat=SimpleNamespace(type="private")))
    assert not await access(SimpleNamespace(from_user=SimpleNamespace(id=12345), chat=SimpleNamespace(type="group")))


@pytest_asyncio.fixture
async def database(tmp_path):
    database = Database(tmp_path / "rate-limit.sqlite3")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_user_callback_rate_limit_runs_before_handler(database):
    middleware = UserRateLimitMiddleware(
        Settings(
            bot_token="test-token",
            admin_telegram_id=1,
            database_path=database.path,
            retention_hours=24,
            max_upload_bytes=1024,
            welcome_text="",
            privacy_notice="",
            notice_version="test",
            have_question="",
            need_question="",
            description_question="",
            file_help_text="",
            info_options=(),
        ),
        database,
    )
    event = Mock(spec=CallbackQuery)
    event.from_user = SimpleNamespace(id=2)
    event.message = SimpleNamespace(chat=SimpleNamespace(type="private"))
    event.answer = AsyncMock()
    handler = AsyncMock(return_value="handled")

    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) is None

    assert handler.await_count == 1
    event.answer.assert_awaited_once_with("کمی آهسته‌تر اقدام کن.", show_alert=True)


@pytest.mark.asyncio
async def test_active_conversation_allows_normal_message_volume(database, monkeypatch):
    settings = Settings(
        bot_token="test-token",
        admin_telegram_id=1,
        database_path=database.path,
        retention_hours=24,
        max_upload_bytes=1024,
        welcome_text="",
        privacy_notice="",
        notice_version="test",
        have_question="",
        need_question="",
        description_question="",
        file_help_text="",
        info_options=(),
    )
    case = await database.create_case(2, {"have": [], "need": []})
    assert await database.start_conversation(case["id"], 1)
    assert await database.activate_conversation(case["id"], 1)
    await database.set_session(2, case["id"], "conversation")
    middleware = UserRateLimitMiddleware(settings, database)
    event = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=2, type="private"),
        from_user=User(id=2, is_bot=False, first_name="User"),
        text="hello",
    )
    clock = iter(1000.0 + index * 0.1 for index in range(40))
    monkeypatch.setattr("app.middleware.time.time", lambda: next(clock))
    handler = AsyncMock(return_value="handled")

    for _ in range(40):
        assert await middleware(handler, event, {}) == "handled"

    assert handler.await_count == 40