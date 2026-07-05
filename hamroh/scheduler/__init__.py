"""Reminder scheduling: the background loop and its declarative config.

Holds the pieces that live above the persistence layer — the scheduler
loop (``reminder_scheduler``) and the declared-reminder config
(``reminders_config``). The SQLite layer stays in ``hamroh.db.reminders``
and the MCP tool in ``hamroh.tools.reminder``.
"""

from __future__ import annotations
