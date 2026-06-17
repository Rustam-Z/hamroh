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

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.data import recall_prompt
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
    assert token in reply.text, (
        f"bot did not return {token!r}; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "reply")


async def test_bot_replies_dm(tester_client: TelegramClient, dm: Conversation) -> None:
    """The bot answers a natural question correctly and promptly in a DM.

    given  a warm bot and a natural question carrying a unique token
    when   the tester sends it in a DM
    then   the bot returns the token in a non-empty reply within MAX_TEXT_REPLY_S.
    """
    await _assert_prompt_reply(tester_client, dm)


async def test_bot_replies_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot answers a natural question correctly and promptly in a group.

    given  a warm bot and a natural question carrying a unique token
    when   the tester sends it in a group
    then   the bot returns the token in a non-empty reply within MAX_TEXT_REPLY_S.
    """
    await _assert_prompt_reply(tester_client, group)
