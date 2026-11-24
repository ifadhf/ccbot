"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window↔session
mapping in <CCBOT_DIR>/session_map.json. Also provides `--install` to
auto-configure the hook in ~/.claude/settings.json.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccbot_dir() (shared with config.py).

Stop/Notification events send a Telegram notification that mentions the
topic owner. Only the newest mentioned notification per topic keeps its
mention: the previous one (tracked in <CCBOT_DIR>/notify_last.json) is
deleted, or edited to drop the mention (CCBOT_NOTIFY_SINGLE_MENTION).

Key functions: hook_main() (CLI entry), _install_hook(), _notify_telegram().
"""

import argparse
import fcntl
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, TypeGuard

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "ccbot hook"


def _find_ccbot_path() -> str:
    """Find the full path to the ccbot executable.

    Priority:
    1. shutil.which("ccbot") - if ccbot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    ccbot_path = shutil.which("ccbot")
    if ccbot_path:
        return ccbot_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccbot is installed in a venv
    python_dir = Path(sys.executable).parent
    ccbot_in_venv = python_dir / "ccbot"
    if ccbot_in_venv.exists():
        return str(ccbot_in_venv)

    # Last resort: assume it will be in PATH
    return "ccbot"


def _is_hook_installed(settings: dict, event: str = "SessionStart") -> bool:
    """Check if ccbot hook is already installed in the settings.

    Detects both 'ccbot hook' and full paths like '/path/to/ccbot hook'.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get(event, [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            # Match 'ccbot hook' or paths ending with 'ccbot hook'
            if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX):
                return True
    return False


def _install_hook() -> int:
    """Install the ccbot hook into Claude's settings.json.

    Returns 0 on success, 1 on error.
    """
    settings_file = _CLAUDE_SETTINGS_FILE
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    events = ("SessionStart", "Stop", "Notification")
    if all(_is_hook_installed(settings, event) for event in events):
        logger.info("Hook already installed in %s", settings_file)
        print(f"Hook already installed in {settings_file}")
        return 0

    # Find the full path to ccbot
    ccbot_path = _find_ccbot_path()
    hook_command = f"{ccbot_path} hook"
    hook_config = {"type": "command", "command": hook_command, "timeout": 30}
    logger.info("Installing hook command: %s", hook_command)

    # Install the hook
    if "hooks" not in settings:
        settings["hooks"] = {}
    for event in events:
        if not _is_hook_installed(settings, event):
            settings["hooks"].setdefault(event, []).append({"hooks": [hook_config]})

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", settings_file)
    print(f"Hook installed successfully in {settings_file}")
    return 0


def _get_tmux_window() -> tuple[str, str, str] | None:
    """Resolve the tmux (session_name, window_id, window_name) for this pane.

    Returns None when not running inside tmux or resolution fails.
    """
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.debug("TMUX_PANE not set, not inside tmux")
        return None

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return None
    return parts[0], parts[1], parts[2]


def _parent_pid(pid: int) -> int:
    """Return the parent pid of pid from /proc, or 0 if it cannot be read."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm (field 2) may contain spaces and parens; ppid follows state.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def _bg_job_id() -> str | None:
    """Return the claude daemon job id of the bg session running this hook.

    Background sessions run in the claude daemon, outside tmux. Each Claude
    process records itself in ~/.claude/sessions/<pid>.json, with a "jobId"
    for bg sessions, so walk up from the hook's parent to find it.
    """
    sessions_dir = Path.home() / ".claude" / "sessions"
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        try:
            info = json.loads((sessions_dir / f"{pid}.json").read_text())
        except (OSError, json.JSONDecodeError):
            pid = _parent_pid(pid)
            continue
        job_id = info.get("jobId") if isinstance(info, dict) else None
        return job_id if isinstance(job_id, str) and job_id else None
    return None


def _is_attach_to_job(argv: list[str], job_id: str) -> bool:
    """Check whether argv is `claude attach <job_id or full session id>`."""
    for i in range(len(argv) - 2):
        if (
            os.path.basename(argv[i]) == "claude"
            and argv[i + 1] == "attach"
            and argv[i + 2].startswith(job_id)
        ):
            return True
    return False


