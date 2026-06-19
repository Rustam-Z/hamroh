"""E2E: the bot answers a real question correctly and promptly — DM and group.

The most basic guarantee: a message gets a correct, prompt response. A warm-up
turn pays the one-time startup cost off the clock, then the timed turn must both
answer correctly (return a unique token) and land its first chunk inside the
text-reply limit. The group case also exercises @mention delivery and group
authorization. (Aggregate p50/p95 across many samples lives in test_eval_e2e.py;
the bot subprocess is launched by the autouse fixture in conftest.)
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.data import recall_prompt, split_message_prompt
from tests.e2e.support.models import Conversation
from tests.e2e.support.config import MAX_TEXT_REPLY_S

log = logging.getLogger(__name__)


async def _assert_prompt_reply(client: TelegramClient, convo: Conversation) -> None:
    # warm-up turn pays the one-time startup cost; not measured
    await send_and_wait(client, convo, "Hello, are you there?")

    question, token = recall_prompt()
    reply = await send_and_wait(client, convo, question)
    log.info(
        "reply latency: first=%.2fs complete=%.2fs",
        reply.t_first_s,
        reply.t_complete_s,
    )

    assert reply.text.strip() or reply.media_kind, "bot sent no reply content"
    assert reply.chunk_count == 1, (
        f"expected exactly 1 message per request, got {reply.chunk_count}; "
        f"reply was {reply.text!r}"
    )
    assert token in reply.text, (
        f"bot did not return {token!r}; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "reply")


async def _assert_multi_message_reply(
    client: TelegramClient, convo: Conversation
) -> None:
    # warm-up turn pays the one-time startup cost; not measured
    await send_and_wait(client, convo, "Hello, are you there?")

    question, tokens = split_message_prompt()
    reply = await send_and_wait(client, convo, question)
    log.info(
        "multi-message reply: chunks=%d first=%.2fs complete=%.2fs",
        reply.chunk_count,
        reply.t_first_s,
        reply.t_complete_s,
    )

    assert reply.chunk_count >= len(tokens), (
        f"expected at least {len(tokens)} separate messages, "
        f"got {reply.chunk_count}; reply was {reply.text!r}"
    )
    for token in tokens:
        assert token in reply.text, (
            f"bot did not include {token!r}; reply was {reply.text!r}"
        )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "reply")


@pytest.mark.smoke
async def test_bot_replies_dm(tester_client: TelegramClient, dm: Conversation) -> None:
    """The bot answers a natural question correctly and promptly in a DM.

    given  a warm bot and a natural question carrying a unique token
    when   the tester sends it in a DM
    then   the bot returns the token in a single reply message within MAX_TEXT_REPLY_S.
    """
    await _assert_prompt_reply(tester_client, dm)


@pytest.mark.smoke
async def test_bot_replies_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot answers a natural question correctly and promptly in a group.

    given  a warm bot and a natural question carrying a unique token
    when   the tester sends it in a group
    then   the bot returns the token in a single reply message within MAX_TEXT_REPLY_S.
    """
    await _assert_prompt_reply(tester_client, group)


async def test_bot_sends_multiple_messages_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot can deliver several separate messages for one request in a DM.

    given  a warm bot and a request to send three separate messages
    when   the tester sends it in a DM
    then   the bot delivers at least three messages, each carrying its token,
           with the first chunk landing within MAX_TEXT_REPLY_S.
    """
    await _assert_multi_message_reply(tester_client, dm)


@pytest.mark.smoke
async def test_bot_sends_multiple_messages_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot can deliver several separate messages for one request in a group.

    given  a warm bot and a request to send three separate messages
    when   the tester sends it in a group
    then   the bot delivers at least three messages, each carrying its token,
           with the first chunk landing within MAX_TEXT_REPLY_S.
    """
    await _assert_multi_message_reply(tester_client, group)
