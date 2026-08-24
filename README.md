<p align="center">
  <img src="assets/about.jpg" alt="About hamroh">
</p>

---

> **Try it live:** a running instance lives in the [@rustamz_workshop](https://t.me/rustamz_workshop) Telegram group — join and message Luna, assistant running on top of hamroh, to see it in action before you install.

**hamroh** runs a persistent AI assistant in your Telegram, an agent that has memory, runs scheduled tasks, can monitor things, and can be extended with any MCP tools and skills you wire up.

Out of the box it:
- Stays in DM chats, groups, and joins conversations when it has something useful to say
- Has cutom memory protocol, skill protocol, MPC connection, tasks scheduler and reminders 
- Runs *self-reflection* — reviews what it got wrong and proposes new rules for your approval
- Executes scheduled research tasks in background subagents while staying responsive to messages
- It is extendable: add MCPs to connect it to anything: GitHub, Jira, email, calendar, your own APIs. Add skills, build custom tools.

The goal is a [Jarvis](https://www.youtube.com/watch?v=Qav7NJIsKL4&t=2s) — an AI that lives with you, monitors what matters, and acts on your behalf. hamroh is the foundation.

## Quickstart (3 minutes)

Runs on a laptop or small VPS.

If you don't know where to run, I recommend [Hetzner](https://www.hetzner.com/cloud/) or [Contabo](https://contabo.com/en/vps/).
 
Pre-requisite: 
* Install Docker compose
* Install Claude Code CLI, or Antigravity CLI
* Generate a Claude auth token on your machine: `claude setup-token` (opens a browser; works with a Claude subscription or API). It prints a token starting with `sk-ant-oat01-…` — you'll paste it into `.env` below. This is the login for the bot on every OS (Linux, macOS, Windows).

> Prefer Google's **Antigravity CLI (`agy`)** over Claude? hamroh supports it as an alternate engine — see [Engine: Claude Code or Antigravity CLI](#engine-claude-code-or-antigravity-cli) below.

**Instructions for running on Linux**
```bash
git clone https://github.com/Rustam-Z/hamroh && cd hamroh

cp .env.example .env && nano .env
#   set TELEGRAM_BOT_TOKEN  (create a bot in @BotFather and copy its token here)
#   set HAMROH_OWNER_ID  (your numeric Telegram user id, from @userinfobot)
#   set CLAUDE_CODE_OAUTH_TOKEN  (run `claude setup-token`, paste the sk-ant-oat01-… token)
#   update if necessary: HAMROH_MODEL and HAMROH_EFFORT

cp access.json.example access.json
#   give access to extra DMs and groups, you can use /access and /deny commands after bot started to update the list

cp plugins.json.example plugins.json && nano plugins.json
#   single source of truth for the bot's capability surface — see below

cp prompts/project.md.example prompts/project.md && nano prompts/project.md
#   set bot name, language, personality

docker compose up -d --build                                              # build and run, wait for "hamroh is live"
docker compose logs -f                                                    # [optional] monitor logs
docker compose exec hamroh python -m hamroh.scripts.trace --follow  # [optional] monitor Claude Code I/O logs
```

DM your bot. It replies.
 
### No docker?

You need Python 3.11+ and the Claude Code CLI (`claude --version`).

```bash
uv sync --extra dev
uv run python -m hamroh                                               # run, wait for "hamroh is live"
uv run python -m hamroh.scripts.trace --follow                        # [optional] monitor, Claude Code I/O logs
```

### Engine: Claude Code or Antigravity CLI

hamroh can run on **two agent engines**, chosen with the `HAMROH_ENGINE` setting in `.env`:

- **`claude`** *(default)* — Anthropic's Claude Code CLI. This is what the Quickstart above sets up.
- **`agy`** — Google's [Antigravity CLI](https://antigravity.google). Same bot, same tools, memory, reminders, skills, and `plugins.json` — only the model runner underneath changes.

**To use the Antigravity engine instead of Claude:**

```bash
# 1. Install the Antigravity CLI (the Docker image already includes it)
curl -fsSL https://antigravity.google/cli/install.sh | bash

# 2. Sign in once — opens a browser and writes ~/.gemini/oauth_creds.json.
#    agy has NO token env var (unlike Claude's CLAUDE_CODE_OAUTH_TOKEN); the
#    credential is that file. On a headless VPS, sign in on your laptop then
#    copy ~/.gemini to the server. In Docker, mount ~/.gemini (see below).
agy

# 3. In .env, switch the engine and pick an agy model:
#   HAMROH_ENGINE=agy
#   HAMROH_MODEL=gemini-3.1-pro-high   # run `agy models` for the exact ids
#   Effort is part of the model id (the -high / -medium / -low suffix), so
#   HAMROH_EFFORT does not apply; CLAUDE_CODE_OAUTH_TOKEN is ignored too.

# then run as usual
docker compose up -d --build      # or: uv run python -m hamroh
```

> **Docker + agy:** the container needs your agy login, so mount `~/.gemini` into it —
> add `- ~/.gemini:/root/.gemini` under the `hamroh` service's `volumes:` in
> `docker-compose.yml` (the file ships this line commented out).

Everything else — access control, skills, memory, reminders, the browser and rendering tools — works identically on both engines. Design notes and the full comparison are in [docs/hamroh-antigravity-plan.md](docs/hamroh-antigravity-plan.md).

## Configuration

> **This README is the high-level intro.** Deeper material lives in
> [docs/](docs/) — full technical manual, deployment walkthrough, tools
> reference, and the systems hamroh descends from. Start at
> [docs/README.md](docs/README.md).

Out of the box: messaging, memory, reminders, web, vision. Want shell access? Code editing? Plug in any other MCP server — GitHub, Jira, Notion, Slack, your own — same one-entry pattern, stdio or remote HTTP/SSE with auth headers.

### Customization

Beyond the config files, you extend the bot by dropping in files — no Python needed for most:

- **Skills** — add a playbook at `skills/<name>/SKILL.md`; the bot reads it on its own initiative. [docs](docs/documentation.md#agent-skills)
- **MCPs & tools** — capability surface, what tools, skills, and MCPs are on `plugins.json` (`stdio` or remote HTTP/SSE), with credentials pulled from `.env` via `${VAR}`. Read [docs/tools.md](docs/tools.md). [docs/documentation.md](docs/documentation.md#what-pluginsjson-controls).
- **Reminders** — custom recurring reminders shipped with the bot are at `default-reminders.json`. [docs](docs/documentation.md#custom-reminders-default-remindersjson).
- **Memory notes** — the bot's notes live under `memories/` (e.g. `memories/notes/references.md`); the bot reads, searches, and writes them, and you can curate them too. Addressed by full path (`memories/...`) and git-tracked, so memories survive restarts and you can commit them. [memories/README.md](memories/README.md)
- **Persona & rules** — extend the system prompt by editing `prompts/project.md`; it's appended to the shipped `prompts/system.md`. [docs](docs/documentation.md#system-prompt). Bot name, language, house rules, owner-specific instructions; appended to the shipped `prompts/system.md`.
- **Access** — who can DM the bot or use it in groups (hot-reloaded, no restart). [docs/documentation.md](docs/documentation.md#access-control).
- `.env` secrets — Telegram bot token, owner id, plus any credentials your `plugins.json` entries reference via `${VAR}` (the example file's GitLab / GitHub entries demonstrate the pattern).

Read more in [docs/documentation.md](docs/documentation.md#run-your-own-agent).

### Telegram @BotFather configs

- Disable "Allow groups" if you don't want others to add bot in groups. 
- Enable "Bot to bot communication" so that bot can see other bot's messages.

## What hamroh can do

A quick tour — the full per-tool surface (args, limits, rails) is in [docs/tools.md](docs/tools.md).

- **Communication & media:** send / reply / edit / delete, reactions, polls; render HTML and LaTeX to PNG; read inbound photos (vision), text-like docs, and PDFs.
- **Memory:** persistent markdown addressed by full path (list / search / read / write / append), 64 KiB/file, read-before-write, survives restarts. One store: a **git-tracked** `memories/...` folder the bot reads, searches, writes, and appends to — and that you can commit and curate. See [`memories/README.md`](memories/README.md).
- **Search & history:** web search / fetch (no internal URLs) and read-only SQL SELECTs on the chat database.
- **Browser:** drives a real headless Chromium for pages `WebFetch` can't reach — navigate, click, fill, read, screenshot, download. On by default.
- **Scheduling:** one-shot + cron reminders, plus git-tracked custom reminders in `default-reminders.json`. Daily self-reflection (on by default) that proposes durable rules for your approval.
- **Skills & self-edit:** operator-curated playbooks under `skills/`; the bot can append rules to `prompts/project.md` (owner-only).
- **Opt-in:** shell, code editing, and subagents — all off by default, toggled in `plugins.json`. Plug in any external MCP server the same way.
- **Can't:** generate images; send/read voice, video, stickers, GIFs; moderate groups; make calls.

## Architecture

```
Telegram  →  Engine (buffer + debounce)  →  Claude worker  →  claude process
                       │                                            │
                       ▼                                            ▼
                    SQLite                                   Local MCP server
```

- **Telegram listener** reads messages, saves them to SQLite, hands
  them off.
- **Engine** bundles messages that arrive close together. If a new
  one arrives while Claude is mid-reply, it's injected into the
  running turn.
- **Claude worker** runs the `claude` subprocess and restarts it on
  crash.
- **MCP server** auto-loads every tool in
  [hamroh/tools/](hamroh/tools/).

The engine handles **one turn at a time**. A long task in chat A
delays chat B until it finishes. Fine for one user; for busy setups,
run a separate bot per chat group.

The system prompt is two files: [prompts/system.md](prompts/system.md)
(generic hamroh behaviour, shipped) and `prompts/project.md`
(your overlay — gitignored, copy from
[prompts/project.md.example](prompts/project.md.example)).

## Security

The bot is public-facing and the security model is enforced in code, not by hope — see [Security model](docs/documentation.md#security-model) for the full list of rails and [docs/tools.md](docs/tools.md) for the per-tool surface.

## Contributing

Issues and PRs welcome.

Architecture deep-dive before bigger changes: [docs/documentation.md](docs/documentation.md).

## License

MIT. See [LICENSE](LICENSE).