def _proc_matches(pid: int, proc_start: object) -> bool:
    """Check that pid is alive and, when known, started at proc_start.

    ~/.claude/sessions/<pid>.json records the process start time in clock
    ticks ("procStart", field 22 of /proc/<pid>/stat). Comparing it keeps a
    stale file from matching an unrelated process that reused the pid.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    if not isinstance(proc_start, str) or not proc_start.isdigit():
        return True
    try:
        # comm (field 2) may contain spaces and parens; fields 3+ follow it.
        return stat.rsplit(")", 1)[1].split()[19] == proc_start
    except IndexError:
        return False


def _parked_viewer_pids(job_id: str) -> list[int]:
    """Return pids of interactive Claude processes parked on bg job job_id.

    Since Claude Code 2.1.31x, /background keeps the original TUI running
    in its tmux pane as a viewer of the daemon job. Instead of exec'ing
    `claude attach <job>`, it records the job as "parkedJobId" in its own
    ~/.claude/sessions/<pid>.json.
    """
    sessions_dir = Path.home() / ".claude" / "sessions"
    pids: list[int] = []
    try:
        files = sorted(sessions_dir.glob("*.json"))
    except OSError:
        return pids
    for path in files:
        try:
            info = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(info, dict) or info.get("parkedJobId") != job_id:
            continue
        pid = info.get("pid")
        if (
            isinstance(pid, int)
            and pid > 1
            and _proc_matches(pid, info.get("procStart"))
        ):
            pids.append(pid)
    return pids


def _pane_for_pid(
    pid: int, panes: dict[int, tuple[str, str, str]]
) -> tuple[str, str, str] | None:
    """Walk up from pid to the tmux pane process that contains it."""
    for _ in range(8):
        if pid in panes:
            return panes[pid]
        pid = _parent_pid(pid)
        if pid <= 1:
            break
    return None


def _get_attached_tmux_window() -> tuple[str, str, str] | None:
    """Resolve the tmux window that displays this hook's bg job.

    After /background, /clear, or any new session inside a daemon session,
    hooks run without TMUX_PANE. The window that displays the job either
    still runs the original TUI parked on the job (current Claude Code) or
    runs `claude attach <job_id>` (older versions, or a manual attach).
    Returns (session_name, window_id, window_name).
    """
    job_id = _bg_job_id()
    if not job_id:
        return None

    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_pid}:#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    panes: dict[int, tuple[str, str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4 and parts[0].isdigit():
            panes[int(parts[0])] = (parts[1], parts[2], parts[3])
    if not panes:
        return None

    for pid in _parked_viewer_pids(job_id):
        found = _pane_for_pid(pid, panes)
        if found is not None:
            logger.debug("bg job %s is parked in a TUI under pane %s", job_id, found)
            return found

    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            raw = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        if not _is_attach_to_job(argv, job_id):
            continue
        found = _pane_for_pid(int(proc.name), panes)
        if found is not None:
            logger.debug("bg job %s is attached in pane %s", job_id, found)
            return found
    logger.debug("bg job %s is not attached in any tmux pane", job_id)
    return None


def _window_id_for_session(cfg_dir: Path, session_id: str) -> str | None:
    """Return the window_id bound to session_id in session_map.json, if any."""
    if not session_id:
        return None
    try:
        session_map = json.loads((cfg_dir / "session_map.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for key, info in session_map.items():
        if isinstance(info, dict) and info.get("session_id") == session_id:
            _, _, window_id = key.partition(":")
            if window_id.startswith("@"):
                return window_id
    return None


def _get_tmux_recovery_metadata(target: str) -> dict[str, str]:
    """Attach live tmux identities so stale hooks cannot confirm a new restore."""
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            target,
            "-p",
            "#{@ccbot-instance-id}:#{@ccbot-recovery-token}",
        ],
        capture_output=True,
        text=True,
    )
    values = result.stdout.strip().split(":")
    if result.returncode or len(values) != 2:
        return {}
    return {
        key: value
        for key, value in zip(
            ("tmux_session_identity", "recovery_token"), values, strict=True
        )
        if _UUID_RE.fullmatch(value)
    }


# Minimum seconds between identical event notifications per window
_NOTIFY_DEBOUNCE_SECS = 5.0

# Per-attempt HTTP timeout for a notification, and the waits before each
# retry. Retries run in a detached child (see _detach), so their total
# duration is not bounded by the hook timeout in settings.json. About 8
# minutes of waiting in total, like the bot's retries for Claude output, so
# a notification survives the same outages as the reply it announces.
_NOTIFY_HTTP_TIMEOUT = 15.0
_NOTIFY_RETRY_DELAYS: tuple[float, ...] = (3.0, 10.0, 30.0, 60.0, 120.0, 240.0)

# Delivery outcomes go to <ccbot_dir>/notify.log because Claude Code drops
# the stderr of a hook that exits 0. Rotated once it exceeds this size.
_NOTIFY_LOG_MAX_BYTES = 512 * 1024

# Removing the previous mention is a single best-effort request per method:
# it must never hold up or block the new notification.
_NOTIFY_CLEANUP_TIMEOUT = 10.0
# Records in notify_last.json older than this are dropped on the next write.
_NOTIFY_LAST_MAX_AGE = 30 * 24 * 3600.0
# Old notifications whose cleanup failed transiently, retried on the next
# mentioned notification in the topic; the oldest are dropped beyond this.
_NOTIFY_PENDING_MAX = 5
# Set to false/0/no/off (env or <ccbot_dir>/.env) to keep every mention.
_SINGLE_MENTION_ENV = "CCBOT_NOTIFY_SINGLE_MENTION"
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _parse_env_value(raw: str) -> str:
    """Parse a .env value like python-dotenv: quotes, or an inline ' #' comment."""
    raw = raw.strip()
    quoted = re.match(r"""^(['"])(.*?)\1""", raw)
    if quoted:
        return quoted.group(2)
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _read_hook_env(ccbot_dir_path: Path, name: str) -> str:
    """Read setting name from env or <ccbot_dir>/.env (no config import).

    In .env the last matching line wins, as with python-dotenv.
    """
    value = os.environ.get(name, "")
    if value:
        return value
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(name)}\s*=(.*)$")
    found = ""
    try:
        for line in (ccbot_dir_path / ".env").read_text().splitlines():
            match = pattern.match(line.strip())
            if match:
                found = _parse_env_value(match.group(1))
    except (OSError, ValueError):
        pass
    return found


def _read_bot_token(ccbot_dir_path: Path) -> str:
    """Read TELEGRAM_BOT_TOKEN from env or <ccbot_dir>/.env (no config import)."""
    return _read_hook_env(ccbot_dir_path, "TELEGRAM_BOT_TOKEN")


def _single_mention_enabled(cfg_dir: Path) -> bool:
    """Return False when CCBOT_NOTIFY_SINGLE_MENTION is false, 0, no or off."""
    value = _read_hook_env(cfg_dir, _SINGLE_MENTION_ENV)
    return value.strip().lower() not in _FALSE_VALUES


def _get_user_mention(user_id: str, token: str, cfg_dir: Path) -> str:
    """Return an HTML mention for user_id, e.g. '<a href="tg://user?id=...">@name</a>'.

    Resolves username/first_name via Bot API getChat once and caches it
    (usernames rarely change) to avoid an extra HTTP round-trip per notify.
    """
    import urllib.request

    cache_file = cfg_dir / "user_cache.json"
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, json.JSONDecodeError):
        cache = {}

    label = cache.get(user_id)
    if not label:
        try:
            url = f"https://api.telegram.org/bot{token}/getChat?chat_id={user_id}"
            with urllib.request.urlopen(url, timeout=6) as resp:
                result = json.load(resp).get("result", {})
            username = result.get("username")
            label = f"@{username}" if username else result.get("first_name", "you")
            cache[user_id] = label
            from .utils import atomic_write_json

            atomic_write_json(cache_file, cache)
        except Exception as e:
            logger.debug("getChat failed for user %s: %s", user_id, e)
            label = "you"

    return f'<a href="tg://user?id={user_id}">{html.escape(label)}</a>'


