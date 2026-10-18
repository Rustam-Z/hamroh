"""E2E: an extension package adds a tool and an owner command to a bot.

Every other e2e test runs the framework as shipped. This module boots a
dedicated bot via the ``extensions_sut`` fixture whose ``plugins.json`` names
a throwaway extension package written into a temp dir. The package ships one
tool and one owner command, each revealing a per-run secret the model has
never seen, and proves both end to end:

* the extension tool is callable like a built-in (its secret lands in a
  reply);
* the extension command is handled by the dispatcher, not the engine (its
  secret comes back as fast as a built-in command).
"""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.assertions import assert_reply_within
from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import (
    MAX_TEXT_REPLY_S,
    MAX_TOOL_GROUP_REPLY_S,
    E2EConfig,
)
from tests.e2e.support.data import new_sentinel
from tests.e2e.support.harness import Sut, launch_sut, stop_sut
from tests.e2e.support.models import Conversation

_PACKAGE = "e2e_ext"

_INIT_SOURCE = '''
"""Throwaway e2e extension: one owner command."""
import os

from telegram import Update
from telegram.ext import ContextTypes


async def _ext_ping(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "ext pong " + os.environ["E2E_EXT_CMD_SECRET"]
    )


def register(dispatcher) -> None:
    dispatcher.add_owner_command("ext_ping", "e2e extension ping", _ext_ping)
'''

_TOOL_SOURCE = '''
"""Throwaway e2e extension: one tool that reveals a per-run secret."""
import os

from pydantic import BaseModel

from hamroh.tools.base import BaseTool, ToolResult


class ExtSecretArgs(BaseModel):
    pass


class ExtSecretTool(BaseTool):
    name = "ext_secret"
    description = "Return the e2e extension secret. Takes no arguments."
    args_model = ExtSecretArgs

    async def run(self, args: ExtSecretArgs) -> ToolResult:
        return ToolResult(content=os.environ["E2E_EXT_TOOL_SECRET"])
'''

_CALL_TOOL = (
    "Call your hamroh tool named `ext_secret` (it takes no arguments) and "
    "reply with the tool's exact output. If the tool does not exist or the "
    "call fails, reply with exactly: TOOL-UNAVAILABLE"
)


def _write_extension(root: Path) -> None:
    """Lay out the ``e2e_ext`` package under ``root``."""
    pkg = root / _PACKAGE
    (pkg / "tools").mkdir(parents=True)
    (pkg / "__init__.py").write_text(textwrap.dedent(_INIT_SOURCE), encoding="utf-8")
    (pkg / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "tools" / "secret.py").write_text(
        textwrap.dedent(_TOOL_SOURCE), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def extensions_sut(
    hamroh_sut: Sut,
    e2e_config: E2EConfig,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Sut, str, str]]:
    """A bot booted with the throwaway extension package enabled.

    Writes the package and a ``plugins.json`` naming it, then launches the SUT
    with ``PYTHONPATH`` pointing at the package and ``HAMROH_PLUGINS_PATH`` at
    the file — the repo-root ``plugins.json`` stays untouched. Same
    stop-launch-revive dance as ``plugins_sut`` (one process per bot token).
    Yields ``(sut, tool_secret, command_secret)``.
    """
    tool_secret = new_sentinel("EXTTOOL")
    command_secret = new_sentinel("EXTCMD")
    root = tmp_path_factory.mktemp("ext")
    _write_extension(root)
    plugins_file = root / "plugins.json"
    plugins_file.write_text(json.dumps({"extensions": [_PACKAGE]}), encoding="utf-8")

    stop_sut(hamroh_sut)
    sut = launch_sut(
        e2e_config,
        tmp_path_factory.mktemp("ext-data"),
        extra_env={
            "HAMROH_PLUGINS_PATH": str(plugins_file),
            "PYTHONPATH": str(root),
            "E2E_EXT_TOOL_SECRET": tool_secret,
            "E2E_EXT_CMD_SECRET": command_secret,
        },
    )
    try:
        yield sut, tool_secret, command_secret
    finally:
        stop_sut(sut)
        revived = launch_sut(e2e_config, hamroh_sut.data_dir)
        hamroh_sut.proc = revived.proc
        hamroh_sut._log = revived._log


@pytest.mark.smoke
async def test_extension_tool_is_callable_in_dm(
    extensions_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """A tool from an extension package works like a built-in.

    given  a bot booted with an extension that ships the ``ext_secret`` tool
    when   the owner asks the bot to call that tool and echo its output
    then   the reply carries the per-run secret only the tool can reveal.
    """
    # given — a secret the model can only learn through the extension tool
    _, tool_secret, _ = extensions_sut

    # when — the owner asks for the tool's output
    reply = await send_and_wait(tester_client, dm, _CALL_TOOL)

    # then — the secret is in the reply
    assert_reply_within(reply, MAX_TOOL_GROUP_REPLY_S, "extension tool call")
    assert tool_secret in reply.text, (
        f"extension tool secret {tool_secret!r} missing from reply: {reply.text!r}"
    )


@pytest.mark.smoke
async def test_extension_command_is_handled_by_dispatcher_in_dm(
    extensions_sut: tuple[Sut, str, str],
    tester_client: TelegramClient,
    dm: Conversation,
) -> None:
    """An owner command from an extension package is answered directly.

    given  a bot booted with an extension that registers ``/ext_ping``
    when   the owner sends /ext_ping in a DM
    then   the bot replies with the command's per-run secret within
           MAX_TEXT_REPLY_S, i.e. without a model turn.
    """
    # given — the command's secret, known only to the extension
    _, _, command_secret = extensions_sut

    # when — the owner sends the extension command
    reply = await send_and_wait(tester_client, dm, "/ext_ping")

    # then — the dispatcher answers with the secret
    assert_reply_within(reply, MAX_TEXT_REPLY_S, "/ext_ping")
    assert command_secret in reply.text, (
        f"/ext_ping reply missing {command_secret!r}: {reply.text!r}"
    )


def test_extension_package_imports_in_this_interpreter(tmp_path: Path) -> None:
    """Guard the fixture itself: the generated package must be importable
    and expose ``register`` plus the tool, independent of any bot."""
    _write_extension(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib

        module = importlib.import_module(_PACKAGE)
        tool_module = importlib.import_module(f"{_PACKAGE}.tools.secret")
        assert callable(module.register), "extension must expose register()"
        assert tool_module.ExtSecretTool.name == "ext_secret"
    finally:
        sys.path.remove(str(tmp_path))
        for name in [m for m in sys.modules if m.startswith(_PACKAGE)]:
            del sys.modules[name]
