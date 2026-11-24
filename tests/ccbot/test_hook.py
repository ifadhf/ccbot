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


class TestParkedBgSession:
    """Current Claude Code parks the original TUI on the bg job it shows."""

    def _write_session(self, tmp_path, pid: int, **fields) -> None:
        sessions = tmp_path / ".claude" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, **fields}))

    def test_attached_window_resolves_parked_tui(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Pane 500 runs a shell; its child 600 is the TUI parked on job 96396cb7.
        self._write_session(
            tmp_path,
            600,
            sessionId="ea92b85f",
            kind="interactive",
            parkedJobId="96396cb7",
        )
        self._write_session(
            tmp_path, 700, sessionId="96396cb7", kind="bg", jobId="96396cb7"
        )
        monkeypatch.setattr(hook_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hook_module, "_bg_job_id", lambda: "96396cb7")
        monkeypatch.setattr(hook_module, "_proc_matches", lambda pid, start: True)
        monkeypatch.setattr(hook_module, "_parent_pid", {600: 500}.get)

        class Result:
            stdout = "400:ccbot:@15:other\n500:ccbot:@16:android-app\n"

        monkeypatch.setattr(hook_module.subprocess, "run", lambda *a, **k: Result())
        assert hook_module._get_attached_tmux_window() == (
            "ccbot",
            "@16",
            "android-app",
        )

    def test_parked_viewer_skips_other_jobs_and_dead_processes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._write_session(tmp_path, 600, parkedJobId="aaaaaaaa")
        self._write_session(tmp_path, 601, parkedJobId="96396cb7")
        self._write_session(tmp_path, 602, parkedJobId="96396cb7")
        monkeypatch.setattr(hook_module.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hook_module, "_proc_matches", lambda pid, start: pid == 602)
        assert hook_module._parked_viewer_pids("96396cb7") == [602]

    def test_proc_matches_checks_start_time(self) -> None:
        pid = hook_module.os.getpid()
        stat = hook_module.Path(f"/proc/{pid}/stat").read_text()
        start = stat.rsplit(")", 1)[1].split()[19]
        assert hook_module._proc_matches(pid, start)
        assert hook_module._proc_matches(pid, None)
        assert not hook_module._proc_matches(pid, str(int(start) + 1))
        assert not hook_module._proc_matches(2**22 + 12345, None)


def _telegram_http_error(code: int, body: dict, method: str = "sendMessage"):
    import urllib.error

    return urllib.error.HTTPError(
        f"https://api.telegram.org/botSECRET/{method}",
        code,
        "error",
        {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(body).encode()),
    )


class TestNotificationDelivery:
    """Stop/Notification messages survive a flaky Telegram connection."""

    @pytest.fixture
    def sleeps(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []
        monkeypatch.setattr("time.sleep", recorded.append)
        return recorded

    def test_retries_transient_errors_until_sent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, sleeps
    ) -> None:
        import urllib.error
        from unittest.mock import MagicMock

        urlopen = MagicMock(
            side_effect=[
                TimeoutError("The read operation timed out"),
                urllib.error.URLError("Temporary failure in name resolution"),
                MagicMock(),
            ]
        )
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert (
            hook_module._post_with_retry("SECRET", b"x=1", "Stop @1", tmp_path)
            is not None
        )
        assert urlopen.call_count == 3
        assert sleeps == list(hook_module._NOTIFY_RETRY_DELAYS[:2])
        log = (tmp_path / "notify.log").read_text()
        assert "Stop @1: sent" in log
        assert "SECRET" not in log

    def test_gives_up_after_all_attempts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, sleeps
    ) -> None:
        from unittest.mock import MagicMock

        urlopen = MagicMock(side_effect=TimeoutError("timed out"))
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert (
            hook_module._post_with_retry("SECRET", b"x=1", "Stop @1", tmp_path) is None
        )
        assert urlopen.call_count == len(hook_module._NOTIFY_RETRY_DELAYS) + 1
        assert "gave up" in (tmp_path / "notify.log").read_text()

    def test_permanent_rejection_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, sleeps
    ) -> None:
        from unittest.mock import MagicMock

        error = _telegram_http_error(
            400, {"description": "Bad Request: message thread not found"}
        )
        urlopen = MagicMock(side_effect=error)
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert (
            hook_module._post_with_retry("SECRET", b"x=1", "Stop @1", tmp_path) is None
        )
        assert urlopen.call_count == 1
        assert sleeps == []
        log = (tmp_path / "notify.log").read_text()
        assert "message thread not found" in log
        assert "SECRET" not in log

    def test_flood_control_waits_retry_after(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, sleeps
    ) -> None:
        from unittest.mock import MagicMock

        error = _telegram_http_error(
            429, {"description": "Too Many Requests", "parameters": {"retry_after": 7}}
        )
        urlopen = MagicMock(side_effect=[error, MagicMock()])
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert (
            hook_module._post_with_retry("SECRET", b"x=1", "Stop @1", tmp_path)
            is not None
        )
        assert sleeps == [7.0]

    def test_hook_detaches_stop_delivery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        notify = MagicMock()
        monkeypatch.setattr(hook_module, "_notify_telegram", notify)
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        payload = {
            "session_id": "22222222-3333-4444-8555-666666666666",
            "cwd": "/home/u/project",
            "hook_event_name": "Stop",
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        hook_main()
        notify.assert_called_once_with("Stop", payload, detach=True)


class _FakeResponse:
    def __init__(self, result: object) -> None:
        self.status = 200
        self._body = json.dumps({"ok": True, "result": result}).encode()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeTelegram:
    """Stands in for urllib.request.urlopen and records Bot API calls."""

    def __init__(self) -> None:
        import collections
        import threading

        self.calls: list[tuple[str, dict[str, str]]] = []
        self.scripts: dict[str, collections.deque] = collections.defaultdict(
            collections.deque
        )
        self._next_id = 100
        self._lock = threading.Lock()

    def script(self, method: str, *outcomes: object) -> None:
        self.scripts[method].extend(outcomes)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def params(self, method: str) -> list[dict[str, str]]:
        return [params for name, params in self.calls if name == method]

    def __call__(self, request, timeout: float | None = None):
        from urllib.parse import parse_qs

        method = request.full_url.rsplit("/", 1)[1]
        params = {k: v[0] for k, v in parse_qs(request.data.decode()).items() if v}
        with self._lock:
            self.calls.append((method, params))
            outcome = self.scripts[method].popleft() if self.scripts[method] else None
            if outcome is None:
                if method == "sendMessage":
                    outcome = {"message_id": self._next_id}
                    self._next_id += 1
                else:
                    outcome = True
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(outcome)


class TestSingleMention:
    """Only the newest mentioned notification in a topic keeps its mention."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.delenv("CCBOT_NOTIFY_SINGLE_MENTION", raising=False)
        monkeypatch.setattr(hook_module, "_read_bot_token", lambda _: "SECRET")

    @pytest.fixture
    def tg(self, monkeypatch: pytest.MonkeyPatch) -> _FakeTelegram:
        fake = _FakeTelegram()
        monkeypatch.setattr("urllib.request.urlopen", fake)
        return fake

    @staticmethod
    def deliver(
        tmp_path,
        event: str = "Stop",
        chat: int = -10011,
        thread: int = 42,
        owner: bool = True,
    ) -> None:
        state: dict = {}
        if owner:
            state["topic_owners"] = {
                f"{chat}:{thread}": {"user_id": 111, "username": "owner"}
            }
        hook_module._deliver_notification(
            tmp_path, state, event, "@1", (chat, thread, str(chat))
        )

    @staticmethod
    def records(tmp_path) -> dict:
        path = tmp_path / "notify_last.json"
        return json.loads(path.read_text()) if path.exists() else {}

    @staticmethod
    def log(tmp_path) -> str:
        path = tmp_path / "notify.log"
        return path.read_text() if path.exists() else ""

    def test_first_send_stores_message_id(self, tmp_path, tg) -> None:
        self.deliver(tmp_path)
        assert tg.methods() == ["sendMessage"]
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == 100
        assert record["event"] == "Stop"
        assert record["plain"] == "✅ Task selesai"
        assert isinstance(record["sent_at"], float)

    def test_second_send_deletes_previous_and_stores_new(self, tmp_path, tg) -> None:
        self.deliver(tmp_path, "Stop")
        self.deliver(tmp_path, "Notification")
        assert tg.methods() == ["sendMessage", "sendMessage", "deleteMessage"]
        assert tg.params("deleteMessage") == [
            {"chat_id": "-10011", "message_id": "100"}
        ]
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == 101
        assert record["event"] == "Notification"
        assert record["plain"] == "🔔 Claude needs your attention"
        assert "deleted previous mention 100" in self.log(tmp_path)

    def test_delete_failure_falls_back_to_edit_without_mention(
        self, tmp_path, tg
    ) -> None:
        tg.script(
            "deleteMessage",
            _telegram_http_error(
                400,
                {"description": "Bad Request: message can't be deleted"},
                "deleteMessage",
            ),
        )
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        assert tg.methods()[-1] == "editMessageText"
        (edit,) = tg.params("editMessageText")
        assert edit == {
            "chat_id": "-10011",
            "message_id": "100",
            "text": "✅ Task selesai",
        }
        assert "parse_mode" not in edit
        assert "tg://user" not in edit["text"]
        assert "stripped mention from 100" in self.log(tmp_path)
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 101

    def test_delete_not_found_skips_edit(self, tmp_path, tg) -> None:
        tg.script(
            "deleteMessage",
            _telegram_http_error(
                400,
                {"description": "Bad Request: message to delete not found"},
                "deleteMessage",
            ),
        )
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        assert "editMessageText" not in tg.methods()
        assert "previous mention 100 already gone" in self.log(tmp_path)

    def test_edit_failure_is_logged_and_new_record_kept(self, tmp_path, tg) -> None:
        tg.script(
            "deleteMessage",
            _telegram_http_error(
                400, {"description": "Bad Request: nope"}, "deleteMessage"
            ),
        )
        tg.script(
            "editMessageText",
            _telegram_http_error(
                403, {"description": "Forbidden: nope"}, "editMessageText"
            ),
        )
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        log = self.log(tmp_path)
        assert "cleanup of 100 failed" in log
        assert "SECRET" not in log
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 101

    @pytest.mark.parametrize("failure", ["permanent", "timeout"])
    def test_send_failure_keeps_previous_record(
        self, tmp_path, tg, failure: str
    ) -> None:
        self.deliver(tmp_path)
        if failure == "permanent":
            tg.script(
                "sendMessage",
                _telegram_http_error(400, {"description": "Bad Request: x"}),
            )
        else:
            attempts = len(hook_module._NOTIFY_RETRY_DELAYS) + 1
            tg.script("sendMessage", *[TimeoutError("timed out")] * attempts)
        self.deliver(tmp_path)
        assert "deleteMessage" not in tg.methods()
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 100

    def test_late_older_message_is_retired_not_recorded(self, tmp_path, tg) -> None:
        hook_module._swap_last_notification(
            tmp_path,
            "-10011:42",
            {"message_id": 200, "event": "Stop", "plain": "x", "sent_at": 1e10},
            "seed",
        )
        tg.script("sendMessage", {"message_id": 150})
        self.deliver(tmp_path)
        assert tg.params("deleteMessage") == [
            {"chat_id": "-10011", "message_id": "150"}
        ]
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 200

    def test_concurrent_swaps_retire_each_message_once(self, tmp_path) -> None:
        import random
        import threading
        import time

        ids = list(range(1, 21))
        random.shuffle(ids)
        barrier = threading.Barrier(len(ids))
        stale: list[int] = []
        stale_lock = threading.Lock()
        now = time.time()

        def swap(message_id: int) -> None:
            barrier.wait()
            old = hook_module._swap_last_notification(
                tmp_path,
                "-10011:42",
                {
                    "message_id": message_id,
                    "event": "Stop",
                    "plain": "x",
                    "sent_at": now,
                },
                "t",
            )
            assert old is not None
            with stale_lock:
                stale.extend(entry["message_id"] for entry in old)

        threads = [threading.Thread(target=swap, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(stale) == list(range(1, 20))
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 20

    @pytest.mark.parametrize("source", ["env", "dotenv"])
    def test_flag_off_keeps_legacy_behavior(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, tg, source: str
    ) -> None:
        if source == "env":
            monkeypatch.setenv("CCBOT_NOTIFY_SINGLE_MENTION", "False")
        else:
            (tmp_path / ".env").write_text("CCBOT_NOTIFY_SINGLE_MENTION=false\n")
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        assert tg.methods() == ["sendMessage", "sendMessage"]
        assert not (tmp_path / "notify_last.json").exists()

    def test_topics_are_isolated(self, tmp_path, tg) -> None:
        self.deliver(tmp_path, thread=42)
        self.deliver(tmp_path, thread=43)
        self.deliver(tmp_path, chat=-10022, thread=42)
        assert "deleteMessage" not in tg.methods()
        records = self.records(tmp_path)
        assert {k: v["message_id"] for k, v in records.items()} == {
            "-10011:42": 100,
            "-10011:43": 101,
            "-10022:42": 102,
        }

    def test_notification_without_mention_is_not_tracked(self, tmp_path, tg) -> None:
        self.deliver(tmp_path, owner=False)
        self.deliver(tmp_path, owner=False)
        assert tg.methods() == ["sendMessage", "sendMessage"]
        assert not (tmp_path / "notify_last.json").exists()

    def test_unparseable_send_response_keeps_previous(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, tg
    ) -> None:
        from unittest.mock import MagicMock

        self.deliver(tmp_path)
        monkeypatch.setattr("urllib.request.urlopen", MagicMock())
        self.deliver(tmp_path)
        assert "without a message_id" in self.log(tmp_path)
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 100

    def test_corrupt_record_file_is_replaced(self, tmp_path, tg) -> None:
        (tmp_path / "notify_last.json").write_text("{not json")
        self.deliver(tmp_path)
        assert tg.methods() == ["sendMessage"]
        assert self.records(tmp_path) == {
            "-10011:42": self.records(tmp_path)["-10011:42"]
        }
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 100

    def test_stale_records_are_pruned(self, tmp_path, tg) -> None:
        import time

        (tmp_path / "notify_last.json").write_text(
            json.dumps(
                {
                    "-1:1": {
                        "message_id": 5,
                        "event": "Stop",
                        "plain": "x",
                        "sent_at": time.time() - 31 * 24 * 3600,
                    },
                    "-1:2": {"message_id": "bad"},
                    "-1:3": "garbage",
                    "-1:4": {
                        "message_id": 6,
                        "event": "Stop",
                        "plain": "x",
                        "sent_at": time.time() - 3600,
                    },
                }
            )
        )
        self.deliver(tmp_path)
        assert set(self.records(tmp_path)) == {"-1:4", "-10011:42"}

    def test_cleanup_network_error_redacts_token(self, tmp_path, tg) -> None:
        import urllib.error

        tg.script(
            "deleteMessage",
            urllib.error.URLError("boom at https://api.telegram.org/botSECRET/x"),
        )
        tg.script("editMessageText", TimeoutError("botSECRET timed out"))
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        log = self.log(tmp_path)
        assert "cleanup of 100 failed" in log
        assert "SECRET" not in log

    def test_post_with_retry_returns_sent_message(self, tmp_path, tg) -> None:
        tg.script("sendMessage", {"message_id": 77, "chat": {"id": -1}})
        result = hook_module._post_with_retry("SECRET", b"x=1", "Stop @1", tmp_path)
        assert result == {"message_id": 77, "chat": {"id": -1}}

    def test_post_with_retry_survives_unreadable_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value.read.side_effect = OSError("reset")
        urlopen = MagicMock(return_value=response)
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        result = hook_module._post_with_retry("SECRET", b"x=1", "Stop @1", tmp_path)
        assert result == {}
        assert urlopen.call_count == 1

    def test_swap_write_failure_logs_untracked_and_skips_delete(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, tg
    ) -> None:
        self.deliver(tmp_path)

        def fail(*_args, **_kwargs) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("ccbot.utils.atomic_write_json", fail)
        self.deliver(tmp_path)
        assert "deleteMessage" not in tg.methods()
        log = self.log(tmp_path)
        assert "new mention 101 untracked" in log
        assert self.records(tmp_path)["-10011:42"]["message_id"] == 100

    def test_retire_exception_is_logged_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, tg
    ) -> None:
        def boom(*_args, **_kwargs) -> bool:
            raise RuntimeError("exploded with SECRET inside")

        monkeypatch.setattr(hook_module, "_retire_notification", boom)
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        log = self.log(tmp_path)
        assert "cleanup error (RuntimeError: exploded with *** inside)" in log
        assert "SECRET" not in log

    @pytest.mark.parametrize("code", [429, 502])
    def test_transient_cleanup_failure_is_retried_on_next_send(
        self, tmp_path, tg, code: int
    ) -> None:
        tg.script(
            "deleteMessage",
            _telegram_http_error(code, {"description": "busy"}, "deleteMessage"),
        )
        tg.script(
            "editMessageText",
            _telegram_http_error(code, {"description": "busy"}, "editMessageText"),
        )
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == 101
        assert record["pending"] == [{"message_id": 100, "plain": "✅ Task selesai"}]
        assert "cleanup of 100 queued for retry" in self.log(tmp_path)

        self.deliver(tmp_path)
        deletes = [p["message_id"] for p in tg.params("deleteMessage")]
        assert deletes == ["100", "101", "100"]
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == 102
        assert "pending" not in record

    def test_network_error_cleanup_goes_to_pending(self, tmp_path, tg) -> None:
        tg.script("deleteMessage", TimeoutError("timed out"))
        tg.script(
            "editMessageText",
            _telegram_http_error(400, {"description": "can't edit"}, "editMessageText"),
        )
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        assert self.records(tmp_path)["-10011:42"]["pending"][0]["message_id"] == 100

    @pytest.mark.parametrize("code", [400, 403])
    def test_permanent_cleanup_failure_is_not_pending(
        self, tmp_path, tg, code: int
    ) -> None:
        for method in ("deleteMessage", "editMessageText"):
            tg.script(method, _telegram_http_error(code, {"description": "no"}, method))
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        assert "pending" not in self.records(tmp_path)["-10011:42"]
        assert "queued" not in self.log(tmp_path)

    def test_edit_outcomes_have_distinct_wording(self, tmp_path, tg) -> None:
        refuse = _telegram_http_error(
            400, {"description": "Bad Request: message can't be deleted"}
        )
        for _ in range(2):
            tg.script("deleteMessage", refuse)
        tg.script(
            "editMessageText",
            _telegram_http_error(
                400, {"description": "Bad Request: message to edit not found"}
            ),
            _telegram_http_error(
                400, {"description": "Bad Request: message is not modified"}
            ),
        )
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        self.deliver(tmp_path)
        log = self.log(tmp_path)
        assert "previous mention 100 already gone (edit: not found)" in log
        assert "previous mention 101 already without mention (not modified)" in log

    def test_pending_is_capped_at_five(self, tmp_path, tg) -> None:
        import time

        (tmp_path / "notify_last.json").write_text(
            json.dumps(
                {
                    "-10011:42": {
                        "message_id": 50,
                        "event": "Stop",
                        "plain": "x",
                        "sent_at": time.time(),
                        "pending": [
                            {"message_id": i, "plain": "x"} for i in range(1, 6)
                        ],
                    }
                }
            )
        )
        for _ in range(6):
            tg.script("deleteMessage", TimeoutError("timed out"))
            tg.script("editMessageText", TimeoutError("timed out"))
        self.deliver(tmp_path)
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == 100
        assert [p["message_id"] for p in record["pending"]] == [2, 3, 4, 5, 50]
        assert "pending cleanup over limit of 5, dropped 1" in self.log(tmp_path)
        assert len(record["pending"]) == 5

    def test_pending_never_holds_newest_message(self, tmp_path) -> None:
        import time

        records = hook_module._prune_last_notifications(
            {
                "k": {
                    "message_id": 9,
                    "sent_at": time.time(),
                    "pending": [{"message_id": 9}, {"message_id": 3, "plain": "p"}],
                }
            },
            time.time(),
        )
        assert records["k"]["pending"] == [{"message_id": 3, "plain": "p"}]

    @pytest.mark.parametrize(
        "content, enabled",
        [
            ("CCBOT_NOTIFY_SINGLE_MENTION=false # keep all\n", False),
            ("CCBOT_NOTIFY_SINGLE_MENTION=0\n", False),
            ("CCBOT_NOTIFY_SINGLE_MENTION=No\n", False),
            ("CCBOT_NOTIFY_SINGLE_MENTION=OFF\n", False),
            ("CCBOT_NOTIFY_SINGLE_MENTION='false'\n", False),
            (
                "CCBOT_NOTIFY_SINGLE_MENTION=true\nCCBOT_NOTIFY_SINGLE_MENTION=off\n",
                False,
            ),
            (
                "CCBOT_NOTIFY_SINGLE_MENTION=off\nCCBOT_NOTIFY_SINGLE_MENTION=true\n",
                True,
            ),
            ("CCBOT_NOTIFY_SINGLE_MENTION=yes # on\n", True),
            ("", True),
        ],
        ids=[
            "inline-comment",
            "zero",
            "no",
            "off",
            "quoted",
            "last-off-wins",
            "last-on-wins",
            "comment-on",
            "unset",
        ],
    )
    def test_flag_parsing(self, tmp_path, content: str, enabled: bool) -> None:
        (tmp_path / ".env").write_text(content)
        assert hook_module._single_mention_enabled(tmp_path) is enabled

    def test_env_file_with_bad_encoding_is_ignored(self, tmp_path) -> None:
        (tmp_path / ".env").write_bytes(b"CCBOT_NOTIFY_SINGLE_MENTION=\xff\xfe\n")
        assert hook_module._read_hook_env(tmp_path, "CCBOT_NOTIFY_SINGLE_MENTION") == ""

    def test_late_older_transient_failure_requeues_onto_newer_record(
        self, tmp_path, tg
    ) -> None:
        hook_module._swap_last_notification(
            tmp_path,
            "-10011:42",
            {"message_id": 200, "event": "Stop", "plain": "x", "sent_at": 1e10},
            "seed",
        )
        tg.script("sendMessage", {"message_id": 150})
        tg.script(
            "deleteMessage",
            _telegram_http_error(503, {"description": "busy"}, "deleteMessage"),
        )
        tg.script(
            "editMessageText",
            _telegram_http_error(503, {"description": "busy"}, "editMessageText"),
        )
        self.deliver(tmp_path)
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == 200
        assert record["pending"] == [{"message_id": 150, "plain": "✅ Task selesai"}]
        assert "cleanup of 150 queued for retry" in self.log(tmp_path)

    def test_entries_newer_than_record_are_never_retired(self, tmp_path, tg) -> None:
        import time

        (tmp_path / "notify_last.json").write_text(
            json.dumps(
                {
                    "-10011:42": {
                        "message_id": 50,
                        "event": "Stop",
                        "plain": "x",
                        "sent_at": time.time(),
                        "pending": [
                            {"message_id": 70, "plain": "x"},
                            {"message_id": 3, "plain": "x"},
                        ],
                    }
                }
            )
        )
        self.deliver(tmp_path)
        deletes = sorted(int(p["message_id"]) for p in tg.params("deleteMessage"))
        assert deletes == [3, 50]

    def test_requeue_skips_entries_not_older_than_record(self, tmp_path) -> None:
        import time

        hook_module._swap_last_notification(
            tmp_path,
            "k",
            {"message_id": 100, "event": "Stop", "plain": "x", "sent_at": time.time()},
            "seed",
        )
        hook_module._requeue_pending(
            tmp_path,
            "k",
            [{"message_id": 100, "plain": "x"}, {"message_id": 150, "plain": "x"}],
            "t",
        )
        assert "pending" not in self.records(tmp_path)["k"]
        log = self.log(tmp_path)
        assert "queued for retry" not in log
        assert "cleanup of 100, 150 not queued" in log

    def test_concurrent_sends_with_transient_failures_retire_each_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, tg
    ) -> None:
        import random
        import threading
        import urllib.error

        # The cap is covered elsewhere; here every failed cleanup must survive.
        monkeypatch.setattr(hook_module, "_NOTIFY_PENDING_MAX", 100)
        rng = random.Random(1234)
        failing = {"on": True}
        deleted: list[int] = []
        original = tg.__call__

        def flaky(request, timeout: float | None = None):
            method = request.full_url.rsplit("/", 1)[1]
            if method in ("deleteMessage", "editMessageText"):
                with tg._lock:
                    fail = failing["on"] and (
                        method == "editMessageText" or rng.random() < 0.4
                    )
                if fail:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        503,
                        "busy",
                        {},
                        None,  # type: ignore[arg-type]
                    )
            response = original(request, timeout)
            if method == "deleteMessage":
                from urllib.parse import parse_qs

                mid = int(parse_qs(request.data.decode())["message_id"][0])
                with tg._lock:
                    deleted.append(mid)
            return response

        monkeypatch.setattr("urllib.request.urlopen", flaky)
        count = 20
        barrier = threading.Barrier(count)

        def send() -> None:
            barrier.wait()
            self.deliver(tmp_path)

        threads = [threading.Thread(target=send) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        failing["on"] = False
        self.deliver(tmp_path)
        self.deliver(tmp_path)

        sent = count + 2
        newest = 100 + sent - 1
        assert sorted(deleted) == list(range(100, newest))
        record = self.records(tmp_path)["-10011:42"]
        assert record["message_id"] == newest
        assert "pending" not in record