def _log_notify(cfg_dir: Path, message: str) -> None:
    """Record a notification outcome in <ccbot_dir>/notify.log and the logger."""
    import time

    logger.info("%s", message)
    log_file = cfg_dir / "notify.log"
    try:
        if log_file.stat().st_size > _NOTIFY_LOG_MAX_BYTES:
            log_file.replace(log_file.with_name("notify.log.1"))
    except OSError:
        pass
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n"
    try:
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _telegram_error_detail(error: Exception) -> tuple[str, int | None]:
    """Return (description, retry_after) from a Bot API HTTP error body."""
    try:
        body = json.loads(error.read().decode(errors="replace"))  # type: ignore[attr-defined]
    except Exception:
        return "", None
    if not isinstance(body, dict):
        return "", None
    description = str(body.get("description", ""))[:200]
    params = body.get("parameters")
    retry_after = params.get("retry_after") if isinstance(params, dict) else None
    return description, retry_after if isinstance(retry_after, int) else None


def _post_with_retry(token: str, data: bytes, label: str, cfg_dir: Path) -> dict | None:
    """POST sendMessage, retrying timeouts, network errors, 429 and 5xx.

    Other 4xx responses are permanent (bad chat or topic) and are not
    retried. Once Telegram accepts the message, returns the Bot API result
    (the sent Message), or {} if the response body cannot be decoded.
    Returns None when the message was not delivered.
    """
    import time
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    attempts = len(_NOTIFY_RETRY_DELAYS) + 1
    for attempt, delay in enumerate((*_NOTIFY_RETRY_DELAYS, None), start=1):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=data), timeout=_NOTIFY_HTTP_TIMEOUT
            ) as resp:
                try:
                    result = json.loads(resp.read()).get("result")
                except Exception:  # odd body: still sent, just untracked
                    result = None
                _log_notify(
                    cfg_dir,
                    f"{label}: sent (HTTP {resp.status}, attempt {attempt}/{attempts})",
                )
                return result if isinstance(result, dict) else {}
        except urllib.error.HTTPError as e:
            description, retry_after = _telegram_error_detail(e)
            if e.code != 429 and e.code < 500:
                _log_notify(cfg_dir, f"{label}: rejected (HTTP {e.code} {description})")
                return None
            error = f"HTTP {e.code} {description}".strip()
            if delay is not None and retry_after:
                delay = max(delay, float(retry_after))
        except Exception as e:  # timeouts, resets, DNS and TLS failures
            # Never let the token reach the log, whatever the error text holds.
            error = f"{type(e).__name__}: {e}".replace(token, "***")[:200]
        if delay is None:
            _log_notify(
                cfg_dir, f"{label}: gave up after {attempts} attempts ({error})"
            )
            return None
        _log_notify(
            cfg_dir,
            f"{label}: attempt {attempt}/{attempts} failed ({error}), "
            f"retrying in {delay:.0f}s",
        )
        time.sleep(delay)
    return None


