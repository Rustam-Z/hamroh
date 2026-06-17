"""Read the running bot's persisted state (SQLite + files) read-only.

WAL mode (``db/database.py``) lets a second connection read a consistent
snapshot while the bot keeps writing; every query opens read-only so a test
can never mutate bot state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def new_png_files(renders_dir: Path, after: float) -> list[Path]:
    """PNG files in ``renders_dir`` modified at or after ``after`` (epoch secs)."""
    if not renders_dir.exists():
        return []
    return [p for p in renders_dir.glob("*.png") if p.stat().st_mtime >= after]


def memory_files_containing(memories_dir: Path, token: str) -> list[Path]:
    """Memory files whose text contains ``token`` — proves disk persistence."""
    return [
        path
        for path in memories_dir.rglob("*")
        if path.is_file() and token in path.read_text(encoding="utf-8", errors="ignore")
    ]


def read_only_query(db_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run a SELECT against the live DB without locking out the bot."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def unauthorized_rows(db_path: Path, token: str) -> list[sqlite3.Row]:
    """`unauthorized_messages` rows whose text contains ``token``."""
    return read_only_query(
        db_path,
        "SELECT * FROM unauthorized_messages WHERE text LIKE ?",
        (f"%{token}%",),
    )


def reminder_rows(db_path: Path, token: str) -> list[sqlite3.Row]:
    """`reminders` rows whose text contains ``token``."""
    return read_only_query(
        db_path, "SELECT * FROM reminders WHERE text LIKE ?", (f"%{token}%",)
    )


def message_rows(db_path: Path, token: str) -> list[sqlite3.Row]:
    """`messages` rows whose text contains ``token`` (either direction).

    Proves a dropped (paused) message was never persisted."""
    return read_only_query(
        db_path, "SELECT * FROM messages WHERE text LIKE ?", (f"%{token}%",)
    )


def tool_calls_since(db_path: Path, since: str) -> list[sqlite3.Row]:
    """`tool_calls` rows recorded at or after ``since`` (a "%Y-%m-%d %H:%M:%S"
    UTC string) — for correlating a test's action to the tools it triggered."""
    return read_only_query(
        db_path,
        "SELECT tool_name, duration_ms, created_at FROM tool_calls "
        "WHERE created_at >= ?",
        (since,),
    )


def reply_info(db_path: Path, token: str) -> sqlite3.Row | None:
    """The inbound message containing ``token`` (its ``reply_to_id`` and
    ``reply_to_text``), matched by text — the bot's Bot-API message_ids
    differ from the Telethon client's, so they can't be cross-queried by id.
    """
    rows = read_only_query(
        db_path,
        "SELECT reply_to_id, reply_to_text FROM messages "
        "WHERE direction = 'in' AND text LIKE ?",
        (f"%{token}%",),
    )
    return rows[0] if rows else None
