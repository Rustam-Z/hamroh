"""The engine's error/reply delivery channel converts Markdown → Telegram HTML.

Regression guard for the shared ``_error_notify`` change: it must send with
``parse_mode="HTML"`` and run text through ``markdown_to_telegram_html`` so agy
replies (and Claude's dropped-text fallback) render instead of showing literal
Markdown. Plain text must pass through safely (escaped, not mangled).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from hamroh.startup import _make_error_notify


def _notify_with_spy():
    dispatcher = MagicMock()
    dispatcher.bot.send_message = AsyncMock()
    return _make_error_notify(dispatcher), dispatcher.bot.send_message


async def test_markdown_reply_is_converted_and_sent_as_html() -> None:
    notify, send = _notify_with_spy()
    await notify(123, "**bold** and `code`", None)
    kwargs = send.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>bold</b>" in kwargs["text"]
    assert "<code>code</code>" in kwargs["text"]


async def test_plain_error_text_passes_through_safely() -> None:
    notify, send = _notify_with_spy()
    await notify(123, "⚠️ auth failed: token & scope invalid", None)
    kwargs = send.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    # No tags for plain text; the ampersand is HTML-escaped so the send is valid.
    assert "auth failed" in kwargs["text"]
    assert "&amp;" in kwargs["text"]


async def test_reply_to_message_id_is_threaded() -> None:
    notify, send = _notify_with_spy()
    await notify(123, "hi", 555)
    assert send.call_args.kwargs["reply_to_message_id"] == 555
