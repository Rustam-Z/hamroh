# scripts/

Operator helpers — shell scripts run by hand on the host (not by the
bot). Python scripts the bot uses live in `hamroh/scripts/`.

| Script | What it does |
|---|---|
| `commit-memories.sh` | Commit and push the bot's memory files (`memories/`). Run on the server right before `git pull` so uncommitted memories never block a deploy. Safe to run anytime, including via cron. |
| `sync-memories.sh` | rsync gitignored server state — `prompts/project.md` and the SQLite DB — between local machine and remote server (pull / push). Memories are git-tracked, so they travel with `git pull` / `git push`, not this script. |
| `prune-backups.sh` | Trim `data/prompt_backups/` to the newest 50 backups. Safe to run anytime; safe to schedule via cron. |

Each script self-documents with `--help` (or its header comment).
Make executable once: `chmod +x scripts/*.sh`.
