"""Scenario dataset shared by the e2e eval (``tests/e2e/support/eval.py``).

Each scenario is one natural request the eval sends N times across DM and
group to measure correctness rate and latency. Prompts use natural phrasing
(never "echo this token") so the bot's prompt-injection defense stays out of
the way. ``check`` is how a reply counts as a pass:

* ``contains`` — the unique token appears in the reply text
* ``photo``    — the reply includes a photo
* ``any``      — any non-empty reply (used when we only time the path)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str  # contains "{token}"
    check: str  # "contains" | "photo" | "any"


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "echo",
        "My reference number is {token}. What is my reference number?",
        "contains",
    ),
    Scenario(
        "memory_write",
        "Remember this note and write it to a memory file: {token}. Reply with OK.",
        "any",
    ),
    Scenario(
        "memory_read",
        "Read your notes memory file and list what is saved there.",
        "any",
    ),
    Scenario(
        "render",
        "Render a tiny HTML table containing {token} and send it to me as a photo.",
        "photo",
    ),
)
