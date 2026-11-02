"""One Claude session per private chat, one shared session for the rest.

``data/sessions.json`` maps a session key to the Claude session id last seen
for it, so each private chat resumes its own conversation after a switch or a
restart. The shared session keeps using ``data/session_id`` as before.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..models import ChatMessage

log = logging.getLogger(__name__)

#: Session key of every non-private chat.
SHARED = "shared"


def session_key(msg: ChatMessage) -> str:
    """``chat:<id>`` for a private chat's message, :data:`SHARED` otherwise."""
    return f"chat:{msg.chat_id}" if msg.private else SHARED


class SessionStore:
    """Private chats' Claude session ids, persisted as a small JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._ids: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """Read the file; a missing or corrupt file means no sessions yet."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("sessions file %s unreadable — starting fresh", self._path)
            return {}
        return {
            k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)
        }

    def get(self, key: str) -> str | None:
        """The session id saved for ``key``, if any."""
        return self._ids.get(key)

    def put(self, key: str, session_id: str | None) -> None:
        """Remember ``session_id`` for ``key`` (``None`` forgets it) and save."""
        if session_id is None:
            self._ids.pop(key, None)
        else:
            self._ids[key] = session_id
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
