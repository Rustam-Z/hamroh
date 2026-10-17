"""Extensions: a bot-specific package named in ``plugins.json`` adds tools
and owner commands without touching the framework.

Covers the ``extensions`` key in the plugins loader, tool discovery across
an extension package, the duplicate-name guard, and ``add_owner_command``
on the dispatcher (handler order, owner gating, command menu).
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import CommandHandler, MessageHandler

from hamroh.access import AccessConfig, save_access
from hamroh.config import Config
from hamroh.mcp_server import build_fastmcp
from hamroh.plugins import PluginsConfigError, load_plugins
from hamroh.telegram_io import DispatcherDeps, TelegramDispatcher
from hamroh.tools import discover_tool_classes
from hamroh.tools.base import ToolContext

OWNER = 42
STRANGER = 100

_TOOL_SOURCE = """
from pydantic import BaseModel
from hamroh.tools.base import BaseTool, ToolResult

class Args(BaseModel):
    pass

class {cls}(BaseTool):
    name = "{name}"
    description = "An extension tool. For tests only."
    args_model = Args

    async def run(self, args):
        return ToolResult(content="ok")
"""


@pytest.fixture
def ext_package(tmp_path: Path) -> str:
    """Write a throwaway extension package to ``tmp_path`` and make it
    importable. Yields its package name; import caches are dropped after."""
    name = "e2e_unit_ext"
    pkg = tmp_path / name
    (pkg / "tools").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "tools" / "__init__.py").write_text("")
    (pkg / "tools" / "hello.py").write_text(
        textwrap.dedent(_TOOL_SOURCE.format(cls="HelloTool", name="ext_hello"))
    )
    sys.path.insert(0, str(tmp_path))
    yield name
    sys.path.remove(str(tmp_path))
    for module in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[module]


# ---------------------------------------------------------------------------
# plugins.json
# ---------------------------------------------------------------------------


def test_extensions_parsed_in_order(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"extensions": ["ext_b", "ext_a"]}))
    plugins = load_plugins(p)
    assert plugins.extensions == ("ext_b", "ext_a"), "load order must be kept"


def test_extensions_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text("{}")
    assert load_plugins(p).extensions == ()


def test_extensions_non_string_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"extensions": [1]}))
    with pytest.raises(PluginsConfigError, match="extensions"):
        load_plugins(p)


def test_extensions_duplicate_crashes(tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text(json.dumps({"extensions": ["ext", "ext"]}))
    with pytest.raises(PluginsConfigError, match="duplicate"):
        load_plugins(p)


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------


def test_extension_tool_is_discovered_next_to_builtins(ext_package: str) -> None:
    names = {c.name for c in discover_tool_classes((ext_package,))}
    assert "ext_hello" in names, "the extension's tool must be discovered"
    assert "time_now" in names, "built-ins must still be discovered"


def test_extension_tool_is_registered_on_the_mcp_server(ext_package: str) -> None:
    _, tools = build_fastmcp(ToolContext(), extensions=(ext_package,))
    assert "ext_hello" in {t.name for t in tools}


def test_extension_tool_shadowing_a_builtin_crashes(
    ext_package: str, tmp_path: Path
) -> None:
    """A second tool called ``time_now`` must fail boot, not silently win."""
    (tmp_path / ext_package / "tools" / "clash.py").write_text(
        textwrap.dedent(_TOOL_SOURCE.format(cls="ClashTool", name="time_now"))
    )
    with pytest.raises(ValueError, match="time_now"):
        build_fastmcp(ToolContext(), extensions=(ext_package,))


def test_unknown_extension_package_crashes() -> None:
    with pytest.raises(ModuleNotFoundError):
        discover_tool_classes(("no_such_hamroh_extension",))


# ---------------------------------------------------------------------------
# Owner commands
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    object.__setattr__(cfg, "owner_id", OWNER)
    save_access(
        cfg.access_path,
        AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[]),
    )
    return cfg


def _update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _dispatcher_with_ping(tmp_path: Path) -> tuple[TelegramDispatcher, AsyncMock]:
    """A dispatcher with one extension command, ``/ping``, already wired."""
    dispatcher = TelegramDispatcher(
        _cfg(tmp_path), MagicMock(), DispatcherDeps(engine=MagicMock(), chat_titles={})
    )
    handler = AsyncMock()
    dispatcher.add_owner_command("ping", "reply pong", handler)
    dispatcher._wire_handlers()
    return dispatcher, handler


def _wired_ping(dispatcher: TelegramDispatcher) -> CommandHandler:
    for h in dispatcher.application.handlers[0]:
        if isinstance(h, CommandHandler) and "ping" in h.commands:
            return h
    raise AssertionError("/ping was not wired into the application")


def test_extension_command_is_wired_before_the_catch_all(tmp_path: Path) -> None:
    """If the catch-all text handler came first it would swallow ``/ping``
    and forward it to the engine as a plain message."""
    dispatcher, _ = _dispatcher_with_ping(tmp_path)
    handlers = dispatcher.application.handlers[0]
    ping_index = handlers.index(_wired_ping(dispatcher))
    catch_all_index = next(
        i for i, h in enumerate(handlers) if isinstance(h, MessageHandler)
    )
    assert ping_index < catch_all_index, "/ping must short-circuit the engine"


@pytest.mark.asyncio
async def test_extension_command_runs_for_owner_only(tmp_path: Path) -> None:
    dispatcher, handler = _dispatcher_with_ping(tmp_path)
    callback = _wired_ping(dispatcher).callback

    await callback(_update(STRANGER), MagicMock())
    handler.assert_not_awaited()

    await callback(_update(OWNER), MagicMock())
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_extension_command_is_listed_in_owner_menu(tmp_path: Path) -> None:
    dispatcher, _ = _dispatcher_with_ping(tmp_path)
    bot = MagicMock(set_my_commands=AsyncMock())
    with patch.object(dispatcher.application, "bot", bot):
        await dispatcher._register_owner_commands()
    registered = {
        (c.command, c.description) for c in bot.set_my_commands.await_args.args[0]
    }
    assert ("ping", "reply pong") in registered, "menu must list the extension command"
    assert ("health", "quick health readout") in registered, "built-ins must stay"
