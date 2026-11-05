"""Who the current turn belongs to, and which tools it may call.

The engine fills :class:`TurnContext` before every turn; tools read it from
``ToolContext.turn`` instead of trusting ids the model passes. That is what
lets a tool act *for the person asking* and nobody else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Where a tool may run: shared chats only (the default), private chats only,
#: or both (sending a reply, reacting, the clock).
Audience = Literal["shared", "private", "both"]


@dataclass
class TurnContext:
    """The turn in flight. Mutated in place by the engine, read by tools."""

    #: True when the turn is a private chat's (exactly one chat).
    private: bool = False
    #: The private chat's id; ``None`` in shared turns (they may mix chats).
    chat_id: int | None = None
    #: Telegram user id of the sender, or the web chat id for a web user.
    user_id: int | None = None
    #: The web frontend's own id for the person (``user`` in the web API);
    #: ``None`` for Telegram.
    web_user: str | None = None

    def enter_shared(self) -> None:
        """Forget any private identity: the next turn is a shared one."""
        self.private, self.chat_id, self.user_id, self.web_user = (
            False,
            None,
            None,
            None,
        )

    def enter_private(self, chat_id: int, user_id: int, web_user: str | None) -> None:
        """Bind the turn to one private chat and its person."""
        self.private, self.chat_id, self.user_id, self.web_user = (
            True,
            chat_id,
            user_id,
            web_user,
        )


def call_refusal(audience: Audience, turn: TurnContext) -> str | None:
    """Why a tool with ``audience`` may not run in ``turn``; ``None`` when it may.

    The private session is spawned without the forbidden tools already; this
    is the second lock, enforced in our own process.
    """
    if turn.private and audience == "shared":
        return "This tool is not available in a private chat."
    if not turn.private and audience == "private":
        return "This tool works only in a private chat with a verified person."
    return None


def reply_refusal(turn: TurnContext, chat_id: int) -> str | None:
    """In a private turn the bot may only answer the chat that asked."""
    if turn.private and chat_id != turn.chat_id:
        return "In a private chat you can only reply to that chat."
    return None
