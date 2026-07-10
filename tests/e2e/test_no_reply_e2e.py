"""E2E: the bot respects an explicit "don't reply" request — DM and group.

The engine used to re-engage the model when a DM turn ended silently, which
forced a reply even when the user asked for none (the silent-stop nudge,
removed in this change). These tests pin the fixed behavior: a warm bot that
is told to stay quiet stays quiet. The warm-up turn first proves the bot is
alive, so 30 seconds of silence afterwards means intent — not a dead bot.
"""

from __future__ import annotations

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.client import expect_silence, send_and_wait
from tests.e2e.support.models import Conversation

#: How long the tester listens for a reply that must never come. Long enough
#: to cover a full model turn (the unwanted replies landed within ~30s).
NO_REPLY_WINDOW_S = 30.0


async def _assert_no_reply(client: TelegramClient, convo: Conversation) -> None:
    # Given a warm bot proven alive by a normal turn
    reply = await send_and_wait(client, convo, "Hello, are you there?")
    assert reply.text.strip() or reply.media_kind, (
        "warm-up failed: the bot did not answer a normal greeting, so a "
        "silence assertion afterwards would prove nothing"
    )

    # When the user explicitly asks for no reply
    replies = await expect_silence(
        client,
        convo,
        "Please don't reply to this message. Just ignore, don't send anything. I am testing.",
        within=NO_REPLY_WINDOW_S,
    )

    # Then the bot stays silent for the whole window
    assert not replies, (
        f"bot must not reply when explicitly asked to stay silent, but sent "
        f"{[m.raw_text for m in replies]!r} within {NO_REPLY_WINDOW_S:.0f}s"
    )


@pytest.mark.smoke
async def test_bot_stays_silent_when_asked_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot sends no message when a DM user explicitly asks for no reply.

    given  a warm bot that already answered a greeting in this DM
    when   the tester sends "please don't reply to this message. just ignore.
           we are testing"
    then   the bot sends no message for NO_REPLY_WINDOW_S seconds.
    """
    await _assert_no_reply(tester_client, dm)


@pytest.mark.smoke
async def test_bot_stays_silent_when_asked_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot sends no message when @mentioned in a group with a no-reply ask.

    given  a warm bot that already answered a greeting in this group
    when   the tester @mentions the bot with "please don't reply to this
           message. just ignore. we are testing"
    then   the bot sends no message for NO_REPLY_WINDOW_S seconds.
    """
    await _assert_no_reply(tester_client, group)
