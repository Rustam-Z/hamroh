"""Forum-topic routing: capture the inbound topic id, send back to it.

In a forum supergroup every message lives inside a topic (Telegram's
``message_thread_id``). Before this fix the bot dropped that id, so
``telegram_send_message`` delivered every reply to the General topic.
These tests pin the full path: dispatcher capture → ``<msg topic=…>``
envelope → send tool passing the id back to the Telegram API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.engine import format_messages_as_xml
from hamroh.models import ChatMessage
from hamroh.telegram_io.dispatcher import _to_chat_message
from hamroh.tools.base import ToolContext
from hamroh.tools.telegram.telegram_send_message import (
    SendMessageArgs,
    TelegramSendMessageTool,
)

FORUM_CHAT = -1003205927799
TOPIC_ID = 199


def _update(*, is_topic: bool, thread_id: int | None) -> MagicMock:
    """Minimal Update mock covering the fields ``_to_chat_message`` reads."""
    user = MagicMock()
    user.id = 42
    user.username = "alice"
    user.first_name = "Alice"

    msg = MagicMock()
    msg.chat_id = FORUM_CHAT
    msg.message_id = 6353
    msg.from_user = user
    msg.text = "hello"
    msg.caption = None
    msg.date = datetime(2026, 7, 9, 10, 31, tzinfo=timezone.utc)
    msg.reply_to_message = None
    msg.is_topic_message = is_topic
    msg.message_thread_id = thread_id

    update = MagicMock()
    update.effective_message = msg
    update.to_dict.return_value = {"u": 1}
    return update


def _inbound(thread_id: int | None) -> ChatMessage:
    return ChatMessage(
        chat_id=FORUM_CHAT,
        message_id=6353,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction="in",
        timestamp=datetime(2026, 7, 9, 10, 31, tzinfo=timezone.utc),
        text="hello",
        message_thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# Dispatcher: capture the topic id from the raw update
# ---------------------------------------------------------------------------


def test_dispatcher_captures_topic_id() -> None:
    # Given a message posted inside a forum topic
    update = _update(is_topic=True, thread_id=TOPIC_ID)
    # When the dispatcher normalizes it
    cm = _to_chat_message(update)
    # Then the topic id survives on the ChatMessage
    assert cm is not None, "topic message must normalize to a ChatMessage"
    assert cm.message_thread_id == TOPIC_ID, (
        "forum topic id must be captured so replies can route back to it"
    )


def test_dispatcher_ignores_reply_thread_outside_forums() -> None:
    # Given a plain-group reply, which also sets message_thread_id
    update = _update(is_topic=False, thread_id=10)
    # When the dispatcher normalizes it
    cm = _to_chat_message(update)
    # Then the non-routable thread id is dropped
    assert cm is not None, "reply message must normalize to a ChatMessage"
    assert cm.message_thread_id is None, (
        "reply-thread ids in non-forum chats must not be treated as topics"
    )


# ---------------------------------------------------------------------------
# Envelope: surface the topic to the model
# ---------------------------------------------------------------------------


def test_envelope_carries_topic_attr() -> None:
    xml = format_messages_as_xml([_inbound(TOPIC_ID)])
    assert f' topic="{TOPIC_ID}"' in xml, (
        "the <msg> envelope must expose topic= so the model can route replies"
    )


def test_envelope_omits_topic_attr_outside_forums() -> None:
    xml = format_messages_as_xml([_inbound(None)])
    assert "topic=" not in xml, "non-forum messages must not carry a topic attr"


# ---------------------------------------------------------------------------
# Send tool: pass the topic id back to Telegram
# ---------------------------------------------------------------------------


def _send_tool() -> TelegramSendMessageTool:
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=555)
    return TelegramSendMessageTool(ToolContext(bot=bot))


@pytest.mark.asyncio
async def test_send_tool_routes_to_topic() -> None:
    # Given a send request targeting a forum topic
    tool = _send_tool()
    args = SendMessageArgs(chat_id=FORUM_CHAT, text="hi", message_thread_id=TOPIC_ID)
    # When the tool delivers it
    result = await tool.run(args)
    # Then the Telegram API call carries the topic id
    assert not result.is_error, f"send must succeed, got: {result.content}"
    kwargs = tool.ctx.bot.send_message.await_args.kwargs
    assert kwargs["message_thread_id"] == TOPIC_ID, (
        "message_thread_id must be forwarded or the message lands in General"
    )


@pytest.mark.asyncio
async def test_send_tool_defaults_to_no_topic() -> None:
    # Given a send request with no topic (DM / plain group)
    tool = _send_tool()
    # When the tool delivers it
    result = await tool.run(SendMessageArgs(chat_id=FORUM_CHAT, text="hi"))
    # Then no thread id is sent
    assert not result.is_error, f"send must succeed, got: {result.content}"
    kwargs = tool.ctx.bot.send_message.await_args.kwargs
    assert kwargs["message_thread_id"] is None, (
        "omitting the topic must send a plain chat-level message"
    )
