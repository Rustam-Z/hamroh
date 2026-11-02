"""Boot-time assembly of private chats: the private spawn profile and the switcher."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from .sessions import SessionStore
from .switcher import SessionSetup, SessionSwitcher, SwitchableWorker
from .turn import TurnContext

if TYPE_CHECKING:  # pragma: no cover
    from ..cc_worker.spec import CcSpawnSpec
    from ..config import Config
    from ..mcp_server import McpServer


def build_private_spec(
    shared: "CcSpawnSpec", mcp: "McpServer", prompt: Path | None
) -> "CcSpawnSpec":
    """A private chat's spawn profile, derived from the shared one.

    Granted: the hamroh tools whose audience is ``private`` or ``both``.
    Removed: every other hamroh tool (denied by name), external MCPs (the
    private MCP config lists only hamroh), the shared memory and skills
    indexes, and the bash / code / subagent groups.
    """
    granted = tuple(t.name for t in mcp.tools if t.audience != "shared")
    denied = tuple(t.name for t in mcp.tools if t.audience == "shared")
    config_path = mcp.write_mcp_config(
        shared.mcp_config_path.with_name("mcp-private.json")
    )
    return dataclasses.replace(
        shared,
        private=True,
        session_id=None,
        hamroh_tool_names=granted,
        denied_hamroh_tool_names=denied,
        mcp_allowed_tools=(),
        mcp_config_path=config_path,
        memory_index="",
        skills_index="",
        enable_bash=False,
        enable_code=False,
        enable_subagents=False,
        private_prompt_path=prompt,
    )


def build_switcher(
    worker: SwitchableWorker, mcp: "McpServer", config: "Config", turn: TurnContext
) -> SessionSwitcher:
    """The switcher the engine uses to run each turn in its own session."""
    setup = SessionSetup(
        shared=worker.spec,
        private=build_private_spec(worker.spec, mcp, config.private_prompt_path),
        store=SessionStore(config.sessions_path),
        turn=turn,
    )
    return SessionSwitcher(worker, setup)
