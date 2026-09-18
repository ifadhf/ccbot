"""Restore topic conversations from the existing CCBot state.json.

Journal missing bindings before starting tmux windows, require a fresh matching
SessionStart hook, and retain failed records for an owner-initiated retry.
No user prompt or interrupted command is replayed by recovery.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from .config import config
from .session import SessionManager, WindowState
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)
SESSION_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
HOOK_TIMEOUT = 15.0


def _read_map() -> dict:
    """Read the hook's atomic snapshot without making a missing hook look ready."""
    try:
        data = json.loads(config.session_map_file.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


async def _prepare_state(
    manager: SessionManager, identity: str, created: float
) -> None:
    """Checkpoint lost bindings before any code can reuse their tmux IDs."""
    windows = {window.window_id: window for window in await tmux_manager.list_windows()}
    same_instance = manager.tmux_session_identity == identity
    # One-time migration from agrinas.4: a new tmux session after the last
    # saved state cannot be a survivor, even if it recycles IDs/names.
    legacy_survivor = (
        not manager.tmux_session_identity and 0 < created <= manager.loaded_state_mtime
    )
    hook_map = _read_map()
    for route_id, topic_id, wid in list(manager.iter_thread_bindings()):
        state = manager.window_states.get(wid, WindowState())
        info = hook_map.get(f"{config.tmux_session_name}:{wid}", {})
        if isinstance(info, dict) and (
            info.get("tmux_session_identity", "") == manager.tmux_session_identity
            and info.get("session_id")
        ):
            # Preserve the latest hook, including /clear just before power
            # loss, even if the monitor had not copied it to state.json yet.
            state = WindowState.from_dict(info)
            manager.window_states[wid] = state
        window = windows.get(wid)
        legacy_match = bool(
            legacy_survivor
            and window
            and window.window_name == manager.get_display_name(wid)
            and window.cwd == state.cwd
            and "claude" in window.pane_current_command.lower()
        )
        if window and (same_instance or legacy_match):
            continue
        manager.stage_recovery(route_id, topic_id)

    # All old IDs have been journaled before a new ID can collide with them.
    manager.tmux_session_identity = identity
    manager._save_state()


async def prepare_recovery(manager: SessionManager) -> None:
    """Detect tmux loss before dispatch, including a reset while the bot stays up."""
    async with manager.recovery_lock:
        identity, created = await tmux_manager.session_identity()
        if identity != manager.tmux_session_identity:
            await _prepare_state(manager, identity, created)


async def recover_sessions(manager: SessionManager) -> None:
    """Recover before stale-window cleanup and before Telegram starts dispatching."""
    async with manager.recovery_lock:
        identity, created = await tmux_manager.session_identity()
        await _prepare_state(manager, identity, created)
        for key in list(manager.pending_recoveries):
            await _recover_topic(manager, key, identity)


async def recover_topic(manager: SessionManager, route_id: int, topic_id: int) -> bool:
    """Retry one saved conversation without permitting concurrent duplicates."""
    async with manager.recovery_lock:
        identity, created = await tmux_manager.session_identity()
        if identity != manager.tmux_session_identity:
            await _prepare_state(manager, identity, created)
        key = f"{route_id}:{topic_id}"
        if key not in manager.pending_recoveries:
            return manager.get_window_for_thread(route_id, topic_id) is not None
        return await _recover_topic(manager, key, identity)


async def cancel_recovery(
    manager: SessionManager, route_id: int, topic_id: int, *, kill: bool = False
) -> bool:
    """Explicitly cancel one topic, serializing with any active recovery attempt."""
    async with manager.recovery_lock:
        record = manager.pending_recoveries.get(f"{route_id}:{topic_id}")
        if record is None:
            return False
        if kill:
            for window in await tmux_manager.list_windows():
                if window.recovery_token and window.recovery_token == record.get(
                    "token"
                ):
                    if not await tmux_manager.kill_window(window.window_id):
                        raise RuntimeError("Could not stop the recovery window")
        manager.unbind_thread(route_id, topic_id)
        return True


async def _recover_topic(manager: SessionManager, key: str, identity: str) -> bool:
    """Commit a binding only after a hook confirms this precise recovery attempt."""
    record = manager.pending_recoveries[key]
    try:
        route_id, topic_id = (int(part) for part in key.split(":"))
        if topic_id <= 1:
            raise ValueError("Only named topics can be recovered")
        if config.topic_owner_lock:
            chat_id = manager.resolve_chat_id(route_id, topic_id)
            owner = manager.topic_owners.get(f"{chat_id}:{topic_id}", {})
            if owner.get("user_id") not in config.allowed_users:
                raise ValueError("The saved topic owner is not allowed")
        elif not config.chat_scoped_topics and route_id not in config.allowed_users:
            raise ValueError("The saved user is not allowed")
        state = WindowState.from_dict(record["window_state"])
        if not SESSION_ID.fullmatch(state.session_id):
            raise ValueError("The saved Claude session ID is missing or invalid")
        if not Path(state.cwd).is_absolute() or not Path(state.cwd).is_dir():
            raise ValueError("The original working directory is unavailable")
        transcript = manager._build_session_file_path(state.session_id, state.cwd)
        if (
            transcript is None
            or not transcript.is_file()
            or not transcript.stat().st_size
        ):
            raise ValueError("The saved Claude transcript is unavailable")
        token = record["token"]
        if not isinstance(token, str) or not SESSION_ID.fullmatch(token):
            raise ValueError("The saved recovery token is invalid")

        # Reuse a launch that survived a bot crash, including a crash between
        # tmux creation and saving the new window ID. Never use display names.
        windows = await tmux_manager.list_windows()
        candidates = [window for window in windows if window.recovery_token == token]
        if len(candidates) > 1:
            raise ValueError(
                "Multiple windows match this recovery; operator review needed"
            )
        if candidates:
            wid = candidates[0].window_id
        else:
            # Refuse a duplicate live Claude conversation bound to another topic.
            if any(
                manager.window_states.get(wid, WindowState()).session_id
                == state.session_id
                for _, _, wid in manager.iter_thread_bindings()
            ):
                raise ValueError("This conversation is already bound to another topic")
            other_tokens = {
                other["token"]
                for other_key, other in manager.pending_recoveries.items()
                if other_key != key
                and other["window_state"].get("session_id") == state.session_id
            }
            if any(window.recovery_token in other_tokens for window in windows):
                raise ValueError(
                    "This conversation is already recovering in another topic"
                )
            success, _, _, wid = await tmux_manager.create_window(
                state.cwd,
                window_name=state.window_name or None,
                resume_session_id=state.session_id,
                recovery_token=token,
            )
            if not success:
                raise ValueError("Could not launch the saved Claude conversation")

        deadline = asyncio.get_running_loop().time() + HOOK_TIMEOUT
        while True:
            info = _read_map().get(f"{config.tmux_session_name}:{wid}", {})
            window = await tmux_manager.find_window_by_id(wid)
            if window is None or window.recovery_token != token:
                raise ValueError("The recovery window exited before it was ready")
            if (
                isinstance(info, dict)
                and info.get("recovery_token") == token
                and info.get("tmux_session_identity") == identity
            ):
                if (
                    info.get("session_id") != state.session_id
                    or info.get("cwd") != state.cwd
                ):
                    raise ValueError(
                        "Claude reported a different conversation or directory"
                    )
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise ValueError(
                    "No matching SessionStart hook; check Claude login and hooks"
                )
            await asyncio.sleep(0.25)

        if any(other == wid for _, _, other in manager.iter_thread_bindings()):
            raise ValueError("The recovery window is already bound to another topic")
        # One atomic state.json update replaces the journal record with the
        # ready binding and restores the original unread offset and ownership.
        state.window_name = window.window_name
        manager.window_states[wid] = state
        manager.window_display_names[wid] = state.window_name
        manager.thread_bindings.setdefault(route_id, {})[topic_id] = wid
        manager.user_window_offsets.setdefault(route_id, {})[wid] = int(
            record.get("offset", 0)
        )
        del manager.pending_recoveries[key]
        manager._save_state()
        logger.info("Recovered topic %s into window %s", key, wid)
        return True
    except (ValueError, TypeError, KeyError, OSError) as exc:
        record["error"] = str(exc)
        manager._save_state()
        logger.warning("Topic %s recovery pending: %s", key, exc)
        return False
