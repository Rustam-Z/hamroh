"""One bot object for every channel: Telegram chats and web chats.

The engine and the MCP tools talk to "the bot" without knowing where a chat
lives. :class:`ChannelBot` looks at the chat id: web chats go to the
:class:`~.hub.WebHub`, everything else goes to the real Telegram bot
unchanged.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from .chat_ids import is_web_chat
from .hub import WebEvent, WebHub


class WebChatUnsupported(Exception):
    """A Telegram-only feature (photo, poll, edit, …) was used in a web chat."""


@dataclass(frozen=True)
class SentMessage:
    """What a send returns for a web chat — the same fields tools read from
    Telegram's ``Message``."""

    chat_id: int
    message_id: int


class ChannelBot:
    """Routes bot calls to Telegram or to the web hub by chat id."""

    def __init__(self, telegram_bot: Any, hub: WebHub) -> None:
        self._telegram = telegram_bot
        self.hub = hub

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        """Send a text message. In web chats ``text`` is Markdown and Telegram
        options like ``parse_mode`` are ignored."""
        if not is_web_chat(chat_id):
            return await self._telegram.send_message(
                chat_id=chat_id, text=text, **kwargs
            )
        message_id = await self.hub.next_message_id(chat_id)
        self.hub.publish(
            WebEvent(
                kind="message",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_to_id=kwargs.get("reply_to_message_id"),
            )
        )
        return SentMessage(chat_id=chat_id, message_id=message_id)

    async def send_chat_action(self, chat_id: int, action: str) -> bool:
        """Show "typing…" in the chat."""
        if not is_web_chat(chat_id):
            return bool(
                await self._telegram.send_chat_action(chat_id=chat_id, action=action)
            )
        self.hub.publish(WebEvent(kind="typing", chat_id=chat_id))
        return True

    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: list[Any]
    ) -> bool:
        """React to a message with an emoji (``ReactionTypeEmoji`` list)."""
        if not is_web_chat(chat_id):
            return bool(
                await self._telegram.set_message_reaction(
                    chat_id=chat_id, message_id=message_id, reaction=reaction
                )
            )
        emoji = reaction[0].emoji if reaction else None
        self.hub.publish(
            WebEvent(
                kind="reaction", chat_id=chat_id, message_id=message_id, emoji=emoji
            )
        )
        return True

    def __getattr__(self, name: str) -> Any:
        """Any other bot method goes to Telegram. If it targets a web chat,
        it fails with a clear message instead of a confusing Telegram error."""
        attr = getattr(self._telegram, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def telegram_only(*args: Any, **kwargs: Any) -> Any:
            if is_web_chat(kwargs.get("chat_id", 0)):
                raise WebChatUnsupported(
                    f"{name} is not available in web chats — "
                    "send a text message instead"
                )
            return await attr(*args, **kwargs)

        return telegram_only
