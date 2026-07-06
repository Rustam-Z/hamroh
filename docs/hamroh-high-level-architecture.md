Here's an orientation for someone seeing it for the first time. The best way to understand the code is to follow one Telegram message through the system — the files line up with that journey.

The big picture

hamroh is a Telegram bot whose "brain" is a Claude Code subprocess. hamroh itself is the harness: it receives Telegram messages, feeds them to Claude Code as a conversation, exposes a set of tools (send message, memory, reminders,
browser…) over a local MCP server, and ships Claude's tool calls back out to Telegram.

Telegram ──▶ dispatcher ──▶ engine ──▶ cc_worker ──▶ [Claude Code subprocess]
                                          ▲                    │
                                          │              calls MCP tools
                                    mcp_server ◀───────────────┘
                                          │
                                    tools/ ──▶ back out to Telegram

The startup / entrypoint

- __main__.py (110 lines) — python -m hamroh. The readable narrative of bringing up the 4 components in order: DB → MCP server → Claude Code subprocess → engine + dispatcher. Start here.
- startup.py (717) — the actual wiring code that __main__ calls (open DB, build the spawn spec, register crash callbacks). Bulky but mechanical; skim it.
- config.py (354) — all configuration/env resolution. The Config object threaded everywhere.

The message lifecycle (the core 4 files)

1. telegram_io/dispatcher.py (466) — the front door. Receives every inbound Telegram update, applies access control (access.py) and rate limiting (rate_limiter.py), persists it, and calls engine.submit().
2. engine/engine.py (816) — the heart of hamroh (its own docstring says so). It debounces messages (batches bursts within ~1s), formats them as XML, ships them to the worker, and runs the control loop that decides what to do when a
turn ends. This is the file we just edited — _handle_turn_result, the stop/sleep/heartbeat actions, dropped-text, silent-stop all live here.
3. cc_worker/worker.py (727) — owns the Claude Code subprocess: spawns it, writes user messages to its stdin as stream-JSON, reads its stdout events, supervises crashes/respawns. send() (which our nudge uses) is here.
  - cc_worker/event_handlers.py (308) — parses Claude's stdout stream into a TurnResult (text blocks, tool calls, the StructuredOutput action). USER_VISIBLE_TOOLS and the dropped_text logic live here.
  - cc_worker/spec.py (399) — builds the exact CLI command + --system-prompt for spawning Claude Code (_compose_system_prompt).
4. mcp_server.py (290) — a local HTTP MCP server that exposes hamroh's tools to the Claude subprocess. This is how Claude "does things" (send a message, write memory). When you saw registered MCP tool telegram_send_message in the
logs, that was this file.

The tools (what Claude can actually do)

- tools/ — one module per capability, all built on tools/base.py (229). Notable: tools/telegram/* (send/reply/react/poll — the user-visible ones), tools/memory.py, tools/reminder.py, tools/browser/browser.py, tools/render_html.py.

Supporting subsystems

- models.py — shared data types: ChatMessage, ControlAction (the stop/sleep/heartbeat you just learned).
- db/ — SQLite layer: database.py (migrations), messages.py (message + tool-call persistence), reminders.py.
- storage/ — file-backed stores: memory.py (the bot's long-term memory files), attachments.py.
- reminder_scheduler.py + reminders_config.py — the cron-like loop that fires scheduled reminders into the engine.
- plugins.py (539) — enables/disables optional external MCP plugins (GitHub, GitLab…) and tool groups, driven by plugins.json.
- cc_failure_classifier.py — turns raw Claude/API errors (bad model, quota, auth) into friendly user messages.

Suggested reading order for a newcomer

1. README.md + __main__.py — what it is and how it boots
2. engine/engine.py — the control loop (the conceptual center)
3. cc_worker/worker.py + event_handlers.py — how Claude is driven and parsed
4. mcp_server.py + one file in tools/telegram/ — how Claude acts on the world
5. prompts/system.md — the instructions that shape Claude's behavior every turn

Want me to walk through any one of these in depth — e.g. trace a single "user says hi → bot replies" end to end through the actual functions?
