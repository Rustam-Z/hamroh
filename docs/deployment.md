# Deployment Guide

This guide covers deploying hamroh to a VPS (Contabo, Hetzner,
DigitalOcean, etc.) using Docker, and setting up a continuous deployment
workflow.

## Prerequisites

- A VPS with SSH access
- A GitHub repo with your hamroh code
- A Telegram bot token (from @BotFather)
- A Claude account (subscription or API) to generate a `CLAUDE_CODE_OAUTH_TOKEN`

## Initial server setup (one-time)

```bash
# SSH into your server
ssh root@your-server-ip

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Node.js + Claude Code CLI
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code

# Generate a long-lived Claude auth token (interactive, opens a browser).
# It prints a token starting with sk-ant-oat01-… — you set it as
# CLAUDE_CODE_OAUTH_TOKEN in .env below. This is the auth path on every OS;
# no `claude login` / Keychain / mounted credentials needed.
claude setup-token

# Clone your private repo (SSH auth — add server's public key to GitHub first)
#   On server: ssh-keygen -t ed25519 (if no key exists)
#   Copy ~/.ssh/id_ed25519.pub → GitHub Settings → SSH keys
git clone git@github.com:your-user/hamroh.git ~/hamroh
cd ~/hamroh

# Configure
cp .env.example .env
vim .env   # set TELEGRAM_BOT_TOKEN, HAMROH_OWNER_ID, CLAUDE_CODE_OAUTH_TOKEN, etc.
cp prompts/project.md.example prompts/project.md
vim prompts/project.md   # customize identity, integrations, team info

# Build and start
docker compose up -d --build

# Verify it's running
docker compose ps
docker compose logs -f   # should see "hamroh is live"
```

DM your bot on Telegram to confirm it replies.

### Enabling capabilities

The bot ships with a tight default surface — Telegram messaging,
memory tools, reminders, and read-only web access only. Shell,
code-editing, subagents, and any external MCPs are **all off by
default**.

Toggles live in `plugins.json` at the repo root. Copy the shipped
template once on first setup:

```bash
cp plugins.json.example plugins.json
```

Then edit:

```jsonc
{
  "tool_groups": { "bash": true, "code": true, "subagents": false },
  "mcps":  [ /* sample Jira / GitLab / GitHub entries — keep, edit, or delete */ ],
  "skills_disabled": [],
  "builtin_tools_disabled": []
}
```

`plugins.json` is gitignored, so different deployments can carry
different toggles without fighting over the file. External MCPs are
declared in `plugins.json` but their credentials live in `.env`,
referenced as `${VAR}`. An MCP whose `${VAR}` references aren't
satisfied is silently skipped at boot. The shipped example carries
sample Jira / GitLab / GitHub entries to copy from — they're not
first-class, just convenient starting points.

For the per-tool list, the schema, "How to add a new MCP", and how
to disable individual built-in tools (e.g. `telegram_create_poll`,
`render_latex`) or skills, see [tools.md](tools.md). Restart the
container after editing either file: `docker compose up -d
--force-recreate`.

## Update workflow

### Manual (SSH)

Every time you push changes to GitHub:

```bash
ssh root@your-server-ip 'cd ~/hamroh && ./scripts/commit-and-push.sh && git pull && docker compose up -d --build'
```

Or step by step:

```bash
ssh root@your-server-ip
cd ~/hamroh
./scripts/commit-and-push.sh   # commit the bot's memories so pull isn't blocked
git pull
docker compose up -d --build
docker compose logs -f   # verify it started correctly
```

`commit-and-push.sh` commits and pushes anything the bot wrote to
`memories/` since the last deploy. Without it, an uncommitted memory
file that also changed upstream makes `git pull` abort.

### Automatic (GitHub Actions)

Create `.github/workflows/deploy.yml` in your repo:

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SERVER_IP }}
          username: root
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd ~/hamroh
            ./scripts/commit-and-push.sh
            git pull
            docker compose up -d --build
