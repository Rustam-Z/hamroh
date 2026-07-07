"""E2E: the bot consults a skill on request, and creates/updates one.

Consult tests assert on the recorded tool call, not the reply text: skill
content gets mangled by Telegram's HTML rendering, so the reply is unreliable.
The create and update tests assert on the SKILL.md that lands on disk.

Each consult test reads a *different* throwaway skill created under ``skills/``
for the session (the ``e2e_skills`` fixture): the shared bot session caches a
skill's content once read, so two tests can't read the same one, and no shipped
skill is pristine (they're consumed by other tests or sensitive).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import MAX_SKILL_REPLY_S
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import REPO_ROOT, Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import tool_calls_since
from tests.e2e.support.waits import wait_until

_SKILL_TOOLS = {"skill_read", "skill_list"}

_CREATE = (
    "Create a new skill for me with the skill_write tool. The skill name is "
    "'{name}'. Its SKILL.md must start with YAML frontmatter carrying "
    "name: {name} and a one-line description, and its body MUST contain this "
    "exact marker line: {token}. Reply with only the word DONE."
)
_UPDATE = (
    "Update the '{name}' skill with the skill_write tool: rewrite its SKILL.md "
    "so the body now contains this exact marker instead: {token}. Reply with "
    "only the word DONE."
)


def _skill_md_path(name: str) -> Path:
    """The on-disk SKILL.md the SUT writes (it reads skills from REPO_ROOT)."""
    return REPO_ROOT / "skills" / name / "SKILL.md"


def _file_holding(path: Path, token: str) -> list[Path]:
    """``[path]`` once it exists and contains ``token``, else ``[]`` — a
    wait_until predicate for a write that lands a beat after the reply."""
    if path.exists() and token in path.read_text(encoding="utf-8"):
        return [path]
    return []


def _seed_skill(name: str, marker: str) -> Path:
    """Write an initial SKILL.md for ``name`` so the update test has one to edit.

    Seeded directly on disk (not via the bot), so the SUT hasn't read it this
    session — the update then has to pass the read-before-write gate.
    """
    path = _skill_md_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: e2e seed skill, about to be updated.\n"
        f"---\n\n# {name}\n\n{marker}\n",
        encoding="utf-8",
    )
    return path


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


@pytest.mark.smoke
async def test_skill_created_in_dm(
    hamroh_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
    e2e_created_skill: str,
) -> None:
    """The owner has the bot create a brand-new skill in a DM.

    given  a unique, not-yet-existing skill name and a marker token
    when   the owner asks the bot to create the skill carrying the marker
    then   skill_write runs and skills/<name>/SKILL.md holds the marker.
    """
    # given — a fresh skill name that doesn't exist yet, and a marker to find
    name = e2e_created_skill
    path = _skill_md_path(name)
    marker = new_sentinel("SKILLCREATE")

    # when — the owner asks the bot to create it
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    reply = await send_and_wait(
        tester_client, dm, _CREATE.format(name=name, token=marker)
    )

    # then — the SKILL.md lands on disk with the marker...
    assert_reply_within(reply, MAX_SKILL_REPLY_S, "skill create")
    written = await wait_until(lambda: _file_holding(path, marker))
    assert written, (
        f"skill {name!r} was not created with {marker!r}; reply was {reply.text!r}"
    )
    # ...written by skill_write, not some other path
    tools = {r["tool_name"] for r in tool_calls_since(hamroh_sut.db_path, since)}
    assert "skill_write" in tools, (
        f"bot created a skill without skill_write; tools used: {sorted(tools)}"
    )


@pytest.mark.smoke
async def test_skill_updated_in_dm(
    hamroh_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
    e2e_created_skill: str,
) -> None:
    """The owner has the bot update an existing skill in a DM.

    given  a skill already on disk carrying an old marker, unread this session
    when   the owner asks the bot to rewrite it with a new marker
    then   the bot reads it first (the write gate), then skill_write lands the
           new marker in skills/<name>/SKILL.md.
    """
    # given — a skill seeded on disk with an old marker
    name = e2e_created_skill
    new_marker = new_sentinel("SKILLNEW")
    path = _seed_skill(name, new_sentinel("SKILLOLD"))

    # when — the owner asks the bot to update it
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    reply = await send_and_wait(
        tester_client, dm, _UPDATE.format(name=name, token=new_marker)
    )

    # then — the file now carries the new marker...
    assert_reply_within(reply, MAX_SKILL_REPLY_S, "skill update")
    reworked = await wait_until(lambda: _file_holding(path, new_marker))
    assert reworked, (
        f"skill {name!r} was not updated with {new_marker!r}; reply was {reply.text!r}"
    )
    # ...via skill_read (the read-before-write gate) then skill_write
    tools = {r["tool_name"] for r in tool_calls_since(hamroh_sut.db_path, since)}
    assert "skill_write" in tools, (
        f"bot updated a skill without skill_write; tools used: {sorted(tools)}"
    )
    assert "skill_read" in tools, (
        f"bot overwrote a skill without reading it first; tools used: {sorted(tools)}"
    )
