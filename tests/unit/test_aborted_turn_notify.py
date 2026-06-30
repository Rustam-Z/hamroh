"""Tool-error-limit aborts must not go silent.

When the tool-error circuit breaker aborts a turn, the worker respawns
through its *intentional*-exit path, so the crash notifier (``_on_cc_crash``)
never fires. The engine is therefore the only place left to tell the waiting
chats — it must send a failure notice and flush any partial text the model
produced before the breaker killed the turn. The ``session-reset`` abort, by
contrast, is owner-initiated and must stay silent.

Regression guard for issue #75 (silent ~10 min stall after an aborted turn).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.engine import TurnCallbacks

WAITING_CHAT = -100


def _engine(tmp_path: Path) -> tuple[Engine, MagicMock, list[tuple[int, str]]]:
    """An engine mid-turn with one waiting chat and a notify capture."""
    worker = MagicMock(reset_session=AsyncMock(), send=AsyncMock())
    sent: list[tuple[int, str]] = []

    async def notify(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    engine = Engine(
        worker,
        Config.for_test(tmp_path),
        EngineOptions(error_notify=notify),
    )
    engine._is_processing.set()
    engine._turn.active_chats = {WAITING_CHAT}
    return engine, worker, sent


@pytest.mark.asyncio
async def test_tool_error_abort_flushes_partial_text_and_notifies(
    tmp_path: Path,
) -> None:
    # Given a turn the breaker aborted after the model wrote a partial reply,
    # with a queued success callback (CC saw the messages before the abort)
    engine, _worker, sent = _engine(tmp_path)
    fired: list[bool] = []

    async def callback() -> None:
        fired.append(True)

    engine._turn_callbacks = [TurnCallbacks(on_success=callback)]
    result = TurnResult(
        aborted_reason="tool-error-limit",
        text_blocks=["Render tool isn't available — here it is as text."],
    )

    # When the engine processes the abort sentinel
    await engine._handle_turn_result(result)

    # Then the partial text is delivered first, then the failure notice
    assert len(sent) == 2, "the partial text and the failure notice both go out"
    assert sent[0] == (
        WAITING_CHAT,
        "Render tool isn't available — here it is as text.",
    ), "the model's half-written reply must reach the user, not be dropped"
    notice_chat, notice_text = sent[1]
    assert notice_chat == WAITING_CHAT, "the waiting chat must be told it failed"
    assert "resend" in notice_text.lower(), "the notice must ask the user to retry"

    # And the turn is wound down cleanly
    assert fired == [True], "success callbacks fire — reminders advance, no loop"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"
    assert not engine._is_processing.is_set(), "engine must be idle again"


@pytest.mark.asyncio
async def test_tool_error_abort_notifies_even_without_partial_text(
    tmp_path: Path,
) -> None:
    # Given a turn the breaker aborted with no text produced yet
    engine, _worker, sent = _engine(tmp_path)
    result = TurnResult(aborted_reason="tool-error-limit")

    # When the engine processes the abort sentinel
    await engine._handle_turn_result(result)

    # Then exactly one failure notice goes to the waiting chat
    assert len(sent) == 1, "only the failure notice is sent when there is no text"
    chat_id, text = sent[0]
    assert chat_id == WAITING_CHAT
    assert "resend" in text.lower(), "the notice must ask the user to retry"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"


@pytest.mark.asyncio
async def test_liveness_wedge_abort_notifies(tmp_path: Path) -> None:
    # Given a turn the liveness watchdog aborted (wedged, no progress)
    engine, _worker, sent = _engine(tmp_path)
    result = TurnResult(aborted_reason="liveness-wedge")

    # When the engine processes the abort sentinel
    await engine._handle_turn_result(result)

    # Then the waiting chat is told, the same as any abnormal abort
    assert len(sent) == 1, "a wedged-turn abort must not be silent either"
    chat_id, text = sent[0]
    assert chat_id == WAITING_CHAT
    assert "resend" in text.lower(), "the notice must ask the user to retry"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"


@pytest.mark.asyncio
async def test_session_reset_abort_stays_silent(tmp_path: Path) -> None:
    # Given an owner-initiated session-reset abort (not a failure)
    engine, _worker, sent = _engine(tmp_path)
    reverted: list[bool] = []

    async def on_failure() -> None:
        reverted.append(True)

    engine._turn_callbacks = [
        TurnCallbacks(on_success=AsyncMock(), on_failure=on_failure)
    ]
    result = TurnResult(aborted_reason="session-reset")

    # When the engine processes the abort
    await engine._handle_turn_result(result)

    # Then no user-facing notice is sent and the failure callback reverts the
    # in-flight work (it will replay into the fresh session)
    assert sent == [], "a deliberate reset must not look like an error to the user"
    assert reverted == [True], "session-reset reverts callbacks for replay"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"
