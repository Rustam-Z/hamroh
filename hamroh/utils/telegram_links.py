"""Pure builders for Telegram deep links."""

from __future__ import annotations

#: Supergroup/channel chat ids are the internal id with a ``-100`` prefix.
_SUPERGROUP_PREFIX = "-100"


def message_link(chat_id: int, message_id: int) -> str | None:
    """A ``https://t.me/c/<internal>/<msg>`` deep link for a supergroup or
    channel message (``-100…`` ids). Returns None for DMs, which have no
    shareable message link."""
    text = str(chat_id)
    if not text.startswith(_SUPERGROUP_PREFIX):
        return None
    return f"https://t.me/c/{text[len(_SUPERGROUP_PREFIX) :]}/{message_id}"


def message_ref(chat_id: int, message_id: int) -> str:
    """A one-line reference to a message: its id plus a deep link when one
    exists (falls back to the bare id for DMs)."""
    link = message_link(chat_id, message_id)
    return f"• message {message_id}: {link}" if link else f"• message {message_id}"
