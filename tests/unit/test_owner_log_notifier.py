"""The owner log notifier: serious log records become an owner DM.

Every ``log.error(...)`` in the codebase must reach the owner in chat, while
lower-severity chatter, duplicates, and the handler's own send failures must
not — otherwise the owner drowns in noise or a failed send loops forever.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from hamroh.helpers.owner_log_notifier import (
    OwnerLogHandler,
    _format_record,
    to_plain_text,
)


def _record(level: int, msg: str, *, created: float = 1000.0) -> logging.LogRecord:
    record = logging.LogRecord("hamroh.test", level, __file__, 1, msg, None, None)
    record.created = created
    return record


async def _drain() -> None:
    """Let handler-scheduled loop tasks run to completion."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_error_record_is_dmed_to_owner() -> None:
    # Given a handler wired to a capturing owner-send
    sent: list[str] = []
    handler = OwnerLogHandler(
        lambda text: _append(sent, text), asyncio.get_running_loop()
    )

    # When an ERROR record is emitted
    handler.emit(_record(logging.ERROR, "mcp server dropped"))
    await _drain()

    # Then the owner receives exactly one DM carrying the message
    assert len(sent) == 1, "one ERROR record must produce one owner DM"
    assert "mcp server dropped" in sent[0], "the log message must reach the owner"


@pytest.mark.asyncio
async def test_below_error_is_ignored() -> None:
    # Given a handler (level ERROR) — WARNING is CC's transient noise
    sent: list[str] = []
    handler = OwnerLogHandler(
        lambda text: _append(sent, text), asyncio.get_running_loop()
    )

    # When a WARNING record is handled through the level filter
    record = _record(logging.WARNING, "api_retry overloaded")
    if record.levelno >= handler.level:
        handler.emit(record)
    await _drain()

    # Then nothing is forwarded — only ERROR-and-above reach the owner
    assert sent == [], "sub-ERROR records must never DM the owner"


@pytest.mark.asyncio
async def test_identical_message_is_suppressed_within_cooldown() -> None:
    # Given a handler with a 60s dedup window
    sent: list[str] = []
    handler = OwnerLogHandler(
        lambda text: _append(sent, text), asyncio.get_running_loop()
    )

    # When the same error fires twice a few seconds apart
    handler.emit(_record(logging.ERROR, "overloaded", created=1000.0))
    handler.emit(_record(logging.ERROR, "overloaded", created=1005.0))
    await _drain()

    # Then only the first is delivered — repeats within the window are muted
    assert len(sent) == 1, "a repeated identical error must be sent only once"


@pytest.mark.asyncio
async def test_same_message_sends_again_after_cooldown() -> None:
    # Given a handler and an error already sent once
    sent: list[str] = []
    handler = OwnerLogHandler(
        lambda text: _append(sent, text), asyncio.get_running_loop()
    )
    handler.emit(_record(logging.ERROR, "overloaded", created=1000.0))

    # When the same error recurs past the cooldown window
    handler.emit(_record(logging.ERROR, "overloaded", created=1100.0))
    await _drain()

    # Then it is delivered again — a persistent problem keeps nagging
    assert len(sent) == 2, "the same error past the cooldown must resend"


@pytest.mark.asyncio
async def test_send_failure_while_sending_does_not_loop() -> None:
    # Given an owner-send that itself logs an ERROR back through the handler
    sent: list[str] = []
    handler: OwnerLogHandler

    async def _send(text: str) -> None:
        sent.append("attempt")
        # A failing send path that logs an ERROR: the guard must stop this
        # from scheduling yet another owner DM.
        handler.emit(_record(logging.ERROR, "send failed"))

    handler = OwnerLogHandler(_send, asyncio.get_running_loop())

    # When the first ERROR triggers a send that re-enters the handler
    handler.emit(_record(logging.ERROR, "boom"))
    await _drain()

    # Then the re-entrant record is dropped by the guard — no runaway loop
    assert sent == ["attempt"], "the reentrancy guard must break the send→log→send loop"


@pytest.mark.asyncio
async def test_dm_carries_https_link_to_causing_message() -> None:
    # Given a supergroup message in flight and a handler wired to link to it
    sent: list[str] = []
    handler = OwnerLogHandler(
        lambda text: _append(sent, text),
        asyncio.get_running_loop(),
        link_provider=lambda: "• message 42: https://t.me/c/1758365237/42",
    )

    # When an error is logged mid-turn
    handler.emit(_record(logging.ERROR, "turn failed"))
    await _drain()

    # Then the owner DM carries a jump-straight-there https link
    assert "https://t.me/c/1758365237/42" in sent[0], (
        "the owner must get a link to the message that caused the error"
    )


@pytest.mark.asyncio
async def test_no_link_appended_when_idle() -> None:
    # Given no message in flight (link provider returns empty)
    sent: list[str] = []
    handler = OwnerLogHandler(
        lambda text: _append(sent, text),
        asyncio.get_running_loop(),
        link_provider=lambda: "",
    )

    # When an error is logged
    handler.emit(_record(logging.ERROR, "startup failed"))
    await _drain()

    # Then the DM is sent with no trailing link section
    assert sent[0].endswith("startup failed"), "no link means no dangling refs block"


async def _append(sink: list[str], text: str) -> None:
    sink.append(text)


def test_format_record_marks_level_and_truncates() -> None:
    # Given a very long CRITICAL message
    record = _record(logging.CRITICAL, "x" * 5000)

    # When it is formatted for the owner
    text = _format_record(record)

    # Then it is flagged critical and kept DM-sized
    assert text.startswith("🔴"), "critical records must be visually distinct"
    assert len(text) <= 1201, "owner DMs must be truncated, not a log dump"


def test_format_record_escapes_html_in_the_log_text() -> None:
    # Given a traceback-like message with HTML-significant characters
    text = _format_record(_record(logging.ERROR, "boom in <module> a & b"))

    # Then they are escaped so HTML parse mode can't choke on the log text
    assert "&lt;module&gt;" in text, "angle brackets in logs must be escaped"
    assert "a &amp; b" in text, "ampersands in logs must be escaped"


def test_to_plain_text_strips_tags_and_unescapes() -> None:
    # Given an HTML owner DM with a blockquote and escaped entities
    html_dm = "⚠️ ERROR\n<blockquote>a &lt; b &amp; c</blockquote>"

    # When it is reduced to the plain-text fallback
    plain = to_plain_text(html_dm)

    # Then tags are gone and entities are restored to their literal characters
    assert plain == "⚠️ ERROR\na < b & c", "fallback must be readable plain text"
