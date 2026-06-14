"""Fixtures and skip-gating for the real end-to-end suite.

The suite is opt-in. ``pytest_collection_modifyitems`` tags every test in
this directory with the ``e2e`` marker and skips the lot whenever the
``claude`` CLI or the ``E2E_*`` credentials are absent — mirroring
``tests/test_mcp_integration.py``. So a plain ``pytest`` stays green for
contributors who have not set up a test bot.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import AsyncIterator, Generator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from tests.e2e.support.harness import (
    E2EConfig,
    Sut,
    launch_sut,
    load_e2e_env,
    missing_env,
    stop_sut,
)
from tests.e2e.support.helpers import Conversation

log = logging.getLogger(__name__)
_HERE = Path(__file__).parent
load_e2e_env()  # pick up tests/e2e/.env.e2e before the skip-gate runs


def _skip_reason() -> str | None:
    """Why the e2e suite can't run here, or ``None`` if it can."""
    if shutil.which("claude") is None:
        return "claude CLI not on PATH"
    missing = missing_env()
    if missing:
        return f"missing env: {', '.join(missing)}"
    return None


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark this directory's tests ``e2e`` and skip them when unconfigured."""
    reason = _skip_reason()
    skip = pytest.mark.skip(reason=f"e2e: {reason}") if reason else None
    for item in items:
        if _HERE not in Path(str(item.fspath)).parents:
            continue
        item.add_marker(pytest.mark.e2e)
        if skip is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    return E2EConfig.from_env()


@pytest.fixture(scope="session")
def pyclaudir_sut(
    e2e_config: E2EConfig, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Sut]:
    """Launch one bot subprocess for the whole session (boot is expensive;
    tests isolate via unique sentinels, not restarts)."""
    sut = launch_sut(e2e_config, tmp_path_factory.mktemp("e2e-data"))
    try:
        yield sut
    finally:
        stop_sut(sut)


@pytest_asyncio.fixture
async def tester_client(e2e_config: E2EConfig) -> AsyncIterator[TelegramClient]:
    """A connected Telethon user client (per test — connect is cheap and
    sidesteps cross-test event-loop scoping)."""
    client = TelegramClient(
        StringSession(e2e_config.session), e2e_config.api_id, e2e_config.api_hash
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


@pytest_asyncio.fixture
async def bot(tester_client: TelegramClient, e2e_config: E2EConfig) -> object:
    """The bot entity — the DM chat and the reply sender are both this."""
    return await tester_client.get_entity(e2e_config.bot_username)


@pytest_asyncio.fixture
async def dm(bot: object) -> Conversation:
    """A direct-message conversation: send to the bot, expect it to reply."""
    return Conversation(chat=bot, reply_from=bot)


async def _group_conversation(
    client: TelegramClient, cfg: E2EConfig, bot: object
) -> Conversation:
    """Send to the test group, @mentioning the bot (privacy-mode safe)."""
    entity = await client.get_entity(cfg.group_id)
    return Conversation(chat=entity, reply_from=bot, mention=cfg.bot_username)


@pytest_asyncio.fixture
async def group(
    tester_client: TelegramClient, e2e_config: E2EConfig, bot: object
) -> Conversation:
    """A group conversation. Tests use the `dm` and `group` fixtures directly
    in separate per-chat test functions (no parametrization)."""
    return await _group_conversation(tester_client, e2e_config, bot)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Stash each phase's report on the item so fixtures can see failures."""
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(autouse=True)
def _dump_sut_log_on_failure(
    request: pytest.FixtureRequest, pyclaudir_sut: Sut
) -> Iterator[None]:
    """On test failure, print the bot's recent output for debugging."""
    yield
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        log.error("pyclaudir SUT log tail:\n%s", pyclaudir_sut.log_tail())
