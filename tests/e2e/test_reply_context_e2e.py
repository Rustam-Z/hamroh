"""E2E: reply linkage is captured, and context carries across turns.

reply (DM and group): given an initial message, when the tester replies to
    it, then the bot records the reply linkage (reply_to_id) on the inbound
    row.
context (DM and group): given a fact stated in one turn, when asked in a
    later turn, then the bot recalls it from conversation context — distinct
    from the disk-backed memory test.
"""

from __future__ import annotations

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.harness import Sut, reply_info
from tests.e2e.support.helpers import (
    MAX_TEXT_REPLY_S,
    Conversation,
    assert_reply_within,
    assert_within,
    measured,
    new_sentinel,
    send,
    send_and_wait,
    wait_until,
)


async def _assert_reply_linkage(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    parent = new_sentinel("PARENT")
    child = new_sentinel("CHILD")

    # given an initial message, and a reply to it
    first = await send(client, convo, f"Topic note: {parent}.")
    await send(client, convo, f"Question {child} about the note.", reply_to=first.id)

    # then the inbound reply row carries the linkage back to the parent, promptly
    # (matched by token text — Bot-API message_ids differ from Telethon's)
    row, elapsed = await measured(wait_until(lambda: reply_info(sut.db_path, child)))
    assert row is not None, f"no inbound row for reply {child!r}"
    assert row["reply_to_id"] is not None, f"reply linkage not captured for {child!r}"
    assert parent in (row["reply_to_text"] or ""), (
        f"reply linked to wrong parent; reply_to_text={row['reply_to_text']!r}"
    )
    assert_within(elapsed, MAX_TEXT_REPLY_S, "reply linkage")


async def _assert_context(client: TelegramClient, convo: Conversation) -> None:
    token = new_sentinel("CTX")
    # given a fact stated in one turn
    await send_and_wait(
        client, convo, f"For this chat, my reference number is {token}."
    )
    # when we ask about it in a later turn
    reply = await send_and_wait(client, convo, "What is my reference number?")
    # then the bot recalls it from conversation context, promptly
    assert token in reply.text, (
        f"bot lost context for {token!r}; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "context recall")


async def test_reply_linkage_is_captured_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_reply_linkage(pyclaudir_sut, tester_client, dm)


async def test_reply_linkage_is_captured_group(
    pyclaudir_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_reply_linkage(pyclaudir_sut, tester_client, group)


async def test_context_carries_across_turns_dm(
    tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_context(tester_client, dm)


async def test_context_carries_across_turns_group(
    tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_context(tester_client, group)
