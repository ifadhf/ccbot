"""Exercise reboot recovery, crash checkpoints, and failed-topic isolation."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ccbot.recovery as recovery
import ccbot.session as session_module
from ccbot.config import config
from ccbot.monitor_state import TrackedSession
from ccbot.session import SessionManager, WindowState
from ccbot.session_monitor import SessionMonitor
from ccbot.tmux_manager import TmuxWindow
from ccbot.utils import atomic_write_json

OLD_INSTANCE = "11111111-1111-1111-1111-111111111111"
NEW_INSTANCE = "22222222-2222-2222-2222-222222222222"
SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_SID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "projects")
    monkeypatch.setattr(config, "chat_scoped_topics", True)
    monkeypatch.setattr(config, "topic_owner_lock", True)
    monkeypatch.setattr(config, "allowed_users", {111, 222})
    monkeypatch.setattr(config, "tmux_session_name", "ccbot")
    monkeypatch.setattr(recovery, "HOOK_TIMEOUT", 0)
    cwd = tmp_path / "work"
    cwd.mkdir()
    manager = SessionManager()
    manager.tmux_session_identity = OLD_INSTANCE
    windows = {}
    launches = []
    modes = {}

    def add_topic(route=-10011, topic=42, wid="@8", sid=SID, name="Original"):
        state = WindowState(sid, str(cwd), name)
        manager.window_states[wid] = state
        manager.bind_thread(route, topic, wid, name)
        manager.user_window_offsets.setdefault(route, {})[wid] = 1234
        manager.set_group_chat_id(route, topic, route)
        manager.claim_topic(route, topic, 111, "owner", "Owner")["announced"] = True
        manager._save_state()
        transcript = manager._build_session_file_path(sid, str(cwd))
        assert transcript is not None
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"sessionId": sid, "cwd": str(cwd)}) + "\n")
        return transcript

    def write_hook(wid, sid, token, identity=NEW_INSTANCE, directory=None):
        data = recovery._read_map()
        data[f"ccbot:{wid}"] = {
            "session_id": sid,
            "cwd": directory or str(cwd),
            "window_name": windows[wid].window_name,
            "tmux_session_identity": identity,
            "recovery_token": token,
        }
        atomic_write_json(config.session_map_file, data)

    async def create(
        directory, window_name=None, resume_session_id=None, recovery_token=None
    ):
        # Verify that an actual durable checkpoint precedes process creation.
        saved = json.loads(config.state_file.read_text())
        assert any(
            r["token"] == recovery_token for r in saved["pending_recoveries"].values()
        )
        assert directory == str(cwd)
        mode = modes.get(resume_session_id, "ready")
        if mode == "launch_failure":
            return False, "failure", "", ""
        wid = f"@{len(launches) + 1}"
        launches.append((directory, resume_session_id, recovery_token, wid))
        windows[wid] = TmuxWindow(
            wid, window_name or "Restored", directory, "claude", recovery_token
        )
        if mode == "crash":
            raise KeyboardInterrupt("Simulated crash after tmux creation")
        if mode == "exit":
            del windows[wid]
        elif mode not in {"timeout", "stale_hook"}:
            write_hook(
                wid,
                OTHER_SID if mode == "wrong_session" else resume_session_id,
                recovery_token,
                directory="/different" if mode == "wrong_directory" else None,
            )
        if mode == "stale_hook":
            write_hook(wid, resume_session_id, recovery_token, identity=OLD_INSTANCE)
        return True, "created", window_name, wid

    tmux = SimpleNamespace(
        session_identity=AsyncMock(return_value=(NEW_INSTANCE, time.time() + 60)),
        list_windows=AsyncMock(side_effect=lambda: list(windows.values())),
        find_window_by_id=AsyncMock(side_effect=windows.get),
        create_window=AsyncMock(side_effect=create),
        kill_window=AsyncMock(),
    )
    monkeypatch.setattr(recovery, "tmux_manager", tmux)
    monkeypatch.setattr(session_module, "tmux_manager", tmux)
    return SimpleNamespace(
        manager=manager,
        windows=windows,
        launches=launches,
        modes=modes,
        add_topic=add_topic,
        write_hook=write_hook,
        tmux=tmux,
        cwd=cwd,
        tmp=tmp_path,
    )


@pytest.mark.asyncio
async def test_cold_boot_resumes_original_topics_and_preserves_owners_offsets(env):
    env.add_topic(wid="@8")
    env.add_topic(route=-10022, topic=42, wid="@1", sid=OTHER_SID, name="Other")
    owners = dict(env.manager.topic_owners)
    await recovery.recover_sessions(env.manager)
    # New IDs collide with old IDs, but records cannot cross between topics.
    assert env.manager.get_window_for_thread(-10011, 42) == "@1"
    assert env.manager.get_window_for_thread(-10022, 42) == "@2"
    assert env.manager.window_states["@1"].session_id == SID
    assert env.manager.window_states["@2"].session_id == OTHER_SID
    assert env.manager.user_window_offsets == {
        -10011: {"@1": 1234},
        -10022: {"@2": 1234},
    }
    assert env.manager.topic_owners == owners
    assert not env.manager.pending_recoveries
    assert SessionManager().thread_bindings == env.manager.thread_bindings
    assert {p.name for p in env.tmp.glob("*.json")} == {
        "state.json",
        "session_map.json",
    }


@pytest.mark.asyncio
async def test_bot_restart_reuses_surviving_windows(env):
    env.add_topic()
    env.manager.tmux_session_identity = NEW_INSTANCE
    env.windows["@8"] = TmuxWindow("@8", "Original", str(env.cwd), "claude")
    await recovery.recover_sessions(env.manager)
    assert env.manager.get_window_for_thread(-10011, 42) == "@8"
    env.tmux.create_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_startup_is_idempotent(env):
    env.add_topic()
    await recovery.recover_sessions(env.manager)
    reloaded = SessionManager()
    await recovery.recover_sessions(reloaded)
    assert reloaded.get_window_for_thread(-10011, 42) == "@1"
    assert len(env.launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("survives", [False, True])
async def test_legacy_state_migrates_without_a_second_state_file(env, survives):
    env.add_topic()
    saved = json.loads(config.state_file.read_text())
    saved.pop("tmux_session_identity")
    saved.pop("pending_recoveries")
    atomic_write_json(config.state_file, saved)
    manager = SessionManager()
    if survives:
        env.tmux.session_identity.return_value = (
            NEW_INSTANCE,
            manager.loaded_state_mtime - 60,
        )
        env.windows["@8"] = TmuxWindow("@8", "Original", str(env.cwd), "claude")
    await recovery.recover_sessions(manager)
    assert bool(env.launches) is not survives
    assert not manager.pending_recoveries
    assert manager.tmux_session_identity == NEW_INSTANCE


@pytest.mark.asyncio
async def test_new_server_never_reuses_old_window_ids_or_names(env):
    env.add_topic()
    env.windows["@8"] = TmuxWindow("@8", "Original", str(env.cwd), "claude")
    await recovery.recover_sessions(env.manager)
    assert env.manager.get_window_for_thread(-10011, 42) == "@1"
    assert env.windows["@8"].recovery_token == ""
    env.tmux.kill_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_latest_hook_before_power_loss_wins_over_monitor_lag(env):
    env.add_topic(sid=OTHER_SID)
    env.add_topic()
    atomic_write_json(
        config.session_map_file,
        {
            "ccbot:@8": {
                "session_id": OTHER_SID,
                "cwd": str(env.cwd),
                "window_name": "Original",
                "tmux_session_identity": OLD_INSTANCE,
            }
        },
    )
    await recovery.recover_sessions(env.manager)
    assert env.launches[0][1] == OTHER_SID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_history",
        "missing_directory",
        "invalid_id",
        "disallowed_owner",
        "general",
        "launch_failure",
        "timeout",
        "stale_hook",
        "wrong_session",
        "wrong_directory",
        "exit",
    ],
)
async def test_failed_recovery_survives_cleanup_and_reload(env, failure):
    topic = 1 if failure == "general" else 42
    transcript = env.add_topic(topic=topic)
    if failure == "missing_history":
        transcript.unlink()
    elif failure == "missing_directory":
        env.cwd.rmdir()
    elif failure == "invalid_id":
        env.manager.window_states["@8"].session_id = "$(touch should-not-exist)"
    elif failure == "disallowed_owner":
        env.manager.topic_owners[f"-10011:{topic}"]["user_id"] = 999
    else:
        env.modes[SID] = failure
    await recovery.recover_sessions(env.manager)
    await env.manager.resolve_stale_ids()
    await env.manager.load_session_map()
    manager = SessionManager()
    record = manager.pending_recoveries[f"-10011:{topic}"]
    assert record["error"]
    assert record["window_state"]["cwd"] == str(env.cwd)
    assert manager.get_window_for_thread(-10011, topic) is None
    assert manager.topic_owners[f"-10011:{topic}"]["username"] == "owner"
    with pytest.raises(ValueError, match="Recover"):
        manager.bind_thread(-10011, topic, "@99")


@pytest.mark.asyncio
async def test_one_failed_topic_does_not_block_healthy_topics(env):
    env.add_topic()
    env.add_topic(topic=43, wid="@9", sid=OTHER_SID)
    env.modes[SID] = "timeout"
    await recovery.recover_sessions(env.manager)
    assert env.manager.get_window_for_thread(-10011, 43) == "@2"
    assert "-10011:42" in env.manager.pending_recoveries


@pytest.mark.asyncio
async def test_delayed_hook_retry_reuses_candidate_even_after_bot_restart(env):
    env.add_topic()
    env.modes[SID] = "timeout"
    await recovery.recover_sessions(env.manager)
    token = env.manager.pending_recoveries["-10011:42"]["token"]
    env.write_hook("@1", SID, token)
    manager = SessionManager()
    assert await recovery.recover_topic(manager, -10011, 42)
    assert len(env.launches) == 1
    assert manager.window_states["@1"].session_id == SID


@pytest.mark.asyncio
async def test_crash_after_launch_before_binding_does_not_duplicate(env):
    env.add_topic()
    env.modes[SID] = "crash"
    with pytest.raises(KeyboardInterrupt, match="Simulated"):
        await recovery.recover_sessions(env.manager)
    manager = SessionManager()
    token = manager.pending_recoveries["-10011:42"]["token"]
    env.write_hook("@1", SID, token)
    await recovery.recover_sessions(manager)
    assert manager.get_window_for_thread(-10011, 42) == "@1"
    assert len(env.launches) == 1


@pytest.mark.asyncio
async def test_concurrent_retries_do_not_duplicate(env):
    env.add_topic()
    env.manager.stage_recovery(-10011, 42)
    results = await asyncio.gather(
        *(recovery.recover_topic(env.manager, -10011, 42) for _ in range(3))
    )
    assert results == [True, True, True]
    assert len(env.launches) == 1


@pytest.mark.asyncio
async def test_duplicate_conversation_is_not_attached_to_two_topics(env):
    env.add_topic()
    env.add_topic(topic=43, wid="@9", sid=SID)
    await recovery.recover_sessions(env.manager)
    assert len(env.launches) == 1
    assert "already bound" in env.manager.pending_recoveries["-10011:43"]["error"]


@pytest.mark.asyncio
async def test_duplicate_pending_conversation_does_not_start_two_processes(env):
    env.add_topic()
    env.add_topic(topic=43, wid="@9", sid=SID)
    env.modes[SID] = "timeout"
    await recovery.recover_sessions(env.manager)
    assert len(env.launches) == 1
    assert "already recovering" in env.manager.pending_recoveries["-10011:43"]["error"]


@pytest.mark.asyncio
async def test_hook_from_reused_id_cannot_overwrite_lost_conversation(env, monkeypatch):
    env.add_topic()
    env.windows["@8"] = TmuxWindow("@8", "Unrelated", str(env.cwd), "claude")
    env.write_hook("@8", OTHER_SID, "")
    await env.manager.load_session_map()
    assert env.manager.window_states["@8"].session_id == SID
    monkeypatch.setattr(session_module, "session_manager", env.manager)
    monitor = SessionMonitor(state_file=env.tmp / "monitor_state.json")
    assert await monitor._load_current_session_map() == {}
    await recovery.prepare_recovery(env.manager)
    assert (
        env.manager.pending_recoveries["-10011:42"]["window_state"]["session_id"] == SID
    )


@pytest.mark.asyncio
async def test_monitor_does_not_consume_unread_output_while_recovery_pending(
    env, monkeypatch
):
    transcript = env.add_topic()
    env.modes[SID] = "timeout"
    await recovery.recover_sessions(env.manager)
    token = env.manager.pending_recoveries["-10011:42"]["token"]
    env.write_hook("@1", SID, token)
    monkeypatch.setattr(session_module, "session_manager", env.manager)
    monitor = SessionMonitor(poll_interval=0, state_file=env.tmp / "monitor_state.json")
    monitor.state.update_session(TrackedSession(SID, str(transcript), 123))
    observed = []

    async def one_cycle(active_ids):
        observed.append(active_ids)
        monitor._running = False
        return []

    monkeypatch.setattr(monitor, "check_for_updates", AsyncMock(side_effect=one_cycle))
    monitor._running = True
    await monitor._monitor_loop()
    assert len(observed) == 1 and SID not in observed[0]
    assert monitor.state.get_session(SID).last_byte_offset == 123
    assert await recovery.recover_topic(env.manager, -10011, 42)
    assert SID in (await monitor._load_current_session_map()).values()


@pytest.mark.asyncio
async def test_status_poll_detects_reset_before_using_recycled_window(env, monkeypatch):
    import ccbot.handlers.status_polling as polling

    env.add_topic()
    env.windows["@8"] = TmuxWindow("@8", "Unrelated", str(env.cwd), "claude")
    monkeypatch.setattr(polling, "session_manager", env.manager)
    monkeypatch.setattr(polling, "tmux_manager", env.tmux)
    update = AsyncMock()
    monkeypatch.setattr(polling, "update_status_message", update)

    async def stop_after_cycle(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(polling.asyncio, "sleep", stop_after_cycle)
    bot = SimpleNamespace(unpin_all_forum_topic_messages=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await polling.status_poll_loop(bot)
    assert "-10011:42" in env.manager.pending_recoveries
    bot.unpin_all_forum_topic_messages.assert_not_awaited()
    update.assert_not_awaited()
    env.tmux.kill_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_retains_offsets_for_pending_conversations(env, monkeypatch):
    transcript = env.add_topic()
    env.manager.stage_recovery(-10011, 42)
    monkeypatch.setattr(session_module, "session_manager", env.manager)
    monitor = SessionMonitor(
        projects_path=config.claude_projects_path,
        state_file=env.tmp / "monitor_state.json",
    )
    monitor.state.update_session(TrackedSession(SID, str(transcript), 123))
    monitor._last_session_map = {"ccbot:@8": SID}
    await monitor._cleanup_all_stale_sessions()
    await monitor._detect_and_cleanup_changes()
    assert monitor.state.get_session(SID).last_byte_offset == 123


@pytest.mark.asyncio
async def test_missing_hook_does_not_erase_bound_window_state(env):
    env.add_topic()
    atomic_write_json(config.session_map_file, {})
    await env.manager.load_session_map()
    assert env.manager.window_states["@8"].session_id == SID


def test_explicit_unbind_cancels_recovery_but_preserves_owner(env):
    env.add_topic()
    env.manager.stage_recovery(-10011, 42)
    env.manager.unbind_thread(-10011, 42)
    assert not SessionManager().pending_recoveries
    assert env.manager.topic_owners["-10011:42"]["user_id"] == 111


@pytest.mark.asyncio
async def test_explicitly_unbound_history_does_not_respawn_on_startup(env):
    transcript = env.add_topic()
    env.manager.unbind_thread(-10011, 42)
    original = transcript.read_bytes()
    assert env.manager.window_states["@8"].session_id == SID
    await recovery.recover_sessions(env.manager)
    await env.manager.resolve_stale_ids()
    reloaded = SessionManager()
    assert not reloaded.thread_bindings
    assert not reloaded.pending_recoveries
    assert reloaded.topic_owners["-10011:42"]["user_id"] == 111
    assert transcript.read_bytes() == original
    env.tmux.create_window.assert_not_awaited()


def test_corrupt_state_is_not_silently_replaced(env):
    config.state_file.write_text("{unfinished")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        SessionManager()
    assert config.state_file.read_text() == "{unfinished"


@pytest.mark.asyncio
async def test_tmux_loss_during_runtime_preserves_all_topics_before_new_windows(env):
    env.add_topic(wid="@8")
    env.add_topic(topic=43, wid="@1", sid=OTHER_SID)
    await recovery.prepare_recovery(env.manager)
    assert not env.manager.thread_bindings
    assert set(env.manager.pending_recoveries) == {"-10011:42", "-10011:43"}
    assert env.launches == []
    env.manager.bind_thread(-10022, 44, "@1", "Unrelated new topic")
    assert (
        env.manager.pending_recoveries["-10011:43"]["window_state"]["session_id"]
        == OTHER_SID
    )


@pytest.mark.asyncio
async def test_retry_after_server_loss_cannot_take_another_topics_stale_id(env):
    env.add_topic(wid="@8")
    env.add_topic(topic=43, wid="@1", sid=OTHER_SID)
    env.manager.stage_recovery(-10011, 42)
    assert await recovery.recover_topic(env.manager, -10011, 42)
    assert env.manager.get_window_for_thread(-10011, 42) == "@1"
    assert env.manager.get_window_for_thread(-10011, 43) is None
    assert (
        env.manager.pending_recoveries["-10011:43"]["window_state"]["session_id"]
        == OTHER_SID
    )


@pytest.mark.asyncio
async def test_checkpoint_failure_prevents_launch(env, monkeypatch):
    env.add_topic()
    original = config.state_file.read_bytes()

    def fail():
        raise OSError("Disk is full")

    monkeypatch.setattr(env.manager, "_save_state", fail)
    with pytest.raises(OSError, match="Disk is full"):
        await recovery.recover_sessions(env.manager)
    assert config.state_file.read_bytes() == original
    assert not env.launches


@pytest.mark.asyncio
async def test_cancel_kills_only_this_topics_recovery_candidate(env):
    env.add_topic()
    env.modes[SID] = "timeout"
    await recovery.recover_sessions(env.manager)
    env.windows["@99"] = TmuxWindow("@99", "Unrelated", str(env.cwd), "claude")
    env.tmux.kill_window.return_value = True
    assert await recovery.cancel_recovery(env.manager, -10011, 42, kill=True)
    env.tmux.kill_window.assert_awaited_once_with("@1")
    assert not SessionManager().pending_recoveries