```

Then add these secrets to your GitHub repo (Settings → Secrets and
variables → Actions):

| Secret | Value |
|--------|-------|
| `SERVER_IP` | Your VPS IP address |
| `SSH_PRIVATE_KEY` | Contents of `~/.ssh/id_ed25519` (generate with `ssh-keygen -t ed25519` and add the public key to the server's `~/.ssh/authorized_keys`) |

Every push to `main` will automatically deploy to your server.

**Note:** Since the repo is private, the server needs SSH access to
GitHub for `git pull` to work. Make sure the server's SSH key
(`~/.ssh/id_ed25519.pub`) is added as either:

- A **deploy key** on the repo (Settings → Deploy keys) — scoped to
  this repo only, recommended
- Or an **SSH key** on your GitHub account (Settings → SSH keys) —
  grants access to all your repos

## The `data/` directory

The `data/` directory is created automatically on first run. It contains
only ephemeral, server-local state — memory no longer lives here. The bot's
memory is the git-tracked `memories/` folder at the repo root (bind-mounted
into the container as `./memories:/app/memories`), so it travels with the
repo and needs no migration. `data/` contains:

- `data/hamroh.db` — SQLite database (messages, users, reminders,
  tool call logs) — starts fresh on new servers
- `data/session_id` — Claude Code session ID for conversation continuity
- `data/attachments/` — inbound photos/docs the dispatcher saved
- `data/renders/` — outbound PNGs from `render_html`
- `data/cc_logs/` — raw Claude Code subprocess logs

Headless Chromium for `render_html` is pre-installed in the Docker
image (`playwright install --with-deps chromium`) — no per-host
provisioning step needed.

**First deployment:** nothing to do — the bot creates everything.

**Migrating from another server:** nothing in `data/` is worth copying. The
bot's memory lives in the git-tracked `memories/` folder, so a fresh
`git clone` (or `git pull`) brings every note with it. Don't copy `session_id`
or `hamroh.db` to a new server — stale session IDs cause CC subprocess crashes,
and the database will rebuild naturally from new messages.

## Syncing memories and config

Memories travel with the repo: the `memories/` folder is tracked in git, so
`git pull` / `git push` move it like any other code. Every deploy commits the
bot's latest notes automatically via `./scripts/commit-and-push.sh`; run it by
hand (or via cron) anytime you want the server's memories pushed sooner.

If you and the bot edit the same memory file, git merges by keeping both
sides' lines (`merge=union` in `.gitattributes`) — no conflict markers, no
manual resolution. Skim the merged file if you both touched the same lines.

For gitignored config that only lives on the server — such as `project.md` —
use the included sync script:

```bash
# Push updated project.md to the server
./scripts/sync-memories.sh push root@your-server-ip
```

After pushing `project.md`, restart for changes to take effect:

```bash
ssh root@your-server-ip 'cd ~/hamroh && docker compose restart'
```

## Common operations

```bash
# View live logs
ssh root@your-server-ip 'cd ~/hamroh && docker compose logs -f'

# Shell into the container
ssh root@your-server-ip 'cd ~/hamroh && docker compose exec hamroh bash'

# Restart without rebuilding
ssh root@your-server-ip 'cd ~/hamroh && docker compose restart'

# Stop the bot
ssh root@your-server-ip 'cd ~/hamroh && docker compose down'

# Check status
ssh root@your-server-ip 'cd ~/hamroh && docker compose ps'
```

## Troubleshooting

### Telegram conflict error

```
Conflict: terminated by other getUpdates request
```

Another instance is polling the same bot token. Make sure only one is
running — check both local (`pkill -f 'python -m hamroh'`) and
Docker (`docker compose down`).

### CC subprocess crashes (rc=1, empty stderr)

Common causes:

- **Stale session ID** — delete `data/session_id` and restart. This
  happens after renaming the project folder or moving to a new server.
- **MCP server not reachable** — the `--strict-mcp-config` flag makes
  Claude exit if any MCP server in the config fails to connect. Check
  that `uvx` and `npx` are available inside the container.

### Claude Code auth failed / `Not logged in · Please run /login`

The bot authenticates with the `CLAUDE_CODE_OAUTH_TOKEN` set in `.env`,
not with `claude login` or the host's stored credentials. If you see an
auth error, the token is missing or has been revoked. Generate a fresh
one and restart:

```bash
ssh root@your-server-ip
cd ~/hamroh
claude setup-token          # prints a new sk-ant-oat01-… token
vim .env                    # set CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
docker compose restart
```

This is the same procedure on Linux, macOS, and Windows. Because the
token comes from an env var, there is no macOS Keychain export and no
`~/.claude/.credentials.json` to keep in sync — the old platform-specific
workarounds no longer apply.
