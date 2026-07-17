"""Builders for owner-facing references to the message that caused an error.

Every reference names the message and its sender and quotes the text, so the
owner sees what happened and to whom. Group messages additionally name the
group (title + id) and carry a shareable ``t.me/c`` deep link; DMs and basic
groups have no such link, so the quoted text is all the owner gets to click.

The refs are HTML — the owner DM is sent with Telegram's HTML parse mode so the
quoted message renders as a real blockquote — so every dynamic field (sender,
group title, message text) is escaped to keep stray ``<`` from breaking markup.
"""

from __future__ import annotations

import html
from typing import Mapping

from ..models import ChatMessage

#: Supergroup/channel chat ids are the internal id with a ``-100`` prefix.
_SUPERGROUP_PREFIX = "-100"

#: A quoted message is capped to this many chars so one long message can't
#: push the owner DM past Telegram's send limit.
_MAX_QUOTE = 500


def message_link(chat_id: int, message_id: int) -> str | None:
    """A ``https://t.me/c/<internal>/<msg>`` deep link for a supergroup or
    channel message (``-100…`` ids). Returns None for DMs, which have no
    shareable message link."""
    text = str(chat_id)
    if not text.startswith(_SUPERGROUP_PREFIX):
        return None
    return f"https://t.me/c/{text[len(_SUPERGROUP_PREFIX) :]}/{message_id}"


def message_ref(msg: ChatMessage, chat_title: str | None = None) -> str:
    """An HTML reference to the message that triggered an error: which message,
    who sent it, and the quoted text. Group messages also name the group (title
    + id) and, for supergroups, a shareable deep link the owner can click."""
    header = f"• message {msg.message_id} from {html.escape(_sender_label(msg))}"
    if msg.chat_id < 0:  # any group; DMs are positive and need no group label
        header += f" in {html.escape(_group_label(msg.chat_id, chat_title))}"
    link = message_link(msg.chat_id, msg.message_id)
    if link is not None:
        header += f": {html.escape(link)}"
    return f"{header}\n{_quote(msg.text)}"


def format_message_refs(
    targets: Mapping[int, ChatMessage],
    chat_titles: Mapping[int, str] | None = None,
) -> str:
    """One :func:`message_ref` entry per triggering message, sorted by chat for
    a stable order. ``chat_titles`` maps chat id to display name so group refs
    can show it. Empty string when there are no targets, so callers can append
    it unconditionally."""
    titles = chat_titles or {}
    return "\n".join(
        message_ref(targets[chat], titles.get(chat)) for chat in sorted(targets)
    )


def _sender_label(msg: ChatMessage) -> str:
    """Human handle for the sender: @username or first name, plus the id."""
    name = f"@{msg.username}" if msg.username else (msg.first_name or "unknown")
    return f"{name} (id {msg.user_id})"


def _group_label(chat_id: int, title: str | None) -> str:
    """The group's title (when known) plus its id."""
    return f'"{title}" (id {chat_id})' if title else f"(id {chat_id})"


def _quote(text: str) -> str:
    """The message text as an HTML blockquote, capped at :data:`_MAX_QUOTE`
    chars (truncated before escaping, so no entity is split)."""
    text = text.strip()
    if len(text) > _MAX_QUOTE:
        text = text[:_MAX_QUOTE].rstrip() + "…"
    return f"<blockquote>{html.escape(text) or '(no text)'}</blockquote>"
