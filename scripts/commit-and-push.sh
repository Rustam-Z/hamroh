#!/usr/bin/env bash
#
# Commit and push all local changes in the server checkout, then it is
# safe to `git pull` and redeploy.
#
# The bot writes its memories straight into the server's git checkout but
# never commits them. Uncommitted changes block `git pull` when the same
# file changed upstream, which breaks deploys. Committing everything first
# keeps the checkout clean:
#
#   ./scripts/commit-and-push.sh && git pull --rebase && docker compose up -d --build
#
# `git add -A` respects .gitignore, so operator config (.env, access.json,
# plugins.json, default-reminders.json, prompts/project.md) and runtime
# data (data/) are never committed — only tracked files like memories/.
#
# When there is something to commit, the script rebases on top of the
# remote (the server may be behind — a deploy is usually triggered by a
# push) and pushes. `[skip ci]` keeps the push from triggering another
# deploy. Safe to run anytime, including via cron.

set -euo pipefail

cd "$(dirname "$0")/.."

# Keep this checkout non-interactive. The server and the laptop both push, so
# the branches diverge routinely; without pull.rebase git stops and asks how to
# reconcile, which fails any unattended run (make update, cron).
git config pull.rebase true
git config rebase.autoStash true

# A rebase interrupted by an earlier failed run leaves .git/rebase-merge behind
# and makes every later git command exit 128. Clear it before doing anything.
git rebase --abort 2>/dev/null || true

git add -A

if git diff --cached --quiet; then
    echo "No local changes to commit."
    exit 0
fi

git -c user.name="hamroh" -c user.email="hamroh@localhost" \
    commit -m "sync server state [skip ci]"
git pull --rebase
git push

echo "Local changes committed and pushed."
