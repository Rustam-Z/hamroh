"""Real end-to-end suite: a Telegram user account drives the live bot.

Opt-in. Every test is gated on the ``claude`` CLI plus the ``E2E_*``
credentials (see ``conftest.py``); without them the suite skips cleanly,
so a plain ``pytest`` stays green. See ``README.md`` for one-time setup.
"""
