"""Regression tests for private groups plus shared group topic routing."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest

import ccbot.bot as bot_module
import ccbot.hook as hook_module
import ccbot.utils as utils_module
from ccbot.config import config
from ccbot.handlers.callback_data import CB_DIR_CANCEL
from ccbot.handlers.message_queue import MessageTask, _can_merge_tasks
from ccbot.routing import conversation_id, get_topic_data
from ccbot.session import SessionManager
from ccbot.session_monitor import NewMessage


@pytest.fixture
def shared(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "chat_scoped_topics", True)
    monkeypatch.setattr(config, "allowed_users", {111, 222})
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    manager = SessionManager()
    monkeypatch.setattr(bot_module, "session_manager", manager)
    monkeypatch.setattr(bot_module, "safe_reply", AsyncMock())
    monkeypatch.setattr(bot_module, "safe_edit", AsyncMock())
    monkeypatch.setattr(bot_module, "enqueue_status_update", AsyncMock())
    monkeypatch.setattr(bot_module, "get_interactive_window", lambda *args: None)
    monkeypatch.setattr(bot_module, "get_interactive_msg_id", lambda *args: None)
    monkeypatch.setattr(bot_module, "clear_topic_state", AsyncMock())
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(side_effect=lambda wid: SimpleNamespace(window_id=wid)),
    )
    monkeypatch.setattr(
        bot_module.tmux_manager, "capture_pane", AsyncMock(return_value="")
    )
    monkeypatch.setattr(
        bot_module.tmux_manager, "list_windows", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        bot_module.tmux_manager, "kill_window", AsyncMock(return_value=True)
    )
    manager.send_to_window = AsyncMock(return_value=(True, "ok"))
    return manager


def make_update(user_id=111, chat_id=-10011, thread_id=42, text="hello"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = "supergroup"
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = update.effective_chat
    update.message.chat.send_action = AsyncMock()
    update.callback_query = None
    return update


def make_context(chat_data=None):
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    context.chat_data = {} if chat_data is None else chat_data
    return context


@pytest.mark.asyncio
async def test_same_person_in_two_groups_and_two_people_in_shared_topic(shared):
    shared.bind_thread(-10011, 42, "@1")
    shared.bind_thread(-10022, 42, "@2")
    for user_id, chat_id in [(111, -10011), (111, -10022), (222, -10022)]:
        await bot_module.text_handler(make_update(user_id, chat_id), make_context())
    assert [call.args[0] for call in shared.send_to_window.call_args_list] == [
        "@1",
        "@2",
        "@2",
    ]
    assert shared.resolve_chat_id(-10011, 42) == -10011
    assert shared.resolve_chat_id(-10022, 42) == -10022


@pytest.mark.asyncio
async def test_authorization_uses_real_sender_even_in_a_bound_group(shared):
    shared.bind_thread(-10022, 42, "@2")
    await bot_module.text_handler(make_update(999, -10022), make_context())
    shared.send_to_window.assert_not_called()
    assert "not authorized" in bot_module.safe_reply.call_args.args[1]


@pytest.mark.asyncio
async def test_topic_close_by_second_user_only_closes_shared_session(shared):
    shared.bind_thread(-10011, 42, "@1")
    shared.bind_thread(-10022, 42, "@2")
    await bot_module.topic_closed_handler(make_update(222, -10022), make_context())
    bot_module.tmux_manager.kill_window.assert_awaited_once_with("@2")
    assert shared.get_window_for_thread(-10011, 42) == "@1"
    assert shared.get_window_for_thread(-10022, 42) is None


@pytest.mark.asyncio
async def test_picker_state_does_not_leak_between_private_and_shared_group(
    shared, monkeypatch
):
    monkeypatch.setattr(
        bot_module, "build_directory_browser", lambda path: ("picker", None, [])
    )
    private_context, shared_context = make_context(), make_context()
    await bot_module.text_handler(make_update(text="private task"), private_context)
    await bot_module.text_handler(
        make_update(chat_id=-10022, text="shared task"), shared_context
    )
    second_user_context = make_context(shared_context.chat_data)
    assert (
        get_topic_data(second_user_context, 42)["_pending_thread_text"] == "shared task"
    )

    update = make_update(222, -10022)
    message = update.message
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.message = message
    update.callback_query.data = CB_DIR_CANCEL
    update.callback_query.answer = AsyncMock()
    await bot_module.callback_handler(update, second_user_context)
    assert get_topic_data(private_context, 42)["_pending_thread_text"] == "private task"
    assert "_pending_thread_text" not in get_topic_data(shared_context, 42)


@pytest.mark.asyncio
async def test_reply_delivered_once_to_shared_group_topic(shared, monkeypatch):
    shared.bind_thread(-10022, 42, "@2")
    shared.get_window_state("@2").session_id = "shared-session"
    enqueue = AsyncMock()
    monkeypatch.setattr(bot_module, "enqueue_content_message", enqueue)
    monkeypatch.setattr(
        shared, "resolve_session_for_window", AsyncMock(return_value=None)
    )
    await bot_module.handle_new_message(
        NewMessage("shared-session", "result", True), AsyncMock()
    )
    enqueue.assert_awaited_once()
    assert enqueue.call_args.kwargs["user_id"] == -10022
    assert enqueue.call_args.kwargs["thread_id"] == 42


def test_group_bindings_survive_restart_without_topic_id_collisions(shared):
    shared.bind_thread(-10011, 42, "@1")
    shared.bind_thread(-10022, 42, "@2")
    restored = SessionManager()
    assert restored.get_window_for_thread(conversation_id(111, -10011), 42) == "@1"
    assert restored.get_window_for_thread(conversation_id(222, -10022), 42) == "@2"
    assert json.loads(config.state_file.read_text())["routing_scope"] == "chat"


def test_old_user_state_requires_explicit_migration(shared):
    config.state_file.write_text(json.dumps({"thread_bindings": {"111": {"42": "@1"}}}))
    with pytest.raises(RuntimeError, match="routing scope differs"):
        SessionManager()


def test_queue_never_merges_different_topics():
    first = MessageTask("content", window_id="@1", thread_id=42, parts=["one"])
    second = MessageTask("content", window_id="@1", thread_id=99, parts=["two"])
    assert not _can_merge_tasks(first, second)


def test_shared_hook_notification_targets_group_without_user_mention(
    shared, monkeypatch, tmp_path
):
    shared.bind_thread(-10022, 42, "@2")
    shared.set_group_chat_id(-10022, 42, -10022)
    monkeypatch.setattr(utils_module, "ccbot_dir", lambda: tmp_path)
    monkeypatch.setattr(
        hook_module, "_get_tmux_window", lambda: ("ccbot", "@2", "project")
    )
    monkeypatch.setattr(hook_module, "_read_bot_token", lambda _: "test-token")
    mention = MagicMock(side_effect=AssertionError("A group ID is not a user ID"))
    monkeypatch.setattr(hook_module, "_get_user_mention", mention)
    urlopen = MagicMock()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    hook_module._notify_telegram("Stop", {})
    mention.assert_not_called()
    urlopen.assert_called_once()
    payload = parse_qs(urlopen.call_args.args[0].data.decode())
    assert payload["chat_id"] == ["-10022"]
    assert payload["message_thread_id"] == ["42"]
    assert "tg://user" not in payload["text"][0]
