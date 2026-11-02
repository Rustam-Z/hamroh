# Web channel

The same bot can talk on Telegram **and** on the web at the same time. It's
one agent with one memory and one Claude session. Your website or app sends
messages over HTTP and gets the bot's replies back as a live stream.

```
Telegram ─┐                                    ┌─▶ Telegram API
          ├─▶ engine ─▶ Claude ─▶ reply tools ─┤
Web API  ─┘                                    └─▶ web stream (SSE)
```

## Turn it on

In `.env`:

```bash
HAMROH_WEB_PORT=8787
HAMROH_WEB_TOKEN=some-long-random-string   # openssl rand -hex 32
# HAMROH_WEB_HOST=0.0.0.0                  # needed inside Docker
# HAMROH_WEB_CORS_ORIGINS=https://app.example.com
```

With Docker, also uncomment the `ports:` lines in `docker-compose.yml`.
Restart the bot. The log shows `web API listening on http://…`.

## Who is a web user

Your frontend decides. Every request carries a `user` string — your own id
for that person (an account id, an email, a session id). Each `user` gets
its own private chat with the bot, like a Telegram DM. The same `user`
always lands in the same chat, so history survives restarts.

Web users are never the bot **owner**, so owner-only actions stay with you
on Telegram. With `HAMROH_WEB_PRIVATE=true` every web user is a **private
chat**: own Claude session, own database, a narrow tool set — see
[private-chats.md](private-chats.md). `access.json` does not apply to the web: the token is the gate.
Anyone holding the token can talk to the bot as any `user`, so keep it on
your server.

## API

Every request needs `Authorization: Bearer <HAMROH_WEB_TOKEN>`.

### Send a message — `POST /api/messages`

```bash
curl -X POST http://127.0.0.1:8787/api/messages \
  -H "Authorization: Bearer $HAMROH_WEB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user": "alice", "name": "Alice", "text": "Hi!"}'
# 202 {"chat_id": 6288266166063199, "message_id": 1}
```

| field         | required | notes                                         |
|---------------|----------|-----------------------------------------------|
| `user`        | yes      | your id for the person, up to 200 chars        |
| `text`        | yes      | up to 4096 chars                               |
| `name`        | no       | name the bot sees, defaults to `user`          |
| `reply_to_id` | no       | message id in the same chat this replies to    |

The answer comes later on the event stream, not in this response. A user who
sends too fast gets `429` with `retry_after` seconds (same limit as Telegram
DMs, `HAMROH_RATE_LIMIT_PER_MIN`).

### Get replies — `GET /api/events?user=alice`

A [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events)
stream. Keep it open; each event is JSON:

```
event: typing
data: {"kind": "typing", "chat_id": 6288266166063199}

event: message
data: {"kind": "message", "chat_id": 6288266166063199, "message_id": 2, "text": "Hi Alice! **Welcome**", "reply_to_id": 1}

event: reaction
data: {"kind": "reaction", "chat_id": 6288266166063199, "message_id": 1, "emoji": "👍"}
```

- `message.text` is **Markdown** — render it with any Markdown library.
- `typing` repeats every few seconds while the bot works. Hide the indicator
  when a message arrives or after ~6 s without a new `typing`.
- The bot may send several messages for one question, or none.
- Lines starting with `:` are keep-alive pings; ignore them.

The stream only carries live events. After a reconnect, call `/api/history`
to catch up.

### Read history — `GET /api/history?user=alice&limit=50`

```json
{"chat_id": 6288266166063199,
 "messages": [{"message_id": 1, "direction": "in", "text": "Hi!", "timestamp": "2026-11-02 15:36:09"},
              {"message_id": 2, "direction": "out", "text": "Hi Alice!", "timestamp": "2026-11-02 15:36:14"}]}
```

Oldest first, `limit` up to 200.

## From a browser

The browser's built-in `EventSource` can't send an `Authorization` header,
and you shouldn't put the token in a web page anyway. Two good setups:

1. **Recommended:** your own backend calls hamroh and passes the stream
   through to the browser, after checking who the user is.
2. For a private, trusted page: set `HAMROH_WEB_CORS_ORIGINS` and read the
   stream with `fetch`:

```js
const res = await fetch(`${API}/api/events?user=alice`, {
  headers: { Authorization: `Bearer ${TOKEN}` },
});
const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
let buffer = "";
for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += value;
  const frames = buffer.split("\n\n");
  buffer = frames.pop();
  for (const frame of frames) {
    const data = frame.split("\n").find((l) => l.startsWith("data: "));
    if (data) handleEvent(JSON.parse(data.slice(6)));
  }
}
```

## What works in web chats

Text replies, reactions and the typing indicator. Telegram-only tools —
photos, files, polls, editing or deleting messages — return a clear error in
a web chat, so the bot knows to answer with text instead.

## How it works

- Web chat ids start at `2**52`, above any Telegram id, so the bot can tell
  them apart (`hamroh/web/chat_ids.py`).
- Inbound web messages are cleaned, saved and handed to the engine just
  like Telegram DMs (`hamroh/web/inbound.py`).
- The engine and tools send through one `ChannelBot`
  (`hamroh/web/channel_bot.py`): web chats go to the in-memory `WebHub`,
  everything else to Telegram unchanged.
- The bot still handles one turn at a time, across Telegram and web.
