"""Unit tests for the agy_worker package: argv, config generation, permissions."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from hamroh.agy_worker import config_writer
from hamroh.agy_worker.spec import (
    FORBIDDEN_FLAG,
    AgySpawnSpec,
    build_argv,
    permission_allow_rules,
)


@pytest.fixture
def spec(tmp_path: Path) -> AgySpawnSpec:
    """A minimal spec with a shipped system prompt on disk."""
    sys_prompt = tmp_path / "system.md"
    sys_prompt.write_text("You are a helpful bot.", encoding="utf-8")
    return AgySpawnSpec(
        binary="agy",
        model="gemini-3-pro",
        system_prompt_path=sys_prompt,
        mcp_server_url="http://127.0.0.1:5111/mcp",
        workspace_dir=tmp_path,
        hamroh_tool_names=("telegram_send_message", "memory_read"),
    )


def _with(spec: AgySpawnSpec, **changes: object) -> AgySpawnSpec:
    return dataclasses.replace(spec, **changes)


# --------------------------------------------------------------------------
# build_argv
# --------------------------------------------------------------------------


def test_build_argv_fresh_turn(spec: AgySpawnSpec) -> None:
    argv = build_argv(spec, "hello")
    assert argv[:3] == ["agy", "--print", "hello"], argv
    assert "--model" in argv and "gemini-3-pro" in argv
    assert "--continue" not in argv and "--conversation" not in argv


def test_build_argv_continue(spec: AgySpawnSpec) -> None:
    argv = build_argv(spec, "hi", resume="continue")
    assert "--continue" in argv, "continue turn must resume the recent conversation"


def test_build_argv_specific_conversation(spec: AgySpawnSpec) -> None:
    argv = build_argv(spec, "hi", resume="abc-123")
    assert argv[-2:] == ["--conversation", "abc-123"], argv


def test_build_argv_never_contains_forbidden_flag(spec: AgySpawnSpec) -> None:
    # Given the guard, no code path may emit the bypass flag.
    argv = build_argv(spec, "hi", resume="continue")
    assert FORBIDDEN_FLAG not in argv


# --------------------------------------------------------------------------
# permissions.allow allowlist — deny-by-default, tool_groups add categories
# --------------------------------------------------------------------------


def test_default_allows_mcp_and_web_only(spec: AgySpawnSpec) -> None:
    # With every tool group off, hamroh's MCP surface + read-only web are
    # reachable (web = parity with Claude's WebSearch/WebFetch). Shell, file and
    # subagent categories are absent, so headless agy auto-denies them.
    rules = permission_allow_rules(spec)
    assert set(rules) == {"mcp(*)", "search_web(*)", "read_url_content(*)"}
    assert "command(*)" not in rules and "read_file(*)" not in rules


def test_bash_group_adds_command_permission(spec: AgySpawnSpec) -> None:
    rules = permission_allow_rules(_with(spec, enable_bash=True))
    assert "mcp(*)" in rules
    assert "command(*)" in rules, "bash group must grant the shell permission"


def test_code_group_adds_read_file_permission(spec: AgySpawnSpec) -> None:
    rules = permission_allow_rules(_with(spec, enable_code=True))
    assert "read_file(*)" in rules
    assert "command(*)" not in rules, "code group must not grant shell"


def test_subagents_group_adds_subagent_permission(spec: AgySpawnSpec) -> None:
    rules = permission_allow_rules(_with(spec, enable_subagents=True))
    assert "subagent(*)" in rules


# --------------------------------------------------------------------------
# config generation (fully sandboxed — never touches the real ~/.gemini)
# --------------------------------------------------------------------------


def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    fake_mcp = tmp_path / "gemini" / "config" / "mcp_config.json"
    fake_settings = tmp_path / "gemini" / "cli" / "settings.json"
    monkeypatch.setattr(config_writer, "GEMINI_MCP_CONFIG", fake_mcp)
    monkeypatch.setattr(config_writer, "GEMINI_CLI_SETTINGS", fake_settings)
    return fake_mcp, fake_settings


def test_install_agy_config_writes_prompt_mcp_and_permissions(
    spec: AgySpawnSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_mcp, fake_settings = _sandbox(tmp_path, monkeypatch)
    config_writer.install_agy_config(spec)

    # AGENTS.md carries the system prompt + tools inventory.
    agents = (spec.workspace_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "You are a helpful bot." in agents
    assert "call_mcp_tool" in agents and "telegram_send_message" in agents

    # MCP config points agy at the local hamroh server.
    mcp = json.loads(fake_mcp.read_text())
    assert mcp["mcpServers"]["hamroh"]["serverUrl"] == "http://127.0.0.1:5111/mcp"

    # permissions.allow is the explicit allowlist (mcp + web when locked down).
    settings = json.loads(fake_settings.read_text())
    assert set(settings["permissions"]["allow"]) == {
        "mcp(*)",
        "search_web(*)",
        "read_url_content(*)",
    }

    # No dead hook artifacts are written.
    assert not (spec.workspace_dir / ".agents").exists()


def test_install_preserves_other_settings_keys(
    spec: AgySpawnSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake_settings = _sandbox(tmp_path, monkeypatch)
    fake_settings.parent.mkdir(parents=True, exist_ok=True)
    fake_settings.write_text(
        json.dumps({"colorScheme": "dark", "trustedWorkspaces": ["/x"]}),
        encoding="utf-8",
    )
    config_writer.install_agy_config(_with(spec, enable_bash=True))
    settings = json.loads(fake_settings.read_text())
    # Existing keys survive; permissions.allow reflects the enabled group.
    assert settings["colorScheme"] == "dark"
    assert settings["trustedWorkspaces"] == ["/x"]
    assert set(settings["permissions"]["allow"]) == {
        "mcp(*)",
        "search_web(*)",
        "read_url_content(*)",
        "command(*)",
    }
