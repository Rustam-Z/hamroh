"""Tool-error-limit aborts must not go silent — the owner must be alerted.

When the tool-error circuit breaker aborts a turn, the worker respawns
through its *intentional*-exit path, so the crash notifier never fires. The
engine is therefore the only place left to raise the alarm — it flushes any
partial text the model produced to the waiting chat, then logs the
internal-error at ERROR. The root ``OwnerLogHandler`` turns that log into an
owner DM (with a deep link to the in-flight message); that delivery is
covered by ``test_owner_log_notifier``. The ``session-reset`` abort, by
contrast, is owner-initiated and must stay silent.

Regression guard for issue #75 (silent ~10 min stall after an aborted turn).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.cc_worker import TurnResult
from hamroh.config import Config
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.engine import TurnCallbacks
from hamroh.models import ChatMessage

#: A supergroup (``-100…``) so the notice can carry a shareable message link.
WAITING_CHAT = -1001234567890
#: The message that kicked the aborted turn.
TRIGGER_MSG = 6382


def _engine(tmp_path: Path) -> tuple[Engine, MagicMock, list[tuple[int, str]]]:
    """An engine mid-turn with one waiting chat and a chat-send capture."""
    worker = MagicMock(reset_session=AsyncMock(), send=AsyncMock())
    sent: list[tuple[int, str]] = []

    async def notify(chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        sent.append((chat_id, text))

    engine = Engine(
        worker,
        Config.for_test(tmp_path),
        EngineOptions(error_notify=notify),
    )
    engine._is_processing.set()
    engine._turn.active_chats = {WAITING_CHAT}
    engine._turn.reply_targets = {
        WAITING_CHAT: ChatMessage(
            chat_id=WAITING_CHAT,
            message_id=TRIGGER_MSG,
            user_id=1,
            direction="in",
            timestamp=datetime(2026, 7, 17, tzinfo=timezone.utc),
            text="render this",
        )
    }
    return engine, worker, sent


def _owner_alert(caplog: pytest.LogCaptureFixture) -> str:
    """The single ERROR the engine logged — what the owner is DM'd."""
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f"exactly one owner alert per abort, got {errors}"
    return errors[0]


@pytest.mark.asyncio
async def test_tool_error_abort_flushes_partial_text_and_alerts_owner(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
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
    with caplog.at_level(logging.ERROR):
        await engine._handle_turn_result(result)

    # Then the model's half-written reply reaches the waiting chat...
    assert sent == [
        (WAITING_CHAT, "Render tool isn't available — here it is as text.")
    ], "the model's half-written reply must reach the user, not be dropped"
    # ...and the owner is alerted to resend (delivered by the log handler)
    assert "resend" in _owner_alert(caplog).lower(), "the alert must ask for a retry"

    # And the turn is wound down cleanly
    assert fired == [True], "success callbacks fire — reminders advance, no loop"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"
    assert not engine._is_processing.is_set(), "engine must be idle again"


@pytest.mark.asyncio
async def test_tool_error_abort_alerts_even_without_partial_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a turn the breaker aborted with no text produced yet
    engine, _worker, sent = _engine(tmp_path)
    result = TurnResult(aborted_reason="tool-error-limit")

    # When the engine processes the abort sentinel
    with caplog.at_level(logging.ERROR):
        await engine._handle_turn_result(result)

    # Then nothing goes to the chat, but the owner is still alerted to resend
    assert sent == [], "no chat message when the model produced no text"
    assert "resend" in _owner_alert(caplog).lower(), "the alert must ask for a retry"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"


@pytest.mark.asyncio
async def test_liveness_wedge_abort_alerts_owner(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a turn the liveness watchdog aborted (wedged, no progress)
    engine, _worker, sent = _engine(tmp_path)
    result = TurnResult(aborted_reason="liveness-wedge")

    # When the engine processes the abort sentinel
    with caplog.at_level(logging.ERROR):
        await engine._handle_turn_result(result)

    # Then the owner is alerted, the same as any abnormal abort
    assert sent == [], "no chat message for a wedged turn with no text"
    assert "resend" in _owner_alert(caplog).lower(), "a wedge must not be silent"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"


@pytest.mark.asyncio
async def test_session_reset_abort_stays_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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
    with caplog.at_level(logging.ERROR):
        await engine._handle_turn_result(result)

    # Then nothing is sent, nothing is logged as an error, and the failure
    # callback reverts the in-flight work (it will replay into the fresh session)
    assert sent == [], "a deliberate reset must not look like an error to the user"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "a deliberate reset must not alert the owner"
    )
    assert reverted == [True], "session-reset reverts callbacks for replay"
    assert engine._turn.active_chats == set(), "no chat is owed a reply anymore"
