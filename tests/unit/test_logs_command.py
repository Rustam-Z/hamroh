"""``/logs`` — owner-only tail of the structured JSON log file."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hamroh.access import AccessConfig, save_access
from hamroh.config import Config
from hamroh.telegram_io import DispatcherDeps, TelegramDispatcher

OWNER = 42
STRANGER = 100


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    object.__setattr__(cfg, "owner_id", OWNER)
    save_access(
        cfg.access_path,
        AccessConfig(policy="owner_only", allowed_users=[], allowed_chats=[]),
    )
    return cfg


def _update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _ctx(args: list[str] | None = None) -> MagicMock:
    return MagicMock(args=args)


def _dispatcher(cfg: Config) -> TelegramDispatcher:
    return TelegramDispatcher(
        cfg, MagicMock(), DispatcherDeps(engine=MagicMock(), chat_titles={})
    )


def _write_log(cfg: Config, *messages: str) -> None:
    lines = [
        json.dumps(
            {
                "ts": "2026-06-25T10:31:00+00:00",
                "level": "INFO",
                "component": "engine",
                "msg": m,
            }
        )
        for m in messages
    ]
    (cfg.log_dir / "hamroh.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_logs_replies_with_recent_lines_to_owner(tmp_path: Path) -> None:
    # Given a log file with two records
    cfg = _cfg(tmp_path)
    _write_log(cfg, "first event", "second event")
    dispatcher = _dispatcher(cfg)
    update = _update(OWNER)

    # When the owner runs /logs
    await dispatcher._cmd_logs(update, _ctx())

    # Then the owner receives the tailed, human-formatted lines
    sent = "\n".join(
        call.args[0] for call in update.effective_message.reply_text.await_args_list
    )
    assert "first event" in sent and "second event" in sent, "both lines must appear"
    assert "10:31:00 INFO engine" in sent, "lines must be the compact rendering"


@pytest.mark.asyncio
async def test_logs_are_sent_as_escaped_html_blockquote(tmp_path: Path) -> None:
    # Given a log line containing an HTML-significant character
    cfg = _cfg(tmp_path)
    _write_log(cfg, "boom in <module>")
    dispatcher = _dispatcher(cfg)
    update = _update(OWNER)

    # When the owner runs /logs
    await dispatcher._cmd_logs(update, _ctx())

    # Then each reply is an HTML blockquote with the log text escaped
    call = update.effective_message.reply_text.await_args_list[0]
    text = call.args[0]
    assert text.startswith("<blockquote>") and text.endswith("</blockquote>"), (
        "log output must be wrapped as a quote, matching owner error DMs"
    )
    assert call.kwargs["parse_mode"] == "HTML", "the quote must render as HTML"
    assert "&lt;module&gt;" in text, "a < in a log line must be escaped, not raw markup"


@pytest.mark.asyncio
async def test_bare_logs_truncates_to_last_4096_chars_in_one_quote(
    tmp_path: Path,
) -> None:
    # Given far more log content than fits in a single Telegram message
    cfg = _cfg(tmp_path)
    _write_log(cfg, *[f"event number {i}" for i in range(400)])
    dispatcher = _dispatcher(cfg)
    update = _update(OWNER)

    # When the owner runs bare /logs
    await dispatcher._cmd_logs(update, _ctx())

    # Then exactly one blockquote is sent and it fits the 4096-char limit
    replies = update.effective_message.reply_text.await_args_list
    assert len(replies) == 1, "bare /logs must send a single message, not chunks"
    text = replies[0].args[0]
    assert len(text) <= 4096, "the single quote must respect Telegram's char limit"
    assert "event number 399" in text, "the most recent line must be included"
    assert "event number 0" not in text, "old lines beyond 4096 chars are truncated"


@pytest.mark.asyncio
async def test_logs_with_argument_keeps_line_based_chunking(tmp_path: Path) -> None:
    # Given a log file and an explicit small line count
    cfg = _cfg(tmp_path)
    _write_log(cfg, *[f"event {i}" for i in range(10)])
    dispatcher = _dispatcher(cfg)
    update = _update(OWNER)

    # When the owner runs /logs 3
    await dispatcher._cmd_logs(update, _ctx(args=["3"]))

    # Then only the last 3 lines are shown, matching the old structure
    sent = "\n".join(
        call.args[0] for call in update.effective_message.reply_text.await_args_list
    )
    assert "event 9" in sent and "event 7" in sent, "the last 3 lines must appear"
    assert "event 6" not in sent, "lines beyond the requested count must be excluded"


@pytest.mark.asyncio
async def test_logs_is_silent_for_non_owner(tmp_path: Path) -> None:
    # Given a populated log file
    cfg = _cfg(tmp_path)
    _write_log(cfg, "secret event")
    dispatcher = _dispatcher(cfg)
    update = _update(STRANGER)

    # When a stranger runs /logs
    await dispatcher._cmd_logs(update, _ctx())

    # Then nothing is sent back
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_logs_reports_when_no_file_yet(tmp_path: Path) -> None:
    # Given no log file has been written
    cfg = _cfg(tmp_path)
    (cfg.log_dir / "hamroh.log").unlink(missing_ok=True)
    dispatcher = _dispatcher(cfg)
    update = _update(OWNER)

    # When the owner runs /logs
    await dispatcher._cmd_logs(update, _ctx())

    # Then the owner is told there are no logs
    update.effective_message.reply_text.assert_awaited_once_with("no logs yet")
