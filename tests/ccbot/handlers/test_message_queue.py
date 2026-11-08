"""Tests for message_queue — Claude output must outlast a connection outage."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TimedOut

import ccbot.handlers.message_queue as mq
import ccbot.handlers.message_sender as sender

USER_ID = 111
THREAD_ID = 42


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sender, "NETWORK_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(mq, "CONTENT_RETRY_DELAYS", (0.0,) * 6)
    monkeypatch.setattr(mq, "_message_queues", {})
    monkeypatch.setattr(mq, "_status_msg_info", {})
    monkeypatch.setattr(mq, "_tool_msg_ids", {})
    monkeypatch.setattr(
        mq.session_manager, "resolve_chat_id", lambda user_id, thread_id: -100
    )
    monkeypatch.setattr(
        mq.tmux_manager, "find_window_by_id", AsyncMock(return_value=None)
    )


def _content(text: str) -> mq.MessageTask:
    return mq.MessageTask(
        task_type="content", window_id="@1", parts=[text], thread_id=THREAD_ID
    )


async def test_content_keeps_retrying_past_the_short_schedule() -> None:
    sent = MagicMock(message_id=7)
    bot = AsyncMock()
    bot.send_message.side_effect = [TimedOut()] * 6 + [sent]
    await mq._process_content_task(bot, USER_ID, _content("done"))
    assert bot.send_message.await_count == 7
    assert bot.send_message.await_args.kwargs["message_thread_id"] == THREAD_ID


async def test_content_is_dropped_and_logged_after_the_retry_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TimedOut()
    with caplog.at_level(logging.ERROR, logger=mq.__name__):
        await mq._process_content_task(bot, USER_ID, _content("done"))
    assert bot.send_message.await_count == len(mq.CONTENT_RETRY_DELAYS) + 1
    assert "Dropped content for window @1" in caplog.text


async def test_status_messages_keep_the_short_retry() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TimedOut()
    await mq._do_send_status_message(bot, USER_ID, THREAD_ID, "@1", "Working")
    assert bot.send_message.await_count == len(sender.NETWORK_RETRY_DELAYS) + 1
    assert (USER_ID, THREAD_ID) not in mq._status_msg_info
