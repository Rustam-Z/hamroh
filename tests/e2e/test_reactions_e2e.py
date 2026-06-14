"""E2E: the bot reacts to a message with an emoji — DM and group.

given  a message asking the bot to react with 👍
when    the tester sends it
then    the 👍 reaction appears on that message — added by the bot.

Mirrors the "emojis" scenario: the bot's add_reaction tool, proven by the
reaction actually showing up on the message via Telethon.
"""

from __future__ import annotations

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.helpers import (
    MAX_REACTION_S,
    Conversation,
    assert_within,
    measured,
    send,
    wait_for_reaction,
)

_EMOJI = "👍"


async def _assert_reacts(client: TelegramClient, convo: Conversation) -> None:
    # when the tester asks the bot to react to the message
    sent = await send(client, convo, f"React to this message with the {_EMOJI} emoji.")
    # then the bot's reaction appears on it, promptly
    reacted, elapsed = await measured(
        wait_for_reaction(client, convo.chat, sent.id, _EMOJI)
    )
    assert reacted, f"bot did not react with {_EMOJI} to message {sent.id}"
    assert_within(elapsed, MAX_REACTION_S, "reaction")


async def test_bot_reacts_with_emoji_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_reacts(tester_client, dm)


async def test_bot_reacts_with_emoji_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_reacts(tester_client, group)
