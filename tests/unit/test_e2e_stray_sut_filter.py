"""``kill_stray_suts`` must never reach across containers.

Regression test for 2026-07-17: on a host where a supervisor container runs
with ``--pid=host`` (so ``pgrep`` sees every peer bot's identically-named
``python -m hamroh`` process), the e2e harness's "clear my own leftovers"
step SIGTERM'd three unrelated live bots at once. The fix scopes candidate
PIDs to the caller's own mount namespace before any signal is sent.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest

from tests.e2e.support import harness


def test_mount_ns_id_is_stable_for_self() -> None:
    """The same PID queried twice yields the same namespace identity."""
    own = os.getpid()
    assert harness._mount_ns_id(own) is not None
    assert harness._mount_ns_id(own) == harness._mount_ns_id(own)


def test_mount_ns_id_returns_none_for_a_dead_pid() -> None:
    """An unreadable/vanished PID must fail closed (never match), not raise."""
    # PID 2**30 is astronomically unlikely to exist; /proc/<pid>/ns/mnt
    # then raises FileNotFoundError, which we must swallow to None.
    assert harness._mount_ns_id(2**30) is None


def test_stray_pids_excludes_a_different_mount_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pgrep match in a different mount namespace (a sibling container's
    bot, not a leftover of ours) must never be returned as a kill candidate."""
    own = os.getpid()
    other_pid = 999999  # stand-in for a sibling container's PID

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=f"{own}\n{other_pid}\n", stderr=""
        )

    def fake_ns_id(pid: int) -> object | None:
        # Same namespace for ourselves; a distinct one for the "sibling".
        return ("self-ns",) if pid == own else ("other-container-ns",)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness, "_mount_ns_id", fake_ns_id)

    assert harness._stray_sut_pids() == []


def test_stray_pids_includes_a_genuine_same_container_leftover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pgrep match that IS in our own mount namespace (a real orphaned
    process from a crashed prior run) is still correctly flagged."""
    own = os.getpid()
    leftover_pid = 888888

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=f"{own}\n{leftover_pid}\n", stderr=""
        )

    def fake_ns_id(pid: int) -> object | None:
        return ("self-ns",)  # everyone shares one namespace here

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness, "_mount_ns_id", fake_ns_id)

    assert harness._stray_sut_pids() == [leftover_pid]


def test_stray_pids_fails_closed_when_own_namespace_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we can't even establish our own namespace identity, kill nothing
    rather than risk a ``None == None`` false match against other failures."""
    own = os.getpid()

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=f"{own}\n123456\n", stderr=""
        )

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness, "_mount_ns_id", lambda pid: None)

    assert harness._stray_sut_pids() == []
