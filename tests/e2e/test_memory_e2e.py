"""E2E: the bot writes to memory, reads it back, searches it, and survives a reset.

write+read (DM and group, separate tests): remember a codeword, confirm it
    lands in a ``memories/`` file, and the bot recalls it.
search (DM and group, separate tests): seed a fact under an unhelpful filename,
    ask a content question, and confirm the bot answers it AND actually called
    ``memory_search`` to find it (not list + read-everything).
read by path (DM only): seed two docs in ``memories/``, then confirm the bot
    reads the fact out of EACH by its full project path — proving ``memory_read``
    works by exact path.
description refresh (DM only): seed a doc whose description describes only its
    old content, add a new fact to it, and confirm the frontmatter description
    on disk no longer describes the file as it was — so ``memory_list`` never
    advertises a stale summary.
reset (DM only): the codeword survives ``/reset_session`` — proving
    cross-session persistence, not just in-context recall. The reset is an
    owner command, kept in a DM to avoid group command-addressing quirks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from hamroh.utils.frontmatter import parse_frontmatter
from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import memory_files_containing, tool_calls_since
from tests.e2e.support.config import MAX_MEMORY_REPLY_S
from tests.e2e.support.waits import wait_until

_REMEMBER = (
    "Remember this codeword and write it to a memory file: {cw}. "
    "Reply with only the word OK."
)
_RECALL = "What was the codeword I asked you to remember? Reply with ONLY the codeword."
# After a reset the shared bot's memory file holds several codewords from
# earlier tests, so recall the full list rather than an ambiguous "the" one.
_RECALL_ALL = "List every codeword you have saved in your memory."
_SEARCH = (
    "Search your memory for {cw} and tell me the launch date saved for it. "
    "Reply with ONLY the date."
)
_READ_DOC = (
    "Read the memory file at {path} and tell me the launch date saved in it. "
    "Reply with ONLY the date."
)
_ADD_FACT = (
    "Add this new fact to your existing memory file at {path}, keeping what is "
    "already written there: {fact}. Reply with only the word DONE."
)

#: Seeded description of a file that holds nothing but a launch date. Once the
#: bot adds a fact about the venue, a refreshed description cannot still be this.
_STALE_DESCRIPTION = "e2e seed: holds only the launch date, nothing else"

#: Tools that can land the new fact. ``memory_append`` refreshes the description
#: structurally (it's a required arg); ``memory_write`` rewrites the whole file
#: including frontmatter. Either satisfies the test — the assertion is on what
#: reached disk, not on which tool the model picked.
_MEMORY_WRITE_TOOLS = {"memory_append", "memory_write"}


def _seed_memory_doc(memories_dir: Path, codeword: str, launch_date: str) -> str:
    """Write a templated doc holding ``launch_date`` under ``memories_dir``.

    Returns the full project path the bot reads by (``memories/notes/{codeword}.md``).
    """
    subpath = f"notes/{codeword}.md"
    path = memories_dir / subpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {codeword}\ndescription: e2e read probe for {codeword}\n---\n\n"
        f"Project {codeword} launch date: {launch_date}\n"
    )
    return f"memories/{subpath}"


def _seed_stale_memory_doc(memories_dir: Path, codeword: str) -> tuple[str, Path]:
    """Seed a doc carrying :data:`_STALE_DESCRIPTION`, ready to be added to.

    Written straight to disk rather than through the bot, so the SUT has not
    read it this session — the update then has to pass the read-before-write
    gate, exactly as a real cross-session update would.

    Returns ``(full project path, on-disk path)``.
    """
    subpath = f"notes/{codeword}.md"
    path = memories_dir / subpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {codeword}\ndescription: {_STALE_DESCRIPTION}\n---\n\n"
        f"Project {codeword} launch date: 2027-03-14\n",
        encoding="utf-8",
    )
    return f"memories/{subpath}", path


def _description_of(path: Path) -> str | None:
    """The frontmatter ``description`` of ``path``, or ``None`` if it has none."""
    metadata, _ = parse_frontmatter(
        path.read_text(encoding="utf-8"), error_cls=AssertionError, label="memory file"
    )
    description = metadata.get("description")
    return description.strip() if isinstance(description, str) else None


def _file_holding(path: Path, token: str) -> list[Path]:
    """``[path]`` once the file contains ``token``, else ``[]``.

    A ``wait_until`` predicate: the write lands a beat after the reply does.
    """
    if token in path.read_text(encoding="utf-8"):
        return [path]
    return []


async def _assert_write_and_read(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    codeword = new_sentinel("BANANA")
    saved = await send_and_wait(client, convo, _REMEMBER.format(cw=codeword))
    assert_reply_within(saved, MAX_MEMORY_REPLY_S, "memory write")
    on_disk = await wait_until(
        lambda: memory_files_containing(sut.memories_dir, codeword)
    )
    assert on_disk, f"{codeword!r} not in any memory file; reply was {saved.text!r}"
    recalled = await send_and_wait(client, convo, _RECALL)
    assert codeword in recalled.text, (
        f"bot did not recall {codeword!r}; reply was {recalled.text!r}"
    )
    assert_reply_within(recalled, MAX_MEMORY_REPLY_S, "memory recall")


async def _assert_search_finds_seeded_fact(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    # Seed a fact under a filename that gives no hint of its contents, so the
    # only way to answer is to search the TEXT — not guess the right file to read.
    codeword = new_sentinel("KIWI")
    launch_date = "2027-03-14"
    seeded = sut.memories_dir / "archive" / "misc-notes.md"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text(f"Project {codeword} launch date: {launch_date}\n")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    reply = await send_and_wait(client, convo, _SEARCH.format(cw=codeword))

    assert launch_date in reply.text, (
        f"bot did not recall the seeded date; reply was {reply.text!r}"
    )
    tools = {row["tool_name"] for row in tool_calls_since(sut.db_path, since)}
    assert "memory_search" in tools, (
        f"bot answered without calling memory_search; tools used: {sorted(tools)}"
    )
    assert_reply_within(reply, MAX_MEMORY_REPLY_S, "memory search")


@pytest.mark.smoke
async def test_memory_write_and_read_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot writes a codeword to memory and reads it back in a DM.

    given  a unique codeword
    when   the owner asks the bot to remember it in a DM
    then   it lands in a memory file and the bot recalls it within MAX_MEMORY_REPLY_S.
    """
    await _assert_write_and_read(hamroh_sut, tester_client, dm)


