"""Structured-output fallback: control JSON must never count as reply prose.

Older CC versions deliver the structured output inside the last text block
instead of ``event["result"]``. That block is internal control JSON (it
carries the ``reason`` field, which per prompts/system.md is never shown to
the user) — once it validates as a control action it must be removed from
``text_blocks``, or the dropped-text rescue would deliver it to the chat
(issue #84 variant).
"""

from __future__ import annotations

from pathlib import Path

from hamroh.cc_worker import CcSpawnSpec, CcWorker, TurnResult
from hamroh.config import Config


def _worker(tmp_path: Path) -> CcWorker:
    sp = tmp_path / "system.md"
    sp.write_text("system")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    spec = CcSpawnSpec(
        binary="/bin/true",  # never actually spawned
        model="claude-opus-4-6",
        system_prompt_path=sp,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )
    return CcWorker(spec, Config.for_test(tmp_path))


def test_control_json_in_last_text_block_is_consumed_not_kept_as_prose(
    tmp_path: Path,
) -> None:
    # Given a worker mid-turn whose only text block is the control JSON
    # (older CC fallback: no result/output field on the result event)
    worker = _worker(tmp_path)
    worker._current_turn = TurnResult(
        text_blocks=['{"action": "skip", "reason": "internal"}']
    )

    # When the bare result event arrives
    worker._handle_event({"type": "result", "subtype": "success"})

    # Then the control action is parsed AND the block is consumed — the
    # reason must never survive as deliverable prose
    result = worker._result_queue.get_nowait()
    assert result.control is not None, "control action must be parsed"
    assert result.control.action == "skip", "action must come from the block"
    assert result.text_blocks == [], "control JSON must not remain as prose"
    assert result.dropped_text is False, (
        "consumed control JSON must never trigger dropped-text delivery"
    )


def test_result_field_present_keeps_prose_text_blocks(tmp_path: Path) -> None:
    # Given a worker mid-turn with genuine reply prose
    worker = _worker(tmp_path)
    worker._current_turn = TurnResult(text_blocks=["real reply prose"])

    # When the result event carries the structured output itself
    worker._handle_event(
        {
            "type": "result",
            "subtype": "success",
            "result": {"action": "stop", "reason": "done"},
        }
    )

    # Then the prose survives for the engine's dropped-text rescue
    result = worker._result_queue.get_nowait()
    assert result.control is not None and result.control.action == "stop", (
        "control action must come from the result field"
    )
    assert result.text_blocks == ["real reply prose"], (
        "reply prose must be preserved when the payload came from the event"
    )
    assert result.dropped_text is True, (
        "undelivered prose on a stop turn must still be rescued"
    )


def test_unparseable_last_block_is_kept_and_control_is_none(
    tmp_path: Path,
) -> None:
    # Given a worker mid-turn whose last block is plain prose, not JSON
    worker = _worker(tmp_path)
    worker._current_turn = TurnResult(text_blocks=["plain prose, not JSON"])

    # When the bare result event arrives (no payload fields)
    worker._handle_event({"type": "result", "subtype": "success"})

    # Then nothing is parsed and the prose is never destroyed
    result = worker._result_queue.get_nowait()
    assert result.control is None, "prose must not be mistaken for a control action"
    assert result.text_blocks == ["plain prose, not JSON"], (
        "a block that fails to parse must never be popped"
    )
