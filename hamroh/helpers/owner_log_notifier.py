"""Forward serious log records to the bot owner over Telegram.

A single :class:`OwnerLogHandler` on the root logger turns every
``log.error(...)`` anywhere in the codebase into an owner DM, so operator
problems surface in chat instead of only in ``docker logs``. There is one
authoritative place to change what the owner sees, rather than a notify call
scattered next to every log site.

Every DM ends with a deep link to the message that caused the error (the
one the current turn is answering), so the owner can jump straight to it.
Transient, self-healing conditions stay below ERROR (e.g. CC's own
``api_retry`` on an overloaded API) and are deliberately not forwarded.

Flooding is contained two ways: identical messages are suppressed for
``cooldown_s`` after the first, and the handler never forwards a record
produced while it is itself sending (reentrancy guard), so a failed send
cannot cascade into more owner DMs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

#: Async function that delivers one text message to the owner.
OwnerSend = Callable[[str], Awaitable[None]]

#: Returns deep-link lines for the message(s) that the current turn is
#: answering — the ones any error was, in effect, caused by. Empty when idle.
LinkProvider = Callable[[], str]

#: Owner messages longer than this are truncated — a DM, not a log dump.
_MAX_LEN = 1200

#: An identical message is suppressed for this many seconds after the first,
#: so one repeating fault can't flood the owner's DMs.
_COOLDOWN_S = 60.0


class OwnerLogHandler(logging.Handler):
    """Root-logger handler that DMs the owner on ERROR-and-above records."""

    def __init__(
        self,
        send: OwnerSend,
        loop: asyncio.AbstractEventLoop,
        *,
        link_provider: LinkProvider | None = None,
    ) -> None:
        """:param send: delivers one message to the owner (must not raise).
        :param loop: the running event loop to schedule sends on.
        :param link_provider: supplies a link to the message that caused the
            error, appended to every DM so the owner can jump straight to it.
        """
        super().__init__(level=logging.ERROR)
        self._send = send
        self._loop = loop
        self._link_provider = link_provider
        self._last_sent: dict[str, float] = {}
        self._sending = False

    def emit(self, record: logging.LogRecord) -> None:
        """Schedule an owner DM for this record, if it clears the guards."""
        if self._sending:
            return  # a send is in flight; its own logs must not loop back
        text = self._with_cause_link(_format_record(record))
        if self._is_duplicate(text, record.created):
            return
        self._last_sent[text] = record.created
        try:
            self._loop.call_soon_threadsafe(self._spawn, text)
        except RuntimeError:
            pass  # loop already closed during shutdown — nothing to do

    def _with_cause_link(self, text: str) -> str:
        """Append a deep link to the message that caused this error, if any."""
        if self._link_provider is None:
            return text
        refs = self._link_provider()
        return f"{text}\n\n{refs}" if refs else text

    def _is_duplicate(self, text: str, now: float) -> bool:
        """True if ``text`` was already sent within the cooldown window.

        Expired entries are pruned here so the dedup map stays bounded.
        """
        self._last_sent = {
            msg: ts for msg, ts in self._last_sent.items() if now - ts < _COOLDOWN_S
        }
        return text in self._last_sent

    def _spawn(self, text: str) -> None:
        """Runs in the loop thread: launch the delivery coroutine."""
        self._loop.create_task(self._deliver(text))

    async def _deliver(self, text: str) -> None:
        """Send one owner DM, guarding against re-entrant forwarding."""
        self._sending = True
        try:
            await self._send(text)
        finally:
            self._sending = False


def _format_record(record: logging.LogRecord) -> str:
    """Render a log record as a short, owner-facing message."""
    icon = "🔴" if record.levelno >= logging.CRITICAL else "⚠️"
    body = f"{icon} {record.levelname} — {record.name}\n{record.getMessage()}"
    if record.exc_info:
        body = f"{body}\n\n{logging.Formatter().formatException(record.exc_info)}"
    if len(body) > _MAX_LEN:
        body = body[:_MAX_LEN].rstrip() + "…"
    return body


def attach_owner_log_notifier(
    send: OwnerSend,
    loop: asyncio.AbstractEventLoop,
    *,
    link_provider: LinkProvider | None = None,
) -> OwnerLogHandler:
    """Install an :class:`OwnerLogHandler` on the root logger and return it."""
    handler = OwnerLogHandler(send, loop, link_provider=link_provider)
    logging.getLogger().addHandler(handler)
    return handler
