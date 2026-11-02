"""Telegram users whose DMs are private chats, and public commands for extensions.

An extension decides who is a *private user* (for example, the staff of a
business who linked their Telegram account to it): their DMs skip
``access.json`` — the extension vouches for them — and run as private chats
(see :mod:`hamroh.privacy`). Groups are never private.

A *public command* is a ``/command`` anyone may send in a DM before they have
access — the way a person links their account (``/start <code>``). It never
reaches the engine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from telegram.ext import CommandHandler, filters

from .commands import CommandHandlerFn, OwnerCommand

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import Application

    from ..db.database import Database

#: ``await check(telegram_user_id)`` → whether that user's DMs are private chats.
PrivateUserCheck = Callable[[int], Awaitable[bool]]


class PrivateUsersMixin:
    """Mixed into ``TelegramDispatcher``; its ``__init__`` sets the attributes."""

    db: "Database"
    private_db: "Database | None"
    application: "Application"
    private_user_check: PrivateUserCheck | None
    public_commands: list[OwnerCommand]
    #: The dispatcher's ``access.json`` gate, used by :meth:`_admit`.
    _is_allowed: Callable[[int, int, str | None], bool]

    def set_private_user_check(self, check: PrivateUserCheck) -> None:
        """Let an extension name the users whose DMs are private chats."""
        self.private_user_check = check

    def add_public_command(
        self, name: str, description: str, handler: CommandHandlerFn
    ) -> None:
        """Add a DM ``/command`` anyone may send (wired in ``start()``)."""
        self.public_commands.append(OwnerCommand(name, description, handler))

    def _wire_public_commands(self) -> None:
        """Register public commands ahead of the catch-all handler, DMs only."""
        for command in self.public_commands:
            self.application.add_handler(
                CommandHandler(
                    command.name, command.handler, filters=filters.ChatType.PRIVATE
                )
            )

    async def _is_private_user(self, user_id: int, chat_type: str | None) -> bool:
        """True when ``user_id``'s DM is a private chat."""
        if chat_type != "private" or self.private_user_check is None:
            return False
        return await self.private_user_check(user_id)

    async def _admit(
        self, chat_id: int, user_id: int, chat_type: str | None
    ) -> bool | None:
        """Gate an edit or reaction: ``True`` for a private user's DM, ``False``
        for a chat ``access.json`` allows, ``None`` to drop it."""
        if await self._is_private_user(user_id, chat_type):
            return True
        if self._is_allowed(chat_id, user_id, chat_type):
            return False
        return None

    def _db_for(self, private: bool) -> "Database":
        """The database a chat's rows go to."""
        if private and self.private_db is not None:
            return self.private_db
        return self.db
