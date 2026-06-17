"""Latency assertions shared by the e2e tests."""

from __future__ import annotations

from tests.e2e.support.models import Reply


def assert_within(elapsed_s: float, limit_s: float, what: str) -> None:
    """Fail unless ``elapsed_s`` is under ``limit_s`` seconds."""
    assert elapsed_s < limit_s, (
        f"{what} took {elapsed_s:.2f}s, over the {limit_s:.0f}s limit"
    )


def assert_reply_within(reply: Reply, limit_s: float, what: str) -> None:
    """Fail unless the bot's first reply chunk arrived within ``limit_s`` seconds.

    Latency is judged on ``t_first_s`` (time to the first chunk) — the best
    proxy for felt responsiveness.
    """
    assert_within(reply.t_first_s, limit_s, f"{what} reply's first chunk")
