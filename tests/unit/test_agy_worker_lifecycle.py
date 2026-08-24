"""Lifecycle tests for AgyWorker using a fake ``agy`` binary (hermetic).

The fake binary is a tiny shell script whose behaviour is controlled by an env
var, so we exercise the one-shot turn model without the real Antigravity CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.agy_worker import CrashLoop
from hamroh.agy_worker.spec import AgySpawnSpec
from hamroh.agy_worker.worker import AgyWorker, WorkerHooks
from hamroh.config import Config


def _fake_agy(tmp_path: Path, *, exit_code: int = 0, stdout: str = "done") -> Path:
    """Write a fake ``agy`` that prints ``stdout`` and exits ``exit_code``."""
    script = tmp_path / "fake_agy.sh"
    script.write_text(
        f'#!/bin/sh\nprintf "%s" "{stdout}"\nexit {exit_code}\n', encoding="utf-8"
    )
    script.chmod(0o755)
    return script


def _worker(tmp_path: Path, agy: Path, **spec_kw: object) -> AgyWorker:
    sysmd = tmp_path / "system.md"
    sysmd.write_text("bot", encoding="utf-8")
    spec = AgySpawnSpec(
        binary=str(agy),
        model="gemini-3-pro",
        system_prompt_path=sysmd,
        mcp_server_url="http://127.0.0.1:5111/mcp",
        workspace_dir=tmp_path,
        **spec_kw,
    )
    return AgyWorker(spec, Config.for_test(tmp_path))


async def test_send_delivers_stdout_as_reply(tmp_path: Path) -> None:
    # agy's stdout IS the reply; it is delivered via the engine's dropped-text
    # path, so the worker surfaces it as text_blocks + dropped_text.
    worker = _worker(tmp_path, _fake_agy(tmp_path, stdout="hello"))
    await worker.send("hi")
    result = await worker.wait_for_result()
    assert result.aborted_reason is None
    assert result.api_error is None
    assert result.text_blocks == ["hello"]
    assert result.dropped_text is True


async def test_clean_run_with_empty_stdout_is_silent(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _fake_agy(tmp_path, stdout=""))
    await worker.send("hi")
    result = await worker.wait_for_result()
    assert result.aborted_reason is None
    assert result.text_blocks == []
    assert result.dropped_text is False


async def test_failed_invocation_reports_error(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _fake_agy(tmp_path, exit_code=1, stdout=""))
    worker._crash_limit = 99  # don't trip the budget on a single failure
    await worker.send("hi")
    result = await worker.wait_for_result()
    assert result.aborted_reason == "agy-error"
    assert result.api_error


async def test_resume_mode_progresses_to_continue(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _fake_agy(tmp_path, stdout="ok"))
    assert worker._resume_mode() is None, "first turn starts fresh"
    await worker.send("hi")
    await worker.wait_for_result()
    assert worker._resume_mode() == "continue", "after a turn, resume the conversation"


async def test_reset_session_clears_continuity(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _fake_agy(tmp_path, stdout="ok"), conversation_id="c-1")
    worker._session_id_path.write_text("c-1")
    await worker.reset_session()
    assert worker.spec.conversation_id is None
    assert worker._resume_mode() is None
    assert not worker._session_id_path.exists()


async def test_crash_budget_raises_crashloop_and_notifies(tmp_path: Path) -> None:
    seen: list[int] = []

    async def on_giveup(n: int) -> None:
        seen.append(n)

    base = _worker(tmp_path, _fake_agy(tmp_path, exit_code=1, stdout=""))
    worker = AgyWorker(
        base.spec, Config.for_test(tmp_path), WorkerHooks(on_giveup=on_giveup)
    )
    worker._crash_limit = 2

    # One failed turn = one recorded failure; the budget accrues across sends.
    await worker.send("first")
    assert (await worker.wait_for_result()).aborted_reason == "agy-error"

    # The second failure trips the budget: CrashLoop propagates out of the turn
    # task, and a sentinel still unblocks the engine.
    await worker.send("second")
    with pytest.raises(CrashLoop):
        await worker._turn_task
    assert seen == [2], "owner is notified once when the budget is spent"
    assert (await worker.wait_for_result()).aborted_reason == "crash-loop"
