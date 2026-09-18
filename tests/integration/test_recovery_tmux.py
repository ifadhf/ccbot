"""Simulate reboot recovery using an isolated tmux server and a fake Claude CLI.

No production tmux socket, credentials, Telegram transport, or Claude process
is used. The fake process exercises the actual SessionStart hook and launch.
"""

import json
from pathlib import Path
import shlex
import shutil
import sys
import uuid

import libtmux
import pytest

import ccbot.recovery as recovery
from ccbot.config import config
from ccbot.session import SessionManager, WindowState
from ccbot.tmux_manager import TmuxManager


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")
async def test_recovery_survives_real_tmux_server_loss(monkeypatch, tmp_path):
    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    source = Path(__file__).resolve().parents[2] / "src"
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "projects")
    monkeypatch.setattr(config, "chat_scoped_topics", True)
    monkeypatch.setattr(config, "topic_owner_lock", True)
    monkeypatch.setattr(config, "allowed_users", {111})
    monkeypatch.setattr(recovery, "HOOK_TIMEOUT", 10)
    workspace = tmp_path / "work"
    workspace.mkdir()
    fake = tmp_path / "fake-claude.py"
    fake.write_text(
        "import io, json, os, sys, time\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "from ccbot.hook import hook_main\n"
        "args = sys.argv[1:]\n"
        "assert len(args) == 2 and args[0] == '--resume'\n"
        f"with open({str(tmp_path / 'launches.jsonl')!r}, 'a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "sys.stdin = io.StringIO(json.dumps({'hook_event_name': 'SessionStart', "
        "'source': 'resume', 'session_id': args[1], 'cwd': os.getcwd()}))\n"
        "sys.argv = ['ccbot', 'hook']\n"
        "hook_main()\n"
        "time.sleep(120)\n"
    )
    monkeypatch.setattr(
        config, "claude_command", shlex.join([sys.executable, str(fake)])
    )
    socket_name = "ccbot-recovery-test-" + uuid.uuid4().hex
    server = libtmux.Server(socket_name=socket_name, config_file="/dev/null")
    tmux = TmuxManager(config.tmux_session_name)
    tmux._server = server
    monkeypatch.setattr(recovery, "tmux_manager", tmux)
    manager = SessionManager()
    manager.tmux_session_identity = "old-instance"
    manager.window_states["@8"] = WindowState(sid, str(workspace), "Saved topic")
    manager.bind_thread(-10011, 42, "@8", "Saved topic")
    manager.claim_topic(-10011, 42, 111, "owner", "Owner")
    transcript = manager._build_session_file_path(sid, str(workspace))
    assert transcript is not None
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"sessionId": sid, "cwd": str(workspace)}) + "\n")
    try:
        await recovery.recover_sessions(manager)
        assert not manager.pending_recoveries, manager.pending_recoveries
        wid = manager.get_window_for_thread(-10011, 42)
        assert wid and manager.window_states[wid].session_id == sid
        identity = manager.tmux_session_identity

        # A bot-only restart must keep the existing process.
        await recovery.recover_sessions(SessionManager())
        assert len((tmp_path / "launches.jsonl").read_text().splitlines()) == 1

        # Kill only this unique test socket to simulate a VM reboot.
        server.kill()
        reloaded = SessionManager()
        await recovery.recover_sessions(reloaded)
        assert reloaded.tmux_session_identity != identity
        assert not reloaded.pending_recoveries, reloaded.pending_recoveries
        wid = reloaded.get_window_for_thread(-10011, 42)
        assert wid and reloaded.window_states[wid].session_id == sid
        assert reloaded.topic_owners["-10011:42"]["user_id"] == 111
        assert [
            json.loads(line)
            for line in (tmp_path / "launches.jsonl").read_text().splitlines()
        ] == [["--resume", sid], ["--resume", sid]]
    finally:
        server.kill()
