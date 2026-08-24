"""Write the file-based configuration ``agy`` reads.

Claude Code took everything as argv flags; ``agy`` reads config from files:

* ``<workspace>/AGENTS.md``                    — the system prompt (rules file)
* ``~/.gemini/config/mcp_config.json``         — the MCP servers (hamroh + externals)
* ``~/.gemini/antigravity-cli/settings.json``  — the ``permissions.allow`` allowlist

Tool gating is done **only** through ``permissions.allow``: headless ``agy -p``
auto-denies any permission-gated tool without an allow rule, so the list is
deny-by-default. (``PreToolUse`` hooks were tried and do **not** fire in print
mode — verified — so hamroh does not use them.)

All writers are idempotent: call :func:`install_agy_config` once at startup.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .spec import AgySpawnSpec, compose_system_prompt, permission_allow_rules

log = logging.getLogger("hamroh.agy_worker")

#: Global CLI config locations. ``~`` is expanded at write time.
GEMINI_MCP_CONFIG = Path("~/.gemini/config/mcp_config.json").expanduser()
GEMINI_CLI_SETTINGS = Path("~/.gemini/antigravity-cli/settings.json").expanduser()

#: Server identifier the hamroh MCP server is registered under.
MCP_SERVER_NAME = "hamroh"


def install_agy_config(spec: AgySpawnSpec) -> None:
    """Write every config file ``agy`` needs for this spec. Idempotent."""
    _write_agents_md(spec)
    _write_mcp_config(spec)
    _write_permissions(spec)


def _write_agents_md(spec: AgySpawnSpec) -> None:
    """Render the composed system prompt to ``<workspace>/AGENTS.md``."""
    path = spec.workspace_dir / "AGENTS.md"
    path.write_text(compose_system_prompt(spec), encoding="utf-8")
    log.info("wrote agy system prompt to %s", path)


def _write_mcp_config(spec: AgySpawnSpec) -> None:
    """Register the local hamroh MCP server (+ externals) in the global CLI
    MCP config. The hamroh server is streamable-HTTP, so it maps to a
    ``serverUrl`` entry."""
    servers: dict[str, dict[str, str]] = {
        MCP_SERVER_NAME: {"serverUrl": spec.mcp_server_url},
    }
    for name, url in spec.extra_mcp_servers:
        servers[name] = {"serverUrl": url}
    GEMINI_MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    GEMINI_MCP_CONFIG.write_text(
        json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8"
    )
    log.info(
        "wrote agy mcp config to %s (%d server(s))", GEMINI_MCP_CONFIG, len(servers)
    )


def _write_permissions(spec: AgySpawnSpec) -> None:
    """Write the explicit ``permissions.allow`` allowlist into the CLI settings.

    Deny-by-default: headless agy auto-denies any permission-gated tool not
    listed here. hamroh's MCP tools are always allowed; shell/file/subagent
    categories only when the matching ``plugins.json`` tool_group is on. Merges
    into the existing settings file without clobbering other keys, and fully
    replaces any ``permissions.allow`` hamroh wrote on a previous run.
    """
    GEMINI_CLI_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if GEMINI_CLI_SETTINGS.exists():
        try:
            data = json.loads(GEMINI_CLI_SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("agy CLI settings is not valid JSON; overwriting minimally")
    rules = list(permission_allow_rules(spec))
    data.setdefault("permissions", {})["allow"] = rules
    GEMINI_CLI_SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("wrote agy permissions.allow to %s: %s", GEMINI_CLI_SETTINGS, rules)
