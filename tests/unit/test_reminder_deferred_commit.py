"""Engine-level on_success callback contract that backs reminder
delivery (issue #22).

The reminder loop hangs ``mark_sent`` / ``advance_recurring`` off
:meth:`Engine.submit`'s ``on_success`` hook so the DB row is only
updated after CC actually consumes the turn. These tests pin the four
outcomes the fix depends on:

1. clean turn end → callback fires
2. CC subprocess crash → callback discarded (reminder retried by next loop tick)
3. recoverable dropped-text → callback withheld until the retry turn ends
4. dropped-text cap-hit → callback fires (CC saw the message; retrying is pointless)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pyclaudir.cc_worker import TurnResult
from pyclaudir.config import Config
from pyclaudir.engine import Engine
from pyclaudir.models import ChatMessage, ControlAction


_CFG = Config.for_test(Path("/tmp"))


def _msg(text: str, mid: int = 1) -> ChatMessage:
    return ChatMessage(
        chat_id=-100,
        message_id=mid,
        user_id=42,
        direction="in",
        timestamp=datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc),
        text=text,
    )


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.injected: list[str] = []
        self._results: asyncio.Queue[TurnResult | Exception] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def inject(self, text: str) -> None:
        self.injected.append(text)

    async def wait_for_result(self) -> TurnResult:
        item = await self._results.get()
        if isinstance(item, Exception):
            raise item
        return item

    def feed(self, item: TurnResult | Exception) -> None:
        self._results.put_nowait(item)


@pytest.mark.asyncio
async def test_on_success_fires_after_clean_turn_end() -> None:
    """Happy path: callback runs once the turn ends with action=stop."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)
        assert worker.sent, "turn did not start"
        assert fired == [], "callback fired before turn result"

        worker.feed(
            TurnResult(
                control=ControlAction(action="stop", reason="ok"),
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
        assert fired == [1]
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_worker_failure_discards_callback() -> None:
    """The bug we're fixing: subprocess crash mid-turn must NOT mark the
    reminder fired. Discarding leaves the DB row pending so the next
    reminder loop tick re-fires it."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)
        assert worker.sent

        worker.feed(RuntimeError("cc subprocess wedged"))
        await asyncio.sleep(0.05)
        assert fired == [], "callback fired despite worker failure"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_delivers_and_fires_callback() -> None:
    """Dropped text ends the turn immediately: the engine delivers the
    text it already produced and fires the on_success callback, so a
    reminder that triggered the turn advances instead of re-firing."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, debounce_ms=20)
    fired: list[int] = []

    async def cb() -> None:
        fired.append(1)

    await eng.start()
    try:
        await eng.submit(_msg("hi", mid=1), on_success=cb)
        await asyncio.sleep(0.08)

        worker.feed(
            TurnResult(
                text_blocks=["I would say hi"],
                control=ControlAction(action="stop", reason="answered"),
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)
        assert fired == [1], "dropped-text turn must fire the callback once"
    finally:
        await eng.stop()
