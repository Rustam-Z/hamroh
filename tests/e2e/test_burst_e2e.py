"""E2E: a burst of messages is fully handled — DM and group.

Mirrors "send multiple messages and get responses". We first warn the bot a
burst is coming so it answers each message fast and reply-only; with zero
debounce it then processes them across one or more turns; either way all three
must be answered within MAX_BURST_S.
"""

from __future__ import annotations

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_within
from tests.e2e.support.client import send_and_wait, send_burst
from tests.e2e.support.data import BURST_PRIMER, recall_prompt
from tests.e2e.support.models import Conversation
from tests.e2e.support.config import MAX_BURST_S
from tests.e2e.support.waits import measured


async def _assert_burst_fully_handled(
    client: TelegramClient, convo: Conversation
) -> None:
    # prime the bot first (untimed) so the burst itself is fast and reply-only
    await send_and_wait(client, convo, BURST_PRIMER)

    # each question carries a distinct token so we can spot a dropped reply
    prompts = [recall_prompt() for _ in range(3)]
    texts = [question for question, _ in prompts]
    tokens = [token for _, token in prompts]

    replies, elapsed = await measured(send_burst(client, convo, texts, tokens))

    for token in tokens:
        assert token in replies, (
            f"burst dropped {token!r}; collected replies were {replies!r}"
        )
    assert_within(elapsed, MAX_BURST_S, "burst")


@pytest.mark.smoke
async def test_handles_message_burst_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """Bot handles a burst of messages without dropping any.

    given  the bot is primed that a burst is coming, plus three distinct tokens
    when   the tester fires all three in a DM, one per second
    then   the bot's replies echo every one within MAX_BURST_S.
    """
    await _assert_burst_fully_handled(tester_client, dm)


@pytest.mark.smoke
async def test_handles_message_burst_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """Bot handles a burst of messages without dropping any.

    given  the bot is primed that a burst is coming, plus three distinct tokens
    when   the tester fires all three in a group, one per second
    then   the bot's replies echo every one within MAX_BURST_S.
    """
    await _assert_burst_fully_handled(tester_client, group)
