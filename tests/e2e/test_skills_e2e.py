"""E2E: the bot consults a skill on request, in a DM and a group.

We assert on the recorded tool call, not the reply text: skill content gets
mangled by Telegram's HTML rendering, so the reply is unreliable.

Each test reads a *different* throwaway skill created under ``skills/`` for the
session (the ``e2e_skills`` fixture): the shared bot session caches a skill's
content once read, so two tests can't read the same one, and no shipped skill is
pristine (they're consumed by other tests or sensitive).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import MAX_SKILL_REPLY_S
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import tool_calls_since
from tests.e2e.support.waits import wait_until

_SKILL_TOOLS = {"skill_read", "skill_list"}


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
    assert calls, f"no skill_read/skill_list tool call recorded for {skill!r}"
    assert_reply_within(reply, MAX_SKILL_REPLY_S, "skill")


@pytest.mark.smoke
async def test_skill_consulted_in_dm(
    hamroh_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
    e2e_skills: tuple[str, str],
) -> None:
    """Bot consults a skill to answer a request in a DM.

    given  a request to read the throwaway e2e DM skill
    when   the tester asks in a DM
    then   the bot invokes a skills tool and replies within MAX_SKILL_REPLY_S.
    """
    await _assert_consults_skill(hamroh_sut, tester_client, dm, e2e_skills[0])


@pytest.mark.smoke
async def test_skill_consulted_in_group(
    hamroh_sut: Sut,
    tester_client: TelegramClient,
    group: Conversation,
    e2e_skills: tuple[str, str],
) -> None:
    """Bot consults a skill to answer a request in a group.

    given  a request to read the throwaway e2e group skill
    when   the tester asks in a group
    then   the bot invokes a skills tool and replies within MAX_SKILL_REPLY_S.
    """
    await _assert_consults_skill(hamroh_sut, tester_client, group, e2e_skills[1])
