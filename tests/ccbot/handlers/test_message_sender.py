"""Tests for message_sender — retrying sends over a flaky connection."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

import ccbot.handlers.message_sender as sender


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sender, "NETWORK_RETRY_DELAYS", (0.0, 0.0))


def _bot(*outcomes: object) -> AsyncMock:
    bot = AsyncMock()
    bot.send_message.side_effect = list(outcomes)
    return bot


def _formatted(call) -> bool:
    return call.kwargs.get("parse_mode") == sender.PARSE_MODE


async def test_timeout_is_retried_with_the_formatted_text() -> None:
    sent = MagicMock()
    bot = _bot(TimedOut(), NetworkError("httpx.ConnectError: "), sent)
    assert (
        await sender.send_with_fallback(bot, -100, "**hi**", message_thread_id=7)
        is sent
    )
    calls = bot.send_message.await_args_list
    assert len(calls) == 3
    assert all(_formatted(c) for c in calls)
    assert all(c.kwargs["message_thread_id"] == 7 for c in calls)


async def test_network_failure_gives_up_without_plain_text_resend() -> None:
    bot = _bot(TimedOut(), TimedOut(), TimedOut())
    assert await sender.send_with_fallback(bot, -100, "hi") is None
    calls = bot.send_message.await_args_list
    assert len(calls) == len(sender.NETWORK_RETRY_DELAYS) + 1
    assert all(_formatted(c) for c in calls)


async def test_rejected_markdown_falls_back_to_plain_text() -> None:
    sent = MagicMock()
    bot = _bot(BadRequest("Can't parse entities"), sent)
    assert await sender.send_with_fallback(bot, -100, "hi") is sent
    first, second = bot.send_message.await_args_list
    assert _formatted(first)
    assert not _formatted(second)


async def test_plain_text_fallback_is_retried_too() -> None:
    sent = MagicMock()
    bot = _bot(BadRequest("Can't parse entities"), TimedOut(), sent)
    assert await sender.send_with_fallback(bot, -100, "hi") is sent
    assert bot.send_message.await_count == 3


async def test_caller_can_ask_for_a_longer_retry() -> None:
    sent = MagicMock()
    bot = _bot(*[TimedOut()] * 5, sent)
    assert (
        await sender.send_with_fallback(bot, -100, "hi", retry_delays=(0.0,) * 5)
        is sent
    )
    assert bot.send_message.await_count == 6
    assert "retry_delays" not in bot.send_message.await_args.kwargs


async def test_flood_control_is_left_to_the_caller() -> None:
    bot = _bot(RetryAfter(3))
    with pytest.raises(RetryAfter):
        await sender.send_with_fallback(bot, -100, "hi")
    assert bot.send_message.await_count == 1


async def test_safe_send_uses_the_same_retry() -> None:
    bot = _bot(TimedOut(), MagicMock())
    await sender.safe_send(bot, -100, "hi", message_thread_id=7)
    calls = bot.send_message.await_args_list
    assert len(calls) == 2
    assert all(c.kwargs["message_thread_id"] == 7 for c in calls)


def test_bad_request_is_not_transient() -> None:
    assert sender.is_transient_network_error(TimedOut())
    assert sender.is_transient_network_error(NetworkError("httpx.ReadError: "))
    assert not sender.is_transient_network_error(BadRequest("Topic_id_invalid"))
    assert not sender.is_transient_network_error(RetryAfter(1))
    assert not sender.is_transient_network_error(ValueError())
