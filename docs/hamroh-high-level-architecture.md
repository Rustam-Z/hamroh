# The big picture

Here's an orientation for someone seeing it for the first time. The best way to understand the code is to follow one Telegram message through the system — the files line up with that journey.

hamroh is a Telegram bot whose "brain" is a Claude Code subprocess. hamroh itself is the harness: it receives Telegram messages, feeds them to Claude Code as a conversation, exposes a set of tools (send message, memory, reminders,
browser…) over a local MCP server, and ships Claude's tool calls back out to Telegram.

```
Telegram ──▶ dispatcher ──▶ engine ──▶ cc_worker ──▶ [Claude Code subprocess]
                                          ▲                    │
                                          │              calls MCP tools
                                    mcp_server ◀───────────────┘
                                          │
                                    tools/ ──▶ back out to Telegram
```

## The startup / entrypoint

- __main__.py (110 lines) — python -m hamroh. The readable narrative of bringing up the 4 components in order: DB → MCP server → Claude Code subprocess → engine + dispatcher. Start here.
- startup.py (717) — the actual wiring code that __main__ calls (open DB, build the spawn spec, register crash callbacks). Bulky but mechanical; skim it.
- config.py (354) — all configuration/env resolution. The Config object threaded everywhere.

## The message lifecycle (the core 4 files)

1. telegram_io/dispatcher.py (466) — the front door. Receives every inbound Telegram update, applies access control (access.py) and rate limiting (rate_limiter.py), persists it, and calls engine.submit().
2. engine/engine.py (816) — the heart of hamroh (its own docstring says so). It debounces messages (batches bursts within ~1s), formats them as XML, ships them to the worker, and runs the control loop that decides what to do when a
turn ends. This is the file we just edited — _handle_turn_result, the stop/skip/heartbeat actions, dropped-text, silent-stop all live here.
3. cc_worker/worker.py (727) — owns the Claude Code subprocess: spawns it, writes user messages to its stdin as stream-JSON, reads its stdout events, supervises crashes/respawns. send() (which our nudge uses) is here.
  - cc_worker/event_handlers.py (308) — parses Claude's stdout stream into a TurnResult (text blocks, tool calls, the StructuredOutput action). USER_VISIBLE_TOOLS and the dropped_text logic live here.
  - cc_worker/spec.py (399) — builds the exact CLI command + --system-prompt for spawning Claude Code (_compose_system_prompt).
4. mcp_server.py (290) — a local HTTP MCP server that exposes hamroh's tools to the Claude subprocess. This is how Claude "does things" (send a message, write memory). When you saw registered MCP tool telegram_send_message in the
logs, that was this file.

## The tools (what Claude can actually do)

