---
name: Memories — the bot's memory store
description: How the bot's single memory folder works (frontmatter, layout, size cap, commit flow).
---

# Memories

This folder **is** the bot's memory — every note it writes itself, plus any
you add by hand.

It's **git-tracked**, so memories survive a volume loss or server rebuild and
carry full history. In Docker it's bind-mounted (`./memories:/app/memories`),
so runtime writes land in your checkout, ready to commit.

The bot has full access: `memory_list`, `memory_search`, `memory_read`,
`memory_write`, and `memory_append` all work here. Every memory is addressed
by its full path starting with `memories/` (e.g. `memories/notes/references.md`);
a bare `notes/references.md` is rejected.

Nothing here is loaded into the system prompt — memories are read on demand.

## How memory files look

Each memory is a `.md` file starting with a `name` / `description` frontmatter
block:

```markdown
---
name: <short human-friendly label>
description: <one-line summary used to find this memory without reading it>
---

<body — the actual remembered content>
```

`memory_list` shows the `description`, so the bot can judge a file's relevance
without reading it. Keep each file under **64 KiB**.

## Layout

```
memories/
├── README.md                          # this file
├── docs/{topic}-{YYYY-MM-DD}.md        # one-off reports / audits
├── notes/
│   ├── groups/{chat_id}.md             # per-group notes
│   ├── users/{telegram_user_id}.md     # per-user notes
│   └── {topic}.md                      # cross-session reference notes
└── self/
    └── learnings.md                    # mistakes and lessons to carry forward
```

## Committing memories

The folder is tracked in git, so commit memories like any other file:

```bash
git add memories/
git commit -m "memories: <what changed>"
git push
```

On the server this happens automatically: every deploy runs
`./scripts/commit-and-push.sh`, which commits and pushes whatever the bot
wrote since the last deploy. If a hand edit and a bot edit ever land in the
same file, git keeps both sides' lines (`merge=union` in `.gitattributes`)
instead of raising a conflict.
