"""Which pending messages go into the next turn.

A turn belongs to one session (see :func:`hamroh.privacy.sessions.session_key`):
the shared session takes any non-private chats together, as before; a private
chat's messages go in a turn of their own. Callbacks travel with the key of the
message they were submitted with, so a reminder's hooks never fire on a private
chat's turn.
"""

from __future__ import annotations

from typing import TypeVar

from ..models import ChatMessage
from ..privacy.sessions import session_key

T = TypeVar("T")


def split_batch(
    pending: list[ChatMessage],
) -> tuple[list[ChatMessage], list[ChatMessage]]:
    """``(batch, rest)``: the messages sharing the first message's session, and the others."""
    if not pending:
        return [], []
    key = session_key(pending[0])
    return split_by_key(pending, key)


def split_by_key(
    pending: list[ChatMessage], key: str
) -> tuple[list[ChatMessage], list[ChatMessage]]:
    """``(same, other)``: messages whose session is ``key``, and the rest, order kept."""
    same = [m for m in pending if session_key(m) == key]
    other = [m for m in pending if session_key(m) != key]
    return same, other


def split_keyed(
    items: list[tuple[str, T]], key: str
) -> tuple[list[T], list[tuple[str, T]]]:
    """``(values for key, remaining items)`` from ``(key, value)`` pairs."""
    mine = [value for k, value in items if k == key]
    rest = [(k, value) for k, value in items if k != key]
    return mine, rest
