"""Verify ownership through real Telegram update dispatch and persisted state."""

import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest
from telegram import Update, User
from telegram.ext import ExtBot

import ccbot.bot as bot_module
import ccbot.hook as hook_module
import ccbot.utils as utils_module
from ccbot.config import config
from ccbot.session import SessionManager


def update_payload(
    kind="text", user_id=111, chat_id=-10011, thread_id=42, username="owner"
):
    sender = {
        "id": user_id,
        "first_name": "User",
        "is_bot": False,
        "username": username,
    }
    message = {
        "message_id": 10,
        "date": 1700000000,
        "from": sender,
        "chat": {"id": chat_id, "type": "supergroup", "is_forum": True},
        "message_thread_id": thread_id,
        "is_topic_message": thread_id != 1,
    }
    if kind == "created":
        message["forum_topic_created"] = {"name": "test", "icon_color": 0}
    elif kind == "document":
        message["document"] = {
            "file_id": "file",
            "file_unique_id": "unique",
            "file_name": "spec.csv",
        }
    elif kind == "photo":
        message["photo"] = [
            {"file_id": "photo", "file_unique_id": "unique", "width": 1, "height": 1}
        ]
    elif kind == "command":
        message.update(
            text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]
        )
    else:
        message["text"] = "hello"
    if kind == "callback":
        return {
            "update_id": 1,
            "callback_query": {
                "id": "button",
                "from": sender,
                "chat_instance": "chat",
                "data": "noop",
                "message": message,
            },
        }
    return {"update_id": 1, "message": message}


@pytest.fixture
async def owner_app(monkeypatch, tmp_path):
    async def initialize(bot):
        bot._bot_user = User(123456, "Bot", True, username="test_bot")
        bot._initialized = True

    monkeypatch.setattr(config, "topic_owner_lock", True)
    monkeypatch.setattr(config, "chat_scoped_topics", True)
    monkeypatch.setattr(config, "allowed_users", {111, 222})
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "telegram_bot_token", "123456:test-token")
    manager = SessionManager()
    monkeypatch.setattr(bot_module, "session_manager", manager)
    monkeypatch.setattr(ExtBot, "initialize", initialize)
    monkeypatch.setattr(ExtBot, "shutdown", AsyncMock())
    monkeypatch.setattr(ExtBot, "answer_callback_query", AsyncMock())
    reply = AsyncMock()
    monkeypatch.setattr(bot_module, "safe_reply", reply)
    handlers = {}
    for name in [
        "text_handler",
        "start_command",
        "document_handler",
        "photo_handler",
        "callback_handler",
    ]:
        handlers[name] = AsyncMock()
        monkeypatch.setattr(bot_module, name, handlers[name])
    app = bot_module.create_bot()
    await app.initialize()
    yield app, manager, reply, handlers
    await app.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "command", "document", "photo", "callback"])
async def test_other_allowed_user_cannot_operate_topic(owner_app, kind):
    app, _manager, reply, handlers = owner_app
    await app.process_update(Update.de_json(update_payload(), app.bot))
    initial = sum(h.await_count for h in handlers.values())
    await app.process_update(Update.de_json(update_payload(kind, user_id=222), app.bot))
    assert sum(h.await_count for h in handlers.values()) == initial
    assert reply.await_count == 1
    assert "@owner" in reply.call_args.args[1]
    assert reply.call_args.kwargs["do_quote"] is True
    restored = SessionManager()
    assert restored.topic_owners["-10011:42"]["user_id"] == 111
    assert restored.topic_owners["-10011:42"]["username"] == "owner"


@pytest.mark.asyncio
async def test_creator_is_reserved_until_first_message(owner_app):
    app, manager, reply, handlers = owner_app
    for payload in [update_payload("created"), update_payload(user_id=222)]:
        await app.process_update(Update.de_json(payload, app.bot))
    reply.assert_not_awaited()
    handlers["text_handler"].assert_not_awaited()
    await app.process_update(
        Update.de_json(update_payload(username="renamed"), app.bot)
    )
    assert "@renamed" in reply.call_args.args[1]
    assert manager.topic_owners["-10011:42"]["username"] == "renamed"
    await app.process_update(
        Update.de_json(update_payload(username="renamed"), app.bot)
    )
    assert reply.await_count == 1


