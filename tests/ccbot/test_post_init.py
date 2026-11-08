"""Tests for post_init — startup must survive a flaky Telegram connection."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, TimedOut

import ccbot.bot as bot_module


@pytest.fixture
def startup(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub everything post_init starts after registering commands."""
    monkeypatch.setattr(bot_module, "recover_sessions", AsyncMock())
    monkeypatch.setattr(
        bot_module.session_manager, "resolve_stale_ids", AsyncMock(), raising=False
    )
    monitor_cls = MagicMock()
    monkeypatch.setattr(bot_module, "SessionMonitor", monitor_cls)
    monkeypatch.setattr(bot_module, "status_poll_loop", AsyncMock())
    return monitor_cls


def _application() -> MagicMock:
    app = MagicMock()
    app.bot.rate_limiter = None
    return app


async def test_command_menu_timeout_does_not_abort_startup(
    monkeypatch: pytest.MonkeyPatch, startup: MagicMock
) -> None:
    monkeypatch.setattr(
        bot_module, "_register_bot_commands", AsyncMock(side_effect=TimedOut())
    )
    await bot_module.post_init(_application())
    startup.return_value.start.assert_called_once()


async def test_rejected_command_menu_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, startup: MagicMock
) -> None:
    monkeypatch.setattr(
        bot_module,
        "_register_bot_commands",
        AsyncMock(side_effect=BadRequest("Bot_command_invalid")),
    )
    with pytest.raises(BadRequest):
        await bot_module.post_init(_application())
    startup.return_value.start.assert_not_called()
