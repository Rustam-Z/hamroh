"""Read the running bot's persisted state (SQLite + files) read-only.

WAL mode (``db/database.py``) lets a second connection read a consistent
snapshot while the bot keeps writing; every query opens read-only so a test
can never mutate bot state.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def new_png_files(renders_dir: Path, after: float) -> list[Path]:
    """PNG files in ``renders_dir`` modified at or after ``after`` (epoch secs)."""
    if not renders_dir.exists():
        return []
    return [p for p in renders_dir.glob("*.png") if p.stat().st_mtime >= after]


def current_cc_session_id(cc_logs_dir: Path) -> str | None:
    """The bot's current Claude Code session id.

    Each CC session writes one ``<session_id>.stream.jsonl`` capture file, so the
    newest such file (by mtime) names the active session. Returns None before any
    session has initialised. A test reads this before and after /reset_session and
    asserts it changed. ``pending-*`` files (spawned but not yet init'd) are
    skipped so the result is always a real session id.
    """
    if not cc_logs_dir.exists():
        return None
    files = [
        p
        for p in cc_logs_dir.glob("*.stream.jsonl")
        if not p.name.startswith("pending-")
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime).name.removesuffix(
        ".stream.jsonl"
    )


def cc_tool_use_names(cc_logs_dir: Path) -> set[str]:
    """Exact names of every tool the current CC session has invoked so far.

    Parsed from the newest ``<session>.stream.jsonl`` capture: assistant
    events carry ``tool_use`` content blocks whose ``name`` is the callable
    tool name (``Bash``, ``Agent``, ``mcp__e2e-echo__echo``, …). A line
    still being written is skipped.
    """
    session = current_cc_session_id(cc_logs_dir)
    if session is None:
        return set()
    names: set[str] = set()
    stream = cc_logs_dir / f"{session}.stream.jsonl"
    for line in stream.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:  # a partially-flushed trailing line
            continue
        content = (event.get("message") or {}).get("content") or []
        names.update(
            block["name"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
    return names


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