@pytest.mark.asyncio
async def test_same_topic_id_in_other_group_has_different_owner(owner_app):
    app, manager, _, _ = owner_app
    for payload in [update_payload(), update_payload(chat_id=-10022, user_id=222)]:
        await app.process_update(Update.de_json(payload, app.bot))
    assert manager.topic_owners["-10011:42"]["user_id"] == 111
    assert manager.topic_owners["-10022:42"]["user_id"] == 222


@pytest.mark.asyncio
async def test_creation_without_thread_metadata_reserves_creator(owner_app):
    app, manager, reply, _ = owner_app
    payload = update_payload("created")
    payload["message"].pop("message_thread_id")
    payload["message"].pop("is_topic_message")
    await app.process_update(Update.de_json(payload, app.bot))
    assert manager.topic_owners["-10011:10"]["user_id"] == 111
    reply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", [{"thread_id": 1}, {"user_id": 999}, {"kind": "callback"}]
)
async def test_general_unauthorized_or_unowned_callback_cannot_claim(
    owner_app, options
):
    app, manager, reply, handlers = owner_app
    await app.process_update(Update.de_json(update_payload(**options), app.bot))
    assert manager.topic_owners == {}
    reply.assert_not_awaited()
    assert not any(h.await_count for h in handlers.values())


@pytest.mark.asyncio
async def test_stale_control_cannot_reach_other_window(owner_app):
    app, manager, _, handlers = owner_app
    await app.process_update(Update.de_json(update_payload(), app.bot))
    manager.bind_thread(-10011, 42, "@1")
    payload = update_payload("callback")
    payload["callback_query"]["data"] = "keys:ent:@2"
    await app.process_update(Update.de_json(payload, app.bot))
    handlers["callback_handler"].assert_not_awaited()


def test_stop_mentions_saved_owner_not_group_id(monkeypatch, tmp_path):
    monkeypatch.setattr(utils_module, "ccbot_dir", lambda: tmp_path)
    monkeypatch.setattr(
        hook_module, "_get_tmux_window", lambda: ("ccbot", "@1", "test")
    )
    monkeypatch.setattr(hook_module, "_read_bot_token", lambda _: "test-token")
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "thread_bindings": {"-10011": {"42": "@1"}},
                "group_chat_ids": {"-10011:42": -10011},
                "topic_owners": {"-10011:42": {"user_id": 111, "username": "owner"}},
            }
        )
    )
    request = MagicMock()
    monkeypatch.setattr("urllib.request.urlopen", request)
    hook_module._notify_telegram("Stop", {})
    payload = parse_qs(request.call_args.args[0].data.decode())
    assert payload["chat_id"] == ["-10011"]
    assert payload["message_thread_id"] == ["42"]
    assert 'href="tg://user?id=111">@owner</a>' in payload["text"][0]


def test_install_adds_missing_hooks_without_replacing_settings(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "permissions": {"defaultMode": "default"},
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/bin/ccbot hook"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "another-hook"}]}
                    ],
                },
            }
        )
    )
    monkeypatch.setattr(hook_module, "_CLAUDE_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(hook_module, "_find_ccbot_path", lambda: "/bin/ccbot")
    assert hook_module._install_hook() == 0
    first = settings_file.read_text()
    assert hook_module._install_hook() == 0
    assert settings_file.read_text() == first
    settings = json.loads(first)
    assert settings["permissions"] == {"defaultMode": "default"}
    assert len(settings["hooks"]["Stop"]) == 2
    for event in ("SessionStart", "Stop", "Notification"):
        assert hook_module._is_hook_installed(settings, event)
