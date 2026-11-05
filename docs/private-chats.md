# Private chats

Some people must talk to the bot alone: what they ask, and what the bot reads
for them, is for nobody else. The staff of a business asking about its
customers is the usual case. Hamroh calls these **private chats**.

A private chat is:

- **a web user**, when the web channel runs with `HAMROH_WEB_PRIVATE=true`;
- **a Telegram DM from a user an extension vouches for** — for example, staff
  who linked their Telegram account to the business's system (see
  "Extension hooks" below).

Everything else — groups, the owner's own DMs, people in `access.json` — works
exactly as before, in the one shared session.

## What a private chat gets

| | Shared chats | A private chat |
|---|---|---|
| Claude session | one, shared by all | its own, resumed every time (`data/sessions.json`) |
| Turn | may mix several chats | only this chat's messages, never mixed |
| Messages and tool log | `data/hamroh.db` | `data/private.db` — no tool can read it |
| Tools | all enabled ones | only tools marked for private chats, plus sending a reply, reacting and the clock |
| Web search / fetch, browser | yes | no — a prompt hidden in data could send that data to any URL |
| Memory notes, skills, database queries | yes | no — they are shared with every chat |
| Replies | any chat | only this chat |

Two locks keep this true. The private session is started without the other
tools at all (`hamroh/cc_worker/private_profile.py`), and the MCP server checks
every call again in our own process (`hamroh/privacy/turn.py`).

**Who is asking** is known to the server, not told by the model: before each
turn the engine fills `ToolContext.turn` (chat, user, web user id). A tool that
acts for a person reads it from there.

Switching between sessions restarts the `claude` process with the other
session (a few seconds). One turn still runs at a time.

## Settings

```bash
HAMROH_WEB_PRIVATE=true                 # every web user is a private chat
HAMROH_PRIVATE_PROMPT=ext/private.md    # extra system-prompt text for private chats
```

## Extension hooks

An extension (see [tools.md](tools.md#extensions--your-own-tools-and-commands-outside-the-framework))
uses three things:

```python
# ext/__init__.py
async def is_staff(telegram_user_id: int) -> bool:
    ...  # ask your system; cache the answer


async def link(update, context) -> None:
    ...  # /start <code>: link this Telegram user to an account


def register(dispatcher) -> None:
    dispatcher.set_private_user_check(is_staff)   # their DMs are private chats
    dispatcher.add_public_command("start", "link your account", link)
```

```python
# ext/tools/orders.py — a tool only private chats may call
class OrdersTool(BaseTool[OrdersArgs]):
    name = "orders"
    audience = "private"   # "shared" (default) | "private" | "both"

    async def run(self, args: OrdersArgs) -> ToolResult:
        turn = self.ctx.turn          # who is asking: set by the server
        person = turn.web_user or turn.user_id
        ...
```

- `set_private_user_check(fn)` — `await fn(user_id)` decides whether a DM is
  private. A private user skips `access.json`: the extension vouches for them.
- `add_public_command(name, description, handler)` — a DM `/command` anyone may
  send before they have access, never reaching the engine.
- `audience` on a tool — where it may run.
