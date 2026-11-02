"""Turn a message from the web frontend into a chat message for the engine.

This is the web twin of the Telegram dispatcher's message handler: clean
the text, check the rate limit, save the message, then hand it to the
engine. From there on, a web message is handled exactly like a Telegram DM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field

from ..db.messages import insert_message, upsert_user
from ..models import ChatMessage
from ..utils.input_normalizer import clean_inbound
from .chat_ids import web_chat_id
from .hub import WebHub

if TYPE_CHECKING:  # pragma: no cover
    from ..db.database import Database
    from ..rate_limiter import RateLimiter

log = logging.getLogger("hamroh.web")


class EnginePort(Protocol):
    """The part of the engine the web channel uses."""

    async def submit(self, msg: ChatMessage) -> None: ...

    def prime_typing(self, chat_id: int) -> None: ...


class WebMessageIn(BaseModel):
    """A message the frontend posted on behalf of one of its users."""

    #: The frontend's own id for the user (any string). One user = one chat.
    user: str = Field(min_length=1, max_length=200)
    #: Same cap as a Telegram message.
    text: str = Field(min_length=1, max_length=4096)
    #: Display name the bot sees. Defaults to ``user``.
    name: str | None = Field(default=None, max_length=64)
    #: Message id in the same chat this message replies to.
    reply_to_id: int | None = None


@dataclass(frozen=True)
class WebInboundDeps:
    """Shared services the web inbound path needs."""

    db: "Database"
    engine: EnginePort
    hub: WebHub
    chat_titles: dict[int, str]
    rate_limiter: "RateLimiter | None" = None


class WebInbound:
    """Accepts web messages and feeds them to the engine."""

    def __init__(self, deps: WebInboundDeps) -> None:
        self._deps = deps

    async def accept(self, incoming: WebMessageIn) -> ChatMessage:
        """Save ``incoming`` and submit it to the engine.

        Raises :class:`~hamroh.rate_limiter.RateLimitExceeded` when the user
        sends too fast. Returns the stored message (with its new id).
        """
        chat_id = web_chat_id(incoming.user)
        if self._deps.rate_limiter is not None:
            await self._deps.rate_limiter.check_and_record(chat_id)
        msg = await self._to_chat_message(chat_id, incoming)
        self._deps.chat_titles[chat_id] = f"web:{msg.first_name}"
        await insert_message(self._deps.db, msg)
        await upsert_user(self._deps.db, msg)
        log.info("web inbound chat=%s msg=%s", chat_id, msg.message_id)
        self._deps.engine.prime_typing(chat_id)
        await self._deps.engine.submit(msg)
        return msg

    async def _to_chat_message(
        self, chat_id: int, incoming: WebMessageIn
    ) -> ChatMessage:
        """Build the engine's message shape. The chat id doubles as the user
        id, the same way a Telegram DM's chat id equals the user's id."""
        text, flags = clean_inbound(incoming.text)
        name, _ = clean_inbound(incoming.name or incoming.user)
        return ChatMessage(
            chat_id=chat_id,
            message_id=await self._deps.hub.next_message_id(chat_id),
            user_id=chat_id,
            first_name=name,
            direction="in",
            timestamp=datetime.now(timezone.utc),
            text=text or "",
            reply_to_id=incoming.reply_to_id,
            input_flags=flags,
        )
