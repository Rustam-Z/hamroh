"""E2E: the owner /kill command shuts the bot process down.

/kill SIGTERMs the process, so this runs against a throwaway ``killable_sut``
rather than the shared session bot. The reply ("Shutting down…") may or may not
land before the process dies, so the assertion watches the process, not the chat.
"""

from __future__ import annotations

import logging

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.client import send
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.config import MAX_KILL_S
from tests.e2e.support.waits import wait_until

log = logging.getLogger(__name__)


async def _assert_kill_exits(
    client: TelegramClient, convo: Conversation, victim: Sut
) -> None:
    await send(client, convo, "/kill")
    # the reply may not arrive before the process dies, so watch the process
    exited = await wait_until(
        lambda: victim.proc.poll() is not None, timeout=MAX_KILL_S
    )
    assert exited, f"bot did not exit within {MAX_KILL_S:.0f}s of /kill"


@pytest.mark.smoke
@pytest.mark.slow
async def test_kill_command_dm(
    tester_client: TelegramClient, dm: Conversation, killable_sut: Sut
) -> None:
    """/kill shuts the bot process down from a DM.

    given  the owner and a throwaway bot
    when   they send /kill in a DM
    then   the bot process exits within MAX_KILL_S.
    """
    await _assert_kill_exits(tester_client, dm, killable_sut)


@pytest.mark.smoke
@pytest.mark.slow
async def test_kill_command_group(
    tester_client: TelegramClient, group: Conversation, killable_sut: Sut
) -> None:
    """/kill shuts the bot process down from a group.

    given  the owner and a throwaway bot
    when   they send /kill in a group
    then   the bot process exits within MAX_KILL_S.
    """
    await _assert_kill_exits(tester_client, group, killable_sut)