class _CallResult(NamedTuple):
    """Outcome of a single Bot API request made by _telegram_call_once."""

    ok: bool
    error: str = ""
    transient: bool = False  # 429, 5xx or a network error: worth retrying


def _telegram_call_once(token: str, method: str, params: dict) -> _CallResult:
    """POST one Bot API request without retries.

    Never raises and never logs; the caller decides what to record. The
    token is stripped from any error text.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=_NOTIFY_CLEANUP_TIMEOUT
        ):
            return _CallResult(True)
    except urllib.error.HTTPError as e:
        description, _ = _telegram_error_detail(e)
        error = f"HTTP {e.code} {description}".strip().replace(token, "***")
        return _CallResult(False, error, e.code == 429 or e.code >= 500)
    except Exception as e:  # timeouts, resets, DNS and TLS failures
        error = f"{type(e).__name__}: {e}".replace(token, "***")[:200]
        return _CallResult(False, error, True)


def _notification_texts(event: str, mention: str) -> tuple[str, str]:
    """Return (text, plain): the HTML message and its text without mention."""
    if event == "Stop":
        plain = "✅ Task selesai"
        return f"{plain} — menunggu input {mention}".rstrip(), plain
    # Notification: always generic. The real payload message (e.g. "Claude
    # needs your permission") is misleading for non-risky prompts like
    # AskUserQuestion, which also fires notification_type=permission_prompt.
    plain = "🔔 Claude needs your attention"
    return (f"{plain} — {mention}" if mention else plain), plain


def _valid_message_id(value: object) -> TypeGuard[int]:
    """Return True for a positive int Telegram message_id (bool excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _load_last_notifications(path: Path) -> dict:
    """Read notify_last.json; a missing or corrupt file counts as empty."""
    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return records if isinstance(records, dict) else {}


def _clean_pending(entries: object, newest_id: int) -> list[dict]:
    """Return well-formed pending entries older than newest_id, oldest first.

    Entries at or above newest_id are dropped: cleanup must never touch the
    tracked newest mention or anything sent after it.
    """
    if not isinstance(entries, list):
        return []
    kept: list[dict] = []
    seen = {newest_id}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("message_id")
        if not _valid_message_id(mid) or mid in seen or mid > newest_id:
            continue
        plain = entry.get("plain")
        seen.add(mid)
        kept.append(
            {"message_id": mid, "plain": plain if isinstance(plain, str) else ""}
        )
    # Message ids grow over time, so the lowest ids are the oldest to drop.
    kept.sort(key=lambda entry: entry["message_id"])
    return kept


def _valid_pending(entries: object, newest_id: int) -> list[dict]:
    """Keep the newest _NOTIFY_PENDING_MAX clean pending entries."""
    return _clean_pending(entries, newest_id)[-_NOTIFY_PENDING_MAX:]


def _prune_last_notifications(records: dict, now: float) -> dict:
    """Drop malformed records and records older than _NOTIFY_LAST_MAX_AGE."""
    kept: dict = {}
    for key, record in records.items():
        if not isinstance(record, dict) or not _valid_message_id(
            record.get("message_id")
        ):
            continue
        sent_at = record.get("sent_at")
        if not isinstance(sent_at, (int, float)) or isinstance(sent_at, bool):
            continue
        if now - sent_at > _NOTIFY_LAST_MAX_AGE:
            continue
        pending = _valid_pending(record.get("pending"), record["message_id"])
        record = {k: v for k, v in record.items() if k != "pending"}
        if pending:
            record["pending"] = pending
        kept[key] = record
    return kept


def _update_last_notifications(cfg_dir: Path, update: Callable[[dict], None]) -> None:
    """Apply update to notify_last.json under an exclusive lock.

    The lock covers only this read/modify/write; raises OSError on failure.
    The file is always rewritten, so pruning and corrupt-file repair persist.
    """
    import time

    from .utils import atomic_write_json

    path = cfg_dir / "notify_last.json"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg_dir / "notify_last.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        records = _prune_last_notifications(_load_last_notifications(path), time.time())
        update(records)
        atomic_write_json(path, records)


def _swap_last_notification(
    cfg_dir: Path, key: str, record: dict, label: str
) -> list[dict] | None:
    """Store record as the topic's newest mention; return the ones to retire.

    Telegram message ids grow within a chat, so the higher id wins: a late
    older send (e.g. after long retries) retires its own message instead of
    the newer one. The returned list holds {"message_id", "plain"} entries:
    the superseded message plus any pending cleanups from earlier failures.
    Returns None when the file cannot be updated (nothing is retired then).
    """
    to_retire: list[dict] = []
    newest = record["message_id"]

    def update(records: dict) -> None:
        nonlocal newest
        old = records.get(key)
        if old is not None and old["message_id"] > record["message_id"]:
            newest = old["message_id"]
            to_retire.append(record)
            to_retire.extend(old.pop("pending", []))
        elif old is not None and old["message_id"] == record["message_id"]:
            return
        else:
            records[key] = {k: v for k, v in record.items() if k != "pending"}
            if old is not None:
                to_retire.append(old)
                to_retire.extend(old.get("pending", []))

    try:
        _update_last_notifications(cfg_dir, update)
    except OSError as e:
        _log_notify(
            cfg_dir,
            f"{label}: could not update notify_last.json "
            f"({type(e).__name__}: {e}), new mention {record['message_id']} "
            "untracked, previous mention kept",
        )
        return None
    return [
        {"message_id": entry["message_id"], "plain": entry.get("plain", "")}
        for entry in to_retire
        if entry["message_id"] < newest
    ]


def _requeue_pending(cfg_dir: Path, key: str, entries: list[dict], label: str) -> None:
    """Add transiently failed cleanups to the topic's pending list."""

    def id_list(items: list) -> str:
        return ", ".join(str(item) for item in items)

    ids = id_list([entry["message_id"] for entry in entries])
    outcome: list[str] = []

    def update(records: dict) -> None:
        current = records.get(key)
        if current is None:
            outcome.append(f"cleanup of {ids} dropped, no record for the topic")
            return
        candidates = _clean_pending(
            [*current.get("pending", []), *entries], current["message_id"]
        )
        pending = candidates[-_NOTIFY_PENDING_MAX:]
        trimmed = [entry["message_id"] for entry in candidates[:-_NOTIFY_PENDING_MAX]]
        if pending:
            current["pending"] = pending
        else:
            current.pop("pending", None)
        kept_ids = {entry["message_id"] for entry in pending}
        queued = [e["message_id"] for e in entries if e["message_id"] in kept_ids]
        skipped = [
            e["message_id"]
            for e in entries
            if e["message_id"] not in kept_ids and e["message_id"] not in trimmed
        ]
        if queued:
            outcome.append(f"cleanup of {id_list(queued)} queued for retry")
        if skipped:
            outcome.append(
                f"cleanup of {id_list(skipped)} not queued (not older than "
                f"tracked mention {current['message_id']})"
            )
        if trimmed:
            outcome.append(
                f"pending cleanup over limit of {_NOTIFY_PENDING_MAX}, "
                f"dropped {id_list(trimmed)}"
            )

    try:
        _update_last_notifications(cfg_dir, update)
    except OSError as e:
        _log_notify(
            cfg_dir,
            f"{label}: could not queue cleanup of {ids} ({type(e).__name__}: {e})",
        )
        return
    for line in outcome:
        _log_notify(cfg_dir, f"{label}: {line}")


def _retire_notification(
    token: str, cfg_dir: Path, chat_id: int, stale: dict, label: str
) -> bool:
    """Delete a superseded notification, or edit it to drop the mention.

    Returns True when cleanup failed transiently (429, 5xx or a network
    error) and is worth retrying later; permanent failures are only logged.
    """
    mid = stale["message_id"]
    deleted = _telegram_call_once(
        token, "deleteMessage", {"chat_id": chat_id, "message_id": mid}
    )
    if deleted.ok:
        _log_notify(cfg_dir, f"{label}: deleted previous mention {mid}")
        return False
    if "message to delete not found" in deleted.error.lower():
        _log_notify(cfg_dir, f"{label}: previous mention {mid} already gone")
        return False
    plain = stale.get("plain")
    if not isinstance(plain, str) or not plain:
        _log_notify(
            cfg_dir,
            f"{label}: cleanup of {mid} failed (delete: {deleted.error}; "
            "no plain text to edit)",
        )
        return deleted.transient
    # No parse_mode: plain text, so no mention entity survives the edit.
    edited = _telegram_call_once(
        token,
        "editMessageText",
        {"chat_id": chat_id, "message_id": mid, "text": plain},
    )
    lowered = edited.error.lower()
    if edited.ok:
        _log_notify(
            cfg_dir,
            f"{label}: stripped mention from {mid} (delete failed: {deleted.error})",
        )
        return False
    if "message to edit not found" in lowered:
        _log_notify(
            cfg_dir, f"{label}: previous mention {mid} already gone (edit: not found)"
        )
        return False
    if "message is not modified" in lowered:
        _log_notify(
            cfg_dir,
            f"{label}: previous mention {mid} already without mention (not modified)",
        )
        return False
    _log_notify(
        cfg_dir,
        f"{label}: cleanup of {mid} failed (delete: {deleted.error}; "
        f"edit: {edited.error})",
    )
    # A later attempt may succeed if either step failed for a passing reason.
    return deleted.transient or edited.transient


def _detach() -> bool:
    """Fork so delivery can outlive the hook; return True in the child.

    Claude Code waits for the hook and kills it at the settings.json
    timeout, which is too short to ride out a slow or flaky Telegram
    connection. The child starts its own session and swaps the inherited
    stdio pipes for /dev/null, so Claude Code sees the hook finish as soon
    as the parent exits. If fork fails, deliver inline instead.
    """
    try:
        pid = os.fork()
    except OSError:
        return True
    if pid:
        return False
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if devnull > 2:
        os.close(devnull)
    return True


def _deliver_notification(
    cfg_dir: Path,
    state: dict,
    event: str,
    window_id: str,
    target: tuple[int, int, str],
) -> None:
    """Build the notification text for target and send it with retries.

    After a mentioned notification is sent, the topic's previous mentioned
    notification is retired (see _swap_last_notification).
    """
    import time
    import urllib.parse

    chat_id, thread_id, user_id = target
    label = f"{event} {window_id} -> thread {thread_id}"

    token = _read_bot_token(cfg_dir)
    if not token:
        logger.warning("No TELEGRAM_BOT_TOKEN available, cannot notify")
        _log_notify(cfg_dir, f"{label}: no bot token, skipped")
        return

    owner = state.get("topic_owners", {}).get(f"{chat_id}:{thread_id}", {})
    if isinstance(owner.get("user_id"), int) and owner["user_id"] > 0:
        name = (
            ("@" + owner["username"])
            if owner.get("username")
            else owner.get("first_name", "pemilik")
        )
        mention = f'<a href="tg://user?id={owner["user_id"]}">{html.escape(name)}</a>'
    else:
        # Legacy private bindings identify a user; shared group IDs do not.
        mention = _get_user_mention(user_id, token, cfg_dir) if int(user_id) > 0 else ""
    text, plain = _notification_texts(event, mention)

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": "false",
        }
    ).encode()
    result = _post_with_retry(token, data, label, cfg_dir)
    if result is None or not mention or not _single_mention_enabled(cfg_dir):
        return

    message_id = result.get("message_id")
    if not _valid_message_id(message_id):
        _log_notify(
            cfg_dir, f"{label}: sent without a message_id, previous mention kept"
        )
        return
    record = {
        "message_id": message_id,
        "event": event,
        "plain": plain,
        "sent_at": time.time(),
    }
    key = f"{chat_id}:{thread_id}"
    stale = _swap_last_notification(cfg_dir, key, record, label)
    if not stale:
        return
    retry: list[dict] = []
    for entry in stale:
        try:
            if _retire_notification(token, cfg_dir, chat_id, entry, label):
                retry.append(entry)
        except Exception as e:
            error = f"{type(e).__name__}: {e}".replace(token, "***")[:200]
            _log_notify(cfg_dir, f"{label}: cleanup error ({error})")
    if retry:
        _requeue_pending(cfg_dir, key, retry, label)


