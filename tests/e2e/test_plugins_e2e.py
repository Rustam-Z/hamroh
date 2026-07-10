"""E2E: plugins.json unlocks tool groups and wires external MCP servers.

Every other e2e test runs the locked-down default (``bash``/``code``/
``subagents`` all false, no external MCPs). This module boots a dedicated bot
via the ``plugins_sut`` fixture — every tool group enabled, plus one enabled
and one disabled throwaway echo MCP — and proves each unlock end to end:

* an unlocked tool really runs (a secret only the tool can reveal lands in
  the reply, and the CC stream capture names the exact tool used);
* an enabled MCP answers with its per-run secret;
* a disabled MCP never spawns, so its secret can never appear.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.conftest import E2E_MCP_DISABLED, E2E_MCP_ENABLED
from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import MAX_SUBAGENT_REPLY_S, MAX_TOOL_GROUP_REPLY_S
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut
from tests.e2e.support.models import Conversation
from tests.e2e.support.state import cc_tool_use_names
from tests.e2e.support.waits import wait_until

_RUN_COMMAND = (
    "Use your Bash tool to run this exact command: cat {path}\n"
    "Then tell me exactly what the command printed."
)
_WRITE_FILE = (
    "Use your Write tool to create a file at {path} whose entire content is "
    "exactly this line: {token}\nReply with only the word DONE once it is written."
)
_SPAWN_SUBAGENT = (
    "Use your Agent tool to spawn one subagent with this task: "
    "'Reply with the exact text {token}'. "
    "Then reply to me with exactly what the subagent returned."
)
_CALL_MCP = (
    "Call your `{tool}` tool with the text argument set to '{token}' "
    "and reply with the tool's exact output. If the tool does not exist or "
    "the call fails, reply with exactly: TOOL-UNAVAILABLE"
)


@pytest.mark.smoke
async def test_bash_enabled_runs_a_command_in_dm(
    plugins_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
    tmp_path: Path,
) -> None:
    """With ``tool_groups.bash`` true, the bot can run a shell command.

    given  a secret written to a file the model has never seen
    when   the owner asks the bot to cat that file with its Bash tool
    then   the reply carries the secret and the CC stream shows a Bash call.
    """
    # given — a secret only a real shell command can reveal
    sut, _, _ = plugins_sut
    secret = new_sentinel("BASHRUN")
    secret_file = tmp_path / "bash_secret.txt"
    secret_file.write_text(secret, encoding="utf-8")

    # when — the owner asks for the file's content via Bash
    reply = await send_and_wait(
        tester_client, dm, _RUN_COMMAND.format(path=secret_file)
    )

    # then — the secret came back, and it came back through Bash
    assert secret in reply.text, (
        f"bash-enabled bot did not return the file secret {secret!r}; "
        f"reply was {reply.text!r}"
    )
    assert "Bash" in cc_tool_use_names(sut.cc_logs_dir), (
        "the reply carried the secret but no Bash tool_use was recorded — "
        "the bot obtained it some other way"
    )
    assert_reply_within(reply, MAX_TOOL_GROUP_REPLY_S, "bash command")


@pytest.mark.smoke
async def test_code_enabled_writes_a_file_in_dm(
    plugins_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """With ``tool_groups.code`` true, the bot can write a file to disk.

    given  a target path in the bot's data dir and a unique marker
    when   the owner asks the bot to create the file with its Write tool
    then   the file lands on disk with the marker, written by the Write tool.
    """
    # given — a fresh target path the bot has to create
    sut, _, _ = plugins_sut
    token = new_sentinel("CODEWRITE")
    target = sut.data_dir / f"e2e_code_{uuid.uuid4().hex[:8]}.txt"

    # when — the owner asks the bot to write it
    reply = await send_and_wait(
        tester_client, dm, _WRITE_FILE.format(path=target, token=token)
    )

    # then — the file exists on disk with the marker...
    written = await wait_until(
        lambda: target.exists() and token in target.read_text(encoding="utf-8")
    )
    assert written, (
        f"code-enabled bot did not create {target} with {token!r}; "
        f"reply was {reply.text!r}"
    )
    # ...and the Write tool (not some other path) produced it
    assert "Write" in cc_tool_use_names(sut.cc_logs_dir), (
        "the file appeared but no Write tool_use was recorded — "
        "the bot wrote it some other way"
    )
    assert_reply_within(reply, MAX_TOOL_GROUP_REPLY_S, "code write")


async def test_subagents_enabled_spawns_an_agent_in_dm(
    plugins_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """With ``tool_groups.subagents`` true, the bot can spawn a subagent.

    given  a task only a spawned subagent should answer
    when   the owner asks the bot to delegate it via its Agent tool
    then   the CC stream shows an Agent call and the answer comes back.
    """
    # given — a marker the subagent must relay back
    sut, _, _ = plugins_sut
    token = new_sentinel("SUBAGENT")

    # when — the owner asks the bot to delegate to a subagent
    reply = await send_and_wait(
        tester_client,
        dm,
        _SPAWN_SUBAGENT.format(token=token),
        timeout=MAX_SUBAGENT_REPLY_S,
    )

    # then — a real Agent tool_use happened and the relayed answer landed
    assert "Agent" in cc_tool_use_names(sut.cc_logs_dir), (
        f"no Agent tool_use recorded — the bot never spawned a subagent; "
        f"reply was {reply.text!r}"
    )
    assert token in reply.text, (
        f"subagent's answer {token!r} missing from the reply {reply.text!r}"
    )
    assert_reply_within(reply, MAX_SUBAGENT_REPLY_S, "subagent")


@pytest.mark.smoke
async def test_enabled_mcp_tool_answers_in_dm(
    plugins_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """An ``enabled: true`` MCP in plugins.json is connected and callable.

    given  the echo MCP server holding a per-run secret only it knows
    when   the owner asks the bot to call the server's echo tool
    then   the reply carries the secret — proof the call went end to end.
    """
    # given — the enabled echo server and its secret
    _, secret, _ = plugins_sut
    token = new_sentinel("MCPCALL")

    # when — the owner asks the bot to call the echo tool
    reply = await send_and_wait(
        tester_client,
        dm,
        _CALL_MCP.format(tool=f"mcp__{E2E_MCP_ENABLED}__echo", token=token),
    )

    # then — the server's secret is in the reply (unknowable without the call)
    assert secret in reply.text, (
        f"enabled MCP secret {secret!r} missing from the reply — the echo "
        f"tool was never really called; reply was {reply.text!r}"
    )
    assert_reply_within(reply, MAX_TOOL_GROUP_REPLY_S, "enabled mcp echo")


@pytest.mark.smoke
async def test_disabled_mcp_tool_is_unreachable_in_dm(
    plugins_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """An ``enabled: false`` MCP in plugins.json is never spawned.

    given  a disabled echo MCP entry holding its own secret
    when   the owner asks the bot to call that server's echo tool anyway
    then   the reply can never carry the disabled server's secret.
    """
    # given — the disabled echo server's secret
    _, _, disabled_secret = plugins_sut
    token = new_sentinel("MCPDENY")

    # when — the owner asks the bot to call the disabled tool
    reply = await send_and_wait(
        tester_client,
        dm,
        _CALL_MCP.format(tool=f"mcp__{E2E_MCP_DISABLED}__echo", token=token),
    )

    # then — the disabled server never ran, so its secret cannot appear
    assert disabled_secret not in reply.text, (
        f"disabled MCP answered with its secret {disabled_secret!r} — the "
        f"server was spawned despite enabled=false; reply was {reply.text!r}"
    )
