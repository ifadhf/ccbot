"""Opt-in VM features stay inert unless their CCBOT_* flag is enabled."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop, CommandHandler, MessageHandler

import ccbot.bot as bot_module
import ccbot.file_transfer as transfer
import ccbot.hook as hook_module
import ccbot.recovery as recovery
from ccbot.config import Config, config
from ccbot.session import SessionManager

FLAGS = ("ignore_general_topic", "session_recovery", "file_transfer")
SID = "11111111-2222-3333-4444-555555555555"


def test_flags_default_off(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    for name in FLAGS:
        monkeypatch.delenv(f"CCBOT_{name.upper()}", raising=False)

    def load() -> Config:
        # Config scrubs these from the environment after reading them.
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
        monkeypatch.setenv("ALLOWED_USERS", "12345")
        return Config()

    cfg = load()
    assert not any(getattr(cfg, name) for name in FLAGS)
    monkeypatch.setenv("CCBOT_FILE_TRANSFER", "true")
    assert load().file_transfer is True


@pytest.mark.parametrize("enabled", [False, True])
async def test_general_topic_reaches_handlers_unless_enabled(monkeypatch, enabled):
    monkeypatch.setattr(config, "ignore_general_topic", enabled)
    message = SimpleNamespace(
        forum_topic_created=None,
        is_topic_message=False,
        message_thread_id=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(is_forum=True),
        effective_message=message,
        message=message,
        callback_query=None,
    )
    if enabled:
        with pytest.raises(ApplicationHandlerStop):
            await bot_module.ignore_general_topic(update, None)  # type: ignore[arg-type]
    else:
        assert await bot_module.ignore_general_topic(update, None) is None  # type: ignore[arg-type]


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(config, "chat_scoped_topics", False)
    monkeypatch.setattr(config, "topic_owner_lock", False)
    return SessionManager()


async def test_recovery_never_touches_tmux_when_disabled(monkeypatch, manager):
    monkeypatch.setattr(config, "session_recovery", False)
    identity = AsyncMock(side_effect=AssertionError("tmux must not be queried"))
    monkeypatch.setattr(recovery.tmux_manager, "session_identity", identity)
    manager.bind_thread(111, 42, "@1", "project")
    await recovery.prepare_recovery(manager)
    await recovery.recover_sessions(manager)
    assert await recovery.recover_topic(manager, 111, 42) is False
    identity.assert_not_called()
    assert manager.tmux_session_identity == ""


@pytest.mark.parametrize("enabled", [False, True])
def test_lost_window_unbinds_unless_recovery_enabled(monkeypatch, manager, enabled):
    monkeypatch.setattr(config, "session_recovery", enabled)
    manager.bind_thread(111, 42, "@1", "project")
    assert manager.release_lost_binding(111, 42) is enabled
    assert manager.get_window_for_thread(111, 42) is None
    assert ("111:42" in manager.pending_recoveries) is enabled
    # Without recovery a lost topic can bind a new window straight away.
    if not enabled:
        manager.bind_thread(111, 42, "@2", "project")
        assert manager.get_window_for_thread(111, 42) == "@2"


@pytest.mark.parametrize("enabled", [False, True])
def test_optional_handlers_registered_only_when_enabled(monkeypatch, enabled):
    monkeypatch.setattr(config, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(config, "session_recovery", enabled)
    monkeypatch.setattr(config, "file_transfer", enabled)
    application = bot_module.create_bot()
    handlers = [h for group in application.handlers.values() for h in group]
    callbacks = {h.callback for h in handlers if isinstance(h, MessageHandler)}
    commands = {
        name for h in handlers if isinstance(h, CommandHandler) for name in h.commands
    }
    assert (bot_module.document_handler in callbacks) is enabled
    assert ("recover" in commands) is enabled


@pytest.fixture
def legacy_topic(monkeypatch, tmp_path):
    """A per-user binding as stored by the laptop bot (no owner records)."""
    monkeypatch.setattr(transfer, "ccbot_dir", lambda: tmp_path)
    for name in ("CCBOT_FILE_TRANSFER", "CCBOT_TOPIC_OWNER_LOCK", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    (tmp_path / ".env").write_text("ALLOWED_USERS=111\nCCBOT_FILE_TRANSFER=true\n")
    state = {
        "thread_bindings": {"111": {"42": "@1"}},
        "group_chat_ids": {"111:42": -10011},
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "session_map.json").write_text(
        json.dumps({"ccbot:@1": {"session_id": SID, "cwd": "/w", "window_name": "w"}})
    )
    return tmp_path


def test_file_helpers_refuse_when_disabled(monkeypatch, legacy_topic):
    (legacy_topic / ".env").write_text("ALLOWED_USERS=111\n")
    monkeypatch.setattr(hook_module, "_get_tmux_window", lambda: ("ccbot", "@1", "w"))
    with pytest.raises(ValueError, match="disabled"):
        transfer.current_topic()


def test_per_user_binding_resolves_without_owner_records(monkeypatch, legacy_topic):
    monkeypatch.setattr(hook_module, "_get_tmux_window", lambda: ("ccbot", "@1", "w"))
    assert transfer.current_topic() == (-10011, 42)
    (legacy_topic / ".env").write_text("ALLOWED_USERS=222\nCCBOT_FILE_TRANSFER=true\n")
    with pytest.raises(ValueError, match="not allowed"):
        transfer.current_topic()


@pytest.mark.parametrize("session_id", [SID, "other-session"])
def test_background_session_resolves_window_by_session_id(
    monkeypatch, legacy_topic, session_id
):
    monkeypatch.setattr(hook_module, "_get_tmux_window", lambda: None)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    if session_id == SID:
        assert transfer.current_topic() == (-10011, 42)
    else:
        with pytest.raises(ValueError, match="CCBot Claude session"):
            transfer.current_topic()


def test_pane_in_another_tmux_session_falls_back_to_session_id(
    monkeypatch, legacy_topic
):
    monkeypatch.setattr(
        hook_module, "_get_tmux_window", lambda: ("personal", "@1", "w")
    )
    with pytest.raises(ValueError):
        transfer.current_topic()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert transfer.current_topic() == (-10011, 42)
