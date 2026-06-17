"""Generic async timing and polling utilities (no Telegram knowledge)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")


async def measured(awaitable: Awaitable[_T]) -> tuple[_T, float]:
    """Await ``awaitable`` and return its result paired with the seconds it took.

    Lets a test time any observable (a reaction, a DB row, a fired reminder)
    without each wait helper having to report elapsed time itself.
    """
    start = time.perf_counter()
    result = await awaitable
    return result, time.perf_counter() - start


async def wait_until(
    predicate: Callable[[], _T], timeout: float = 15.0, interval: float = 1.0
) -> _T:
    """Poll a sync ``predicate`` until it returns a truthy value or timeout.

    Returns that value (or the last falsy one). Used for DB rows that appear
    a beat after a message is delivered.
    """
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        await asyncio.sleep(interval)
        value = predicate()
    return value
