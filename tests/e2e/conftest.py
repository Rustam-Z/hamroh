"""Fixtures and skip-gating for the real end-to-end suite.

The suite is opt-in. ``pytest_collection_modifyitems`` tags every test in
this directory with the ``e2e`` marker and skips the lot whenever the
``claude`` CLI or the ``E2E_*`` credentials are absent — mirroring
``tests/test_mcp_integration.py``. So a plain ``pytest`` stays green for
contributors who have not set up a test bot.
"""

from __future__ import annotations

import itertools
import json
import logging
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Generator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from tests.e2e.support.config import (
    E2EConfig,
    group_ids,
    load_env,
    missing_env,
)
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import (
    REPO_ROOT,
    Sut,
    dump_sut_logs,
    kill_stray_suts,
    launch_sut,
    reset_sut_logs,
    stop_sut,
)
from tests.e2e.support.models import Conversation

log = logging.getLogger(__name__)
_HERE = Path(__file__).parent
load_env()  # pick up the root .env before the skip-gate runs


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
def _free_bot_token() -> None:
    """Kill stray hamroh processes once, so the SUT can claim the token.

    Only one process may poll a bot token; an orphan from a crashed run (or a
    dev bot) would make Telegram reject the SUT's getUpdates.
    """
    kill_stray_suts()


#: Throwaway reference skills written under ``skills/`` for the session. Two
#: distinct ones so the DM and group skill-consult tests each read a *pristine*
#: skill — the shared session caches a skill's content once read, so they can't
#: share. None of the shipped skills are usable (consumed by other tests or
#: sensitive), so the suite fakes its own.
_E2E_SKILL_NAMES = ("e2e-probe-dm", "e2e-probe-group")


def _e2e_skill_md(name: str) -> str:
    """Minimal valid SKILL.md whose frontmatter name matches its directory."""
    return (
        f"---\nname: {name}\n"
        "description: Throwaway reference skill for the e2e suite — proves the "
        "bot reads a skill on request. No real content.\n---\n\n"
        f"# Skill: {name}\n\n"
        "A probe skill used only by end-to-end tests. When asked to read it, "
        "reply with a one-line summary: the e2e probe skill was read.\n"
    )


@pytest.fixture(scope="session")
def e2e_skills() -> Iterator[tuple[str, str]]:
    """Write two pristine throwaway skills under ``skills/`` for the session.

    The SUT reads skills from ``REPO_ROOT/skills`` (its cwd) with no env
    override, so a skill-consult test needs a real file there. Created before the
    bot launches (``hamroh_sut`` depends on this) so they land in the baked
    skills index, and removed on teardown so they never get committed (see
    .gitignore). Returns ``(dm_skill, group_skill)``.
    """
    dirs = [REPO_ROOT / "skills" / name for name in _E2E_SKILL_NAMES]
    for skill_dir, name in zip(dirs, _E2E_SKILL_NAMES):
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(_e2e_skill_md(name), encoding="utf-8")
    try:
        yield _E2E_SKILL_NAMES
    finally:
        for skill_dir in dirs:
            shutil.rmtree(skill_dir, ignore_errors=True)


@pytest.fixture
def e2e_created_skill() -> Iterator[str]:
    """A unique, gitignored skill name the bot is free to create then update.

    The name starts with ``e2e-`` so it matches the ``.gitignore`` throwaway
    pattern (``skills/e2e-*/``) and never gets committed, and it's lowercase-
    hyphen so it satisfies the skill-name spec. Function-scoped: each test gets
    a fresh, never-before-seen name (so the read-before-write gate is exercised
    honestly) and the directory is removed on teardown.
    """
    name = f"e2e-created-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        shutil.rmtree(REPO_ROOT / "skills" / name, ignore_errors=True)


