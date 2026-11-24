"""Permission mode step (CCBOT_PERMISSION_CHOICE) between the pickers and launch."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, User

import ccbot.bot as bot_module
import ccbot.tmux_manager as tmux_module
from ccbot.config import config
from ccbot.handlers.callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_WIN_BIND,
    CB_PERM_BYPASS,
    CB_PERM_CANCEL,
    CB_PERM_NORMAL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
)
from ccbot.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    UNBOUND_WINDOWS_KEY,
    STATE_SELECTING_WINDOW,
    PERM_PENDING_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_PERMISSION,
    STATE_SELECTING_SESSION,
    build_permission_picker,
)
from ccbot.routing import get_topic_data
from ccbot.session import ClaudeSession, SessionManager, WindowState
from ccbot.tmux_manager import BYPASS_PERMISSIONS_FLAG, TmuxManager

SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BYPASS_WARNING_PANE = (
    " WARNING: Claude Code running in Bypass Permissions mode\n"
    "\n"
    " In Bypass Permissions mode, Claude Code will not ask for your approval\n"
    " before running potentially dangerous commands.\n"
    "\n"
    " ❯ 1. No, exit\n"
    "   2. Yes, I accept\n"
    "\n"
    " Enter to confirm · Esc to exit\n"
)


# ── build_claude_command / create_window ─────────────────────────────────


@pytest.fixture
def claude_cmd(monkeypatch):
    monkeypatch.setattr(config, "claude_command", "claude")


def test_build_claude_command_normal_matches_claude_command(claude_cmd):
    assert tmux_module.build_claude_command() == "claude"
    assert tmux_module.build_claude_command(SID) == f"claude --resume {SID}"


def test_build_claude_command_bypass_new(claude_cmd):
    assert (
        tmux_module.build_claude_command(skip_permissions=True)
        == "claude --dangerously-skip-permissions"
    )


def test_build_claude_command_bypass_resume_puts_flag_before_resume(claude_cmd):
    assert (
        tmux_module.build_claude_command(SID, skip_permissions=True)
        == f"claude --dangerously-skip-permissions --resume {SID}"
    )


def test_build_claude_command_does_not_duplicate_flag_in_claude_command(monkeypatch):
    monkeypatch.setattr(
        config, "claude_command", "claude --dangerously-skip-permissions"
    )
    assert (
        tmux_module.build_claude_command(skip_permissions=True)
        == "claude --dangerously-skip-permissions"
    )


@pytest.fixture
def fake_tmux(monkeypatch, claude_cmd):
    manager = TmuxManager("ccbot-test-never-used")
    pane = MagicMock()
    window = MagicMock(window_id="@5", active_pane=pane)
    session = MagicMock()
    session.new_window.return_value = window
    monkeypatch.setattr(manager, "get_or_create_session", lambda: session)
    monkeypatch.setattr(manager, "find_window_by_name", AsyncMock(return_value=None))
    return SimpleNamespace(manager=manager, session=session, pane=pane)


@pytest.mark.asyncio
async def test_create_window_sends_bypass_command_to_pane(fake_tmux, tmp_path):
    ok, _, _, wid = await fake_tmux.manager.create_window(
        str(tmp_path), skip_permissions=True
    )
    assert ok and wid == "@5"
    fake_tmux.pane.send_keys.assert_called_once_with(
        "claude --dangerously-skip-permissions", enter=True
    )


@pytest.mark.asyncio
async def test_create_window_normal_command_unchanged(fake_tmux, tmp_path):
    await fake_tmux.manager.create_window(str(tmp_path), resume_session_id=SID)
    fake_tmux.pane.send_keys.assert_called_once_with(
        f"claude --resume {SID}", enter=True
    )


@pytest.mark.asyncio
async def test_create_window_recovery_shell_includes_bypass_flag(fake_tmux, tmp_path):
    token = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    ok, *_ = await fake_tmux.manager.create_window(
        str(tmp_path),
        resume_session_id=SID,
        recovery_token=token,
        skip_permissions=True,
    )
    assert ok
    shell = fake_tmux.session.new_window.call_args.kwargs["window_shell"]
    assert shell == f"claude --dangerously-skip-permissions --resume {SID}"
    fake_tmux.pane.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_create_window_rejects_non_uuid_resume_even_with_bypass(
    fake_tmux, tmp_path
):
    ok, msg, _, _ = await fake_tmux.manager.create_window(
        str(tmp_path), resume_session_id="$(rm -rf ~)", skip_permissions=True
    )
    assert not ok and "Invalid session ID" in msg
    fake_tmux.session.new_window.assert_not_called()


@pytest.mark.asyncio
async def test_create_window_rejects_skip_permissions_without_start_claude(
    fake_tmux, tmp_path
):
    ok, msg, _, _ = await fake_tmux.manager.create_window(
        str(tmp_path), start_claude=False, skip_permissions=True
    )
    assert not ok and msg == "Invalid launch options"
    fake_tmux.session.new_window.assert_not_called()


# ── Telegram flow ────────────────────────────────────────────────────────


@pytest.fixture
def perm(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "chat_scoped_topics", True)
    monkeypatch.setattr(config, "topic_owner_lock", False)
    monkeypatch.setattr(config, "session_recovery", False)
    monkeypatch.setattr(config, "fixed_workdir", None)
    monkeypatch.setattr(config, "permission_choice", True)
    monkeypatch.setattr(config, "allowed_users", {111, 222})
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    manager = SessionManager()
    monkeypatch.setattr(bot_module, "session_manager", manager)
    monkeypatch.setattr(bot_module, "safe_reply", AsyncMock())
    monkeypatch.setattr(bot_module, "safe_edit", AsyncMock())
    monkeypatch.setattr(bot_module, "safe_send", AsyncMock())
    monkeypatch.setattr(bot_module, "enqueue_status_update", AsyncMock())
    monkeypatch.setattr(bot_module, "get_interactive_window", lambda *args: None)
    create = AsyncMock(return_value=(True, "Created window 'p' at /p", "p", "@5"))
    monkeypatch.setattr(bot_module.tmux_manager, "create_window", create)
    monkeypatch.setattr(
        bot_module.tmux_manager, "capture_pane", AsyncMock(return_value="")
    )
    monkeypatch.setattr(
        bot_module.tmux_manager, "list_windows", AsyncMock(return_value=[])
    )
    manager.wait_for_session_map_entry = AsyncMock(return_value=True)
    manager.override_session_map_entry = AsyncMock()
    manager.list_sessions_for_directory = AsyncMock(return_value=[])
    manager.send_to_window = AsyncMock(return_value=(True, "ok"))
    workdir = tmp_path / "proj"
    workdir.mkdir()
    return SimpleNamespace(manager=manager, create=create, workdir=str(workdir))


def make_context(chat_data=None):
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    context.chat_data = {} if chat_data is None else chat_data
    return context


def make_message_update(user_id=111, chat_id=-10011, thread_id=42, text="hello"):
    update = MagicMock()
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = "supergroup"
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = update.effective_chat
    update.message.chat.send_action = AsyncMock()
    update.callback_query = None
    return update


def make_callback(data, user_id=111, chat_id=-10011, thread_id=42):
    update = make_message_update(user_id, chat_id, thread_id)
    message = update.message
    update.message = None
    update.callback_query = MagicMock(spec=CallbackQuery)
    update.callback_query.message = message
    update.callback_query.message.chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


def topic(context, thread_id=42):
    data = get_topic_data(context, thread_id)
    assert data is not None
    return data


def seed_session_picker(context, path, sessions=None, text="hello"):
    data = topic(context)
    data[STATE_KEY] = STATE_SELECTING_SESSION
    data[SESSIONS_KEY] = sessions or []
    data["_selected_path"] = path
    data["_pending_thread_id"] = 42
    data["_pending_thread_text"] = text
    data["_pending_user_id"] = 111
    return data


def seed_browser(context, path, text="hello"):
    data = topic(context)
    data[STATE_KEY] = STATE_BROWSING_DIRECTORY
    data[BROWSE_PATH_KEY] = path
    data["_pending_thread_id"] = 42
    data["_pending_thread_text"] = text
    data["_pending_user_id"] = 111
    return data


def alert_text(update):
    call = update.callback_query.answer.call_args
    return call.args[0] if call.args else call.kwargs.get("text", "")


async def open_picker(perm, context, resume=False):
    sessions = [ClaudeSession(SID, "Fix the login bug", 3, "/nonexistent")]
    seed_session_picker(context, perm.workdir, sessions)
    data = f"{CB_SESSION_SELECT}0" if resume else CB_SESSION_NEW
    await bot_module.callback_handler(make_callback(data), context)
    return topic(context)[PERM_PENDING_KEY]["token"]


@pytest.mark.asyncio
async def test_flag_off_session_new_creates_window_without_mode_step(perm, monkeypatch):
    monkeypatch.setattr(config, "permission_choice", False)
    context = make_context()
    seed_session_picker(context, perm.workdir)
    await bot_module.callback_handler(make_callback(CB_SESSION_NEW), context)
    perm.create.assert_awaited_once()
    assert perm.create.call_args.args == (perm.workdir,)
    assert perm.create.call_args.kwargs.get("skip_permissions", False) is False
    assert perm.manager.get_window_for_thread(-10011, 42) == "@5"
    text = bot_module.safe_edit.call_args.args[1]
    assert text == "✅ Created window 'p' at /p\n\nCreated. Send messages here."
    assert "skip_permissions" not in json.loads(config.state_file.read_text())[
        "window_states"
    ].get("@5", {})


@pytest.mark.asyncio
async def test_flag_off_dir_confirm_without_sessions_creates_directly(
    perm, monkeypatch
):
    monkeypatch.setattr(config, "permission_choice", False)
    context = make_context()
    seed_browser(context, perm.workdir)
    await bot_module.callback_handler(make_callback(CB_DIR_CONFIRM), context)
    perm.create.assert_awaited_once()
    assert perm.create.call_args.kwargs.get("skip_permissions", False) is False
    assert perm.manager.get_window_for_thread(-10011, 42) == "@5"
    assert STATE_KEY not in topic(context)


@pytest.mark.asyncio
async def test_flag_on_session_new_shows_permission_picker(perm):
    context = make_context()
    token = await open_picker(perm, context)
    perm.create.assert_not_awaited()
    data = topic(context)
    assert data[STATE_KEY] == STATE_SELECTING_PERMISSION
    pending = data[PERM_PENDING_KEY]
    assert pending["path"] == perm.workdir
    assert pending["resume_session_id"] is None
    assert pending["thread_id"] == 42 and pending["user_id"] == 111
    assert len(token) == 8
    # Pending first message is still held for after the choice.
    assert data["_pending_thread_text"] == "hello"
    markup = bot_module.safe_edit.call_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callbacks == [
        f"{CB_PERM_NORMAL}{token}",
        f"{CB_PERM_BYPASS}{token}",
        f"{CB_PERM_CANCEL}{token}",
    ]
    assert "New session" in bot_module.safe_edit.call_args.args[1]


@pytest.mark.asyncio
async def test_flag_on_session_select_shows_picker_and_keeps_resume_id(perm):
    context = make_context()
    await open_picker(perm, context, resume=True)
    perm.create.assert_not_awaited()
    assert topic(context)[PERM_PENDING_KEY]["resume_session_id"] == SID
    assert "Resume: Fix the login bug" in bot_module.safe_edit.call_args.args[1]


@pytest.mark.asyncio
async def test_flag_on_dir_confirm_without_sessions_shows_permission_picker(perm):
    context = make_context()
    seed_browser(context, perm.workdir)
    await bot_module.callback_handler(make_callback(CB_DIR_CONFIRM), context)
    perm.create.assert_not_awaited()
    assert topic(context)[STATE_KEY] == STATE_SELECTING_PERMISSION
    assert topic(context)[PERM_PENDING_KEY]["path"] == perm.workdir


@pytest.mark.asyncio
async def test_normal_choice_creates_window_without_bypass(perm):
    context = make_context()
    token = await open_picker(perm, context)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_NORMAL}{token}"), context
    )
    perm.create.assert_awaited_once()
    assert perm.create.call_args.kwargs["skip_permissions"] is False
    assert perm.manager.get_window_state("@5").skip_permissions is False
    assert "Mode: 🛡 Normal" in bot_module.safe_edit.call_args.args[1]
    assert STATE_KEY not in topic(context)
    assert PERM_PENDING_KEY not in topic(context)


@pytest.mark.asyncio
async def test_bypass_choice_creates_new_window_with_bypass(perm):
    context = make_context()
    token = await open_picker(perm, context)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_BYPASS}{token}"), context
    )
    perm.create.assert_awaited_once()
    assert perm.create.call_args.args == (perm.workdir,)
    assert perm.create.call_args.kwargs["resume_session_id"] is None
    assert perm.create.call_args.kwargs["skip_permissions"] is True
    assert perm.manager.get_window_for_thread(-10011, 42) == "@5"
    saved = json.loads(config.state_file.read_text())
    assert saved["window_states"]["@5"]["skip_permissions"] is True
    assert "⚠️ Bypass" in bot_module.safe_edit.call_args.args[1]
    assert SessionManager().window_states["@5"].skip_permissions is True


@pytest.mark.asyncio
async def test_bypass_choice_resume_passes_resume_id_and_bypass(perm):
    context = make_context()
    token = await open_picker(perm, context, resume=True)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_BYPASS}{token}"), context
    )
    kwargs = perm.create.call_args.kwargs
    assert kwargs["resume_session_id"] == SID and kwargs["skip_permissions"] is True
    perm.manager.override_session_map_entry.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_text_forwarded_after_choice(perm):
    context = make_context()
    token = await open_picker(perm, context)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_NORMAL}{token}"), context
    )
    perm.manager.send_to_window.assert_awaited_once_with("@5", "hello")
    assert "_pending_thread_text" not in topic(context)


@pytest.mark.asyncio
async def test_cancel_clears_selection_and_pending_text(perm):
    context = make_context()
    token = await open_picker(perm, context)
    update = make_callback(f"{CB_PERM_CANCEL}{token}")
    await bot_module.callback_handler(update, context)
    perm.create.assert_not_awaited()
    data = topic(context)
    for key in (STATE_KEY, PERM_PENDING_KEY, "_pending_thread_id"):
        assert key not in data
    assert "_pending_thread_text" not in data
    assert bot_module.safe_edit.call_args.args[1] == "Cancelled"


@pytest.mark.asyncio
async def test_stale_token_rejected_without_creating(perm):
    context = make_context()
    await open_picker(perm, context)
    update = make_callback(f"{CB_PERM_BYPASS}deadbeef")
    await bot_module.callback_handler(update, context)
    perm.create.assert_not_awaited()
    assert "expired" in alert_text(update)
    assert topic(context)[STATE_KEY] == STATE_SELECTING_PERMISSION


@pytest.mark.asyncio
async def test_second_tap_after_creation_is_expired(perm):
    context = make_context()
    token = await open_picker(perm, context)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_BYPASS}{token}"), context
    )
    update = make_callback(f"{CB_PERM_BYPASS}{token}")
    await bot_module.callback_handler(update, context)
    perm.create.assert_awaited_once()
    assert "expired" in alert_text(update)


@pytest.mark.asyncio
async def test_choice_from_other_topic_rejected(perm, monkeypatch):
    # Per-user picker state (chat scoping off) is shared across topics.
    monkeypatch.setattr(config, "chat_scoped_topics", False)
    perm.manager.thread_bindings.clear()
    context = make_context()
    token = await open_picker(perm, context)
    update = make_callback(f"{CB_PERM_BYPASS}{token}", thread_id=43)
    await bot_module.callback_handler(update, context)
    perm.create.assert_not_awaited()
    assert "topic mismatch" in alert_text(update)


@pytest.mark.asyncio
async def test_other_user_cannot_choose_in_shared_topic(perm):
    context = make_context()
    token = await open_picker(perm, context)
    other = make_context(context.chat_data)
    update = make_callback(f"{CB_PERM_BYPASS}{token}", user_id=222)
    await bot_module.callback_handler(update, other)
    perm.create.assert_not_awaited()
    assert "Only the person who started" in alert_text(update)
    assert topic(context)[STATE_KEY] == STATE_SELECTING_PERMISSION


@pytest.mark.asyncio
async def test_choice_rejected_when_flag_turned_off(perm, monkeypatch):
    context = make_context()
    token = await open_picker(perm, context)
    monkeypatch.setattr(config, "permission_choice", False)
    update = make_callback(f"{CB_PERM_BYPASS}{token}")
    await bot_module.callback_handler(update, context)
    perm.create.assert_not_awaited()
    assert "expired" in alert_text(update)


@pytest.mark.asyncio
async def test_text_during_permission_step_prompts_to_use_picker(perm):
    context = make_context()
    await open_picker(perm, context)
    await bot_module.text_handler(make_message_update(text="again"), context)
    assert "permission mode" in bot_module.safe_reply.call_args.args[1]
    perm.manager.send_to_window.assert_not_awaited()
    assert topic(context)["_pending_thread_text"] == "hello"


@pytest.mark.asyncio
async def test_chat_scoped_duplicate_blocks_second_session_at_choice(perm):
    context = make_context()
    token = await open_picker(perm, context)
    perm.manager.bind_thread(-10011, 42, "@1", "other")
    update = make_callback(f"{CB_PERM_BYPASS}{token}")
    await bot_module.callback_handler(update, context)
    perm.create.assert_not_awaited()
    assert "already has a session" in alert_text(update)
    assert perm.manager.get_window_for_thread(-10011, 42) == "@1"


@pytest.mark.asyncio
async def test_chat_scoped_duplicate_blocks_picker(perm):
    perm.manager.bind_thread(-10011, 42, "@1", "other")
    context = make_context()
    seed_session_picker(context, perm.workdir)
    update = make_callback(CB_SESSION_NEW)
    await bot_module.callback_handler(update, context)
    assert "already has a session" in alert_text(update)
    assert PERM_PENDING_KEY not in topic(context)


def test_permission_callback_data_within_64_bytes(tmp_path):
    _, markup = build_permission_picker(str(tmp_path), "ffffffff", "x" * 200)
    for row in markup.inline_keyboard:
        for button in row:
            assert len(str(button.callback_data).encode()) <= 64
            assert "@" not in str(button.callback_data)


@pytest.mark.asyncio
async def test_bypass_warning_holds_pending_text(perm):
    perm.manager.wait_for_session_map_entry = AsyncMock(return_value=False)
    bot_module.tmux_manager.capture_pane.return_value = BYPASS_WARNING_PANE
    context = make_context()
    token = await open_picker(perm, context)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_BYPASS}{token}"), context
    )
    perm.manager.send_to_window.assert_not_awaited()
    assert perm.manager.get_window_for_thread(-10011, 42) == "@5"
    assert "_pending_thread_text" not in topic(context)
    sent = bot_module.safe_send.call_args.args[2]
    assert "accept Bypass Permissions mode" in sent


def test_bypass_flag_constant():
    assert BYPASS_PERMISSIONS_FLAG == "--dangerously-skip-permissions"


@pytest.mark.asyncio
async def test_topic_cleanup_clears_permission_picker_of_that_topic_only():
    from ccbot.handlers.cleanup import clear_topic_state

    data = {
        STATE_KEY: STATE_SELECTING_PERMISSION,
        PERM_PENDING_KEY: {"thread_id": 42, "token": "deadbeef"},
        "_pending_thread_id": 42,
        "_pending_thread_text": "hello",
    }
    await clear_topic_state(111, 43, None, data)
    assert data[STATE_KEY] == STATE_SELECTING_PERMISSION
    await clear_topic_state(111, 42, None, data)
    assert data == {}


# ── Review fixes: double taps, foreign users, fail-closed Bypass guard ──


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_double_tap_session_new_does_not_swap_directory(perm, monkeypatch, flag):
    monkeypatch.setattr(config, "permission_choice", flag)
    monkeypatch.setattr(config, "chat_scoped_topics", False)  # no duplicate guard
    context = make_context()
    seed_session_picker(context, perm.workdir)
    await bot_module.callback_handler(make_callback(CB_SESSION_NEW), context)
    if flag:
        pending = dict(topic(context)[PERM_PENDING_KEY])
    second = make_callback(CB_SESSION_NEW)
    await bot_module.callback_handler(second, context)
    assert "expired" in alert_text(second)
    assert perm.create.await_count == (0 if flag else 1)
    if flag:
        assert topic(context)[PERM_PENDING_KEY] == pending
    else:
        assert perm.create.call_args.args == (perm.workdir,)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_double_tap_dir_confirm_does_not_swap_directory(perm, monkeypatch, flag):
    monkeypatch.setattr(config, "permission_choice", flag)
    monkeypatch.setattr(config, "chat_scoped_topics", False)
    context = make_context()
    seed_browser(context, perm.workdir)
    await bot_module.callback_handler(make_callback(CB_DIR_CONFIRM), context)
    second = make_callback(CB_DIR_CONFIRM)
    await bot_module.callback_handler(second, context)
    assert "expired" in alert_text(second)
    assert perm.create.await_count == (0 if flag else 1)
    if flag:
        assert topic(context)[PERM_PENDING_KEY]["path"] == perm.workdir


@pytest.mark.asyncio
async def test_double_tap_session_select_is_expired(perm, monkeypatch):
    monkeypatch.setattr(config, "permission_choice", False)
    monkeypatch.setattr(config, "chat_scoped_topics", False)
    context = make_context()
    sessions = [ClaudeSession(SID, "Fix the login bug", 3, "/nonexistent")]
    seed_session_picker(context, perm.workdir, sessions)
    await bot_module.callback_handler(make_callback(f"{CB_SESSION_SELECT}0"), context)
    second = make_callback(f"{CB_SESSION_SELECT}0")
    await bot_module.callback_handler(second, context)
    assert "expired" in alert_text(second)
    perm.create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize(
    "data", [CB_DIR_CONFIRM, CB_SESSION_NEW, f"{CB_SESSION_SELECT}0"]
)
async def test_foreign_user_cannot_drive_setup_pickers(perm, monkeypatch, flag, data):
    monkeypatch.setattr(config, "permission_choice", flag)
    context = make_context()
    if data.startswith("db:"):
        seed_browser(context, perm.workdir)
        state = STATE_BROWSING_DIRECTORY
    else:
        sessions = [ClaudeSession(SID, "Fix the login bug", 3, "/nonexistent")]
        seed_session_picker(context, perm.workdir, sessions)
        state = STATE_SELECTING_SESSION
    other = make_context(context.chat_data)
    update = make_callback(data, user_id=222)
    await bot_module.callback_handler(update, other)
    assert "Only the person who started" in alert_text(update)
    perm.create.assert_not_awaited()
    assert topic(context)[STATE_KEY] == state
    assert topic(context)["_pending_thread_text"] == "hello"


@pytest.mark.asyncio
async def test_text_handler_records_initiating_user(perm):
    context = make_context()
    await bot_module.text_handler(make_message_update(text="start"), context)
    assert topic(context)[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert topic(context)["_pending_user_id"] == 111


async def choose_bypass(perm, context):
    token = await open_picker(perm, context)
    await bot_module.callback_handler(
        make_callback(f"{CB_PERM_BYPASS}{token}"), context
    )


@pytest.mark.asyncio
async def test_bypass_holds_text_when_pane_unreadable(perm):
    bot_module.tmux_manager.capture_pane.return_value = None
    context = make_context()
    await choose_bypass(perm, context)
    perm.manager.send_to_window.assert_not_awaited()
    assert perm.manager.get_window_for_thread(-10011, 42) == "@5"
    assert "Resend it once Claude is ready" in bot_module.safe_send.call_args.args[2]


@pytest.mark.asyncio
async def test_bypass_holds_text_after_hook_timeout_with_empty_pane(perm):
    perm.manager.wait_for_session_map_entry = AsyncMock(return_value=False)
    context = make_context()
    await choose_bypass(perm, context)
    perm.manager.send_to_window.assert_not_awaited()
    assert "_pending_thread_text" not in topic(context)
    assert "Resend it once Claude is ready" in bot_module.safe_send.call_args.args[2]


@pytest.mark.asyncio
async def test_bypass_warning_held_even_when_hook_confirmed(perm):
    bot_module.tmux_manager.capture_pane.return_value = BYPASS_WARNING_PANE
    context = make_context()
    await choose_bypass(perm, context)
    perm.manager.send_to_window.assert_not_awaited()
    assert "accept Bypass Permissions mode" in bot_module.safe_send.call_args.args[2]


@pytest.mark.asyncio
async def test_bypass_forwards_text_when_hook_confirmed_and_pane_clear(perm):
    context = make_context()
    await choose_bypass(perm, context)
    perm.manager.send_to_window.assert_awaited_once_with("@5", "hello")


@pytest.mark.asyncio
async def test_held_text_notice_failure_is_logged(perm, caplog):
    bot_module.tmux_manager.capture_pane.return_value = BYPASS_WARNING_PANE
    bot_module.safe_send.return_value = None
    context = make_context()
    with caplog.at_level("WARNING", logger="ccbot.bot"):
        await choose_bypass(perm, context)
    assert "first message was held" in caplog.text


@pytest.mark.asyncio
async def test_message_during_bypass_warning_is_not_forwarded(perm, monkeypatch):
    perm.manager.get_window_state("@5").skip_permissions = True
    perm.manager.bind_thread(-10011, 42, "@5", "p")
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@5")),
    )
    bot_module.tmux_manager.capture_pane.return_value = BYPASS_WARNING_PANE
    show_ui = AsyncMock()
    monkeypatch.setattr(bot_module, "handle_interactive_ui", show_ui)
    await bot_module.text_handler(make_message_update(text="second"), make_context())
    perm.manager.send_to_window.assert_not_awaited()
    show_ui.assert_awaited_once()
    assert "accept Bypass Permissions mode" in bot_module.safe_reply.call_args.args[1]


@pytest.mark.asyncio
async def test_window_picker_bind_resets_stale_bypass(perm, monkeypatch):
    perm.manager.window_states["@7"] = WindowState(
        SID, perm.workdir, "old", skip_permissions=True
    )
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(
            return_value=SimpleNamespace(
                window_id="@7", window_name="old", recovery_token=""
            )
        ),
    )
    context = make_context()
    data = topic(context)
    data[STATE_KEY] = STATE_SELECTING_WINDOW
    data[UNBOUND_WINDOWS_KEY] = ["@7"]
    data["_pending_thread_id"] = 42
    await bot_module.callback_handler(make_callback(f"{CB_WIN_BIND}0"), context)
    assert perm.manager.get_window_for_thread(-10011, 42) == "@7"
    assert perm.manager.window_states["@7"].skip_permissions is False
    saved = json.loads(config.state_file.read_text())
    assert "skip_permissions" not in saved["window_states"]["@7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [f"{CB_WIN_BIND}0", "wb:new"])
async def test_foreign_user_cannot_drive_window_picker(perm, data):
    perm.manager.window_states["@7"] = WindowState(SID, perm.workdir, "old")
    context = make_context()
    picker = topic(context)
    picker[STATE_KEY] = STATE_SELECTING_WINDOW
    picker[UNBOUND_WINDOWS_KEY] = ["@7"]
    picker["_pending_thread_id"] = 42
    picker["_pending_thread_text"] = "hello"
    picker["_pending_user_id"] = 111
    update = make_callback(data, user_id=222)
    await bot_module.callback_handler(update, make_context(context.chat_data))
    assert "Only the person who started" in alert_text(update)
    assert perm.manager.get_window_for_thread(-10011, 42) is None
    assert picker[STATE_KEY] == STATE_SELECTING_WINDOW
    assert picker["_pending_thread_text"] == "hello"


# ── Re-review fixes: anchored warning, gating, cancel by anyone ──

QUOTED_PHRASES_PANE = (
    "● Starting Claude Code running in Bypass Permissions mode shows a dialog\n"
    "  where you pick 2. Yes, I accept to continue.\n"
    "  The dialog says Claude Code running in Bypass Permissions mode, then\n"
    "  2. Yes, I accept\n"
    "  appears near the bottom.\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
)


@pytest.fixture
def bound_window(perm, monkeypatch):
    perm.manager.bind_thread(-10011, 42, "@5", "p")
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@5")),
    )
    show_ui = AsyncMock()
    monkeypatch.setattr(bot_module, "handle_interactive_ui", show_ui)
    return show_ui


@pytest.mark.asyncio
async def test_flag_off_pane_with_warning_phrases_sends_text(
    perm, monkeypatch, bound_window
):
    monkeypatch.setattr(config, "permission_choice", False)
    perm.manager.get_window_state("@5").skip_permissions = True
    bot_module.tmux_manager.capture_pane.return_value = BYPASS_WARNING_PANE
    await bot_module.text_handler(make_message_update(text="go"), make_context())
    perm.manager.send_to_window.assert_awaited_once_with("@5", "go")


@pytest.mark.asyncio
@pytest.mark.parametrize("pane", [BYPASS_WARNING_PANE, QUOTED_PHRASES_PANE])
async def test_non_bypass_window_is_never_refused(perm, bound_window, pane):
    bot_module.tmux_manager.capture_pane.return_value = pane
    await bot_module.text_handler(make_message_update(text="go"), make_context())
    perm.manager.send_to_window.assert_awaited_once_with("@5", "go")


@pytest.mark.asyncio
async def test_quoted_phrases_in_bypass_window_are_not_refused(perm, bound_window):
    perm.manager.get_window_state("@5").skip_permissions = True
    bot_module.tmux_manager.capture_pane.return_value = QUOTED_PHRASES_PANE
    await bot_module.text_handler(make_message_update(text="go"), make_context())
    perm.manager.send_to_window.assert_awaited_once_with("@5", "go")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recorded", "flag"), [(False, True), (True, True), (True, False)]
)
async def test_interactive_ui_shows_warning_only_for_bypass_windows(
    perm, monkeypatch, recorded, flag
):
    import ccbot.handlers.interactive_ui as ui

    monkeypatch.setattr(config, "permission_choice", flag)
    perm.manager.get_window_state("@5").skip_permissions = recorded
    monkeypatch.setattr(ui, "session_manager", perm.manager)
    fake_tmux = SimpleNamespace(
        find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5")),
        capture_pane=AsyncMock(return_value=BYPASS_WARNING_PANE),
    )
    monkeypatch.setattr(ui, "tmux_manager", fake_tmux)
    bot = AsyncMock()
    shown = await ui.handle_interactive_ui(bot, -10011, "@5", 42)
    expected = recorded and flag
    assert shown is expected
    assert bot.send_message.await_count == (1 if expected else 0)
    ui._interactive_msgs.clear()
    ui._interactive_mode.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [CB_DIR_CANCEL, "rs:cancel", "wb:cancel"])
async def test_non_initiator_can_cancel_setup(perm, data):
    context = make_context()
    if data.startswith("db:"):
        seed_browser(context, perm.workdir)
    elif data.startswith("rs:"):
        seed_session_picker(context, perm.workdir)
    else:
        picker = topic(context)
        picker[STATE_KEY] = STATE_SELECTING_WINDOW
        picker[UNBOUND_WINDOWS_KEY] = ["@7"]
        picker["_pending_thread_id"] = 42
        picker["_pending_thread_text"] = "hello"
        picker["_pending_user_id"] = 111
    update = make_callback(data, user_id=222)
    await bot_module.callback_handler(update, make_context(context.chat_data))
    assert bot_module.safe_edit.call_args.args[1] == "Cancelled"
    assert STATE_KEY not in topic(context)
    assert "_pending_thread_text" not in topic(context)
    perm.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_initiator_can_cancel_permission_step(perm):
    context = make_context()
    token = await open_picker(perm, context)
    update = make_callback(f"{CB_PERM_CANCEL}{token}", user_id=222)
    await bot_module.callback_handler(update, make_context(context.chat_data))
    assert bot_module.safe_edit.call_args.args[1] == "Cancelled"
    assert PERM_PENDING_KEY not in topic(context)
    perm.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["browser", "session", "permission"])
async def test_non_initiator_text_replaces_stale_setup(perm, step):
    context = make_context()
    if step == "browser":
        seed_browser(context, perm.workdir)
    elif step == "session":
        seed_session_picker(context, perm.workdir)
    else:
        await open_picker(perm, context)
    other = make_context(context.chat_data)
    await bot_module.text_handler(make_message_update(user_id=222, text="mine"), other)
    data = topic(context)
    assert PERM_PENDING_KEY not in data and "_selected_path" not in data
    # A fresh directory browser now belongs to the second user.
    assert data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert data["_pending_user_id"] == 222
    assert data["_pending_thread_text"] == "mine"


@pytest.mark.asyncio
async def test_initiator_text_still_prompts_to_use_picker(perm):
    context = make_context()
    seed_browser(context, perm.workdir)
    await bot_module.text_handler(make_message_update(text="again"), context)
    assert "directory browser" in bot_module.safe_reply.call_args.args[1]
    assert topic(context)["_pending_thread_text"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", [CB_DIR_CONFIRM, CB_SESSION_NEW, f"{CB_SESSION_SELECT}0"]
)
async def test_missing_initiator_after_restart_answers_expired(perm, data):
    # After a restart picker state is gone, including _pending_user_id.
    context = make_context()
    update = make_callback(data, user_id=222)
    await bot_module.callback_handler(update, context)
    assert "expired" in alert_text(update)
    assert "Only the person" not in alert_text(update)
    perm.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_other_users_text_during_creation_stays_out_of_new_window(
    perm, monkeypatch, flag
):
    import asyncio

    monkeypatch.setattr(config, "permission_choice", flag)
    started, release = asyncio.Event(), asyncio.Event()

    async def gated_wait(wid, timeout):
        started.set()
        await release.wait()
        return True

    perm.manager.wait_for_session_map_entry = AsyncMock(side_effect=gated_wait)
    context = make_context()
    if flag:
        token = await open_picker(perm, context)
        data = f"{CB_PERM_NORMAL}{token}"
    else:
        seed_session_picker(context, perm.workdir)
        data = CB_SESSION_NEW
    task = asyncio.create_task(
        bot_module.callback_handler(make_callback(data), context)
    )
    await asyncio.wait_for(started.wait(), 1)
    # B writes while A's window is starting and the topic is not bound yet.
    other = make_context(context.chat_data)
    await bot_module.text_handler(
        make_message_update(user_id=222, text="B's task"), other
    )
    release.set()
    await task
    assert perm.manager.send_to_window.await_args_list == [
        ((("@5", "hello")),),
    ]
    picker = topic(context)
    assert picker[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert picker["_pending_user_id"] == 222
    assert picker["_pending_thread_text"] == "B's task"
    assert picker["_pending_thread_id"] == 42
