# Real end-to-end tests

These tests drive the **real bot over the real Telegram API**: a Telegram
*user* account sends a message, the live bot (a real `claude` subprocess)
replies, and we assert on the result and measure how long it took.

A bot can't message or read another bot, so the "sender" must be a normal
user account. We use [Telethon](https://docs.telethon.dev) for that.

The whole suite is **opt-in**. Without the `E2E_*` environment variables (or
without the `claude` CLI), every test **skips** — a plain `pytest` stays green.

## What you need (one-time)

| Thing | How to get it |
| --- | --- |
| A **test bot** | Talk to [@BotFather](https://t.me/BotFather) → `/newbot`. Use a *separate* bot from production. Gives you `E2E_BOT_TOKEN` and the bot's `@username`. |
| **User API creds** | <https://my.telegram.org> → API development tools → `api_id` + `api_hash` for the account that will play the tester. |
| A **session string** | Generated once with the snippet below (so tests log in without a phone prompt). |
| A **test group** | Create a group, add both the tester account and the test bot. Use its numeric id. |

Then:

1. **DM the bot once** from the tester account (send `/start`). Telegram won't
   deliver bot→user messages to someone who never opened the chat.
2. The harness always **@mentions** the bot in group messages, so the bot's
   privacy mode can stay on. (If you prefer, disable it via BotFather
   `/setprivacy → Disable`.)

### Generate the session string

Run the helper once, in your own terminal:

```bash
.venv/bin/python tests/e2e/support/make_session.py
```

It asks for your `api_id`/`api_hash` (if not already in the env file), then your
phone number, the login code, and your 2FA password (if set). It writes
`E2E_TG_SESSION` and `E2E_OWNER_ID` into `tests/e2e/.env.e2e` for you — then you
fill in the bot and group values there by hand. Treat the session string like a
password: it grants full access to that account.

## Environment variables

| Var | Meaning |
| --- | --- |
| `E2E_TG_API_ID` / `E2E_TG_API_HASH` | Tester account's API creds. |
| `E2E_TG_SESSION` | The session string from above. |
| `E2E_BOT_TOKEN` | The test bot's token. |
| `E2E_BOT_USERNAME` | The test bot's username (with or without `@`). |
| `E2E_OWNER_ID` | The tester account's **own** numeric user id. The bot treats it as owner, so DMs and owner commands pass. |
| `E2E_GROUP_ID` | The test group's numeric id (the `-100…` form for supergroups). |
| `E2E_MODEL` | *(optional)* model the bot runs, default `claude-sonnet-4-6` (cheap + fast). |

These are read from `tests/e2e/.env.e2e` automatically (no manual `export`),
or from the real environment, which takes precedence. The file is gitignored.

The bot is launched in a throwaway data directory with a temporary
`access.json` that allows the tester and the group, so your real
`data/` and `access.json` are never touched.

## Running

```bash
# everything (skips cleanly if E2E_* unset)
pytest -m e2e

# skip the slow reminder-fire test (~90s)
pytest -m "e2e and not slow"

# one file
pytest tests/e2e/test_memory_e2e.py -m e2e
```

Each run starts one bot subprocess for the whole session and reuses it; tests
stay independent by using a unique token per test. The bot's own log lines
(`[RX]`/`[TX]` traffic, `hot-path … t_ms=…` timing) stream live — the harness
forwards them through the `pyclaudir.sut` logger and `log_cli` (set in
`pyproject.toml`) prints them. On failure, the last 400 lines are also dumped.

Every reply spawns a real `claude` turn, so a run costs real model tokens and
takes real wall-clock time. Keep `E2E_MODEL` cheap and `effort` low (the harness
already sets `low`).

## Speed eval

The eval runs as part of the suite: `test_eval_e2e.py` sends each scenario in
`support/scenarios.py` across DM and group, logs a per-(feature, chat) table of
pass rate, p50/p95 latency, and mean tool time per turn, and fails only if the
correctness pass-rate drops below `E2E_EVAL_MIN_PASS` (default 0.9). Latency is
reported, not gated — a single sample is too noisy. Raise the run count for
trustworthy percentiles:

```bash
E2E_EVAL_RUNS=5 pytest -m e2e
```

The `tool_s` column (sum of `tool_calls.duration_ms`) versus the turn latency is
the attribution signal: when a turn takes seconds but its tools take a fraction
of one (e.g. memory read/write), the time is Claude inference + Telegram
round-trips, not tool I/O.

## Response-time limits

On top of the eval's aggregate reporting, every DM/group feature test
**hard-asserts** that its observable lands within a per-kind limit (the
`MAX_*` constants in `support/helpers.py`). Reply latency is judged on the
first chunk (`assert_reply_within`); other observables are timed with
`measured(...)` and checked with `assert_within`:

| Observable | Limit | Tests |
| --- | --- | --- |
| text answer | 5s | basic, responsiveness, context, reply-linkage |
| writes/reads a memory file | 10s | memory |
| adds an emoji reaction | 10s | reactions |
| reads a skill / schedules a reminder | 30s | skills, reminders (scheduled) |
| every reply to a 3-message burst | 30s | burst |
| renders an image | 60s | render |
| a scheduled reminder fires | 160s | reminders (fires, delayed by design) |

These are a forcing requirement, not a description of today's speed: a plain
text turn currently runs ~5–8s, so the 5s text tests can go red until the bot
gets faster. The lone exception is the access test — it asserts the bot stays
**silent** in an unauthorized group, so there is no reply to time (its 8s
silence window is the time bound).

## Layout

```
tests/e2e/
├── conftest.py              fixtures + skip-gate (must live here for pytest)
├── README.md
├── test_*.py               the actual tests
└── support/                all machinery — not collected as tests
    ├── harness.py          launches the bot subprocess, config, env, DB/memory helpers
    ├── helpers.py          Telethon client: send a message, time/collect the reply
    └── make_session.py     one-time login to capture your session string
```
