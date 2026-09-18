"""Exercise automatic topic setup, directory policy, and independent sessions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update, User
from telegram.ext import ExtBot

import ccbot.bot as bot_module
from ccbot.config import Config, config
from ccbot.session import SessionManager


def payload(kind="created", topic=42, user_id=111, chat_id=-10011):
    sender = {
        "id": user_id,
        "first_name": "Owner",
        "username": "owner",
        "is_bot": False,
    }
    message = {
        "message_id": topic,
        "date": 1700000000,
        "from": sender,
        "chat": {"id": chat_id, "type": "supergroup", "is_forum": True},
        "message_thread_id": topic,
        "is_topic_message": topic != 1,
    }
    if kind == "created":
        message["forum_topic_created"] = {"name": f"Topic {topic}", "icon_color": 0}
    elif kind == "document":
        message["document"] = {
            "file_id": "file",
            "file_unique_id": "unique",
            "file_name": "input.csv",
        }
        message["caption"] = "Read this CSV"
    else:
        message["text"] = "Task pertama"
    return {"update_id": topic, "message": message}


@pytest.fixture
async def fixed_app(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_module, "prepare_recovery", AsyncMock())
    workspace = tmp_path / "agrinas"
    workspace.mkdir()
    monkeypatch.setattr(config, "fixed_workdir", workspace)
    monkeypatch.setattr(config, "topic_owner_lock", True)
    monkeypatch.setattr(config, "chat_scoped_topics", True)
    monkeypatch.setattr(config, "allowed_users", {111, 222})
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(bot_module, "_fixed_workspace_lock", asyncio.Lock())
    manager = SessionManager()
    monkeypatch.setattr(bot_module, "session_manager", manager)
    windows = {}

    async def create(workdir, window_name=None):
        await asyncio.sleep(0)
        wid = f"@{len(windows) + 1}"
        windows[wid] = SimpleNamespace(window_id=wid, cwd=workdir)
        return True, "created", window_name or "agrinas", wid

    async def hook_ready(wid, timeout):
        state = manager.get_window_state(wid)
        state.cwd = windows[wid].cwd
        state.session_id = f"00000000-0000-0000-0000-{int(wid[1:]):012d}"
        manager._save_state()
        return True

    async def initialize(bot):
        bot._bot_user = User(123456, "Bot", True, username="test_bot")
        bot._initialized = True

    monkeypatch.setattr(ExtBot, "initialize", initialize)
    monkeypatch.setattr(ExtBot, "shutdown", AsyncMock())
    monkeypatch.setattr(ExtBot, "send_chat_action", AsyncMock())
    monkeypatch.setattr(ExtBot, "answer_callback_query", AsyncMock())
    monkeypatch.setattr(
        bot_module.tmux_manager, "create_window", AsyncMock(side_effect=create)
    )
    monkeypatch.setattr(
        bot_module.tmux_manager, "find_window_by_id", AsyncMock(side_effect=windows.get)
    )
    monkeypatch.setattr(
        bot_module.tmux_manager, "capture_pane", AsyncMock(return_value="")
    )
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "list_windows",
        AsyncMock(side_effect=AssertionError("No window picker in fixed mode")),
    )
    monkeypatch.setattr(
        bot_module,
        "build_directory_browser",
        MagicMock(side_effect=AssertionError("No directory picker in fixed mode")),
    )
    monkeypatch.setattr(
        bot_module,
        "build_session_picker",
        MagicMock(side_effect=AssertionError("No resume picker in fixed mode")),
    )
    monkeypatch.setattr(bot_module, "safe_reply", AsyncMock())
    monkeypatch.setattr(bot_module, "enqueue_status_update", AsyncMock())
    monkeypatch.setattr(bot_module, "get_interactive_window", lambda *args: None)
    monkeypatch.setattr(
        bot_module,
        "receive_attachment",
        AsyncMock(return_value=workspace / "input.csv"),
    )
    manager.wait_for_session_map_entry = AsyncMock(side_effect=hook_ready)
    manager.send_to_window = AsyncMock(return_value=(True, "sent"))
    app = bot_module.create_bot()
    await app.initialize()
    yield app, manager, windows, workspace
    await app.shutdown()


@pytest.mark.asyncio
async def test_creation_immediately_binds_and_first_message_uses_same_session(
    fixed_app,
):
    app, manager, windows, workspace = fixed_app
    await app.process_update(Update.de_json(payload(), app.bot))
    assert manager.get_window_for_thread(-10011, 42) == "@1"
    assert windows["@1"].cwd == str(workspace)
    bot_module.safe_reply.assert_not_awaited()
    await app.process_update(Update.de_json(payload("text"), app.bot))
    bot_module.tmux_manager.create_window.assert_awaited_once()
    manager.send_to_window.assert_awaited_once_with("@1", "Task pertama")
    assert "@owner" in bot_module.safe_reply.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "document"])
async def test_first_input_without_creation_event_also_skips_pickers(fixed_app, kind):
    app, manager, _, workspace = fixed_app
    await app.process_update(Update.de_json(payload(kind), app.bot))
    assert manager.get_window_for_thread(-10011, 42) == "@1"
    sent = manager.send_to_window.call_args.args
    assert sent[0] == "@1"
    if kind == "document":
        assert "Read this CSV" in sent[1] and str(workspace / "input.csv") in sent[1]
    else:
        assert sent[1] == "Task pertama"


@pytest.mark.asyncio
async def test_topics_get_separate_sessions_in_same_folder(fixed_app):
    app, manager, windows, workspace = fixed_app
    for topic in (42, 43):
        await app.process_update(Update.de_json(payload(topic=topic), app.bot))
    assert {w.cwd for w in windows.values()} == {str(workspace)}
    assert len({manager.get_window_for_thread(-10011, t) for t in (42, 43)}) == 2
    assert len({state.session_id for state in manager.window_states.values()}) == 2


@pytest.mark.asyncio
async def test_concurrent_creation_does_not_duplicate_session(fixed_app):
    app, manager, windows, _ = fixed_app
    update = Update.de_json(payload(), app.bot)
    result = await asyncio.gather(
        *(bot_module._ensure_fixed_topic_window(update, MagicMock()) for _ in range(2))
    )
    assert result == ["@1", "@1"]
    assert len(windows) == 1
    assert manager.get_window_for_thread(-10011, 42) == "@1"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unauthorized", "general", "different_owner"])
async def test_rejected_updates_cannot_create_sessions(fixed_app, case):
    app, manager, windows, _ = fixed_app
    if case == "different_owner":
        manager.claim_topic(-10011, 42, 222, "other", "Other")
    update = payload(
        "text",
        topic=1 if case == "general" else 42,
        user_id=999 if case == "unauthorized" else 111,
    )
    await app.process_update(Update.de_json(update, app.bot))
    assert windows == {}
    manager.send_to_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_directory_does_not_fall_back_to_picker(fixed_app):
    app, manager, windows, workspace = fixed_app
    workspace.rmdir()
    await app.process_update(Update.de_json(payload("text"), app.bot))
    assert windows == {}
    assert manager.get_window_for_thread(-10011, 42) is None
    assert "unavailable" in bot_module.safe_reply.call_args.args[1]


@pytest.mark.asyncio
async def test_hook_timeout_keeps_binding_for_retry(fixed_app):
    app, manager, windows, _ = fixed_app
    manager.wait_for_session_map_entry = AsyncMock(side_effect=[False, True])
    await app.process_update(Update.de_json(payload("text"), app.bot))
    manager.send_to_window.assert_not_awaited()
    assert "still starting" in bot_module.safe_reply.call_args.args[1]
    await app.process_update(Update.de_json(payload("text"), app.bot))
    assert len(windows) == 1
    manager.send_to_window.assert_awaited_once_with("@1", "Task pertama")


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["db:confirm", "wb:sel:0", "rs:sel:0"])
async def test_old_picker_buttons_cannot_override_fixed_folder(fixed_app, data):
    app, _, windows, _ = fixed_app
    await app.process_update(Update.de_json(payload(), app.bot))
    msg = payload("text")["message"]
    update = {
        "update_id": 99,
        "callback_query": {
            "id": "button",
            "from": msg["from"],
            "chat_instance": "chat",
            "data": data,
            "message": msg,
        },
    }
    await app.process_update(Update.de_json(update, app.bot))
    assert len(windows) == 1
    assert "fixed workspace" in app.bot.answer_callback_query.call_args.kwargs["text"]


@pytest.mark.parametrize("kind", ["unset", "directory", "relative", "missing", "file"])
def test_config_validates_fixed_directory(monkeypatch, tmp_path, kind):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ALLOWED_USERS", "111")
    file = tmp_path / "plain-file"
    file.write_text("x")
    raw = {
        "unset": "",
        "directory": str(tmp_path),
        "relative": "relative",
        "missing": str(tmp_path / "missing"),
        "file": str(file),
    }[kind]
    monkeypatch.setenv("CCBOT_FIXED_WORKDIR", raw)
    if kind in ("relative", "missing", "file"):
        with pytest.raises(ValueError, match="CCBOT_FIXED_WORKDIR"):
            Config()
    else:
        assert Config().fixed_workdir == (tmp_path if raw else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "document"])
async def test_pending_recovery_blocks_fresh_session_and_file_dispatch(fixed_app, kind):
    app, manager, windows, _ = fixed_app
    await app.process_update(Update.de_json(payload(), app.bot))
    windows.clear()
    manager.stage_recovery(-10011, 42)
    await app.process_update(Update.de_json(payload(kind), app.bot))
    assert windows == {}
    assert bot_module.tmux_manager.create_window.await_count == 1
    manager.send_to_window.assert_not_awaited()
    bot_module.receive_attachment.assert_not_awaited()
    assert "/recover" in bot_module.safe_reply.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [111, 222, 999])
async def test_only_topic_owner_can_retry_recovery(fixed_app, monkeypatch, user_id):
    app, manager, windows, _ = fixed_app
    await app.process_update(Update.de_json(payload(), app.bot))
    windows.clear()
    manager.stage_recovery(-10011, 42)
    retry = AsyncMock(return_value=False)
    monkeypatch.setattr(bot_module, "recover_topic", retry)
    update = payload("text", user_id=user_id)
    update["message"]["text"] = "/recover"
    update["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 8}]
    await app.process_update(Update.de_json(update, app.bot))
    if user_id == 111:
        retry.assert_awaited_once_with(manager, -10011, 42)
    else:
        retry.assert_not_awaited()
    assert manager.pending_recoveries


@pytest.mark.asyncio
async def test_explicit_unbind_allows_fresh_session_after_failed_recovery(fixed_app):
    app, manager, windows, _ = fixed_app
    await app.process_update(Update.de_json(payload(), app.bot))
    windows.clear()
    manager.stage_recovery(-10011, 42)
    update = payload("text")
    update["message"]["text"] = "/unbind"
    update["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 7}]
    await app.process_update(Update.de_json(update, app.bot))
    assert not manager.pending_recoveries
    await app.process_update(Update.de_json(payload("text"), app.bot))
    assert bot_module.tmux_manager.create_window.await_count == 2
    assert manager.topic_owners["-10011:42"]["user_id"] == 111


@pytest.mark.asyncio
async def test_missing_window_on_first_message_preserves_previous_session(fixed_app):
    app, manager, windows, _ = fixed_app
    await app.process_update(Update.de_json(payload(), app.bot))
    previous = manager.window_states["@1"].session_id
    windows.clear()
    await app.process_update(Update.de_json(payload("text"), app.bot))
    assert (
        manager.pending_recoveries["-10011:42"]["window_state"]["session_id"]
        == previous
    )
    assert bot_module.tmux_manager.create_window.await_count == 1
    manager.send_to_window.assert_not_awaited()
