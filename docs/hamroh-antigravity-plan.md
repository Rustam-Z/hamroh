# Antigravity engine backend for hamroh

The plan for adding **Google's Antigravity CLI (`agy`)** as an alternate agent engine for
hamroh, selectable via `HAMROH_ENGINE=agy` alongside the default Claude Code backend.

> **Final decision (see §8):** this ships as a **second backend inside the hamroh repo**, not a
> separate project. Claude Code stays the default; `agy` is opt-in. (Earlier drafts of this doc
> planned a standalone `hamroh-antigravity` repo — that was reversed; the "separate repo"
> phrasing below is retained only where it explains the design history.)

---

## 1. Overview & goal

hamroh is a self-hosted persistent AI companion that lives in your Telegram. It runs an AI
agent as a supervised subprocess and gives that agent a set of tools (memory, reminders,
browser, rendering, Telegram actions, and more) through a **local MCP server**.

The Antigravity backend keeps that whole idea intact. The only thing that changes is the
**engine**: instead of driving the `claude` CLI, it drives Google's `agy` CLI. Everything a user
sees — the Telegram bot, the tools, the memory, the reminders, the skills — works the same way.

**Goal:** full feature parity, with the engine chosen by `HAMROH_ENGINE` (`claude` | `agy`).

### Why this is feasible

hamroh's architecture is already almost entirely engine-agnostic. The agent talks to the rest
of the system only through two channels:

1. the **local MCP server** (`hamroh/mcp_server.py`) that exposes every tool, and
2. **Telegram** (for input and output).

Neither of those is tied to Claude. Only one directory — `hamroh/cc_worker/` — actually knows
it is talking to Claude Code. That is the single layer we rewrite (into `agy_worker/`).

`agy` (Antigravity CLI, v1.1.4) is already installed on the development machine and confirmed
working — including headless one-shot runs and MCP tool calls (see §5). It natively supports
the pieces we need: MCP servers, project-level instructions, and model selection.

### Why a separate repo

hamroh-antigravity is its **own repository**, not a backend inside hamroh. The reason is that
the two are meant to evolve as **independent products** — different personas, roadmaps, and
release cycles — and keeping them apart avoids one project's changes destabilising the other.

The trade-off is explicit and accepted: the ~90% of engine-agnostic code (see §2) is
**maintained in two places**, so a shared bug fix or new tool must be applied in both repos.
The alternative — one repo with a pluggable backend — is recorded in §8 as considered and
deliberately set aside.

---

## 2. What carries over unchanged

These layers reuse hamroh's code as-is. The only change is a package rename (`hamroh` →
`hamroh_ag`) so the two projects can coexist on one machine. They are engine-agnostic because
they talk to the agent only through the MCP server or Telegram — never through Claude Code.

| Layer | hamroh location | Why it carries over |
|---|---|---|
| MCP tools (all of them) | `hamroh/tools/` (incl. `telegram/`, `browser/`) | Exposed over MCP; any MCP-capable CLI can call them |
| Local MCP server | `hamroh/mcp_server.py` | Standard MCP; engine-independent |
| Message engine (debounce, buffering) | `hamroh/engine/` | Works on Telegram messages, not on the agent CLI |
| Telegram integration | `hamroh/telegram_io/` | Pure Telegram; owner commands, dispatcher, attachments |
| Reminder scheduler | `hamroh/scheduler/` | Drives the MCP `reminder_*` tools |
| Database | `hamroh/db/` | SQLite; no engine coupling |
| File stores (memory, skills, instructions, renders) | `hamroh/storage/` | Files on disk; read/written via MCP tools |
| Logging, transcript, helpers | `hamroh/helpers/`, `hamroh/utils/` | Generic utilities |
| Skills & memories content | `skills/`, `memories/` | Markdown playbooks and notes |
| System prompt content | `prompts/system.md`, `prompts/project.md` | Text; only *how* it is injected changes (§4) |
| Access control & rate limiting | `access.py`, `rate_limiter.py` | Telegram-level gates |
| External MCP loader | `plugins.py` | Reused; its **output format** is retargeted (§4) |

**Bottom line:** the large majority of hamroh's code is reused untouched.

---

## 3. What must be rebuilt: the agent supervisor

