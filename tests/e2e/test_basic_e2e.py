"""E2E: the bot answers a message — separately in a DM and in a group.

given  a simple message
when    the tester sends it
then    the bot sends back a (non-empty) reply, within the text-reply limit.

The most basic guarantee: a message gets a prompt response. We assert that a
reply arrives and that its first chunk lands within MAX_TEXT_REPLY_S. Content
correctness is covered by the context, memory, and other feature tests. The
group case also exercises @mention delivery and group authorization. The bot
subprocess is launched by the autouse fixture in conftest.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.helpers import (
    MAX_TEXT_REPLY_S,
    Conversation,
    assert_reply_within,
    send_and_wait,
)

log = logging.getLogger(__name__)


async def _assert_replies(client: TelegramClient, convo: Conversation) -> None:
    # when the tester sends a simple message
    reply = await send_and_wait(client, convo, "Hi, are you online?")
    # then the bot sends back some reply ...
    assert reply.text.strip() or reply.media_kind, "bot sent no reply content"
    # ... promptly
    log.info(
        "reply latency: first=%.2fs complete=%.2fs",
        reply.t_first_s,
        reply.t_complete_s,
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "text")


async def test_bot_replies_dm(tester_client: TelegramClient, dm: Conversation) -> None:
    await _assert_replies(tester_client, dm)


async def test_bot_replies_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_replies(tester_client, group)
