"""Put the Claude worker in the right session before each turn.

The worker runs one ``claude`` process at a time. When the next turn belongs to
another session — a different private chat, or back to the shared one — the
switcher saves the current session id, respawns the worker with the target
session and its tool profile, and binds :class:`TurnContext` to the turn's
person. Turns in the same session cost nothing extra.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..models import ChatMessage
from ..web.chat_ids import is_web_chat
from .sessions import SHARED, SessionStore, session_key
from .turn import TurnContext

if TYPE_CHECKING:  # pragma: no cover
    from ..cc_worker.spec import CcSpawnSpec

log = logging.getLogger(__name__)


class SwitchableWorker(Protocol):
    """The part of ``CcWorker`` the switcher uses."""

    spec: "CcSpawnSpec"

    @property
    def session_id(self) -> str | None: ...

    async def switch_session(self, spec: "CcSpawnSpec") -> None: ...


@dataclass(frozen=True)
class SessionSetup:
    """What the switcher works with: the two spawn profiles (the shared
    session's and a private chat's), the private sessions' store, and the
    turn context tools read."""

    shared: "CcSpawnSpec"
    private: "CcSpawnSpec"
    store: SessionStore
    turn: TurnContext


class SessionSwitcher:
    """Owns which session the worker is in and who the turn belongs to."""

    def __init__(self, worker: SwitchableWorker, setup: SessionSetup) -> None:
        self._worker = worker
        self._specs = setup
        self._store = setup.store
        self._turn = setup.turn
        self._current = SHARED
        self._shared_id: str | None = worker.spec.session_id

    @property
    def current(self) -> str:
        """Key of the session the worker is in now."""
        return self._current

    async def enter(self, batch: list[ChatMessage]) -> None:
        """Switch to ``batch``'s session if needed and bind the turn's person."""
        first = batch[0]
        key = session_key(first)
        if key != self._current:
            self.remember()
            await self._worker.switch_session(self._spec_for(key))
            log.info("session switch %s -> %s", self._current, key)
            self._current = key
        if first.private:
            web_user = first.username if is_web_chat(first.chat_id) else None
            self._turn.enter_private(first.chat_id, first.user_id, web_user)
        else:
            self._turn.enter_shared()

    def remember(self) -> None:
        """Save the worker's live session id under the current key."""
        session_id = self._worker.session_id
        if self._current == SHARED:
            self._shared_id = session_id
        else:
            self._store.put(self._current, session_id)

    def persist_shared(self, path: Path) -> None:
        """Write the shared session id where the next start looks for it."""
        self.remember()
        if self._shared_id:
            path.write_text(self._shared_id)

    def _spec_for(self, key: str) -> "CcSpawnSpec":
        if key == SHARED:
            return dataclasses.replace(self._specs.shared, session_id=self._shared_id)
        return dataclasses.replace(self._specs.private, session_id=self._store.get(key))
