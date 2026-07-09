"""Pure Telegram deep-link builders — supergroup messages get a shareable
``t.me/c`` link; DMs have none."""

from __future__ import annotations

from hamroh.utils.telegram_links import message_link, message_ref


def test_supergroup_message_gets_a_deep_link() -> None:
    # Given a supergroup chat id (``-100`` prefix) and a message
    # When a link is built
    # Then the internal id drops the prefix and the message is addressable
    assert message_link(-1001234567890, 6382) == "https://t.me/c/1234567890/6382"


def test_dm_has_no_shareable_link() -> None:
    # Given a DM (positive user id), which has no ``t.me/c`` message link
    assert message_link(587272213, 42) is None


def test_ref_carries_the_link_for_supergroups() -> None:
    ref = message_ref(-1001234567890, 6382)
    assert "6382" in ref, "the id must be named"
    assert "https://t.me/c/1234567890/6382" in ref, "the link must be included"


def test_ref_falls_back_to_bare_id_for_dms() -> None:
    assert message_ref(587272213, 42) == "• message 42"
