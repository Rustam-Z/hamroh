"""Antigravity CLI (``agy``) subprocess worker — an alternate backend.

Selected via ``HAMROH_ENGINE=agy`` (the default backend is Claude Code's
``cc_worker``). Unlike Claude Code (a long-lived stream-json process), ``agy
--print`` is **one-shot per turn**: the prompt goes in as argv, the final
answer comes back as plain text on stdout, and the process exits. Conversation
continuity is carried by ``--continue`` on the next invocation.

To stay a drop-in for the engine, this worker reuses ``cc_worker``'s shared
contract (:class:`TurnResult`, :class:`CrashLoop`, :class:`WorkerHooks`) and
exposes the same methods (``send`` / ``inject`` / ``wait_for_result`` /
``reset_session`` / ``start`` / ``supervise`` / ``stop`` / ``session_id``), so
``engine`` and ``startup`` treat both backends identically.

Mid-turn ``inject`` cannot reach a running print-mode process, so injected text
is folded into a follow-up ``agy`` invocation *within the same logical turn*
before the :class:`TurnResult` is emitted — no message is dropped.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time

from ..config import Config
from ..cc_worker.events import CrashLoop, TurnResult
from ..cc_worker.worker import WorkerHooks
from ..helpers.transcript import log_cc_text, log_cc_user
from ..tools.base import Heartbeat
from .config_writer import install_agy_config
from .spec import FORBIDDEN_FLAG, AgySpawnSpec, build_argv

log = logging.getLogger("hamroh.agy_worker")


class AgyWorker:
    """Drive one ``agy`` turn per :meth:`send`, emulating the CcWorker API.

    Crash budget: if an ``agy`` invocation fails (non-zero exit or empty
    output) we count it; ``crash_limit`` failures within
    ``crash_window_seconds`` raises :class:`CrashLoop` so the OS-level
    supervisor restarts the whole process.
    """

    def __init__(
        self, spec: AgySpawnSpec, config: Config, hooks: WorkerHooks = WorkerHooks()
    ) -> None:
        self.spec = spec
        self.heartbeat = hooks.heartbeat or Heartbeat()
        self._on_giveup = hooks.on_giveup
        self._session_id_path = config.session_id_path
        self._crash_limit = config.crash_limit
        self._crash_window_seconds = config.crash_window_seconds
        self._crash_times: list[float] = []
        self._result_queue: asyncio.Queue[TurnResult] = asyncio.Queue()
        self._pending_injects: list[str] = []
        self._turn_task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        #: Interface-compatibility with ``CcWorker``: the shutdown signal
        #: handler in ``startup`` sets this. There is no supervisor loop to
        #: stop here, so setting it is harmless — ``stop()`` does the work.
        self._stop_supervisor = asyncio.Event()
        #: True once at least one turn has run this process lifetime, so
        #: subsequent turns resume with ``--continue``. Across restarts the
        #: persisted ``conversation_id`` (if any) resumes the first turn.
        self._has_conversation = spec.conversation_id is not None

    @property
    def session_id(self) -> str | None:
        return self.spec.conversation_id

    # ------------------------------------------------------------------
    # Lifecycle (no persistent process — start writes config, supervise no-op)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Write the file-based agy config. No process is spawned until the
        first :meth:`send`."""
        install_agy_config(self.spec)
        log.info(
            "agy worker ready (model=%s, workspace=%s)",
            self.spec.model,
            self.spec.workspace_dir,
        )

    async def supervise(self) -> None:
        """No-op: there is no long-lived subprocess to watch. Crash recovery is
        handled per-invocation inside :meth:`_run_turn`."""

    async def stop(self) -> None:
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._terminate_proc()

    async def _terminate_proc(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, text: str) -> None:
        """Start a new turn: run ``agy --print`` with ``text`` in the
        background; the :class:`TurnResult` lands on the result queue."""
        log_cc_user(text)
        self._pending_injects = []
        self._turn_task = asyncio.create_task(self._run_turn(text), name="agy-turn")

    async def wait_for_result(self) -> TurnResult:
        return await self._result_queue.get()

    async def inject(self, text: str) -> None:
        """Queue text that arrived mid-turn. It is folded into a follow-up
        ``agy`` invocation before the current turn's result is emitted, so
        print mode's inability to accept mid-turn input never drops a message."""
        self._pending_injects.append(text)

    async def reset_session(self) -> None:
        """Drop conversation continuity so the next turn starts fresh."""
        log.warning("session reset: dropping agy conversation continuity")
        self.spec = dataclasses.replace(self.spec, conversation_id=None)
        self._has_conversation = False
        self._session_id_path.unlink(missing_ok=True)
        if self._turn_task and not self._turn_task.done():
            self._result_queue.put_nowait(TurnResult(aborted_reason="session-reset"))
            self._turn_task.cancel()
        await self._terminate_proc()

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def _run_turn(self, text: str) -> None:
        """Run one logical turn: invoke ``agy``, folding any mid-turn injects
        into follow-up invocations, then emit a single :class:`TurnResult`."""
        try:
            prompt = text
            stdout, stderr, rc = "", "", 0
            while True:
                stdout, stderr, rc = await self._invoke_agy(prompt)
                if self._pending_injects:
                    prompt = "\n".join(self._pending_injects)
                    self._pending_injects = []
                    continue
                break
            self._result_queue.put_nowait(self._build_result(stdout, stderr, rc))
        except asyncio.CancelledError:
            raise
        except CrashLoop:
            # Budget exhausted (on_giveup already fired). Unblock the engine
            # with a sentinel, then re-raise so the OS-level supervisor can
            # restart the whole process.
            self._result_queue.put_nowait(TurnResult(aborted_reason="crash-loop"))
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("agy turn crashed")
            self._result_queue.put_nowait(
                TurnResult(aborted_reason="worker-error", api_error=str(exc))
            )

    def _resume_mode(self) -> str | None:
        """Resume argument for the next invocation: a specific conversation id
        on the first turn after a restart, then ``--continue`` thereafter."""
        if self.spec.conversation_id and not self._has_conversation:
            return self.spec.conversation_id
        return "continue" if self._has_conversation else None

    async def _invoke_agy(self, prompt: str) -> tuple[str, str, int]:
        """Spawn one ``agy --print`` and return ``(stdout, stderr, returncode)``.
        Counts crashes and raises :class:`CrashLoop` when the budget is spent."""
        argv = build_argv(self.spec, prompt, resume=self._resume_mode())
        assert FORBIDDEN_FLAG not in argv, "refusing to spawn agy with bypass flag"
        log.info(
            "spawning agy (model=%s, resume=%s)", self.spec.model, self._resume_mode()
        )
        self.heartbeat.beat()
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.spec.workspace_dir),
            env={**os.environ},
            limit=4 * 1024 * 1024,
        )
        out, err = await self._proc.communicate()
        rc = self._proc.returncode or 0
        self._proc = None
        self.heartbeat.beat()
        stdout = out.decode("utf-8", errors="replace").strip()
        stderr = err.decode("utf-8", errors="replace").strip()
        # A clean run that produced text means the conversation now exists.
        if rc == 0 and stdout:
            self._has_conversation = True
        else:
            await self._record_failure(rc, stderr)
        return stdout, stderr, rc

    async def _record_failure(self, rc: int, stderr: str) -> None:
        """Count one failed invocation; raise :class:`CrashLoop` if the budget
        is exhausted within the rolling window."""
        log.error("agy invocation failed rc=%s: %s", rc, stderr[-400:])
        now = time.monotonic()
        self._crash_times = [
            t for t in self._crash_times if now - t < self._crash_window_seconds
        ]
        self._crash_times.append(now)
        if len(self._crash_times) >= self._crash_limit:
            if self._on_giveup is not None:
                try:
                    await self._on_giveup(len(self._crash_times))
                except Exception:
                    log.debug("on_giveup callback failed", exc_info=True)
            raise CrashLoop(
                f"agy failed {self._crash_limit} times in "
                f"{self._crash_window_seconds:.0f}s; bailing out"
            )

    def _build_result(self, stdout: str, stderr: str, rc: int) -> TurnResult:
        """Turn the raw ``agy`` output into a :class:`TurnResult`.

        In agy print mode the model's reply comes back as plain text on stdout,
        so that text *is* the message: it is delivered to the chat through the
        engine's dropped-text path (``dropped_text=True`` + ``text_blocks``).
        Rich actions (reactions, photos, polls, edits) still go through the
        telegram MCP tools directly. A clean run with empty stdout means the
        model chose to stay silent."""
        result = TurnResult(stderr_tail=stderr.splitlines()[-10:])
        if rc != 0:
            result.aborted_reason = "agy-error"
            result.api_error = stderr[-400:] or "agy produced no output"
            return result
        if stdout:
            log_cc_text(stdout)  # surface the agy reply in the logs
            result.text_blocks = [stdout]
            result.dropped_text = True
        return result