- tools/ — one module per capability, all built on tools/base.py (229). Notable: tools/telegram/* (send/reply/react/poll — the user-visible ones), tools/memory.py, tools/reminder.py, tools/browser/browser.py, tools/render_html.py.

## Supporting subsystems

- models.py — shared data types: ChatMessage, ControlAction (the stop/skip/heartbeat you just learned).
- db/ — SQLite layer: database.py (migrations), messages.py (message + tool-call persistence), reminders.py.
- storage/ — file-backed stores: memory.py (the bot's long-term memory files), attachments.py.
- reminder_scheduler.py + reminders_config.py — the cron-like loop that fires scheduled reminders into the engine.
- plugins.py (539) — enables/disables optional external MCP plugins (GitHub, GitLab…) and tool groups, driven by plugins.json.
- cc_failure_classifier.py — turns raw Claude/API errors (bad model, quota, auth) into friendly user messages.

## Suggested reading order for a newcomer

1. README.md + __main__.py — what it is and how it boots
2. engine/engine.py — the control loop (the conceptual center)
3. cc_worker/worker.py + event_handlers.py — how Claude is driven and parsed
4. mcp_server.py + one file in tools/telegram/ — how Claude acts on the world
5. prompts/system.md — the instructions that shape Claude's behavior every turn

##  What are text_blocks?

When Claude responds, its output is a sequence of content blocks: tool_use blocks (calling tools), thinking blocks, and text blocks — plain prose. In a normal chat app the text blocks are the reply the user reads. In hamroh they go
nowhere: the user only sees what's sent via telegram_send_message / telegram_reply_to_message. A text block exists only inside the CC session — like the ● Just a reaction. T-0 approaching. narration lines in that other bot's log.
Your system prompt tells the model not to write them, which is why healthy turns end with text_blocks=0.

When Claude responds, its response is a list of pieces

Every time Claude finishes thinking, its answer isn't one blob — it's a list of pieces, one after another. Each piece has a type:

Claude's response = [
  { type: "thinking",  ... }                          ← private reasoning
  { type: "text",      "France looks strongest..." }  ← plain written words
  { type: "tool_use",  telegram_reply_to_message(...) } ← an action
]

A text block is the second kind: plain written words, not an action. It's Claude saying something rather than doing something.

In Claude the app, saying = replying. In hamroh, it doesn't.

When you use Claude on claude.ai, the text blocks are literally the reply you read on screen. Saying and replying are the same thing there.

In hamroh they are not. Your user is on Telegram, and the only way words reach Telegram is the telegram_send_message tool call — an action. A text block isn't connected to anything. It gets written into the Claude Code session log
and stops there. Nobody's phone buzzes.

Think of it like an employee working a support inbox:

- Tool call = actually sending the customer an email.
- Text block = saying the answer out loud at their desk. Perfectly good answer — but the customer never hears it.

Why hamroh tracks them at all

Because Claude is trained on the claude.ai world, it sometimes slips into old habits: it writes its answer as a text block ("France looks strongest...") and ends the turn satisfied — talking at its desk, no email sent. From your
user's side: total silence.

That's why:

- Your system prompt says "don't write text blocks" — healthy turns show text_blocks=0 in the log.
- The worker counts them anyway, and if the turn ends with text blocks but no Telegram tool call, it sets dropped_text=True — meaning "Claude wrote words that never reached the user."
- The engine then rescues those words and sends them to the chat itself (unless the action was skip, where leftover text is just the model muttering about why it's staying silent, so it's discarded).

That other bot's log you pasted was full of text blocks — ● Nodira posted her final sign-off. 16 minutes to T-0. Each ● line is one. That bot lets the model narrate freely because the narration is only for the operator's log.
Hamroh forbids them because in your setup a text block is a reply that silently fell on the floor.

## Why dropped_text?

It's the safety net for the most damaging failure this design allows: the model types its answer as a text block and ends the turn, convinced it replied — while the user stares at a silent chat. The worker computes it in
event_handlers.py:274: text blocks exist AND no user-visible tool was called.

## Why does the engine care about it differently per action:

- stop means "I already delivered a reply via a Telegram tool." dropped_text on a stop contradicts that claim, so the engine treats it as a mistake and recovers — it delivers the stranded text to the waiting chat instead of losing
it (engine.py:727).
- skip means "I'm deliberately sending nothing" (group chatter not addressed to the bot, etc.). Leftover text on a skip is internal narration, not a lost reply — delivering it would break the deliberate silence, so it's logged and
discarded (engine.py:748). That distinction is exactly what your recent commit "never deliver skip narration as dropped text" fixed.

So dropped_text + action together disambiguate "forgot to hit send" (recover it) from "chose silence and muttered to itself" (discard it). Without the flag you'd either lose real replies or spam users with the bot's inner
monologue.

## Why reason in StructuredOutput?

Three jobs:

1. Operator observability. The [CC.done] action=stop reason=greeted owner back line is the only place you learn why a turn ended the way it did. When the bot ignores a group message, reason on the skip tells you whether that was
judgment or a bug — nothing else in the log can.
2. A forcing function on the model. reason is required non-empty for stop/skip (enforced by ControlAction's validator) precisely because those are the actions that end a turn with consequences for the user. Making the model justify
the claim "I replied" / "silence is correct" makes it commit deliberately rather than ending turns lazily. For sleep/heartbeat it's optional — nothing to justify.
3. Diagnosis of silent stops. The engine's silent-stop detector (engine.py:770) exists because models sometimes narrate their reply into the reason field ("acknowledged test invite") instead of sending it. Having the reason on
record is how you spot that pattern.

It's capped at 100 chars (REASON_MAX_LENGTH) so this metadata stays a few tokens per turn — the docstring in cc_schema.py explains the cost math.

## Error handling and crash handling

Tool-error breaker (2 fields) — per-turn safety

- tool_error_max_count / tool_error_window_seconds → cached in the worker, drive _record_tool_error/_trip_tool_error_breaker. If N failing tool calls land within the window (no success between), it aborts the turn and restarts the Claude subprocess instead
of letting the model spin on a broken tool. Needed — without it a stuck-tool loop runs unbounded.

Liveness watchdog (2 fields) — the thing from your original bug

- liveness_timeout_seconds / liveness_poll_seconds → _wedge_silence + _liveness_loop. Detects a genuinely hung subprocess and kills it for respawn. Needed — this is what turns a real hang into auto-recovery instead of eternal
silence.

Crash supervisor (4 fields) — restart-after-exit

All four are live in worker.py's crash path:
- crash_backoff_base (worker.py:361,395,426) — initial + exponential backoff wait.
- crash_backoff_cap (worker.py:425) — ceiling on that wait.
- crash_limit (worker.py:412,419) — how many crashes trip the bail-out.
- crash_window_seconds (worker.py:409,420) — rolling window for that count.

Together: respawn on crash with growing backoff; if it crashes too often too fast, give up and exit so an outer supervisor (systemd/docker) restarts the whole process. All needed — remove any and the backoff math or the crash-loop
guard breaks.
