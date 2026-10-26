"""Tests for /history — only the conversation text is shown."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.history import send_history

_MESSAGES = [
    {"role": "user", "text": "cek repo", "content_type": "text"},
    {"role": "assistant", "text": "secret plan", "content_type": "thinking"},
    {"role": "assistant", "text": "**Bash**(git status)", "content_type": "tool_use"},
    {"role": "assistant", "text": "On branch main", "content_type": "tool_result"},
    {"role": "assistant", "text": "❯ `/model`", "content_type": "local_command"},
    {"role": "assistant", "text": "Repo bersih.", "content_type": "text"},
]


@pytest.fixture
def _history_session():
    with (
        patch(
            "ccbot.handlers.history.session_manager.get_recent_messages",
            AsyncMock(return_value=(list(_MESSAGES), len(_MESSAGES))),
        ),
        patch(
            "ccbot.handlers.history.session_manager.get_display_name",
            return_value="repo",
        ),
    ):
        yield


@pytest.mark.usefixtures("_history_session")
class TestHistoryTextOnly:
    @pytest.mark.parametrize("show_user", [True, False])
    async def test_drops_tools_thinking_and_commands(self, show_user: bool) -> None:
        reply = AsyncMock()
        with (
            patch("ccbot.handlers.history.config.show_user_messages", show_user),
            patch("ccbot.handlers.history.safe_reply", reply),
        ):
            await send_history(object(), "@1")

        text = reply.call_args.args[1]
        assert "Repo bersih." in text
        for hidden in ("secret plan", "git status", "On branch main", "/model"):
            assert hidden not in text
        assert ("👤 cek repo" in text) is show_user
        expected_total = 2 if show_user else 1
        assert f"Messages ({expected_total} total)" in text
