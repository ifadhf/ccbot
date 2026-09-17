"""Exercise real update dispatch: General stays silent; other chats still work."""

from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import ExtBot

import ccbot.bot as bot_module
from ccbot.config import config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "thread_id", "topic_message", "forum", "handled"),
    [
        ("text", None, None, True, False),
        ("text", 1, True, True, False),
        ("text", 99, False, True, False),
        ("command", None, None, True, False),
        ("command", 1, True, True, False),
        ("photo", None, None, True, False),
        ("voice", 1, True, True, False),
        ("callback", None, None, True, False),
        ("callback", 1, True, True, False),
        ("text", 42, True, True, True),
        ("command", 42, True, True, True),
        ("callback", 42, True, True, True),
        ("command", None, None, False, True),
    ],
)
async def test_general_topic_dispatch(
    monkeypatch, kind, thread_id, topic_message, forum, handled
):
    # No Telegram requests or real tmux/session operations occur in this test.
    async def initialize_bot(bot):
        bot._bot_user = User(123456, "Test", True, username="test_bot")
        bot._initialized = True

    monkeypatch.setattr(ExtBot, "initialize", initialize_bot)
    monkeypatch.setattr(ExtBot, "shutdown", AsyncMock())
    monkeypatch.setattr(config, "telegram_bot_token", "123456:test-token")
    handlers = {}
    for name in [
        "start_command",
        "text_handler",
        "photo_handler",
        "voice_handler",
        "callback_handler",
        "unsupported_content_handler",
    ]:
        handlers[name] = AsyncMock()
        monkeypatch.setattr(bot_module, name, handlers[name])

    application = bot_module.create_bot()
    await application.initialize()
    try:
        sender = {"id": 999, "first_name": "Sender", "is_bot": False}
        message = {
            "message_id": 10,
            "date": 1700000000,
            "chat": {
                "id": -10011 if forum else 999,
                "type": "supergroup" if forum else "private",
                "is_forum": forum,
            },
            "from": sender,
            "message_thread_id": thread_id,
            "is_topic_message": topic_message,
        }
        if kind == "command":
            message.update(
                text="/start",
                entities=[{"type": "bot_command", "offset": 0, "length": 6}],
            )
        elif kind == "photo":
            message["photo"] = [
                {
                    "file_id": "photo",
                    "file_unique_id": "photo-1",
                    "width": 1,
                    "height": 1,
                }
            ]
        elif kind == "voice":
            message["voice"] = {
                "file_id": "voice",
                "file_unique_id": "voice-1",
                "duration": 1,
            }
        else:
            message["text"] = "hello"
        payload = {"update_id": 1, "message": message}
        if kind == "callback":
            payload = {
                "update_id": 1,
                "callback_query": {
                    "id": "callback-1",
                    "from": sender,
                    "chat_instance": "chat-1",
                    "data": "noop",
                    "message": message,
                },
            }
        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
        assert sum(handler.await_count for handler in handlers.values()) == int(handled)
    finally:
        await application.shutdown()
