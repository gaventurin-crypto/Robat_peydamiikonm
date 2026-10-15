import logging
from types import SimpleNamespace

import pytest

import app.main as main


class FakeDatabase:
    def __init__(self, path):
        self.path = path
        self.initialized = False

    async def initialize(self):
        self.initialized = True

    async def purge_closed_cases(self, retention_hours):
        return 0, 0


class FakeBot:
    def __init__(self):
        self.commands = None
        self.webhook_deleted = False
        self.closed = False
        self.session = SimpleNamespace(close=self.close)

    async def close(self):
        self.closed = True

    async def set_my_commands(self, commands):
        self.commands = commands

    async def delete_webhook(self, drop_pending_updates):
        self.webhook_deleted = True


class FakeDispatcher:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.routers = []
        self.polling_started = False

    def include_router(self, router):
        self.routers.append(router)

    def errors(self):
        def register(handler):
            self.error_handler = handler
            return handler

        return register

    def resolve_used_update_types(self):
        return []

    async def start_polling(self, bot, allowed_updates):
        self.polling_started = True


@pytest.mark.asyncio
async def test_run_initializes_polling_and_closes_bot(monkeypatch):
    settings = SimpleNamespace(
        database_path=":memory:",
        admin_telegram_id=900,
        retention_hours=24,
        max_upload_bytes=1024,
        bot_token="test-token",
        prepare_storage=lambda: None,
    )
    database = FakeDatabase(":memory:")
    bot = FakeBot()
    dispatcher = FakeDispatcher()
    middleware_calls = {"admin": [], "user": []}
    monkeypatch.setattr(
        main.conversation_router.message, "outer_middleware", middleware_calls["admin"].append
    )
    monkeypatch.setattr(
        main.conversation_router.callback_query, "outer_middleware", middleware_calls["admin"].append
    )
    monkeypatch.setattr(
        main.user_router.message, "outer_middleware", middleware_calls["user"].append
    )
    monkeypatch.setattr(
        main.user_router.callback_query, "outer_middleware", middleware_calls["user"].append
    )
    monkeypatch.setattr(main.Settings, "from_environment", classmethod(lambda cls: settings))
    monkeypatch.setattr(main, "Database", lambda path: database)
    monkeypatch.setattr(main, "Bot", lambda token: bot)
    monkeypatch.setattr(main, "Dispatcher", lambda **kwargs: dispatcher)

    await main.run()

    assert database.initialized
    assert dispatcher.polling_started
    assert dispatcher.routers == [main.conversation_router, main.user_router]
    assert middleware_calls["admin"] == []
    assert len(middleware_calls["user"]) == 2
    assert middleware_calls["user"][0] is middleware_calls["user"][1]
    assert [command.command for command in bot.commands] == ["start"]
    assert bot.webhook_deleted
    assert bot.closed


def test_secret_redaction_filter_masks_token():
    redaction = main.SecretRedactionFilter("test-secret-token")
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "token=%s", ("test-secret-token",), None)

    assert redaction.filter(record)
    assert record.getMessage() == "token=[REDACTED]"