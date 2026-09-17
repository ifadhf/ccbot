"""Separate Telegram sender authorization from topic/session routing.

When CCBOT_CHAT_SCOPED_TOPICS is enabled, a chat and topic identify one shared
session. Picker state is also scoped to that chat and topic, so one person's
private-group picker cannot affect the shared group's picker.
"""

from typing import Any

from telegram.ext import ContextTypes

from .config import config


def conversation_id(user_id: int, chat_id: int | None) -> int:
    """Return the routing key; never use this key for user authorization."""
    if config.chat_scoped_topics:
        if chat_id is None:
            raise ValueError("Chat context is required for shared topic routing")
        return chat_id
    return user_id


def get_topic_data(
    context: ContextTypes.DEFAULT_TYPE, thread_id: int | None
) -> dict[str, Any] | None:
    """Return shared picker state for this topic, or legacy per-user state."""
    if not config.chat_scoped_topics:
        return context.user_data
    if context.chat_data is None:
        return None
    topics = context.chat_data.setdefault("ccbot_topics", {})
    return topics.setdefault(thread_id or 0, {})
