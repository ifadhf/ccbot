"""Status polling keeps updating status when an interactive match shows nothing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ccbot.handlers.status_polling as polling


@pytest.fixture
def poll(monkeypatch):
    tmux = SimpleNamespace(
        find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5")),
        capture_pane=AsyncMock(return_value="pane"),
    )
    monkeypatch.setattr(polling, "tmux_manager", tmux)
    monkeypatch.setattr(polling, "get_interactive_window", lambda *args: None)
    monkeypatch.setattr(polling, "is_interactive_ui", lambda pane: True)
    monkeypatch.setattr(polling, "parse_status_line", lambda pane: "Working")
    enqueue = AsyncMock()
    monkeypatch.setattr(polling, "enqueue_status_update", enqueue)
    return enqueue


@pytest.mark.asyncio
async def test_filtered_interactive_match_falls_through_to_status(poll, monkeypatch):
    monkeypatch.setattr(polling, "handle_interactive_ui", AsyncMock(return_value=False))
    await polling.update_status_message(AsyncMock(), -10011, "@5", thread_id=42)
    poll.assert_awaited_once()
    assert poll.await_args.args[3] == "Working"


@pytest.mark.asyncio
async def test_shown_interactive_ui_still_skips_status(poll, monkeypatch):
    monkeypatch.setattr(polling, "handle_interactive_ui", AsyncMock(return_value=True))
    await polling.update_status_message(AsyncMock(), -10011, "@5", thread_id=42)
    poll.assert_not_awaited()
