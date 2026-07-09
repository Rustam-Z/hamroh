"""All settings for hamroh, read from environment variables.

Every setting the bot uses is in this file. The rest of the code should
get values by calling ``Config.from_env()`` — that way tests can build
their own ``Config`` without touching environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# python-dotenv loads variables from a .env file. It's optional so tests
# don't have to install it.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - best effort
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _required(name: str) -> str:
    value = _env(name)
    if value is None:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = (_env(name, default) or default).strip().lower()
    if value not in allowed:
        raise RuntimeError(f"{name} must be one of {allowed}, got {value!r}")
    return value


@dataclass(frozen=True)
class Config:
    """All settings the bot uses at runtime."""

    #: The bot's API token from @BotFather. Used to log in to Telegram.
    #: Env var: ``TELEGRAM_BOT_TOKEN`` (required).
    telegram_bot_token: str
    #: Telegram user ID of the bot's owner (you). Owner-only commands
    #: like ``/kill`` and ``/access`` check this. Direct-message-only
    #: mode also uses it to decide who can talk to the bot.
    #: Env var: ``HAMROH_OWNER_ID`` (required).
    owner_id: int
    #: Which Claude model to use. Passed to ``claude --model``.
    #: Env var: ``HAMROH_MODEL`` (required).
    model: str
    #: How hard Claude thinks before answering. Passed to ``claude --effort``.
    #: Env var: ``HAMROH_EFFORT`` (required, e.g. ``"high"``).
    effort: str
    #: Name or full path of the ``claude`` program to run.
    #: Env var: ``CLAUDE_CODE_BIN`` (default ``"claude"``).
    claude_code_bin: str
    #: Folder where the bot stores its data: the database, claude logs, the
    #: access list, and the session ID. The folder is created automatically
    #: by ``ensure_dirs``. (Memory files live in ``memories/`` at the repo
    #: root, not here — see ``memories_dir``.)
    #: Env var: ``HAMROH_DATA_DIR`` (default ``"./data"``).
    data_dir: Path
    #: Whether the daily self-reflection loop runs at all. On by default
    #: — set this to ``false`` to turn it off. While on, it's seeded at
    #: startup and the bot can't cancel it (operator-only switch). While
    #: off, no self-reflection reminder is seeded and any existing one is
    #: cancelled.
    #: Env var: ``HAMROH_SELF_REFLECTION_ENABLED`` (default ``True``).
    self_reflection_enabled: bool
    #: When the daily self-reflection task runs, if enabled. Standard cron
    #: format, in UTC time. Only matters when ``self_reflection_enabled``.
    #: Env var: ``HAMROH_SELF_REFLECTION_CRON`` (default ``"0 0 * * *"``,
    #: which means midnight UTC every day).
    self_reflection_cron: str
    #: How long to wait (in milliseconds) after a message before sending
    #: it to Claude. If more messages come in during this wait, they are
    #: bundled together into one turn. Set to ``0`` to send each message
    #: right away.
    #: Env var: ``HAMROH_DEBOUNCE_MS`` (default ``0``).
    debounce_ms: int
    #: Max messages per minute the bot will accept from one user in
    #: direct messages. The owner is not limited. Group chats are not
    #: limited either.
    #: Env var: ``HAMROH_RATE_LIMIT_PER_MIN`` (default ``20``).
    rate_limit_per_min: int
    # Tool-group toggles (subagents / bash / code) live in
    # ``plugins.json`` ``tool_groups`` — single source of truth.
    # Boot-time only: edit the file and restart.
    #: Per-file size cap (bytes) for inbound Telegram attachments. Files
    #: larger than this are rejected without download. Photos and documents
    #: both use this cap. 20 MB by default.
    #: Env var: ``HAMROH_ATTACHMENT_MAX_BYTES`` (default 20_000_000).
    attachment_max_bytes: int

    # ----- Settings for handling tool errors -----
    # These control what happens when Claude is still running fine, but
    # one of its tool calls keeps failing inside a turn.

    #: How many failed tool calls in one turn trip the breaker. When
    #: reached (within ``tool_error_window_seconds``, with no successful
    #: tool call in between), the turn is aborted and the Claude
    #: subprocess is restarted.
    #: Env var: ``HAMROH_TOOL_ERROR_MAX_COUNT`` (default 10).
    tool_error_max_count: int
    #: Time-based version of the rule above. If errors keep coming in
    #: for this many seconds after the first one, the bot stops the
    #: turn — even if the count is still under the limit.
    #: Env var: ``HAMROH_TOOL_ERROR_WINDOW_SECONDS`` (default 600, i.e. 10 min).
    tool_error_window_seconds: float

    # ----- Settings for spotting a stuck Claude process -----
    # A separate watcher checks if Claude has gone silent in the middle
    # of a turn (no output, no tool activity). If yes, it kills Claude
    # so the supervisor can start it again.

    #: Max seconds of silence allowed during a turn. If Claude produces
    #: no output and no tool activity for longer than this, the watcher
    #: kills it. Silence between turns (when the bot is idle) is fine
    #: and ignored.
    #: Env var: ``HAMROH_LIVENESS_TIMEOUT_SECONDS`` (default 600, i.e. 10 min).
    liveness_timeout_seconds: float
    #: How often the watcher wakes up to check. Smaller numbers catch a
    #: stuck process sooner but use a bit more CPU.
    #: Env var: ``HAMROH_LIVENESS_POLL_SECONDS`` (default 30).
    liveness_poll_seconds: float

    # ----- Settings for restarting Claude after a crash -----
    # The supervisor watches the Claude process. When it exits, the
    # supervisor waits a bit and starts it again. The wait gets longer
    # after each crash. If too many crashes happen in a short time, the
    # supervisor gives up and exits — and something outside (systemd,
    # docker, etc.) is expected to restart the whole bot.

    #: How long to wait before the first restart, in seconds. Each
    #: extra crash doubles the wait (``base * 2^(n-1)``), up to
    #: ``crash_backoff_cap``. Smaller = recovers faster from a one-off
    #: glitch but spins more on real problems.
    #: Env var: ``HAMROH_CRASH_BACKOFF_BASE`` (default 2.0).
    crash_backoff_base: float
    #: Maximum wait between restarts. Once the wait reaches this value,
    #: it stops growing. Stops the bot from waiting minutes between
    #: retries when something is really wrong.
    #: Env var: ``HAMROH_CRASH_BACKOFF_CAP`` (default 64.0).
    crash_backoff_cap: float
    #: How many crashes within ``crash_window_seconds`` count as "too
    #: many". When this is reached, the bot tells the owner and active
    #: chats, then exits.
    #: Env var: ``HAMROH_CRASH_LIMIT`` (default 10).
    crash_limit: int
    #: Time window used together with ``crash_limit``. Only crashes
    #: from the last ``crash_window_seconds`` are counted.
    #: Env var: ``HAMROH_CRASH_WINDOW_SECONDS`` (default 600.0,
    #: which is 10 minutes).
    crash_window_seconds: float

    #: Run the shared Chromium headless (no visible window). Default ``True``.
    #: Set to ``False`` to watch the browser tools (and renders) drive a real
    #: window — handy for debugging a flow locally. Needs a display, so keep it
    #: ``True`` on servers/CI.
    #: Env var: ``HAMROH_BROWSER_HEADLESS`` (default ``True``).
    browser_headless: bool
    #: Root logging level for the console and the JSON log file. One of
    #: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``. High-volume library
    #: loggers (httpx, mcp) stay quieted regardless.
    #: Env var: ``HAMROH_LOG_LEVEL`` (default ``"INFO"``).
    log_level: str
    #: How much of Claude Code's activity the ``[CC.*]`` log lines show.
    #: ``compact`` (default) keeps one short line per event, cut at 200
    #: characters. ``full`` prints whole message bodies, tool arguments, and
    #: a first-lines preview of tool results — reads like a Claude Code
    #: transcript. The raw JSON capture in ``data/cc_logs/`` is always
    #: complete regardless of this setting.
    #: Env var: ``HAMROH_LOG_TRANSCRIPT`` (``"compact"`` or ``"full"``).
    log_transcript: str

    # Derived paths
    db_path: Path = field(init=False)
    #: The bot's memory folder: git-tracked ``memories/`` at the repo root, so
    #: memories are committable and survive a volume loss. Defaults to
    #: ``<repo>/memories``; ``HAMROH_MEMORIES_DIR`` redirects it (the e2e harness
    #: points it at a temp dir for isolation).
    memories_dir: Path = field(init=False)
    session_id_path: Path = field(init=False)
    cc_logs_dir: Path = field(init=False)
    access_path: Path = field(init=False)
    #: Git-tracked JSON file of operator-declared reminders at the repo root.
    #: Read-only source of truth: the startup reconciler seeds/cancels rows to
    #: match it (see ``hamroh.scheduler.reminders_config``). Sibling of ``access_path``.
    committed_reminders_path: Path = field(init=False)
    #: The ``plugins.json`` at the repo root — tool-group toggles, external
    #: MCPs, skill/tool disables (see ``hamroh.plugins``). ``HAMROH_PLUGINS_PATH``
    #: redirects it (the e2e harness boots a bot with its own copy).
    plugins_path: Path = field(init=False)
    attachments_dir: Path = field(init=False)
    renders_dir: Path = field(init=False)
    log_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", self.data_dir / "hamroh.db")
        object.__setattr__(self, "session_id_path", self.data_dir / "session_id")
        object.__setattr__(self, "cc_logs_dir", self.data_dir / "cc_logs")
        # access.json and memories/ live at the repo root, not the gitignored
        # data/: access.json is operator config, memories/ is git-tracked so it
        # commits. Tests override both via for_test(); HAMROH_MEMORIES_DIR also
        # redirects memories_dir.
        project_root = Path(__file__).resolve().parent.parent
        object.__setattr__(self, "access_path", project_root / "access.json")
        object.__setattr__(self, "memories_dir", project_root / "memories")
        object.__setattr__(
            self, "committed_reminders_path", project_root / "default-reminders.json"
        )
        object.__setattr__(self, "plugins_path", project_root / "plugins.json")
        object.__setattr__(self, "attachments_dir", self.data_dir / "attachments")
        object.__setattr__(self, "renders_dir", self.data_dir / "renders")
        object.__setattr__(self, "log_dir", self.data_dir / "logs")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            owner_id=int(_required("HAMROH_OWNER_ID")),
            model=_required("HAMROH_MODEL"),
            effort=_required("HAMROH_EFFORT"),
            claude_code_bin=_env("CLAUDE_CODE_BIN", "claude") or "claude",
            data_dir=Path(_env("HAMROH_DATA_DIR", "./data") or "./data").resolve(),
            self_reflection_enabled=_bool("HAMROH_SELF_REFLECTION_ENABLED", True),
            self_reflection_cron=(
                _env("HAMROH_SELF_REFLECTION_CRON", "0 0 * * *") or "0 0 * * *"
            ),
            debounce_ms=_int("HAMROH_DEBOUNCE_MS", 0),
            rate_limit_per_min=_int("HAMROH_RATE_LIMIT_PER_MIN", 20),
            attachment_max_bytes=_int("HAMROH_ATTACHMENT_MAX_BYTES", 20_000_000),
            tool_error_max_count=_int("HAMROH_TOOL_ERROR_MAX_COUNT", 10),
            tool_error_window_seconds=_float("HAMROH_TOOL_ERROR_WINDOW_SECONDS", 600.0),
            liveness_timeout_seconds=_float("HAMROH_LIVENESS_TIMEOUT_SECONDS", 600.0),
            liveness_poll_seconds=_float("HAMROH_LIVENESS_POLL_SECONDS", 30.0),
            crash_backoff_base=_float("HAMROH_CRASH_BACKOFF_BASE", 2.0),
            crash_backoff_cap=_float("HAMROH_CRASH_BACKOFF_CAP", 64.0),
            crash_limit=_int("HAMROH_CRASH_LIMIT", 10),
            crash_window_seconds=_float("HAMROH_CRASH_WINDOW_SECONDS", 600.0),
            browser_headless=_bool("HAMROH_BROWSER_HEADLESS", True),
            log_level=(_env("HAMROH_LOG_LEVEL", "INFO") or "INFO").upper(),
            log_transcript=_choice(
                "HAMROH_LOG_TRANSCRIPT", "compact", ("compact", "full")
            ),
        )
        cls._apply_env_path_overrides(cfg)
        return cfg

    @staticmethod
    def _apply_env_path_overrides(cfg: "Config") -> None:
        """Redirect repo-root config paths to env-specified files.

        ``access.json``, ``default-reminders.json``, ``plugins.json`` and
        ``memories/`` normally sit at the repo root (see ``__post_init__``).
        The e2e harness points them at temp paths via ``HAMROH_ACCESS_PATH``
        / ``HAMROH_REMINDERS_PATH`` / ``HAMROH_PLUGINS_PATH`` /
        ``HAMROH_MEMORIES_DIR`` so a test can authorize a group, seed a
        reminder, enable a tool group or write a memory without touching the
        repo copies.
        """
        access_override = _env("HAMROH_ACCESS_PATH")
        if access_override:
            object.__setattr__(cfg, "access_path", Path(access_override).resolve())
        reminders_override = _env("HAMROH_REMINDERS_PATH")
        if reminders_override:
            object.__setattr__(
                cfg, "committed_reminders_path", Path(reminders_override).resolve()
            )
        plugins_override = _env("HAMROH_PLUGINS_PATH")
        if plugins_override:
            object.__setattr__(cfg, "plugins_path", Path(plugins_override).resolve())
        memories_override = _env("HAMROH_MEMORIES_DIR")
        if memories_override:
            object.__setattr__(cfg, "memories_dir", Path(memories_override).resolve())

    @classmethod
    def for_test(cls, data_dir: Path) -> "Config":
        """Build a Config with fixed values, ignoring environment variables.

        Used by tests so they don't depend on whatever is set on the
        machine running them.
        """
        cfg = cls(
            telegram_bot_token="test-token",
            owner_id=0,
            model="claude-opus-4-7",
            effort="high",
            claude_code_bin="claude",
            data_dir=data_dir.resolve(),
            self_reflection_enabled=False,
            self_reflection_cron="0 0 * * *",
            debounce_ms=1000,
            rate_limit_per_min=20,
            attachment_max_bytes=20_000_000,
            tool_error_max_count=3,
            tool_error_window_seconds=300.0,
            liveness_timeout_seconds=300.0,
            liveness_poll_seconds=30.0,
            crash_backoff_base=2.0,
            crash_backoff_cap=64.0,
            crash_limit=10,
            crash_window_seconds=600.0,
            browser_headless=True,
            log_level="INFO",
            log_transcript="compact",
        )
        cls._override_test_paths(cfg, data_dir.resolve())
        return cfg

    @staticmethod
    def _override_test_paths(cfg: "Config", root: Path) -> None:
        """Point per-test file paths inside the tmp dir, off the repo root.

        Tests use isolated tmp dirs, so access.json, the memories folder, the
        reminders file and plugins.json each get their own copy and never
        touch the repo root.
        """
        object.__setattr__(cfg, "access_path", root / "access.json")
        object.__setattr__(cfg, "memories_dir", root / "memories")
        object.__setattr__(
            cfg, "committed_reminders_path", root / "default-reminders.json"
        )
        object.__setattr__(cfg, "plugins_path", root / "plugins.json")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memories_dir.mkdir(parents=True, exist_ok=True)
        self.cc_logs_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
