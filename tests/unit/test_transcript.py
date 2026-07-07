"""Transcript logger formatting and chat-title cache."""

from __future__ import annotations

import logging

import pytest

from hamroh.helpers.transcript import (
    ChatRef,
    MsgRef,
    UserRef,
    log_cc_text,
    log_cc_tool_result,
    log_cc_tool_use,
    log_cc_user,
    log_delete,
    log_edit,
    log_inbound,
    log_inbound_edit,
    log_outbound,
    log_reaction,
    set_cc_render_mode,
)


@pytest.fixture()
def caplog_tx(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="hamroh.tx")
    return caplog


def test_inbound_dm_format(caplog_tx) -> None:
    log_inbound(
        ChatRef(12345, {12345: "Alice"}, "private"),
        UserRef(12345, "Alice"),
        MsgRef(42, "hi"),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[RX]" in line
    assert "DM" in line
    assert "Alice" in line
    assert "12345" in line
    assert "m42" in line
    assert "| hi" in line


def test_inbound_group_format(caplog_tx) -> None:
    log_inbound(
        ChatRef(-1001234567890, {-1001234567890: "Team Chat"}, "supergroup"),
        UserRef(42, "Alice"),
        MsgRef(10, "hello team", reply_to_id=5),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[RX]" in line
    assert 'G "Team Chat"' in line
    assert "Alice[42]" in line
    assert "m10" in line
    assert "→m5" in line
    assert "| hello team" in line


def test_inbound_dropped_format(caplog_tx) -> None:
    log_inbound(
        ChatRef(999, {}, "private"),
        UserRef(999, "Stranger"),
        MsgRef(1, "leaked spam"),
        allowed=False,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[DROP]" in line
    assert "(chat not allowed)" in line
    assert "| leaked spam" in line


def test_outbound_uses_cached_title(caplog_tx) -> None:
    titles = {-1001234567890: "Team Chat"}
    log_outbound(
        ChatRef(-1001234567890, titles),
        MsgRef(99, "hello!", reply_to_id=10),
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[TX]" in line
    assert 'G "Team Chat"' in line
    assert "m99" in line
    assert "→m10" in line
    assert "| hello!" in line


def test_outbound_falls_back_to_chat_id_only(caplog_tx) -> None:
    log_outbound(
        ChatRef(-1009999999999, {}),
        MsgRef(1, "hi"),
    )
    line = caplog_tx.records[-1].getMessage()
    assert "[TX]" in line
    assert "-1009999999999" in line
    assert "| hi" in line


def test_inbound_truncates_long_body(caplog_tx) -> None:
    body = "x" * 500
    log_inbound(
        ChatRef(1, {}, "private"),
        UserRef(1, None),
        MsgRef(1, body),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "…" in line
    assert len(line.split("|", 1)[1]) < 250


def test_inbound_flattens_newlines(caplog_tx) -> None:
    log_inbound(
        ChatRef(1, {}, "private"),
        UserRef(1, None),
        MsgRef(1, "line one\nline two\rline three"),
        allowed=True,
    )
    line = caplog_tx.records[-1].getMessage()
    assert "\n" not in line.split("|", 1)[1]
    assert "line one line two line three" in line


def test_edit_and_delete_and_reaction(caplog_tx) -> None:
    titles = {-1: "G"}
    log_edit(chat_id=-1, chat_titles=titles, message_id=5, text="new body")
    log_delete(chat_id=-1, chat_titles=titles, message_id=5)
    log_reaction(chat_id=-1, chat_titles=titles, message_id=5, emoji="👍")
    log_inbound_edit(
        ChatRef(-1, titles),
        UserRef(42, "Alice"),
        MsgRef(5, "user fixed typo"),
    )
    msgs = [r.getMessage() for r in caplog_tx.records[-4:]]
    assert any(m.startswith("[EDIT]") for m in msgs)
    assert any(m.startswith("[DEL]") for m in msgs)
    assert any(m.startswith("[REACT]") and "👍" in m for m in msgs)
    assert any(m.startswith("[RX↺]") for m in msgs)


# ---------------------------------------------------------------------------
# [CC.*] rendering modes (HAMROH_LOG_TRANSCRIPT=full|compact)
# ---------------------------------------------------------------------------


@pytest.fixture()
def caplog_cc(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="hamroh.cc")
    return caplog


@pytest.fixture()
def full_mode():
    """Switch [CC.*] rendering to full for one test, then restore compact."""
    set_cc_render_mode("full")
    yield
    set_cc_render_mode("compact")


def test_cc_compact_truncates_and_flattens_by_default(caplog_cc) -> None:
    log_cc_text("line one\nline two" + "x" * 500)
    line = caplog_cc.records[-1].getMessage()
    assert "\n" not in line, "compact mode must flatten newlines"
    assert "…" in line, "compact mode must truncate long bodies"


def test_cc_full_keeps_multiline_text_verbatim(caplog_cc, full_mode) -> None:
    body = "first paragraph\n\nsecond paragraph " + "x" * 500
    log_cc_text(body)
    line = caplog_cc.records[-1].getMessage()
    assert body in line, "full mode must keep the text block verbatim"


def test_cc_full_keeps_user_envelope_verbatim(caplog_cc, full_mode) -> None:
    envelope = '<msg id="1" chat="2">line one\nline two</msg>'
    log_cc_user(envelope)
    line = caplog_cc.records[-1].getMessage()
    assert envelope in line, "full mode must keep the inbound envelope verbatim"


def test_cc_full_keeps_tool_args_untruncated(caplog_cc, full_mode) -> None:
    log_cc_tool_use(
        tool_name="telegram_send_message",
        tool_use_id="toolu_01AbCdEf",
        args={"chat_id": 1, "text": "y" * 500},
    )
    line = caplog_cc.records[-1].getMessage()
    assert "y" * 500 in line, "full mode must keep tool args untruncated"


def test_cc_full_tool_result_shows_preview_with_more_lines(
    caplog_cc, full_mode
) -> None:
    content = "\n".join(f"line {i}" for i in range(1, 26))
    log_cc_tool_result(tool_use_id="toolu_01AbCdEf", content=content, is_error=False)
    line = caplog_cc.records[-1].getMessage()
    assert "line 10" in line, "preview must include the first 10 lines"
    assert "line 11" not in line, "preview must stop after 10 lines"
    assert "(+15 more lines)" in line, "preview must count the hidden lines"


def test_cc_full_short_tool_result_has_no_more_lines_marker(
    caplog_cc, full_mode
) -> None:
    log_cc_tool_result(tool_use_id="toolu_01AbCdEf", content="ok\ndone", is_error=False)
    line = caplog_cc.records[-1].getMessage()
    assert "ok\ndone" in line, "short results must render in full"
    assert "more lines" not in line, "short results must not show a marker"


def test_tool_context_chat_titles_default_is_independent_dict() -> None:
    """Two ToolContext instances must not share the same dict."""
    from hamroh.tools.base import ToolContext

    a = ToolContext()
    b = ToolContext()
    a.chat_titles[1] = "x"
    assert 1 not in b.chat_titles
