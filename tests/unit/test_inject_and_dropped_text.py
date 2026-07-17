"""Step 9 invariants: inject mechanism and dropped-text detection."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal

import pytest

from pathlib import Path

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.engine import SILENT_STOP_NUDGE
from hamroh.models import ChatMessage, ControlAction


_CFG = Config.for_test(Path("/tmp"))


def _msg(text: str, mid: int, chat_id: int = -100) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
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


@pytest.mark.asyncio
async def test_dropped_text_delivers_answer_to_user() -> None:
    """A turn that ends with a text block but no ``telegram_send_message`` must
    deliver that text to the waiting chat — not burn a retry turn nagging
    the model to resend. It must also thread to the message that kicked the
    turn — the model never got to call ``telegram_reply_to_message`` itself
    (that's the whole reason this recovery path exists), so the engine
    threads on its behalf instead of the recovered text landing unthreaded."""
    worker = FakeWorker()
    delivered: list[tuple[int, str, int | None]] = []

    async def capture(chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        delivered.append((chat_id, text, reply_to_message_id))

    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, error_notify=capture))
    await eng.start()
    try:
        # Given a user message that starts a turn
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1, "the user turn was handed to the worker"

        # When the turn ends with a text block but no telegram_send_message call
        worker.feed(
            TurnResult(
                text_blocks=["Here is your answer"],
                control=ControlAction(action="stop", reason="answered"),
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)

        # Then the text is delivered as-is to the waiting chat, threaded to
        # the message that kicked the turn (mid=1), and no corrective
        # message is re-sent to the worker (no wasted retry).
        assert delivered == [(-100, "Here is your answer", 1)], (
            f"answer was not delivered in-thread to the user; got {delivered!r}"
        )
        assert len(worker.sent) == 1, "a retry turn was wrongly kicked into the worker"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_dropped_text_operator_failure_alerts_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the dropped text is actually an operator failure (e.g. a bad
    model name), the classified guidance is logged at ERROR — the root
    OwnerLogHandler DMs it to the owner alone with the diagnostic snippet —
    rather than echoed to the chat as if it were a real answer. The owner
    delivery + message link are covered in ``test_owner_log_notifier``."""
    worker = FakeWorker()
    delivered: list[tuple[int, str]] = []

    async def capture(chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        delivered.append((chat_id, text))

    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, error_notify=capture))
    await eng.start()
    try:
        # Given a turn whose only text block is a model-access error
        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)
        diagnostic = (
            "There's an issue with the selected model (claude-sonnet-4-7). "
            "It may not exist or you may not have access to it."
        )

        # When that turn is reported as dropped text
        with caplog.at_level(logging.ERROR):
            worker.feed(
                TurnResult(text_blocks=[diagnostic], control=None, dropped_text=True)
            )
            await asyncio.sleep(0.05)

        # Then nothing is echoed to the chat, and the owner is alerted with
        # the classified guidance and the diagnostic snippet.
        assert delivered == [], "operator failures must not reach the chat"
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, f"exactly one owner alert, got {errors}"
        assert "hamroh_model" in errors[0].lower(), "classified guidance shown to owner"
        assert "claude-sonnet-4-7" in errors[0], "diagnostic snippet preserved"
        assert len(worker.sent) == 1, "no retry turn kicked into the worker"
    finally:
        await eng.stop()


def _silent(action: Literal["stop", "skip"], reason: str) -> TurnResult:
    """A terminal turn that delivered nothing — no text block, no
    user-visible tool call."""
    return TurnResult(
        text_blocks=[],
        control=ControlAction(action=action, reason=reason),
        user_visible_action=False,
        dropped_text=False,
    )


