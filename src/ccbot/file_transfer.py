"""Transfer explicitly selected files for the Telegram topic bound to this pane.

Incoming documents use a private per-topic inbox. The CLI uploads only regular
files in that topic's outbox, resolves the destination from saved bindings, and
keeps the bot token out of Claude prompts and command arguments.
Disabled unless CCBOT_FILE_TRANSFER=true; backgrounded Claude sessions resolve
their window through session_map.json by CLAUDE_CODE_SESSION_ID.
"""

import argparse
import asyncio
import io
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from telegram import Bot, Message
from telegram.error import TelegramError

from .utils import ccbot_dir

MAX_INCOMING = 20_000_000
MAX_OUTGOING = 50_000_000


def topic_directories(chat_id: int, thread_id: int) -> dict[str, Path]:
    """Create private inbox/outbox directories without following symlinks."""
    if thread_id <= 1:
        raise ValueError("Files require a named Telegram topic.")
    root = ccbot_dir().resolve()
    topic = root / "files" / str(chat_id) / str(thread_id)
    paths = {name: topic / name for name in ("inbox", "outbox")}
    for directory in [root / "files", topic.parent, topic, *paths.values()]:
        if directory.is_symlink():
            raise ValueError("File directories must not be symlinks.")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return paths


async def receive_attachment(message: Message, chat_id: int, thread_id: int) -> Path:
    """Download a document or photo with bounded size and a unique safe filename."""
    attachment = message.document or (message.photo[-1] if message.photo else None)
    if attachment is None:
        raise ValueError("No file attachment.")
    if attachment.file_size and attachment.file_size > MAX_INCOMING:
        raise ValueError("Incoming file exceeds 20 MB.")
    original = getattr(attachment, "file_name", None) or "photo.jpg"
    filename = re.sub(
        r"[^A-Za-z0-9._-]", "_", original.replace("\\", "/").split("/")[-1]
    )
    filename = filename.lstrip(".")[:150] or "attachment.bin"
    remote = await attachment.get_file()
    data = await remote.download_as_bytearray()
    if len(data) > MAX_INCOMING:
        raise ValueError("Incoming file exceeds 20 MB.")
    path = (
        topic_directories(chat_id, thread_id)["inbox"]
        / f"{uuid.uuid4().hex}_{filename}"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return path


def _settings() -> dict[str, str | None]:
    """Read CCBot settings the way Config does: environment over <ccbot_dir>/.env."""
    from dotenv import dotenv_values

    return {**dotenv_values(ccbot_dir() / ".env"), **os.environ}


def _flag(values: dict[str, str | None], name: str) -> bool:
    return (values.get(name) or "false").lower() == "true"


def _current_window(values: dict[str, str | None]) -> str:
    """Return the CCBot window ID of the Claude session running this command."""
    from .hook import _get_tmux_window

    session_name = values.get("TMUX_SESSION_NAME") or "ccbot"
    window = _get_tmux_window()
    if window is not None and window[0] == session_name:
        return window[1]
    # Backgrounded sessions (/background) run in the claude daemon outside
    # tmux; session_map records which CCBot window shows them.
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if session_id:
        try:
            session_map = json.loads((ccbot_dir() / "session_map.json").read_text())
        except (OSError, ValueError):
            session_map = {}
        prefix = f"{session_name}:"
        found = [
            key.removeprefix(prefix)
            for key, info in session_map.items()
            if key.startswith(prefix)
            and isinstance(info, dict)
            and info.get("session_id") == session_id
        ]
        if len(found) == 1:
            return found[0]
    raise ValueError("Run this command inside a topic's CCBot Claude session.")


def current_topic() -> tuple[int, int]:
    """Find exactly one allowed topic bound to the current CCBot session."""
    values = _settings()
    if not _flag(values, "CCBOT_FILE_TRANSFER"):
        raise ValueError("File transfer is disabled (set CCBOT_FILE_TRANSFER=true).")
    window_id = _current_window(values)
    state = json.loads((ccbot_dir() / "state.json").read_text())
    matches = []
    for route_id, bindings in state.get("thread_bindings", {}).items():
        for thread_id, bound_id in bindings.items():
            if bound_id == window_id:
                chat_id = state.get("group_chat_ids", {}).get(f"{route_id}:{thread_id}")
                if chat_id and int(thread_id) > 1:
                    matches.append((int(route_id), int(chat_id), int(thread_id)))
    if len(matches) != 1:
        raise ValueError("This window must be bound to exactly one named topic.")
    route_id, chat_id, thread_id = matches[0]
    allowed = {
        int(uid.strip())
        for uid in (values.get("ALLOWED_USERS") or "").split(",")
        if uid.strip()
    }
    if _flag(values, "CCBOT_TOPIC_OWNER_LOCK"):
        owner = state.get("topic_owners", {}).get(f"{chat_id}:{thread_id}", {})
        if owner.get("user_id") not in allowed:
            raise ValueError("The topic has no authorized owner.")
    elif not _flag(values, "CCBOT_CHAT_SCOPED_TOPICS") and route_id not in allowed:
        # Per-user routing: the binding's key is the Telegram user ID.
        raise ValueError("The topic's user is not allowed.")
    return chat_id, thread_id


def read_outgoing(path: Path, outbox: Path) -> bytes:
    """Read a bounded regular file directly inside this topic's outbox."""
    path = path.expanduser().absolute()
    if path.is_symlink() or path.parent.resolve() != outbox.resolve():
        raise ValueError("Save the file directly in this topic's outbox first.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Only regular files without hard links can be uploaded.")
        if not 0 < info.st_size <= MAX_OUTGOING:
            raise ValueError("Outgoing files must contain data and be at most 50 MB.")
        data = stream.read(MAX_OUTGOING + 1)
    if len(data) > MAX_OUTGOING:
        raise ValueError("Outgoing file exceeds 50 MB.")
    return data


async def send_file(path: Path, caption: str = "") -> int:
    """Upload once and return Telegram's message ID only after confirmed success."""
    from .hook import _read_bot_token

    if len(caption) > 1024:
        raise ValueError("Caption exceeds 1024 characters.")
    chat_id, thread_id = current_topic()
    paths = topic_directories(chat_id, thread_id)
    data = read_outgoing(path, paths["outbox"])
    token = _read_bot_token(ccbot_dir())
    if not token:
        raise ValueError("The bot token is not configured.")
    async with Bot(token=token) as bot:
        result = await bot.send_document(
            chat_id=chat_id,
            message_thread_id=thread_id,
            document=io.BytesIO(data),
            filename=path.name,
            caption=caption or None,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=20,
        )
    return result.message_id


def file_main() -> None:
    """Implement `ccbot files` and `ccbot send-file PATH [--caption TEXT]`."""
    parser = argparse.ArgumentParser(prog="ccbot " + sys.argv[1])
    if sys.argv[1] == "send-file":
        parser.add_argument("path", type=Path)
        parser.add_argument("--caption", default="")
    args = parser.parse_args(sys.argv[2:])
    try:
        if sys.argv[1] == "files":
            paths = topic_directories(*current_topic())
            print(json.dumps({name: str(path) for name, path in paths.items()}))
        else:
            message_id = asyncio.run(send_file(args.path, args.caption))
            print(json.dumps({"sent": True, "message_id": message_id}))
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    except (OSError, TelegramError, RuntimeError) as exc:
        # Network exceptions may contain URLs including the secret bot token.
        parser.exit(
            1,
            f"File operation failed ({type(exc).__name__}); delivery is unconfirmed. Check Telegram before retrying.\n",
        )
