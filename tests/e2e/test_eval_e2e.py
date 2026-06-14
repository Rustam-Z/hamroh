"""E2E: the latency + correctness eval runs as part of the suite.

given  a warm bot and the shared scenario set
when    each scenario runs ``E2E_EVAL_RUNS`` times in a chat (a DM, a group)
then    that chat's per-feature latency table is logged, and its correctness
        pass-rate stays at or above ``E2E_EVAL_MIN_PASS``.

The DM and group cases are separate tests (they fail and run independently).
Latency is reported, not gated: a single sample flakes near an SLO, so the
table is informational (raise ``E2E_EVAL_RUNS`` for trustworthy percentiles).
The correctness pass-rate over the matrix is the stable signal we assert.
A warm-up turn pays the one-time startup cost off the clock. Both tests reuse
the one session bot via ``pyclaudir_sut`` — no second bot is spawned.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.eval import chat_label, run_eval
from tests.e2e.support.harness import Sut
from tests.e2e.support.helpers import Conversation, send_and_wait

log = logging.getLogger(__name__)
_RUNS = int(os.environ.get("E2E_EVAL_RUNS", "1"))
_MIN_PASS = float(os.environ.get("E2E_EVAL_MIN_PASS", "0.9"))


async def _eval_chat(
    client: TelegramClient, convo: Conversation, db_path: Path
) -> None:
    # when every scenario runs in this chat
    report = await run_eval(client, convo, db_path, _RUNS)
    chat = chat_label(convo)
    log.info("\n=== eval %s (%d runs/scenario) ===\n%s", chat, _RUNS, report.table)
    # then the bot answered correctly across it (latency is just logged)
    assert report.pass_rate >= _MIN_PASS, (
        f"{chat} eval pass-rate {report.pass_rate:.0%} < {_MIN_PASS:.0%}\n{report.table}"
    )


async def test_eval_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    # given a warm bot (the first turn pays startup cost; not measured)
    await send_and_wait(tester_client, dm, "Hello, are you there?")
    await _eval_chat(tester_client, dm, pyclaudir_sut.db_path)


async def test_eval_group(
    pyclaudir_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    await _eval_chat(tester_client, group, pyclaudir_sut.db_path)
