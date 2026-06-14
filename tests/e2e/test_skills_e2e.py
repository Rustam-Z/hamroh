"""E2E: the bot consults its skills, in a DM and in the group.

given  a request to read a specific skill
when    the tester asks
then    the bot invokes a skills tool (read_skill/list_skills) to do so.

We assert on the recorded tool call, not the reply text: skill content gets
mangled by Telegram's HTML rendering, so the reply is unreliable. Each chat
reads a *different* skill the rest of the suite never touches — the shared bot
session caches a skill's content once read, so any skill another test already
consumed (e.g. reminder-format, render-style) would not re-trigger the tool.
"""

from __future__ import annotations

from datetime import datetime, timezone

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.harness import Sut, tool_calls_since
from tests.e2e.support.helpers import (
    MAX_SKILL_REPLY_S,
    Conversation,
    assert_reply_within,
    send_and_wait,
    wait_until,
)

_SKILL_TOOLS = {"read_skill", "list_skills"}


async def _assert_consults_skill(
    sut: Sut, client: TelegramClient, convo: Conversation, skill: str
) -> None:
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    reply = await send_and_wait(
        client, convo, f"Read your '{skill}' skill and summarize its content for me."
    )
    calls = await wait_until(
        lambda: [
            r
            for r in tool_calls_since(sut.db_path, since)
            if r["tool_name"] in _SKILL_TOOLS
        ]
    )
    assert calls, f"no read_skill/list_skills tool call recorded for {skill!r}"
    assert_reply_within(reply, MAX_SKILL_REPLY_S, "skill")


async def test_skill_consulted_in_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_consults_skill(pyclaudir_sut, tester_client, dm, "trends")


async def test_skill_consulted_in_group(
    pyclaudir_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_consults_skill(
        pyclaudir_sut, tester_client, group, "trends-uzbekistan"
    )
