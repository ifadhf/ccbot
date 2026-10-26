"""Tests for Claude Code session tracking hook."""

import io
import json
import sys

import pytest

import ccbot.hook as hook_module
from ccbot.hook import _UUID_RE, _is_attach_to_job, _is_hook_installed, hook_main


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "ccbot hook", "timeout": 5}
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/ccbot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()


class TestBgSessionWindow:
    """bg sessions run in the claude daemon without TMUX_PANE."""

    @pytest.mark.parametrize(
        "argv, expected",
        [
            (["/home/u/.local/bin/claude", "attach", "1a2b3c4d"], True),
            (["claude", "attach", "1a2b3c4d-0000-4000-8000-000000000001"], True),
            (["claude", "attach", "9f8e7d6c"], False),
            (["claude", "--resume", "1a2b3c4d"], False),
            (["vim", "attach", "1a2b3c4d"], False),
            (["claude", "attach"], False),
        ],
        ids=["short", "full-id", "other-job", "not-attach", "not-claude", "no-arg"],
    )
    def test_is_attach_to_job(self, argv: list[str], expected: bool) -> None:
        assert _is_attach_to_job(argv, "1a2b3c4d") is expected

    def test_bg_job_id_walks_up_to_claude_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        sessions = tmp_path / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "300.json").write_text(
            json.dumps({"sessionId": "x", "kind": "bg", "jobId": "1a2b3c4d"})
        )
        monkeypatch.setattr(hook_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hook_module.os, "getppid", lambda: 100)
        parents = {100: 200, 200: 300, 300: 1}
        monkeypatch.setattr(hook_module, "_parent_pid", lambda pid: parents[pid])
        assert hook_module._bg_job_id() == "1a2b3c4d"

    def test_bg_job_id_none_for_foreground_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        sessions = tmp_path / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "100.json").write_text(json.dumps({"sessionId": "x"}))
        monkeypatch.setattr(hook_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hook_module.os, "getppid", lambda: 100)
        assert hook_module._bg_job_id() is None

    def test_session_start_maps_attached_window(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setattr(
            hook_module,
            "_get_attached_tmux_window",
            lambda: ("ccbot", "@9", "project"),
        )
        targets: list[str] = []

        def metadata(target: str) -> dict[str, str]:
            targets.append(target)
            return {"tmux_session_identity": "11111111-2222-4333-8444-555555555555"}

        monkeypatch.setattr(hook_module, "_get_tmux_recovery_metadata", metadata)
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        payload = {
            "session_id": "22222222-3333-4444-8555-666666666666",
            "cwd": "/home/u/project",
            "hook_event_name": "SessionStart",
            "source": "clear",
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        hook_main()

        session_map = json.loads((tmp_path / "session_map.json").read_text())
        assert session_map["ccbot:@9"] == {
            "session_id": "22222222-3333-4444-8555-666666666666",
            "cwd": "/home/u/project",
            "window_name": "project",
            "tmux_session_identity": "11111111-2222-4333-8444-555555555555",
        }
        assert targets == ["@9"]