def _notify_telegram(event: str, payload: dict, detach: bool = False) -> None:
    """Send a direct Telegram notification for Stop/Notification hook events.

    Resolves this pane's window to its bound Telegram topic via state.json,
    then calls sendMessage over HTTPS. Silent no-op when the window is not
    bound to any topic (e.g. Claude sessions outside ccbot).

    With detach=True (the real hook), delivery and its retries run in a
    detached child process so they do not hold up Claude Code.
    """
    import time

    from .utils import atomic_write_json, ccbot_dir

    if event == "Notification":
        # This variant just re-announces the idle state Stop already sent
        # ~60s earlier ("Claude is waiting for your input") — skip it.
        # Other Notification kinds (e.g. permission requests) still fire.
        msg = (payload.get("message") or "").lower()
        if "waiting for your input" in msg or "waiting for input" in msg:
            logger.debug("Skipping idle-nudge Notification (redundant with Stop)")
            return

    cfg_dir = ccbot_dir()

    window = _get_tmux_window()
    if window is not None:
        _, window_id, _ = window
    else:
        # Backgrounded sessions (/background) run in the claude daemon
        # outside tmux; the bot follows them in session_map, so look there,
        # then at the window whose TUI is parked on (or attached to) the job.
        found = _window_id_for_session(cfg_dir, payload.get("session_id", ""))
        if found is None:
            attached = _get_attached_tmux_window()
            if attached is None:
                return
            found = attached[1]
        window_id = found

    # Find the thread binding for this window
    state_file = cfg_dir / "state.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        logger.debug("No readable state.json, skipping notification")
        return

    thread_bindings: dict = state.get("thread_bindings", {})
    group_chat_ids: dict = state.get("group_chat_ids", {})
    target: tuple[int, int, str] | None = None  # (chat_id, thread_id, user_id)
    for uid, bindings in thread_bindings.items():
        for thread_id, wid in bindings.items():
            if wid == window_id:
                chat_id = group_chat_ids.get(f"{uid}:{thread_id}")
                if chat_id:
                    target = (int(chat_id), int(thread_id), uid)
                break
    if target is None:
        logger.debug("Window %s not bound to a topic, skipping", window_id)
        return

    # Debounce identical event notifications per window
    debounce_file = cfg_dir / "notify_debounce.json"
    now = time.time()
    key = f"{window_id}:{event}"
    try:
        debounce = json.loads(debounce_file.read_text())
    except (OSError, json.JSONDecodeError):
        debounce = {}
    last = float(debounce.get(key, 0))
    if now - last < _NOTIFY_DEBOUNCE_SECS:
        logger.debug("Debounced %s (%.1fs since last)", key, now - last)
        return
    debounce[key] = now
    # Drop stale entries so the file doesn't grow forever
    debounce = {k: v for k, v in debounce.items() if now - float(v) < 3600}
    try:
        atomic_write_json(debounce_file, debounce)
    except OSError:
        pass

    if detach and not _detach():
        return  # parent: hand control back to Claude Code right away
    try:
        _deliver_notification(cfg_dir, state, event, window_id, target)
    finally:
        if detach:
            os._exit(0)


def hook_main() -> None:
    """Process a Claude Code hook event from stdin, or install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply here)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into ~/.claude/settings.json",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event in ("Stop", "Notification"):
        _notify_telegram(event, payload, detach=True)
        return

    if event != "SessionStart":
        logger.debug("Ignoring unsupported event: %s", event)
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane; bg sessions
    # in the claude daemon fall back to the window running `claude attach`.
    window = _get_tmux_window() or _get_attached_tmux_window()
    if window is None:
        logger.warning("Cannot determine tmux window")
        return
    tmux_session_name, window_id, window_name = window
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"
    recovery_metadata = _get_tmux_recovery_metadata(window_id)
    if window_name.startswith("__ccbot_recovery_"):
        token = window_name.removeprefix("__ccbot_recovery_")
        if _UUID_RE.fullmatch(token):
            recovery_metadata.setdefault("recovery_token", token)

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                    **recovery_metadata,
                }

                # Clean up old-format key ("session:window_name") if it exists.
                # Previous versions keyed by window_name instead of window_id.
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
