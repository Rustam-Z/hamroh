"""Launch the bot subprocess, detect readiness, and authorize the tester.

The system under test (``Sut``) is a real ``python -m pyclaudir`` process; this
module owns its lifecycle and the ``access.json`` that gates its messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from pyclaudir.access import AccessConfig, save_access

from tests.e2e.support.client import send_and_wait
from tests.e2e.support.config import E2EConfig, child_env
from tests.e2e.support.models import Conversation

#: Repo root (…/pyclaudir). The SUT runs with this as its cwd so it picks
#: up the operator's ``plugins.json`` and ``prompts/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
#: ``__main__.py`` logs this exact line once the dispatcher starts polling.
#: It means the bot's stack is up and accepting messages. The ``claude`` CLI
#: already connects its MCP servers and loads its tools at spawn (its first
#: ``system/init`` event lists them as connected), so this line does NOT need to
#: wait for that. What it can't tell us is that the *model* is warm: the first
#: turn still pays an inference cold-start. So readiness adds a warm-up
#: round-trip (see ``_warm_up_round_trip``) on top of this line.
READY_LINE = "pyclaudir is live"
_READY_TIMEOUT_S = 90.0
#: A first-ever turn is slow: the model spins up (first-token cold-start) before
#: any reply is produced. The text itself is irrelevant — we just need one turn.
_WARMUP_TEXT = "ping"
_WARMUP_TIMEOUT_S = 120.0
_LOG_RING = 400  # keep the last N output lines for failure dumps
#: The bot runs as a subprocess; forward its output through this logger so
#: pytest's live log (``log_cli``) streams the RX/TX/timing lines as they
#: happen, not just on failure.
_SUT_LOG = logging.getLogger("pyclaudir.sut")
log = logging.getLogger(__name__)


@dataclass
class Sut:
    """A running pyclaudir subprocess plus the paths a test inspects."""

    proc: subprocess.Popen[str]
    data_dir: Path
    _log: deque[str]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pyclaudir.db"

    @property
    def memories_dir(self) -> Path:
        return self.data_dir / "memories"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def access_path(self) -> Path:
        # The SUT reads the real root access.json (REPO_ROOT is its cwd), so
        # access-management tests inspect and rewrite that same file.
        return REPO_ROOT / "access.json"

    def log_tail(self) -> str:
        return "".join(self._log)


def _drain(
    proc: subprocess.Popen[str], log: deque[str], ready: threading.Event
) -> None:
    """Pump child output into the ring buffer and the live log; flag readiness
    on READY_LINE."""
    assert proc.stdout is not None
    for line in proc.stdout:
        log.append(line)
        if line.strip():
            _SUT_LOG.info(line.rstrip())
        if READY_LINE in line:
            ready.set()


async def _warm_up_round_trip(cfg: E2EConfig, timeout: float) -> None:
    """Drive one DM turn and wait for the bot's full reply.

    This spends the model's first-turn cold-start here, in setup, so the first
    real test measures warm latency instead of inference spin-up. It also
    exercises the ``send_message`` round-trip once, end to end. ``send_and_wait``
    drains until the reply is quiet, leaving the DM silent before tests run.
    """
    client = TelegramClient(StringSession(cfg.session), cfg.api_id, cfg.api_hash)
    await client.connect()
    try:
        bot = await client.get_entity(cfg.bot_username)
        convo = Conversation(chat=bot, reply_from=bot)
        await send_and_wait(client, convo, _WARMUP_TEXT, timeout=timeout)
    finally:
        await client.disconnect()


def _finish_readiness(cfg: E2EConfig, sut: Sut) -> None:
    """Drive the warm-up round-trip after the ``READY_LINE`` is seen.

    On failure the SUT is stopped and the error carries the bot's log tail.
    """
    try:
        asyncio.run(_warm_up_round_trip(cfg, _WARMUP_TIMEOUT_S))
    except Exception as exc:
        stop_sut(sut)
        raise RuntimeError(
            f"pyclaudir warm-up round-trip failed: {exc}\n"
            f"--- last output ---\n{sut.log_tail()}"
        ) from exc


def _stray_sut_pids() -> list[int]:
    """PIDs of leftover ``python -m pyclaudir`` processes (this one excluded).

    The bot's argv ends with ``pyclaudir`` so we anchor the pattern there; the
    ``claude`` child only *contains* "pyclaudir" (inside its system prompt) and
    is correctly skipped. A missing ``pgrep`` or no matches yields an empty list.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "m pyclaudir$"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    own = os.getpid()
    return [pid for line in result.stdout.split() if (pid := int(line)) != own]


def kill_stray_suts(timeout: float = 10.0) -> None:
    """SIGTERM any leftover ``python -m pyclaudir`` process before the SUT starts.

    Only one process may poll a bot token, so an orphan from a crashed run (or a
    dev bot) would make Telegram reject the SUT's getUpdates. Runs once per
    session, ahead of the shared SUT launch. SIGTERM first, then SIGKILL the
    stragglers — mirroring ``stop_sut``.
    """
    pids = _stray_sut_pids()
    if not pids:
        return
    log.warning("killing stray pyclaudir processes before e2e suite: %s", pids)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while _stray_sut_pids() and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid in _stray_sut_pids():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def launch_sut(cfg: E2EConfig, data_dir: Path) -> Sut:
    """Start ``python -m pyclaudir`` and block until it is 100% ready.

    Readiness is two-stage: wait for the ``READY_LINE`` (stack up, MCP/tools
    loaded), then drive a warm-up round-trip so the model's first-turn cold-start
    happens before the first test runs.
    """
    data_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "pyclaudir"],
        cwd=REPO_ROOT,
        env=child_env(data_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log: deque[str] = deque(maxlen=_LOG_RING)
    ready = threading.Event()
    threading.Thread(target=_drain, args=(proc, log, ready), daemon=True).start()

    sut = Sut(proc, data_dir, log)
    if not ready.wait(_READY_TIMEOUT_S):
        stop_sut(sut)
        raise RuntimeError(
            f"pyclaudir did not become ready in {_READY_TIMEOUT_S:.0f}s\n"
            f"--- last output ---\n{sut.log_tail()}"
        )
    _finish_readiness(cfg, sut)
    return sut


def stop_sut(sut: Sut, timeout: float = 15.0) -> None:
    """Graceful SIGTERM (the SUT shuts down cleanly), SIGKILL on hang."""
    proc = sut.proc
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5.0)


def set_access(sut: Sut, access: AccessConfig) -> None:
    """Rewrite the SUT's access.json — the bot hot-reloads it per message."""
    save_access(sut.access_path, access)