@pytest.fixture(scope="session")
def hamroh_sut(
    _free_bot_token: None,
    e2e_config: E2EConfig,
    e2e_skills: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Sut]:
    """Launch one bot subprocess for the whole session (boot is expensive;
    tests isolate via unique sentinels, not restarts). Depends on ``e2e_skills``
    so the throwaway probe skills exist before the bot bakes its skills index."""
    sut = launch_sut(e2e_config, tmp_path_factory.mktemp("e2e-data"))
    try:
        yield sut
    finally:
        stop_sut(sut)


@pytest.fixture
def killable_sut(
    hamroh_sut: Sut, e2e_config: E2EConfig, tmp_path: Path
) -> Iterator[Sut]:
    """A throwaway bot the test is free to /kill, with the shared SUT revived after.

    Only one process may poll a bot token, so we stop the shared session SUT to
    free the token, launch a victim on the same token, and hand it over. On
    teardown the victim is gone (the test killed it) and we relaunch the shared
    SUT *in place* — swapping ``proc``/``_log`` on the same object keeps the
    session fixture's reference valid, so later tests reuse the revived bot.
    """
    stop_sut(hamroh_sut)
    victim = launch_sut(e2e_config, tmp_path / "victim")
    try:
        yield victim
    finally:
        stop_sut(victim)
        revived = launch_sut(e2e_config, hamroh_sut.data_dir)
        hamroh_sut.proc = revived.proc
        hamroh_sut._log = revived._log


