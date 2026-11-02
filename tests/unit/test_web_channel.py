"""Web channel: chat ids, the channel bot router, and the HTTP API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hamroh.config import Config, WebConfig
from hamroh.db.database import Database
from hamroh.models import ChatMessage
from hamroh.rate_limiter import RateLimitConfig, RateLimiter
from hamroh.tools.base import ToolContext
from hamroh.tools.telegram.telegram_send_message import (
    SendMessageArgs,
    TelegramSendMessageTool,
)
from hamroh.web.channel_bot import ChannelBot, WebChatUnsupported
from hamroh.web.chat_ids import WEB_CHAT_ID_BASE, is_web_chat, web_chat_id
from hamroh.web.hub import WebEvent, WebHub
from hamroh.web.inbound import WebInbound, WebInboundDeps
from hamroh.web.server import WebApi, _event_stream, build_app

TOKEN = "secret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ALICE_CHAT = web_chat_id("alice")


class FakeEngine:
    """Records what the web inbound path hands to the engine."""

    def __init__(self) -> None:
        self.submitted: list[ChatMessage] = []
        self.primed: list[int] = []

    async def submit(self, msg: ChatMessage) -> None:
        self.submitted.append(msg)

    def prime_typing(self, chat_id: int) -> None:
        self.primed.append(chat_id)


@pytest.fixture()
async def db(tmp_path: Path):
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    database = await Database.open(cfg.db_path)
    yield database
    await database.close()


def _client(db: Database, engine: FakeEngine, **deps_kw) -> httpx.AsyncClient:
    """HTTP client wired to an in-process web API app."""
    hub = WebHub(db)
    deps = WebInboundDeps(db=db, engine=engine, hub=hub, chat_titles={}, **deps_kw)
    api = WebApi(inbound=WebInbound(deps), hub=hub, db=db, token=TOKEN)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(api)), base_url="http://test"
    )


# ---------------------------------------------------------------- chat ids


def test_web_chat_id_is_stable_and_in_the_web_range() -> None:
    # Given / When: the same frontend user key is mapped twice
    first, second = web_chat_id("alice"), web_chat_id("alice")

    # Then: it's the same id, in the reserved web range, safe for JavaScript
    assert first == second, "same user key must map to the same chat"
    assert WEB_CHAT_ID_BASE <= first < 2**53, f"id {first} outside web range"
    assert web_chat_id("bob") != first, "different users must get different chats"


@pytest.mark.parametrize("telegram_id", [123456789, -1001234567890, 2**52 - 1])
def test_telegram_ids_are_not_web_chats(telegram_id: int) -> None:
    assert not is_web_chat(telegram_id), f"{telegram_id} wrongly treated as web"


# ---------------------------------------------------------------- channel bot


async def test_send_to_web_chat_publishes_event_instead_of_telegram() -> None:
    # Given: a channel bot with a client watching Alice's chat
    telegram = MagicMock()
    telegram.send_message = AsyncMock()
    bot = ChannelBot(telegram, WebHub())
    queue = bot.hub.subscribe(ALICE_CHAT)

    # When: the bot replies in Alice's web chat
    sent = await bot.send_message(
        chat_id=ALICE_CHAT, text="**hi**", reply_to_message_id=1, parse_mode="HTML"
    )

    # Then: the browser gets the message and Telegram is never called
    event = queue.get_nowait()
    assert event == WebEvent("message", ALICE_CHAT, sent.message_id, "**hi**", 1)
    telegram.send_message.assert_not_called()


async def test_send_to_telegram_chat_goes_to_telegram() -> None:
    telegram = MagicMock()
    telegram.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot = ChannelBot(telegram, WebHub())

    sent = await bot.send_message(chat_id=42, text="hi", parse_mode="HTML")

    assert sent.message_id == 7, "Telegram's message must be returned as-is"
    telegram.send_message.assert_awaited_once_with(
        chat_id=42, text="hi", parse_mode="HTML"
    )


async def test_typing_and_reaction_reach_web_clients() -> None:
    bot = ChannelBot(MagicMock(), WebHub())
    queue = bot.hub.subscribe(ALICE_CHAT)

    await bot.send_chat_action(chat_id=ALICE_CHAT, action="typing")
    await bot.set_message_reaction(
        chat_id=ALICE_CHAT, message_id=3, reaction=[SimpleNamespace(emoji="👍")]
    )

    assert queue.get_nowait().kind == "typing", "typing event expected first"
    reaction = queue.get_nowait()
    assert (reaction.kind, reaction.message_id, reaction.emoji) == (
        "reaction",
        3,
        "👍",
    ), f"unexpected reaction event {reaction}"


async def test_telegram_only_methods_fail_clearly_in_web_chats() -> None:
    telegram = MagicMock()
    telegram.send_photo = AsyncMock(return_value="ok")
    bot = ChannelBot(telegram, WebHub())

    with pytest.raises(WebChatUnsupported, match="send_photo"):
        await bot.send_photo(chat_id=ALICE_CHAT, photo=b"")
    assert await bot.send_photo(chat_id=42, photo=b"") == "ok", (
        "Telegram chats must still reach Telegram"
    )
    telegram.send_photo.assert_awaited_once()


async def test_send_tool_gives_web_chats_raw_markdown() -> None:
    # Given: the send tool wired to a channel bot
    bot = ChannelBot(MagicMock(), WebHub())
    queue = bot.hub.subscribe(ALICE_CHAT)
    tool = TelegramSendMessageTool(ToolContext(bot=bot))

    # When: the model sends Markdown to a web chat
    result = await tool.run(SendMessageArgs(chat_id=ALICE_CHAT, text="**bold**"))

    # Then: the browser gets Markdown, not Telegram HTML
    assert not result.is_error, result.content
    assert queue.get_nowait().text == "**bold**", "web text must stay Markdown"


# ---------------------------------------------------------------- hub


async def test_message_ids_continue_after_stored_messages(db: Database) -> None:
    # Given: Alice's chat already has message 5 saved
    await db.execute(
        "INSERT INTO messages (chat_id, message_id, user_id, direction, timestamp, "
        "text) VALUES (?, 5, 1, 'in', '2026-01-01 00:00:00', 'x')",
        (ALICE_CHAT,),
    )

    # When / Then: a fresh hub (e.g. after restart) continues from 6
    hub = WebHub(db)
    assert await hub.next_message_id(ALICE_CHAT) == 6, "must not reuse ids"
    assert await hub.next_message_id(ALICE_CHAT) == 7, "ids must keep counting"


# ---------------------------------------------------------------- HTTP API


async def test_requests_without_token_are_rejected(db: Database) -> None:
    async with _client(db, FakeEngine()) as client:
        no_auth = await client.get("/api/history", params={"user": "alice"})
        bad_auth = await client.get(
            "/api/history",
            params={"user": "alice"},
            headers={"Authorization": "Bearer nope"},
        )

    assert no_auth.status_code == 401, "missing token must be rejected"
    assert bad_auth.status_code == 401, "wrong token must be rejected"


async def test_post_message_reaches_engine_and_history(db: Database) -> None:
    # Given: the web API with a fake engine
    engine = FakeEngine()
    async with _client(db, engine) as client:
        # When: the frontend posts a message for Alice
        posted = await client.post(
            "/api/messages",
            json={"user": "alice", "name": "Alice", "text": "hello"},
            headers=AUTH,
        )
        history = await client.get(
            "/api/history", params={"user": "alice"}, headers=AUTH
        )

    # Then: it is accepted, handed to the engine, and saved
    assert posted.status_code == 202, posted.text
    assert posted.json() == {"chat_id": ALICE_CHAT, "message_id": 1}
    msg = engine.submitted[0]
    assert (msg.chat_id, msg.user_id, msg.first_name, msg.text) == (
        ALICE_CHAT,
        ALICE_CHAT,
        "Alice",
        "hello",
    ), f"unexpected engine message {msg}"
    assert engine.primed == [ALICE_CHAT], "typing indicator must be primed"
    texts = [m["text"] for m in history.json()["messages"]]
    assert texts == ["hello"], f"history should hold the message, got {texts}"


async def test_bad_message_body_is_rejected(db: Database) -> None:
    engine = FakeEngine()
    async with _client(db, engine) as client:
        response = await client.post(
            "/api/messages", json={"user": "alice", "text": ""}, headers=AUTH
        )

    assert response.status_code == 400, response.text
    assert engine.submitted == [], "invalid messages must not reach the engine"


async def test_rate_limited_user_gets_429(db: Database) -> None:
    limiter = RateLimiter(db, RateLimitConfig(limit=1))
    async with _client(db, FakeEngine(), rate_limiter=limiter) as client:
        body = {"user": "alice", "text": "hi"}
        first = await client.post("/api/messages", json=body, headers=AUTH)
        second = await client.post("/api/messages", json=body, headers=AUTH)

    assert first.status_code == 202, first.text
    assert second.status_code == 429, "second message within the limit window"


async def test_event_stream_sends_published_events() -> None:
    # Given: an open event stream for Alice
    hub = WebHub()
    stream = _event_stream(hub, ALICE_CHAT)
    assert (await anext(stream)).startswith(": connected"), "greeting first"

    # When: the bot replies
    next_frame = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    hub.publish(WebEvent("message", ALICE_CHAT, 2, "hi"))

    # Then: the client gets an SSE frame, and closing unsubscribes it
    frame = await next_frame
    assert frame.startswith("event: message\ndata: "), frame
    assert '"text": "hi"' in frame, frame
    await stream.aclose()
    assert not hub._subscribers, "closed stream must unsubscribe"


def test_web_channel_is_off_by_default() -> None:
    assert not WebConfig().enabled, "web API must be opt-in"
