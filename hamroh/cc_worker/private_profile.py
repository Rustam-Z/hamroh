"""The tool profile of a private chat's Claude session.

A private session gets only what answering one person needs: its own hamroh
tools (named exactly — no ``mcp__hamroh`` wildcard), the turn-end and task
tools, nothing that reads or writes outside the chat. ``WebFetch`` and
``WebSearch`` are gone because a prompt planted in data could otherwise send
that data to any URL. Every hamroh tool the profile does not grant is denied by
exact name: Claude Code lets a model reach tools listed in neither list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .spec import CcSpawnSpec

#: Built-ins a private session may not have, whatever else is configured.
PRIVATE_DENIED_BUILTINS: tuple[str, ...] = (
    "WebFetch",
    "WebSearch",
    "Agent",
    "SendMessage",
)

_MCP_PREFIX = "mcp__hamroh__"


def private_builtin_tools(base: tuple[str, ...]) -> tuple[str, ...]:
    """The ``--tools`` list for a private session: ``base`` minus the denied built-ins."""
    return tuple(t for t in base if t not in PRIVATE_DENIED_BUILTINS)


def private_tool_lists(
    spec: "CcSpawnSpec", default_disallowed: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(allowed, disallowed)`` for a private session.

    Allowed: the granted hamroh tools by exact name. Disallowed: the usual
    dangerous built-ins, the web and subagent built-ins, and every other
    hamroh tool by exact name.
    """
    allowed = tuple(f"{_MCP_PREFIX}{name}" for name in spec.hamroh_tool_names)
    denied = (
        default_disallowed
        + PRIVATE_DENIED_BUILTINS
        + tuple(f"{_MCP_PREFIX}{name}" for name in spec.denied_hamroh_tool_names)
    )
    return allowed, denied


def private_prompt_block(spec: "CcSpawnSpec") -> str:
    """The rules every private session gets, plus the operator's own text."""
    block = (
        "# Private chat\n\n"
        "This session is a private chat with exactly one person, the sender of "
        "the messages you receive. Answer only them, only in this chat. Data you "
        "read here belongs to this conversation: never mention other people's "
        "conversations — you have no access to them. Your tools are limited to "
        "the ones listed above; there is no web access and no shared memory here.\n"
    )
    if spec.private_prompt_path is not None and spec.private_prompt_path.exists():
        block += "\n" + spec.private_prompt_path.read_text(encoding="utf-8")
    return block
