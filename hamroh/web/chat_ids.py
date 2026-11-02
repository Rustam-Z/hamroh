"""Chat ids for web users.

Telegram user ids use at most 52 bits, so every id from ``2**52`` up is
free. Web chats live there: ``2**52 <= id < 2**53``. The upper bound keeps
ids exact in JavaScript numbers, so a browser can pass them around safely.
"""

from __future__ import annotations

import hashlib

#: First chat id reserved for web users. Below it, ids belong to Telegram.
WEB_CHAT_ID_BASE = 2**52


def is_web_chat(chat_id: int) -> bool:
    """True when ``chat_id`` belongs to a web user, not a Telegram chat."""
    return chat_id >= WEB_CHAT_ID_BASE


def web_chat_id(user_key: str) -> int:
    """Stable chat id for a web user, derived from the frontend's user key.

    The same key always maps to the same chat, so history survives restarts
    without a mapping table.
    """
    digest = hashlib.sha256(user_key.encode("utf-8")).digest()
    return WEB_CHAT_ID_BASE + int.from_bytes(digest[:8], "big") % WEB_CHAT_ID_BASE
