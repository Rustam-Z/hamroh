"""Skill-file tools — list, read, and write skill playbooks.

Skills are curated markdown playbooks under ``skills/<name>/SKILL.md``.
``skill_list`` and ``skill_read`` are public reference reads — no owner
gate. ``skill_write`` creates or updates a skill and is **owner-gated by
system-prompt policy** (not enforced in code), like ``instruction_append``.
Access goes through :class:`hamroh.storage.skills_store.SkillsStore`, which is
path-hardened; writes land in the git-tracked ``skills/`` checkout.

The intended use: when a reminder injects a message of the form
``<skill name="X">run</skill>`` inside a ``<reminder>`` envelope, the
bot calls ``skill_read("X")`` and executes the playbook's steps.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from .base import BaseTool, ToolResult


class ListSkillsArgs(BaseModel):
    pass


class ListSkillsTool(BaseTool[ListSkillsArgs]):
    name = "skill_list"
    description = (
        "List available agent skills (playbooks) under the project's "
        "skills/ directory, following the Agent Skills spec "
        "(https://agentskills.io/specification). Returns each skill's "
        "name and its frontmatter description — enough to decide which "
        "skill is relevant without loading the full body. Fetch the "
        "body via skill_read only when you're ready to execute. Skills "
        'are typically invoked via `<skill name="X">run</skill>` '
        "inside a `<reminder>` envelope."
    )
    args_model = ListSkillsArgs

    async def run(self, args: ListSkillsArgs) -> ToolResult:
        store = self.ctx.skills_store
        if store is None:
            return ToolResult(content="skills store unavailable", is_error=True)
        files = await asyncio.to_thread(store.list)
        if not files:
            return ToolResult(content="(no skills)")
        lines = [f"- **{f.name}** — {f.description}" for f in files]
        return ToolResult(
            content="\n".join(lines),
            data={
                "skills": [
                    {"name": f.name, "description": f.description} for f in files
                ],
            },
        )


class ReadSkillArgs(BaseModel):
    name: str = Field(
        description="Skill name (e.g. 'self-reflection'). Must be a single directory name.",
    )


class ReadSkillTool(BaseTool[ReadSkillArgs]):
    name = "skill_read"
    description = (
        "Read the playbook (SKILL.md) for a given agent skill. "
        "Returns the full markdown content. Call skill_list first if "
        "you're not sure what's available. Call this when a `<reminder>` "
        'envelope contains `<skill name="X">run</skill>`.'
    )
    args_model = ReadSkillArgs

    async def run(self, args: ReadSkillArgs) -> ToolResult:
        store = self.ctx.skills_store
        if store is None:
            return ToolResult(content="skills store unavailable", is_error=True)
        try:
            text = await asyncio.to_thread(store.read, args.name)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(content=text, data={"name": args.name})


class WriteSkillArgs(BaseModel):
    name: str = Field(
        description=(
            "Single-component skill directory name, e.g. 'weekly-digest' "
            "(lowercase letters/digits/hyphens). The frontmatter `name` must "
            "equal this."
        ),
    )
    content: str = Field(
        description=(
            "Full SKILL.md text. MUST start with YAML frontmatter carrying "
            "`name` (matching the name arg) and a one-line `description`, "
            "followed by the playbook body."
        ),
    )


class WriteSkillTool(BaseTool[WriteSkillArgs]):
    name = "skill_write"
    description = (
        "Create or update a skill playbook at skills/<name>/SKILL.md. Use "
        "ONLY when the bot owner has approved the new/updated playbook (e.g. "
        "via the self-reflection loop). Refuse for any non-owner sender. The "
        "content must carry valid frontmatter whose name matches <name>; max "
        "256 KiB. Overwriting an existing skill requires calling skill_read on "
        "it first this session. Visible via skill_list/skill_read immediately; "
        "the preloaded skills index in your prompt refreshes on the next "
        "restart. skills/ is git-tracked — commit the result like a memory."
    )
    args_model = WriteSkillArgs

    async def run(self, args: WriteSkillArgs) -> ToolResult:
        store = self.ctx.skills_store
        if store is None:
            return ToolResult(content="skills store unavailable", is_error=True)
        try:
            written = await asyncio.to_thread(store.write, args.name, args.content)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=(
                f"wrote {written} bytes to skills/{args.name}/SKILL.md. "
                "Restart the container to refresh the preloaded skills index."
            ),
            data={"name": args.name, "bytes": written},
        )