@pytest.fixture(scope="module")
def default_reminders_sut(
    hamroh_sut: Sut,
    e2e_config: E2EConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Sut, str, str]]:
    """A bot booted with a committed ``default-reminders.json`` seeded at startup.

    Writes two recurring reminders (every minute, to the owner): one enabled
    whose text carries ``token``, and one ``"enabled": false`` whose text carries
    ``disabled_token``. Then launches a dedicated SUT pointed at that file via
    ``HAMROH_REMINDERS_PATH`` — so the test never touches the repo-root copy. Same
    stop-launch-revive swap as ``killable_sut`` (only one process may poll the bot
    token). The every-minute cron lets the @slow fire test see it deliver within
    one poll cycle. Yields ``(sut, token, disabled_token)``.
    """
    token = new_sentinel("DEFAULTREMIND")
    disabled_token = new_sentinel("DISABLEDREMIND")
    reminders_file = (
        tmp_path_factory.mktemp("committed-reminders") / "default-reminders.json"
    )
    reminders_file.write_text(
        json.dumps(
            {
                "reminders": [
                    {
                        "name": "e2e-default",
                        "cron": "* * * * *",
                        "chat": "owner",
                        "text": token,
                    },
                    {
                        "name": "e2e-disabled",
                        "cron": "* * * * *",
                        "chat": "owner",
                        "enabled": False,
                        "text": disabled_token,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    stop_sut(hamroh_sut)
    sut = launch_sut(
        e2e_config,
        tmp_path_factory.mktemp("default-reminders-data"),
        extra_env={"HAMROH_REMINDERS_PATH": str(reminders_file)},
    )
    try:
        yield sut, token, disabled_token
    finally:
        stop_sut(sut)
        revived = launch_sut(e2e_config, hamroh_sut.data_dir)
        hamroh_sut.proc = revived.proc
        hamroh_sut._log = revived._log


#: The MCP server names the ``plugins_sut`` bot is booted with. The enabled one
#: must answer tool calls; the disabled one must never even spawn.
E2E_MCP_ENABLED = "e2e-echo"
E2E_MCP_DISABLED = "e2e-echo-off"
_ECHO_MCP_SCRIPT = _HERE / "support" / "echo_mcp.py"


def _echo_mcp_entry(name: str, secret: str, enabled: bool) -> dict[str, object]:
    """One stdio plugins.json entry running the throwaway echo MCP server.

    The server holds ``secret`` in its env and returns it from every ``echo``
    call — the model can only learn the secret through a real tool call.
    """
    return {
        "name": name,
        "command": sys.executable,
        "args": [str(_ECHO_MCP_SCRIPT)],
        "env": {"E2E_MCP_SECRET": secret},
        "allowed_tools": [f"mcp__{name}"],
        "enabled": enabled,
    }


def _write_plugins_json(path: Path, secret: str, disabled_secret: str) -> None:
    """Write the plugins.json the ``plugins_sut`` bot boots with: every tool
    group true, plus one enabled and one disabled echo MCP entry."""
    path.write_text(
        json.dumps(
            {
                "tool_groups": {"bash": True, "code": True, "subagents": True},
                "mcps": [
                    _echo_mcp_entry(E2E_MCP_ENABLED, secret, enabled=True),
                    _echo_mcp_entry(E2E_MCP_DISABLED, disabled_secret, enabled=False),
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def plugins_sut(
    hamroh_sut: Sut,
    e2e_config: E2EConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Sut, str, str]]:
    """A bot with every tool group enabled and two echo MCPs (on and off).

    Writes a dedicated ``plugins.json`` — ``bash``/``code``/``subagents`` all
    true, plus two stdio echo MCP entries, one enabled and one disabled, each
    holding its own fresh secret. The SUT reads it via ``HAMROH_PLUGINS_PATH``,
    so the repo-root ``plugins.json`` (the locked-down default every other test
    runs under) is untouched. Same stop-launch-revive swap dance as
    ``killable_sut`` (only one process may poll the bot token). Yields
    ``(sut, secret, disabled_secret)``: a reply carrying ``secret`` proves the
    enabled MCP really answered; the disabled server never spawns, so
    ``disabled_secret`` can never appear in any reply.
    """
    secret = new_sentinel("MCPON")
    disabled_secret = new_sentinel("MCPOFF")
    plugins_file = tmp_path_factory.mktemp("plugins") / "plugins.json"
    _write_plugins_json(plugins_file, secret, disabled_secret)
    stop_sut(hamroh_sut)
    sut = launch_sut(
        e2e_config,
        tmp_path_factory.mktemp("plugins-data"),
        extra_env={"HAMROH_PLUGINS_PATH": str(plugins_file)},
    )
    try:
        yield sut, secret, disabled_secret
    finally:
        stop_sut(sut)
        revived = launch_sut(e2e_config, hamroh_sut.data_dir)
        hamroh_sut.proc = revived.proc
        hamroh_sut._log = revived._log


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


@pytest.fixture(scope="session")
def _group_id_pool() -> itertools.cycle[int]:
    """Round-robin over the groups in access.json (raises if there are none)."""
    return itertools.cycle(group_ids())


@pytest.fixture
def group_id(_group_id_pool: itertools.cycle[int]) -> int:
    """One test group per test, rotating through access.json's allowed_chats.

    Resolved once per test, so a test's `group` conversation and any id it builds
    access rules from refer to the same chat.
    """
    return next(_group_id_pool)


async def _group_conversation(
    client: TelegramClient, cfg: E2EConfig, group_id: int, bot: object
) -> Conversation:
    """Send to the test group, @mentioning the bot (privacy-mode safe)."""
    entity = await client.get_entity(group_id)
    return Conversation(chat=entity, reply_from=bot, mention=cfg.bot_username)


@pytest_asyncio.fixture
async def group(
    tester_client: TelegramClient, e2e_config: E2EConfig, group_id: int, bot: object
) -> Conversation:
    """A group conversation. Tests use the `dm` and `group` fixtures directly
    in separate per-chat test functions (no parametrization)."""
    return await _group_conversation(tester_client, e2e_config, group_id, bot)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Stash each phase's report on the item so fixtures can see failures."""
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    """Clear the SUT output rings right before the test body so a failure dump
    carries only this test's lines — never the shared bot's earlier output."""
    reset_sut_logs()
    return (yield)


@pytest.fixture(autouse=True)
def _dump_sut_log_on_failure(
    request: pytest.FixtureRequest, hamroh_sut: Sut
) -> Iterator[None]:
    """On test failure, print the recent output of whichever SUT ran the test."""
    yield
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        log.error("hamroh SUT log tail:\n%s", dump_sut_logs())
