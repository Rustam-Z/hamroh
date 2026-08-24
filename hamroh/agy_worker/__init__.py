"""Antigravity CLI (``agy``) subprocess worker — an alternate engine backend.

Selected via ``HAMROH_ENGINE=agy``. Reuses ``cc_worker``'s shared contract
(:class:`TurnResult`, :class:`CrashLoop`, :class:`WorkerHooks`) so the engine
treats this backend identically to the default Claude Code one.
"""

from __future__ import annotations

from ..cc_worker.events import CrashLoop, TurnResult
from ..cc_worker.worker import WorkerHooks
from .spec import (
    FORBIDDEN_FLAG,
    AgySpawnSpec,
    build_argv,
    permission_allow_rules,
)
from .worker import AgyWorker

__all__ = [
    "AgySpawnSpec",
    "AgyWorker",
    "CrashLoop",
    "FORBIDDEN_FLAG",
    "TurnResult",
    "WorkerHooks",
    "build_argv",
    "permission_allow_rules",
]
