"""In-memory hub that delivers the bot's output to connected web clients.

Each open event stream (one browser tab) subscribes to its chat and gets a
queue. When the bot replies, reacts or is typing in a web chat, the event
is put on every queue of that chat. Nothing is buffered for offline
clients — messages are in the database, so a client that reconnects reads
``/api/history`` to catch up.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from ..db.database import Database

EventKind = Literal["message", "typing", "reaction"]


@dataclass(frozen=True)
class WebEvent:
    """One thing the bot did in a web chat, sent to the browser as JSON."""

    kind: EventKind
    chat_id: int
    message_id: int | None = None
    text: str | None = None
    reply_to_id: int | None = None
    emoji: str | None = None

    def to_json(self) -> str:
        """JSON body for the event stream, without empty fields."""
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None})


class WebHub:
    """Fan-out of bot events to web clients, plus message id allocation."""

    def __init__(self, db: "Database | None" = None) -> None:
        self._db = db
        self._subscribers: dict[int, set[asyncio.Queue[WebEvent]]] = {}
        #: Last message id used per chat. Loaded from the database the first
        #: time a chat is seen, then counted up in memory.
        self._last_ids: dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def next_message_id(self, chat_id: int) -> int:
        """Next free message id in ``chat_id``.

        Telegram hands out message ids; for web chats we do it ourselves.
        User and bot messages share one counter, like in Telegram.
        """
        async with self._lock:
            if chat_id not in self._last_ids:
                self._last_ids[chat_id] = await self._max_stored_id(chat_id)
            self._last_ids[chat_id] += 1
            return self._last_ids[chat_id]

    async def _max_stored_id(self, chat_id: int) -> int:
        """Highest message id already saved for ``chat_id`` (0 if none)."""
        if self._db is None:
            return 0
        row = await self._db.fetch_one(
            "SELECT COALESCE(MAX(message_id), 0) AS last FROM messages "
            "WHERE chat_id = ?",
            (chat_id,),
        )
        return int(row["last"]) if row is not None else 0

    def subscribe(self, chat_id: int) -> asyncio.Queue[WebEvent]:
        """Start receiving events for ``chat_id``. Pair with :meth:`unsubscribe`."""
        queue: asyncio.Queue[WebEvent] = asyncio.Queue()
        self._subscribers.setdefault(chat_id, set()).add(queue)
        return queue

    def unsubscribe(self, chat_id: int, queue: asyncio.Queue[WebEvent]) -> None:
        """Stop sending events to ``queue`` (the client disconnected)."""
        queues = self._subscribers.get(chat_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._subscribers[chat_id]

    def publish(self, event: WebEvent) -> None:
        """Send ``event`` to every client watching its chat."""
        for queue in self._subscribers.get(event.chat_id, ()):
            queue.put_nowait(event)