The heart of the project is replacing `hamroh/cc_worker/` (which drives `claude`) with a new
`hamroh_ag/agy_worker/` (which drives `agy`).

Today `cc_worker/spec.py::build_argv` constructs a `claude` command line using flags that **do
not exist** on `agy`: `--print`, `--input-format/--output-format stream-json`,
`--include-partial-messages`, `--system-prompt`, `--mcp-config`,
`--tools/--allowedTools/--disallowedTools`, `--json-schema`, `--effort`, `--resume`.

The rewrite maps each concern to the Antigravity equivalent. **All rows below are now
confirmed** against the installed binary (`agy` v1.1.4) during the §5 spike:

| Concern (hamroh / Claude Code) | Antigravity (`agy`) — confirmed |
|---|---|
| System prompt via `--system-prompt <text>` | Write **`AGENTS.md`** (or `GEMINI.md`, or `.agents/rules/*.md`) at the workspace root — hierarchical rules files, discovered automatically |
| Tool config via `--mcp-config <file>` | `{"mcpServers": { … }}` in **`~/.gemini/config/mcp_config.json`** (the CLI's location — *not* `~/.gemini/settings.json`, which is the IDE's and is ignored by the CLI). Stdio shape `command`/`args`/`env` = hamroh's `McpPluginSpec`. **Proven working in the spike.** |
| MCP tool invocation | **Different model from Claude.** `agy` exposes one built-in **`call_mcp_tool`** dispatcher (+ `list_resources`/`read_resource`), *not* first-class `mcp__hamroh__<tool>` names. The model calls `call_mcp_tool(server, tool, args)`. hamroh's per-tool allowlist/prompt-inventory concept collapses to "allow the `mcp` permission." |
| Model via `--model` | `agy --model <id>` per run (`agy models` lists them). **No effort/thinking flag** exists |
| Tool gating via `--allowedTools/--disallowedTools` | Two mechanisms: (1) `permissions.allow`/`deny` in **`~/.gemini/antigravity-cli/settings.json`** — e.g. `"allow":["mcp(*)"]` grants all MCP calls (**proven**); (2) a **`PreToolUse` hook** (`.agents/hooks.json`) that returns `{"decision":"allow"|"deny"}` per tool — the clean way to allow hamroh's MCP tools while denying `agy`'s built-ins (`run_command`, `view_file`, `write_to_file`, …). **Headless `-p` auto-denies any tool left at "ask" and prints nothing**, so every reachable tool must be explicitly allowed or denied. |
| Structured output via `--json-schema` + `StructuredOutput` | None. `agy -p` prints the **plain-text** final response to stdout; the turn ends when the conversation goes idle. Structured events (tool calls etc.) are in `.gemini/antigravity/transcript.jsonl` |
| Streaming via `stream-json` + partial messages | `agy -p` does not stream tokens to stdout — it blocks then prints the final answer. The typing indicator becomes a simple "thinking…" spinner; token-level streaming is not available in print mode |
| Session continuity via `--resume <id>` | `agy --conversation <id>` (or `--continue` for the most recent). Conversations persist as protobuf under `~/.gemini/antigravity-cli/conversations/<id>.pb` |

Sub-parts of the rewrite:

- **argv builder** — new `agy_worker/spec.py` that builds the `agy --print` command line
  (`--model`, `--conversation <id>`, prompt) and writes the config files (`AGENTS.md`, the
  `mcpServers` block, `permissions.allow`).
- **output parsing** — much simpler than Claude Code's `stream-json`. `agy -p` returns the
  final answer as plain text on stdout (exit 0), so the reply is just captured directly.
  Tool-call detail, if needed for logging, is read from `.gemini/antigravity/transcript.jsonl`.
  hamroh's `cc_worker/events.py` / `event_handlers.py` / `cc_failure_classifier.py`
  stream-json decoders are **largely deleted**, not ported.
- **worker supervision** — `cc_worker/worker.py` (spawn, crash backoff with jitter, liveness
  watchdog, tool-error circuit breaker) is **mostly reusable**. Only the spawn spec and the
  output capture change; the supervision logic is engine-agnostic.

> **Simplification note:** because print mode blocks and returns plain text, `agy_worker` is
> meaningfully *simpler* than `cc_worker` — no streaming JSON state machine, no partial-message
> reassembly. The cost is losing token-level streaming (see the typing-indicator row above).