@pytest.mark.asyncio
async def test_silent_stop_in_dm_reengages_once() -> None:
    """A DM turn that ends ``stop`` having delivered nothing is a premature
    turn end (``stop`` promises a reply was sent) — the engine re-engages
    once with the corrective nudge, and only once, so a persistently silent
    model can't loop."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        # Given a direct message (chat_id > 0) that starts a turn
        await eng.submit(_msg("ping", mid=1, chat_id=42))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1, "the user turn was handed to the worker"

        # When the turn ends stop having delivered nothing
        worker.feed(_silent("stop", "need to reply first"))
        await asyncio.sleep(0.05)

        # Then the model is re-engaged exactly once with the corrective nudge
        assert len(worker.sent) == 2, "silent stop in a DM must re-engage the model"
        assert worker.sent[1] == SILENT_STOP_NUDGE, "corrective nudge was sent"

        # And when it stays silent, the turn finishes without a second retry
        worker.feed(_silent("stop", "still nothing"))
        await asyncio.sleep(0.05)
        assert len(worker.sent) == 2, "re-engagement is bounded to a single retry"
        assert eng.turn_elapsed_s is None, "turn must finish clean after the retry"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_skip_in_dm_finishes_clean() -> None:
    """``skip`` is the model's explicit "deliberately not replying" signal
    (e.g. the user asked for no reply) — the engine must finish the turn
    clean without re-engaging, even in a DM."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        # Given a direct message (chat_id > 0) that starts a turn
        await eng.submit(_msg("please don't respond", mid=1, chat_id=42))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1, "the user turn was handed to the worker"

        # When the turn ends with a deliberate skip
        worker.feed(_silent("skip", "user asked no reply"))
        await asyncio.sleep(0.05)

        # Then the turn finishes clean — silence is respected, no retry turn
        assert len(worker.sent) == 1, "skip must not re-engage the model"
        assert eng.turn_elapsed_s is None, "turn must finish clean after skip"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_silent_stop_in_group_reengages_once() -> None:
    """A silent ``stop`` in a group is the same contract violation as in a
    DM (``stop`` promises a reply was sent; deliberate silence is ``skip``)
    — the model gets the one-shot nudge, and a ``skip`` answer finishes the
    turn clean without forcing a reply onto group chatter."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        # Given a group message (chat_id < 0) that starts a turn
        await eng.submit(_msg("hi all", mid=1, chat_id=-100))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # When the turn ends stop having delivered nothing
        worker.feed(_silent("stop", "group chatter"))
        await asyncio.sleep(0.05)

        # Then the model is re-engaged exactly once with the corrective nudge
        assert len(worker.sent) == 2, "silent stop in a group must re-engage"
        assert worker.sent[1] == SILENT_STOP_NUDGE, "corrective nudge was sent"

        # And a skip answer to the nudge finishes the turn clean
        worker.feed(_silent("skip", "chatter, nothing to add"))
        await asyncio.sleep(0.05)
        assert len(worker.sent) == 2, "skip after the nudge must end the turn"
        assert eng.turn_elapsed_s is None, "turn must finish clean after skip"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_skip_in_group_finishes_clean() -> None:
    """Group chatter the model deliberately ignores ends with ``skip`` —
    the turn finishes clean, no nudge, no forced reply."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        # Given a group message that starts a turn
        await eng.submit(_msg("hi all", mid=1, chat_id=-100))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # When the turn ends with a deliberate skip
        worker.feed(_silent("skip", "group chatter, not for me"))
        await asyncio.sleep(0.05)

        # Then the turn finishes clean — no corrective nudge
        assert len(worker.sent) == 1, "skip must not re-engage the model"
        assert eng.turn_elapsed_s is None, "turn must finish clean after skip"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_skip_with_dropped_text_discards_narration() -> None:
    """A ``skip`` turn that left a text block behind (the model narrated its
    decision before skipping) must stay silent — the narration is internal
    per the system-prompt contract and must never be delivered to the chat
    (issue #84: skip reasoning leaked into a group as a real message)."""
    worker = FakeWorker()
    delivered: list[tuple[int, str]] = []

    async def capture(chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        delivered.append((chat_id, text))

    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, error_notify=capture))
    await eng.start()
    try:
        # Given a group message that starts a turn
        await eng.submit(_msg("Thanx for response", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1, "the user turn was handed to the worker"

        # When the turn ends skip but the model narrated its decision as text
        worker.feed(
            TurnResult(
                text_blocks=["Just a thanks in a human-to-human thread."],
                control=ControlAction(action="skip", reason="bare thanks"),
                user_visible_action=False,
                dropped_text=True,
            )
        )
        await asyncio.sleep(0.05)

        # Then nothing is delivered, no retry is kicked, the turn ends clean
        assert delivered == [], "skip narration must never reach the chat"
        assert len(worker.sent) == 1, "skip must not re-engage the model"
        assert eng.turn_elapsed_s is None, "turn must finish clean after skip"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_health_introspection_accessors() -> None:
    """``pending_count`` / ``turn_elapsed_s`` back the /health readout."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        assert eng.turn_elapsed_s is None, "idle engine reports no running turn"
        assert eng.pending_count == 0, "fresh engine has an empty buffer"

        await eng.submit(_msg("hi", mid=1))
        await asyncio.sleep(0.08)  # debounce fires, turn starts
        elapsed = eng.turn_elapsed_s
        assert elapsed is not None and elapsed >= 0, "running turn reports elapsed time"

        worker.feed(
            TurnResult(
                text_blocks=[],
                control=ControlAction(action="stop", reason="ok"),
                user_visible_action=True,
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
        assert eng.turn_elapsed_s is None, "finished turn reports idle again"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_pending_count_reflects_buffered_messages() -> None:
    """Messages waiting on a long debounce are visible as queue depth."""
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=5000))
    await eng.start()
    try:
        await eng.submit(_msg("queued", mid=1))
        assert eng.pending_count == 1, "buffered message must show as queued"
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_inject_drained_between_turns_when_pending() -> None:
    worker = FakeWorker()
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20))
    await eng.start()
    try:
        await eng.submit(_msg("first", mid=1))
        await asyncio.sleep(0.08)
        assert len(worker.sent) == 1

        # Two messages arrive while turn is in progress → inject path
        await eng.submit(_msg("mid-a", mid=2))
        await eng.submit(_msg("mid-b", mid=3))
        await asyncio.sleep(0.05)
        # Both injected (the second submit's _maybe_inject drains all pending)
        joined = "\n".join(worker.injected)
        assert "mid-a" in joined and "mid-b" in joined

        # Turn finishes cleanly with stop (a reply was delivered)
        worker.feed(
            TurnResult(
                text_blocks=[],
                control=ControlAction(action="stop", reason="ok"),
                user_visible_action=True,
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.05)
    finally:
        await eng.stop()
