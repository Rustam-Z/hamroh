"""Spawn-time configuration for the ``agy`` (Antigravity CLI) subprocess.

``agy`` is invoked one-shot per turn: ``agy --print "<prompt>" --model <id>
[--continue]``. Unlike Claude Code there are no ``--system-prompt`` /
``--mcp-config`` / ``--tools`` flags — those are file-based:

* system prompt  → ``AGENTS.md`` at the workspace root (see :mod:`config_writer`)
* MCP servers    → ``~/.gemini/config/mcp_config.json``
* tool gating    → a ``PreToolUse`` hook in ``.agents/hooks.json``

This module owns the dataclass that captures a turn's spawn config, the
built-in tool sets that hamroh's ``tool_groups`` map onto, and
:func:`build_argv` — the single place that turns an :class:`AgySpawnSpec`
into the exact argv handed to ``asyncio.create_subprocess_exec``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Tool gating — the explicit permissions.allow allowlist.
#
# ``agy -p`` (headless) is DENY-BY-DEFAULT: any permission-gated built-in with
# no allow rule is auto-denied. This is the ONLY working gate — ``PreToolUse``
# hooks do NOT fire in print mode (verified), so we do not use them. The rules
# below go into ``~/.gemini/antigravity-cli/settings.json`` ``permissions.allow``
# and are the complete list of what the model can reach.
#
# Permission CATEGORIES (verified against agy v1.1.x by reading the auto-deny
# messages): ``mcp`` (every hamroh MCP tool, via the ``call_mcp_tool``
# dispatcher), ``command`` (``run_command``/shell), ``read_file`` (``view_file``
# and filesystem reads). Read-only web tools (``search_web``/``read_url_content``)
# are NOT permission-gated by agy, so they are always available — matching
# Claude's always-on ``WebSearch``/``WebFetch``.
# --------------------------------------------------------------------------

#: Always granted — hamroh's entire tool surface goes through MCP.
MCP_ALLOW: tuple[str, ...] = ("mcp(*)",)

#: Read-only web tools, always granted — parity with Claude's always-on
#: ``WebSearch`` / ``WebFetch``. Verified that agy does NOT currently
#: permission-gate these (they fetch live data with no allow rule), so these
#: rules are explicit-intent + future-proofing should agy start gating them.
WEB_ALLOW: tuple[str, ...] = ("search_web(*)", "read_url_content(*)")

#: Granted only when the ``bash`` tool group is on (mirrors Claude's ``bash``).
BASH_ALLOW: tuple[str, ...] = ("command(*)",)

#: Granted only when the ``code`` tool group is on. ``read_file`` covers reads;
#: agy's file *writes* run in its own scratch sandbox and are not permission-
#: gated, so they can't be toggled from here.
CODE_ALLOW: tuple[str, ...] = ("read_file(*)",)

#: Granted only when the ``subagents`` tool group is on. Category name is
#: best-effort (not yet confirmed against a live auto-deny message).
SUBAGENT_ALLOW: tuple[str, ...] = ("subagent(*)",)

#: Forbidden flag — never pass this. hamroh gates tools via the explicit
#: ``permissions.allow`` list; a blanket bypass is refused at build time.
FORBIDDEN_FLAG = "--dangerously-skip-permissions"


@dataclass(frozen=True)
class AgySpawnSpec:
    """Everything needed to spawn one ``agy --print`` turn and to render the
    file-based config (``AGENTS.md`` + the global ``mcp_config.json`` /
    ``permissions.allow``)."""

    binary: str
    model: str
    system_prompt_path: Path
    #: The URL of the local hamroh MCP server (streamable-HTTP), written into
    #: ``mcp_config.json`` as a ``serverUrl`` entry.
    mcp_server_url: str
    #: Workspace root where ``AGENTS.md`` is written and where ``agy`` is invoked.
    workspace_dir: Path
    project_prompt_path: Path | None = None
    #: Persisted agy conversation id to resume with ``--conversation``. When
    #: None the first turn creates a fresh conversation.
    conversation_id: str | None = None
    #: Per-turn timeout handed to ``agy --print-timeout``.
    print_timeout: str = "5m"
    #: Extra ``mcpServers`` entries (external plugin MCPs) merged alongside the
    #: local hamroh server.
    extra_mcp_servers: tuple[tuple[str, str], ...] = ()
    enable_subagents: bool = False
    subagents_prompt_path: Path | None = None
    enable_bash: bool = False
    enable_code: bool = False
    #: Pre-rendered "available skills" block appended to ``AGENTS.md``.
    skills_index: str = ""
    #: Pre-rendered memory index appended to ``AGENTS.md``.
    memory_index: str = ""
    #: Names of the enabled hamroh MCP tools (bare), used to render the
    #: "# Your tools" inventory in ``AGENTS.md`` so the model knows what it can
    #: reach through ``call_mcp_tool``.
    hamroh_tool_names: tuple[str, ...] = ()


def permission_allow_rules(spec: AgySpawnSpec) -> tuple[str, ...]:
    """The explicit ``permissions.allow`` list for this spec (deny-by-default).

    Only these permission categories are reachable; every other permission-gated
    built-in is auto-denied by headless agy. hamroh's MCP tools (``mcp(*)``) are
    always allowed; the shell / file / subagent categories are gated by
    ``plugins.json`` ``tool_groups``, mirroring the Claude engine's
    ``bash`` / ``code`` / ``subagents`` toggles.
    """
    rules: list[str] = list(MCP_ALLOW) + list(WEB_ALLOW)
    if spec.enable_bash:
        rules += list(BASH_ALLOW)
    if spec.enable_code:
        rules += list(CODE_ALLOW)
    if spec.enable_subagents:
        rules += list(SUBAGENT_ALLOW)
    return tuple(rules)


def build_argv(
    spec: AgySpawnSpec, prompt: str, *, resume: str | None = None
) -> list[str]:
    """Construct the exact argv for one ``agy --print`` turn.

    ``resume`` controls conversation continuity:

    * ``None``        — start a fresh conversation.
    * ``"continue"``  — resume the most recent conversation (``--continue``).
    * ``"<id>"``      — resume a specific conversation (``--conversation <id>``).

    The system prompt, MCP config and tool gate are all file-based (written by
    :mod:`config_writer`), so they are not argv.
    """
    argv: list[str] = [
        spec.binary,
        "--print",
        prompt,
        "--model",
        spec.model,
        "--print-timeout",
        spec.print_timeout,
    ]
    if resume == "continue":
        argv.append("--continue")
    elif resume:
        argv += ["--conversation", resume]

    if FORBIDDEN_FLAG in argv:
        raise RuntimeError(
            f"refusing to build argv containing {FORBIDDEN_FLAG!r}; this flag "
            "is forbidden in hamroh under all circumstances — tools are gated "
            "by the PreToolUse hook instead"
        )
    return argv


def compose_system_prompt(spec: AgySpawnSpec) -> str:
    """Assemble the AGENTS.md body: shipped base + project overlay + runtime
    block + (optionally) skills index + memory index + tools inventory +
    subagent docs. Mirrors hamroh's Claude-side ``_compose_system_prompt``."""
    runtime_block = (
        "# Runtime\n\n"
        "You are running with:\n"
        f"- model: `{spec.model}`\n\n"
        "If a user asks which model you are running on, answer honestly with "
        "this exact value.\n\n"
        "# How your reply is delivered\n\n"
        "Your normal written response is sent to the user automatically as the "
        "chat reply — just write it. Do NOT call `telegram_send_message` for a "
        "plain text reply, or the user will receive it twice. Use the "
        "`telegram_*` tools only for richer actions: reacting, replying to a "
        "specific earlier message, sending a photo or document, creating or "
        "closing a poll, or editing/deleting a message. To stay silent, reply "
        "with an empty message.\n"
    )
    system_prompt = spec.system_prompt_path.read_text(encoding="utf-8")
    if spec.project_prompt_path and spec.project_prompt_path.exists():
        system_prompt += "\n\n" + spec.project_prompt_path.read_text(encoding="utf-8")
    system_prompt += "\n\n" + runtime_block
    if spec.skills_index:
        system_prompt += "\n\n" + spec.skills_index
    if spec.memory_index:
        system_prompt += "\n\n" + spec.memory_index
    tools_index = render_tools_index(spec)
    if tools_index:
        system_prompt += "\n\n" + tools_index
    if spec.enable_subagents and spec.subagents_prompt_path:
        if spec.subagents_prompt_path.exists():
            system_prompt += "\n\n" + spec.subagents_prompt_path.read_text(
                encoding="utf-8"
            )
    return system_prompt


def render_tools_index(spec: AgySpawnSpec) -> str:
    """Render the "# Your tools" block for AGENTS.md.

    In agy every hamroh tool is reached through ``call_mcp_tool``, so the block
    lists the hamroh tool names and tells the model to invoke them via the
    dispatcher on the ``hamroh`` server. Returns "" when there are no tools."""
    if not spec.hamroh_tool_names:
        return ""
    tools = "\n".join(f"- `{n}`" for n in sorted(spec.hamroh_tool_names))
    return (
        "# Your tools\n\n"
        "All of your tools live on the MCP server named `hamroh`. Call them "
        "with the `call_mcp_tool` tool, passing the server name `hamroh`, the "
        "exact tool name below, and its arguments. Never invent tool names — "
        "copy them verbatim.\n\n"
        f"{tools}\n"
    )
