"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window↔session
mapping in <CCBOT_DIR>/session_map.json. Also provides `--install` to
auto-configure the hook in ~/.claude/settings.json.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccbot_dir() (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
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
from pathlib import Path

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


def _get_tmux_recovery_metadata() -> dict[str, str]:
    """Attach live tmux identities so stale hooks cannot confirm a new restore."""
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            os.environ.get("TMUX_PANE", ""),
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


def _read_bot_token(ccbot_dir_path: Path) -> str:
    """Read TELEGRAM_BOT_TOKEN from env or <ccbot_dir>/.env (no config import)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    env_file = ccbot_dir_path / ".env"
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


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


def _notify_telegram(event: str, payload: dict) -> None:
    """Send a direct Telegram notification for Stop/Notification hook events.

    Resolves this pane's window to its bound Telegram topic via state.json,
    then calls sendMessage over HTTPS. Silent no-op when the window is not
    bound to any topic (e.g. Claude sessions outside ccbot).
    """
    import time
    import urllib.parse
    import urllib.request

    from .utils import atomic_write_json, ccbot_dir

    if event == "Notification":
        # This variant just re-announces the idle state Stop already sent
        # ~60s earlier ("Claude is waiting for your input") — skip it.
        # Other Notification kinds (e.g. permission requests) still fire.
        msg = (payload.get("message") or "").lower()
        if "waiting for your input" in msg or "waiting for input" in msg:
            logger.debug("Skipping idle-nudge Notification (redundant with Stop)")
            return

    window = _get_tmux_window()
    if window is None:
        return
    _, window_id, _ = window

    cfg_dir = ccbot_dir()

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
    chat_id, thread_id, user_id = target

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

    token = _read_bot_token(cfg_dir)
    if not token:
        logger.warning("No TELEGRAM_BOT_TOKEN available, cannot notify")
        return

    owner = state.get("topic_owners", {}).get(f"{chat_id}:{thread_id}", {})
    if isinstance(owner.get("user_id"), int) and owner["user_id"] > 0:
        label = (
            ("@" + owner["username"])
            if owner.get("username")
            else owner.get("first_name", "pemilik")
        )
        mention = f'<a href="tg://user?id={owner["user_id"]}">{html.escape(label)}</a>'
    else:
        # Legacy private bindings identify a user; shared group IDs do not.
        mention = _get_user_mention(user_id, token, cfg_dir) if int(user_id) > 0 else ""
    if event == "Stop":
        text = f"✅ Task selesai — menunggu input {mention}".rstrip()
    else:  # Notification
        # Always generic: the real payload message (e.g. "Claude needs your
        # permission") is misleading for non-risky prompts like AskUserQuestion,
        # which also fires notification_type=permission_prompt.
        text = "🔔 Claude needs your attention"
        if mention:
            text += f" — {mention}"

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": "false",
        }
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=6
        ) as resp:
            logger.info("Sent %s notification (HTTP %s)", event, resp.status)
    except Exception as e:
        logger.warning("Failed to send %s notification (%s)", event, type(e).__name__)


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
        _notify_telegram(event, payload)
        return

    if event != "SessionStart":
        logger.debug("Ignoring unsupported event: %s", event)
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    window = _get_tmux_window()
    if window is None:
        logger.warning("Cannot determine tmux window")
        return
    tmux_session_name, window_id, window_name = window
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"
    recovery_metadata = _get_tmux_recovery_metadata()
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
