"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
Transient network errors (timeouts, connection resets, DNS failures) are
retried before a send gives up: briefly by default, longer when the caller
passes its own retry_delays (the queue worker does this for Claude output).
"""

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import BadRequest, NetworkError, RetryAfter

from ..markdown_v2 import convert_markdown
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Default seconds to wait before each retry of a send that hit a transient
# network error, so a send makes len(NETWORK_RETRY_DELAYS) + 1 attempts in
# total. Kept short because handlers that reply to the user block update
# processing while they wait.
NETWORK_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)

_T = TypeVar("_T")


def is_transient_network_error(exc: BaseException) -> bool:
    """Return True for transport failures that are worth retrying.

    PTB derives BadRequest from NetworkError, but Telegram rejects the same
    request the same way every time, so BadRequest is not transient.
    """
    return isinstance(exc, NetworkError) and not isinstance(exc, BadRequest)


async def _retry_transient(
    call: Callable[[], Awaitable[_T]],
    what: str,
    delays: Sequence[float] | None = None,
) -> _T:
    """Await call(), retrying transient network errors after each delay.

    delays defaults to NETWORK_RETRY_DELAYS. A timeout can fire after
    Telegram has already accepted the request, so a retry may duplicate a
    message. A duplicate is preferred over losing Claude output.
    """
    if delays is None:
        delays = NETWORK_RETRY_DELAYS
    attempts = len(delays) + 1
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            return await call()
        except Exception as e:
            if delay is None or not is_transient_network_error(e):
                raise
            logger.warning(
                "%s failed (%s), retrying in %.0fs (attempt %d/%d)",
                what,
                e,
                delay,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    retry_delays: Sequence[float] | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Transient network errors are retried first, waiting retry_delays
    (default NETWORK_RETRY_DELAYS) between attempts. Plain text is only
    tried when the formatted message fails for another reason (e.g.
    Telegram rejects the MarkdownV2), because resending as plain text
    cannot fix a network problem and only adds duplicates.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        formatted = _ensure_formatted(text)
        return await _retry_transient(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=formatted,
                parse_mode=PARSE_MODE,
                **kwargs,
            ),
            f"Send to {chat_id}",
            retry_delays,
        )
    except RetryAfter:
        raise
    except Exception as e:
        if is_transient_network_error(e):
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None
    try:
        return await _retry_transient(
            lambda: bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            ),
            f"Plain-text send to {chat_id}",
            retry_delays,
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await target.edit_message_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    await send_with_fallback(bot, chat_id, text, **kwargs)