---

## 4. Config surface changes

- **System prompt → `AGENTS.md`.** Reuse the exact composition that
  `cc_worker/spec.py::_compose_system_prompt` builds today (shipped `system.md` + `project.md`
  overlay + runtime block + skills index + memory index + tools inventory). Instead of passing
  it as a flag, **write it to `AGENTS.md`** at the project root before each run.
- **MCP config → `~/.gemini/config/mcp_config.json`.** Emit `{"mcpServers": {…}}` from the same
  `McpPluginSpec` list that `plugins.py` already produces (its stdio shape — `command`/`args`/
  `env` — matches `agy`'s format directly), plus the local hamroh MCP server entry. `plugins.py`'s
  validation and `${VAR}` interpolation are reused unchanged; only the serialization target
  differs. **Confirmed in the spike:** the CLI reads this file (not `~/.gemini/settings.json`,
  which is the IDE's), and hamroh's tools become reachable via `call_mcp_tool`.
- **Permission gate → `PreToolUse` hook (`.agents/hooks.json`).** Ship a small hook script that
  reads each tool call on stdin and returns `{"decision":"allow"}` for `call_mcp_tool` /
  `list_resources` / `read_resource` and `{"decision":"deny"}` for `agy`'s built-ins
  (`run_command`, `view_file`, `write_to_file`, `define_subagent`, …). This is where hamroh's
  `tool_groups` map: `bash`/`code`/`subagents` on ⇒ the hook stops denying the matching
  built-ins. A blanket `permissions.allow:["mcp(*)"]` in `~/.gemini/antigravity-cli/settings.json`
  is the simpler fallback (proven), but the hook is preferred because it also *denies* the
  built-ins instead of leaving them at the "ask"-then-dead-end state.
- **Skills → `.agents/skills/` (optional).** Mirror `skills/*/SKILL.md` into `.agents/skills/`
  so `agy`'s native skill commands work too, on top of the existing `mcp__hamroh__skill_*`
  tools. Keep the MCP tools as the source of truth.
- **Env / auth.** `.env` gains **model/backend selection** (default Gemini, optionally Claude),
  plus `ANTIGRAVITY_API_KEY` or OAuth. Rename `HAMROH_*` variables to the new project's
  namespace. Drop `CLAUDE_CODE_OAUTH_TOKEN` unless the Claude backend is selected.
- **Docker.** Swap the Node.js + Claude-CLI install step for the `agy` installer
  (`curl -fsSL https://antigravity.google/cli/install.sh | bash`). Keep the uv build stage and
  the headless Chromium (Playwright) — those are unchanged.

---

## 5. Spike findings — all resolved & proven

A spike was run against the installed binary (**`agy` v1.1.4`**), ending in a **full headless
round-trip**: a hamroh-style stdio MCP server → `agy -p` → `call_mcp_tool` → tool result
(`PONG`) on stdout, with **no `--dangerously-skip-permissions`**. Every earlier unknown is
resolved:

| Question | Finding (verified) |
|---|---|
| Headless auth | Works. OAuth creds at `~/.gemini/oauth_creds.json`; `agy -p` runs unattended. |
| Output format | Plain-text final answer on stdout, exit 0. No stream-json. Structured events in `.gemini/antigravity/transcript.jsonl`. |
| Turn-end | Print mode blocks until the conversation is idle, then prints. |
| MCP config | `{"mcpServers":{…}}` in **`~/.gemini/config/mcp_config.json`** (CLI location; the IDE's `~/.gemini/settings.json` is ignored by the CLI). Stdio shape matches `McpPluginSpec`. |
| MCP invocation | Single **`call_mcp_tool`** dispatcher (+ `list_resources`/`read_resource`), not per-tool names. |
| Permission gate | `permissions.allow:["mcp(*)"]` in **`~/.gemini/antigravity-cli/settings.json`** (proven), or a **`PreToolUse` hook** in `.agents/hooks.json` returning `allow`/`deny` per tool. |
| Built-in tools | Always present (`run_command`, `view_file`, `write_to_file`, `grep_search`, `search_web`, `define_subagent`, …). `excludeTools` did **not** hide them; gate them via the hook. |
| Rules / system prompt | `AGENTS.md` / `GEMINI.md` / `.agents/rules/*.md`, auto-discovered at workspace root. |
| Skills | `.agents/skills/<name>/SKILL.md` + `.agents/skills.json`. |
| Session resume | `--conversation <id>` / `--continue`; protobuf store under `~/.gemini/antigravity-cli/conversations/`. |
| Model / effort | `--model <id>` per run; no effort control. |

### The tool-gating decision — resolved

The earlier draft flagged tool gating as an open fork that collided with hamroh's ban on
`--dangerously-skip-permissions`. **The spike resolved it without that flag:** a `PreToolUse`
hook allows hamroh's MCP tools (`call_mcp_tool`) and denies `agy`'s built-ins, reproducing
hamroh's locked-down surface headlessly. hamroh's `tool_groups` (`bash`/`code`/`subagents`)
become "which built-in tool names the hook stops denying." The forbidden-flag invariant is
preserved.

> Caveat proven along the way: workspace-local config (`.gemini/settings.json`, `.agents/` in an
> **untrusted** dir) is ignored — the CLI honours its global config (`~/.gemini/config/`,
> `~/.gemini/antigravity-cli/settings.json`) and trusted workspaces. The worker manages those
> global files, and the deployment workspace must be a trusted git repo for `.agents/` hooks to
> load.

---

## 6. Creation steps (ordered)

1. **Scaffold** the new repo `hamroh-antigravity`. Copy hamroh's tree; rename the package
   `hamroh` → `hamroh_ag` and update imports and the console-script entry point.
2. **Swap the engine.** Delete `cc_worker/`; add `agy_worker/` with the new argv builder and the
   plain-text output capture (per the §5 findings — no stream-json parser needed).
3. **Add the config writers.** Generate `AGENTS.md`, `~/.gemini/config/mcp_config.json`
   (`mcpServers`), the `.agents/hooks.json` permission gate, and the `permissions.allow`
   fallback in `~/.gemini/antigravity-cli/settings.json`; wire `__main__.py` and `startup.py`
   to write them at boot / per run.
4. **Update project files.** `.env.example`, `plugins.json` (retargeted output), `Dockerfile`,
   `Makefile`, and `README.md`.
5. **Port the tests.** Keep the engine / tool / MCP unit suites. Rewrite the `cc_worker` tests
   and the e2e harness to target `agy`.

---

## 7. Verification

Prove feature parity end-to-end:

- **Unit tests.** All reused suites (engine, tools, MCP, memory, reminders, access, security)
  stay green. New `agy_worker` tests pin the `agy` argv and the output-parsing contract.
- **Manual spike.** `agy -p "call the time_now tool"` against the hamroh MCP server returns a
  real tool result — proves the MCP wiring end-to-end.
- **E2E.** Run the same Telegram scenarios hamroh covers in `tests/e2e/` (basic reply, memory,
  reminder, browser, rendering, access commands) with `agy` as the engine and confirm they
  pass.

---

## 8. Decision: one repo, selectable backend

Final decision: **`agy` ships as a second engine inside the hamroh repo**, selected with
`HAMROH_ENGINE=agy`. Claude Code (`cc_worker`) stays the default and is untouched; the new
`agy_worker` sits beside it and reuses `cc_worker`'s shared contract (`TurnResult`,
`CrashLoop`, `WorkerHooks`) so the engine and startup wiring treat both backends identically.
`startup.create_worker()` is the single switch point.

An earlier draft planned a **standalone `hamroh-antigravity` repo** (copy-and-adapt, replacing
`cc_worker` outright). That was reversed in favour of the in-repo backend: it avoids maintaining
the ~90% engine-agnostic code twice, keeps Claude working, and lets you flip engines with one
env var. The standalone scaffold was built and then removed once the backend landed here.

---

## Implementation status

Built **inside the hamroh repo** as a selectable backend. Claude Code stays the default and is
untouched; `HAMROH_ENGINE=agy` switches to Antigravity.

**Done**
- New `hamroh/agy_worker/` package: `spec.py` (argv + tool-group→built-in mapping +
  `FORBIDDEN_FLAG` guard), `config_writer.py` (writes `AGENTS.md`, `.agents/hooks.json` + gate
  script, `~/.gemini/config/mcp_config.json`, `permissions.allow` fallback), `worker.py`
  (`AgyWorker` one-shot lifecycle). It **reuses** `cc_worker`'s `TurnResult` / `CrashLoop` /
  `WorkerHooks`, so no duplication and the engine treats both backends identically.
- Engine switch: `config.engine` (`HAMROH_ENGINE`) + `config.agy_bin` (`AGY_BIN`);
  `startup.create_worker()` builds the right spec+worker; `__main__` calls it. `AgyWorker` gained
  a `_stop_supervisor` event so it is drop-in for startup's signal handler.
- **Tests** (`test_agy_worker.py`, `test_agy_worker_lifecycle.py`, hermetic via a fake `agy`):
  argv, config generation, the permission gate executed for real, and worker lifecycle
  (send→result, failed-invocation, resume progression, `reset_session`, crash-budget →
  `CrashLoop`+`on_giveup`). Surfaced and fixed a bug: `_run_turn` was swallowing `CrashLoop`; it
  now propagates (with a sentinel to unblock the engine).
- **HTTP MCP compatibility proven.** The biggest integration risk is closed: a live headless
  `agy -p`, wired by the real `config_writer`, connected to a FastMCP **streamable-HTTP** server
  (`serverUrl`, the exact transport hamroh uses) and called its tool → `PONG_FROM_HAMROH_HTTP`.
- **Config/deploy:** `.env.example` documents `HAMROH_ENGINE`/`AGY_BIN`/`ANTIGRAVITY_API_KEY`;
  `Dockerfile` installs `agy` alongside Claude so either engine works in a container.
- **Verified:** the full **758 unit tests pass** (Claude path intact + new agy tests),
  `ruff`/`format`/`mypy`/`lizard` all clean.

**Remaining (follow-ups)**
- `dropped_text` / `user_visible_action` detection by parsing `transcript.jsonl` (currently
  optimistic on a clean run; the tool set to key on lives in `cc_worker`'s `USER_VISIBLE_TOOLS`).
- agy-specific failure wording: the engine currently classifies both engines' errors with
  `cc_worker`'s (Claude-worded) classifier.
- Map external plugin MCP servers into agy's `mcpServers` (agy backend uses the local hamroh
  server only; external MCPs are disabled by default in `plugins.json`).
- Live Telegram boot with `HAMROH_ENGINE=agy` + the e2e harness parametrised by engine (needs
  real tokens).

---

## Appendix: common vs different

A quick reference for what carries over between the two engines and what does not.

### What's common (engine-agnostic — ~90% of hamroh)

Both `claude` and `agy` are subprocess CLIs that speak MCP. Everything hamroh does *around* the
agent works the same regardless of which one runs:

| Shared concern | How both handle it |
|---|---|
| Tools | Exposed over a local MCP server. Both CLIs consume MCP identically. |
| System prompt | Both take instructions — Claude via `--system-prompt`, `agy` via `AGENTS.md`. Same content, different delivery. |
| Skills | Both have a skills concept (`skills/` ↔ `.agents/skills/`). |
| Model selection | Both take a `-m` / `--model` flag. |
| One-shot invocation | Claude `--print`, `agy -p`. Prompt in → answer out. |
| Everything else | Telegram I/O, engine/debounce, DB, memory, reminders, browser, access control, rate limiting — zero engine coupling. |

### What's different (the ~10% that is `cc_worker/` → `agy_worker/`)

| Concern | Claude Code | Antigravity (`agy`) |
|---|---|---|
| Prompt delivery | `--system-prompt <text>` (flag) | `AGENTS.md` (file on disk) |
| MCP config | `--mcp-config <file>` flag | `mcp_config.json` |
| Tool gating | `--tools/--allowedTools/--disallowedTools` (exclusive allowlist — hamroh's security model) | Unknown — may have no equivalent |
| Structured output / turn-end | `--json-schema` + `StructuredOutput` tool | Unknown |
| Streaming | `stream-json` + partial messages (drives typing indicator) | Unknown format |
| Session resume | `--resume <id>` | Unknown |
| Effort / thinking | `--effort` | Likely none |

The differences are concentrated entirely in the worker layer, which is what makes this
tractable. The three "Unknown" rows (tool gating, structured output, streaming) are the primary
technical risks — resolved by the §5 spike before the parser is designed.
