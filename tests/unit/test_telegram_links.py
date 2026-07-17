"""Owner-facing message references — supergroup messages get a shareable
``t.me/c`` link; DMs have none, so they name the sender and quote the text."""

from __future__ import annotations

from datetime import datetime, timezone

from hamroh.models import ChatMessage
from hamroh.utils.telegram_links import message_link, message_ref


def _msg(chat_id: int, message_id: int, **kw: object) -> ChatMessage:
    """A minimal inbound ChatMessage for the ref builders."""
    return ChatMessage(
        chat_id=chat_id,
        message_id=message_id,
        user_id=kw.get("user_id", 587272213),  # type: ignore[arg-type]
        username=kw.get("username"),  # type: ignore[arg-type]
        first_name=kw.get("first_name"),  # type: ignore[arg-type]
        direction="in",
        timestamp=datetime(2026, 7, 17, tzinfo=timezone.utc),
        text=kw.get("text", ""),  # type: ignore[arg-type]
    )


def test_supergroup_message_gets_a_deep_link() -> None:
    # Given a supergroup chat id (``-100`` prefix) and a message
    # When a link is built
    # Then the internal id drops the prefix and the message is addressable
    assert message_link(-1001234567890, 6382) == "https://t.me/c/1234567890/6382"


def test_dm_has_no_shareable_link() -> None:
    # Given a DM (positive user id), which has no ``t.me/c`` message link
    assert message_link(587272213, 42) is None


def test_group_ref_names_group_sender_link_and_quotes_the_text() -> None:
    # Given a supergroup message with a known group title
    msg = _msg(-1001234567890, 6382, username="alice", text="deploy is down")

    # When the ref is built with the group's title
    ref = message_ref(msg, chat_title="Ops Room")

    # Then it carries the message id, sender, group name + id, deep link, and
    # the quoted text in an HTML blockquote — the same detail a DM gets, plus
    # the group and link
    assert "6382" in ref, "the message id must be named"
    assert "@alice" in ref, "the sender must be shown"
    assert "Ops Room" in ref, "the group name must be shown"
    assert "-1001234567890" in ref, "the group id must be shown"
    assert "https://t.me/c/1234567890/6382" in ref, "the deep link must be included"
    assert "<blockquote>deploy is down</blockquote>" in ref, "the text must be quoted"


def test_group_ref_without_a_known_title_still_shows_the_group_id() -> None:
    # Given a basic group (negative id, no ``-100`` prefix, no link) and no title
    ref = message_ref(_msg(-4287, 12, text="hi"))

    # Then the group id is shown even without a name, and there is no link
    assert "(id -4287)" in ref, "an unnamed group must still show its id"
    assert "https://" not in ref, "a basic group has no shareable link"


def test_dm_ref_names_the_sender_and_quotes_the_text() -> None:
    # Given a DM with no shareable link
    msg = _msg(587272213, 42, username="alice", text="fix the deploy please")

    # When the ref is built
    ref = message_ref(msg)

    # Then it names the sender (id + @username) and quotes the message so the
    # owner knows what happened and to whom without a link to click
    assert "42" in ref, "the message id must still be named"
    assert "587272213" in ref, "the sender id must be shown"
    assert "@alice" in ref, "the username must be shown"
    assert "<blockquote>fix the deploy please</blockquote>" in ref, "text is quoted"


def test_quote_escapes_html_so_markup_cannot_break() -> None:
    # Given a DM whose text contains HTML-significant characters
    ref = message_ref(_msg(1, 5, text="a < b && c > d"))

    # Then they are escaped inside the blockquote, not left as raw markup
    assert "<blockquote>a &lt; b &amp;&amp; c &gt; d</blockquote>" in ref, (
        "message text must be HTML-escaped so a stray < can't break the send"
    )


def test_dm_ref_falls_back_to_first_name_then_unknown() -> None:
    # Given DMs without a username
    with_name = message_ref(_msg(1, 7, first_name="Bob", text="hi"))
    anonymous = message_ref(_msg(2, 8, text="hi"))

    # Then the first name is used, or "unknown" when there is none
    assert "Bob" in with_name, "first name is the fallback handle"
    assert "unknown" in anonymous, "a nameless sender is labelled unknown"


def test_dm_ref_caps_a_long_quote() -> None:
    # Given a DM whose text is far longer than the quote cap
    ref = message_ref(_msg(1, 9, text="x" * 5000))

    # Then the quote is truncated with an ellipsis, not dumped whole
    assert "…" in ref, "an over-long quote must be truncated"
    assert len(ref) < 700, "the ref must stay small even for a huge message"