@pytest.mark.smoke
async def test_memory_write_and_read_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot writes a codeword to memory and reads it back in a group.

    given  a unique codeword
    when   the owner asks the bot to remember it in a group
    then   it lands in a memory file and the bot recalls it within MAX_MEMORY_REPLY_S.
    """
    await _assert_write_and_read(hamroh_sut, tester_client, group)


async def test_memory_search_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """The bot searches memory contents to answer a question in a DM.

    given  a fact seeded under a filename that hides its contents
    when   the owner asks a content question about it in a DM
    then   the bot calls memory_search, answers correctly, within MAX_MEMORY_REPLY_S.
    """
    await _assert_search_finds_seeded_fact(hamroh_sut, tester_client, dm)


@pytest.mark.smoke
async def test_memory_search_group(
    hamroh_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    """The bot searches memory contents to answer a question in a group.

    given  a fact seeded under a filename that hides its contents
    when   the owner asks a content question about it in a group
    then   the bot calls memory_search, answers correctly, within MAX_MEMORY_REPLY_S.
    """
    await _assert_search_finds_seeded_fact(hamroh_sut, tester_client, group)


@pytest.mark.smoke
async def test_memory_read_by_full_path_dm(
    hamroh_sut: Sut,
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """The bot reads two ``memories/`` docs by their exact full project paths.

    given  two docs seeded in ``memories/``, each with a distinct launch date
    when   the owner asks the bot to read each file by its full project path in a DM
    then   the bot returns both dates and used memory_read — proving reads work
           by exact path, within MAX_MEMORY_REPLY_S each.
    """
    # given — two docs under memories/, each with its own date
    first_date = "2027-03-14"
    first_rel = _seed_memory_doc(
        hamroh_sut.memories_dir, new_sentinel("ALPHA"), first_date
    )
    second_date = "2028-09-22"
    second_rel = _seed_memory_doc(
        hamroh_sut.memories_dir, new_sentinel("BETA"), second_date
    )
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # when — ask the bot to read the first doc
    first_reply = await send_and_wait(
        tester_client, dm, _READ_DOC.format(path=first_rel)
    )
    # then — it reports the first date
    assert first_date in first_reply.text, (
        f"bot did not read doc {first_rel!r}; reply was {first_reply.text!r}"
    )
    assert_reply_within(first_reply, MAX_MEMORY_REPLY_S, "memory read (first)")

    # when — ask the bot to read the second doc
    second_reply = await send_and_wait(
        tester_client, dm, _READ_DOC.format(path=second_rel)
    )
    # then — it reports the second date
    assert second_date in second_reply.text, (
        f"bot did not read doc {second_rel!r}; reply was {second_reply.text!r}"
    )
    assert_reply_within(second_reply, MAX_MEMORY_REPLY_S, "memory read (second)")

    # then — it actually called memory_read (not guessed from context)
    tools = {row["tool_name"] for row in tool_calls_since(hamroh_sut.db_path, since)}
    assert "memory_read" in tools, (
        f"bot answered without calling memory_read; tools used: {sorted(tools)}"
    )


@pytest.mark.smoke
async def test_memory_description_refreshed_when_fact_added_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """Adding a fact to a memory file refreshes its frontmatter description.

    given  a memory doc on disk whose description describes only its old
           content, unread by the bot this session
    when   the owner asks the bot to add a new, unrelated fact to that file
    then   the fact lands in the body, the description no longer reads as the
           seeded one, and the bot read the file before writing it.
    """
    # given — a doc whose description advertises only the launch date
    codeword = new_sentinel("CHERRY")
    venue = new_sentinel("VENUE")
    project_path, on_disk = _seed_stale_memory_doc(hamroh_sut.memories_dir, codeword)
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # when — the owner adds a fact the old description could not cover
    fact = f"the launch venue is {venue}"
    reply = await send_and_wait(
        tester_client, dm, _ADD_FACT.format(path=project_path, fact=fact)
    )

    # then — the new fact reaches the body, keeping the old content
    assert_reply_within(reply, MAX_MEMORY_REPLY_S, "memory description refresh")
    updated = await wait_until(lambda: _file_holding(on_disk, venue))
    assert updated, f"{venue!r} never reached {project_path}; reply was {reply.text!r}"
    assert "2027-03-14" in on_disk.read_text(encoding="utf-8"), (
        f"adding a fact to {project_path} destroyed the launch date already in it"
    )

    # then — the description was rewritten, so memory_list won't show a stale one
    description = _description_of(on_disk)
    assert description is not None, (
        f"{project_path} lost its frontmatter description on update"
    )
    assert description != _STALE_DESCRIPTION, (
        f"{project_path} still advertises its seeded description "
        f"{_STALE_DESCRIPTION!r} after {fact!r} was added"
    )

    # then — it landed via a memory write tool, after reading the file first
    tools = {row["tool_name"] for row in tool_calls_since(hamroh_sut.db_path, since)}
    assert tools & _MEMORY_WRITE_TOOLS, (
        f"the fact reached disk without {sorted(_MEMORY_WRITE_TOOLS)}; "
        f"tools used: {sorted(tools)}"
    )
    assert "memory_read" in tools, (
        f"bot updated {project_path} without reading it first; "
        f"tools used: {sorted(tools)}"
    )


@pytest.mark.smoke
async def test_memory_survives_session_reset_dm(
    hamroh_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    """A codeword in memory survives /reset_session in a DM.

    given  a codeword written to a memory file
    when   the Claude session is reset in a DM
    then   the bot still recalls it from disk within MAX_MEMORY_REPLY_S.
    """
    codeword = new_sentinel("MANGO")
    await send_and_wait(tester_client, dm, _REMEMBER.format(cw=codeword))
    on_disk = await wait_until(
        lambda: memory_files_containing(hamroh_sut.memories_dir, codeword)
    )
    assert on_disk, f"{codeword!r} was not persisted to a memory file"

    # reset drops the in-context session; memories on disk survive
    await send_and_wait(tester_client, dm, "/reset_session", timeout=60)

    recalled = await send_and_wait(tester_client, dm, _RECALL_ALL)
    assert codeword in recalled.text, (
        f"bot did not recall {codeword!r} after reset; reply was {recalled.text!r}"
    )
    assert_reply_within(recalled, MAX_MEMORY_REPLY_S, "post-reset recall")
