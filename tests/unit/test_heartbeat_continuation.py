"""Non-terminal ``heartbeat`` action — the model posts a status and keeps working.

When a turn ends with ``action="heartbeat"`` the engine must NOT finish the
turn: it re-engages the same CC session so the model picks its task back up.
A consecutive-heartbeat cap stops a misbehaving model from spinning the loop
forever. Success callbacks and the ``processed`` commit are deferred to the
final ``stop`` so a crash mid-continuation still replays the work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.engine import (
    HEARTBEAT_CONTINUE_NUDGE,
    MAX_HEARTBEAT_CONTINUATIONS,
)
from hamroh.models import ChatMessage, ControlAction

_CFG = Config.for_test(Path("/tmp"))


def _msg(text: str, mid: int) -> ChatMessage:
    return ChatMessage(
        chat_id=-100,
        message_id=mid,
        user_id=42,
        username="alice",
        first_name="Alice",
        direction="in",
        timestamp=datetime(2026, 4, 11, 10, 31, tzinfo=timezone.utc),
        text=text,
    )


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.injected: list[str] = []
        self._results: asyncio.Queue[TurnResult] = asyncio.Queue()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def inject(self, text: str) -> None:
        self.injected.append(text)

    async def wait_for_result(self) -> TurnResult:
        return await self._results.get()

    def feed(self, result: TurnResult) -> None:
        self._results.put_nowait(result)


def _heartbeat() -> TurnResult:
    return TurnResult(
        text_blocks=[],
        control=ControlAction(action="heartbeat"),
        dropped_text=False,
    )


def _stop() -> TurnResult:
    # user_visible_action=True: a clean stop implies the model delivered its
    # reply via telegram_send_message — otherwise the silent-stop nudge fires.
    return TurnResult(
        text_blocks=[],
        control=ControlAction(action="stop", reason="done"),
        user_visible_action=True,
        dropped_text=False,
    )


@pytest.mark.asyncio
async def test_heartbeat_re_engages_without_ending_turn() -> None:
    """The headline behaviour: a ``heartbeat`` result resumes the same session
    with a continuation nudge and the turn keeps running."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        # Given a user message that starts a turn
        await eng.submit(_msg("research X", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1, "the user turn was handed to the worker"

        # When the turn ends with a non-terminal heartbeat
        worker.feed(_heartbeat())
        await asyncio.sleep(0.05)

        # Then the worker is re-engaged with the continuation nudge and the
        # engine still considers itself processing (turn not finished).
        assert worker.sent[-1] == HEARTBEAT_CONTINUE_NUDGE, "expected resume nudge"
        assert len(worker.sent) == 2, "the turn must continue, not end"
        assert eng.turn_elapsed_s is not None, "the turn must still be running"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_success_callback_fires_only_on_final_stop() -> None:
    """Heartbeat continuations defer the success callback to the real stop."""
    worker = FakeWorker()
    fired: list[int] = []

    async def on_success() -> None:
        fired.append(1)

    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        await eng.submit(_msg("long task", mid=1), on_success=on_success)
        await asyncio.sleep(0.08)

        # First the model checkpoints with a heartbeat — callback must NOT fire.
        worker.feed(_heartbeat())
        await asyncio.sleep(0.05)
        assert fired == [], "success callback fired before the turn finished"

        # Then it really stops — now the callback fires exactly once.
        worker.feed(_stop())
        await asyncio.sleep(0.05)
        assert fired == [1], "success callback must fire once on the final stop"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_pending_messages_folded_into_continuation() -> None:
    """A message that lands during the heartbeat window joins the continuation
    instead of waiting for the next turn."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=5000))
    await eng.start()
    try:
        await eng.submit(_msg("start", mid=1))
        await asyncio.sleep(0.02)
        # The debounce is long; force the first turn directly.
        await eng._kick()
        assert len(worker.sent) == 1

        # Buffer a follow-up, then deliver the heartbeat. _maybe_inject is not
        # taken because the engine is briefly idle between turns, so the
        # message sits in _pending and the heartbeat must fold it in.
        async with eng._lock:
            eng._pending.append(_msg("also check Y", mid=2))
        worker.feed(_heartbeat())
        await asyncio.sleep(0.05)

        assert "also check Y" in worker.sent[-1], "pending message folded in"
        assert worker.sent[-1] != HEARTBEAT_CONTINUE_NUDGE
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_runaway_heartbeats_are_capped() -> None:
    """A model that always returns heartbeat is finalized at the cap instead of
    spinning the control loop forever."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        await eng.submit(_msg("loop", mid=1), on_success=None)
        await asyncio.sleep(0.08)

        # Feed one more heartbeat than the cap allows.
        for _ in range(MAX_HEARTBEAT_CONTINUATIONS + 1):
            worker.feed(_heartbeat())
            await asyncio.sleep(0.02)

        # Exactly the cap's worth of continuations were sent (plus the first
        # user turn); the over-cap heartbeat finalized the turn.
        assert len(worker.sent) == 1 + MAX_HEARTBEAT_CONTINUATIONS, (
            f"continuations not capped; sent={len(worker.sent)}"
        )
        assert eng.turn_elapsed_s is None, "turn must be finalized past the cap"
    finally:
        await eng.stop()
