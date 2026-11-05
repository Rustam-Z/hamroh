"""Private chats: own session, own database, narrow tools, identity from the server.

A private chat (a web user in private mode, or a Telegram user an extension
vouches for) must never share a turn, a Claude session, a database or a tool
with anyone else. These tests pin each of those walls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from hamroh.cc_worker import TurnResult
from hamroh.cc_worker.spec import CcSpawnSpec, build_argv
from hamroh.config import Config
from hamroh.db.database import Database
from hamroh.engine import Engine, EngineOptions
from hamroh.engine.batching import split_batch, split_by_key, split_keyed
from hamroh.mcp_server import _run_tool
from hamroh.models import ChatMessage, ControlAction
from hamroh.privacy.sessions import SHARED, SessionStore, session_key
from hamroh.privacy.switcher import SessionSetup, SessionSwitcher
from hamroh.privacy.turn import TurnContext, call_refusal, reply_refusal
from hamroh.tools.base import BaseTool, ToolContext, ToolResult
from hamroh.web.chat_ids import web_chat_id
from hamroh.web.hub import WebHub
from hamroh.web.inbound import WebInbound, WebInboundDeps, WebMessageIn
from tests.unit.test_dispatcher_persistence_gating import (
    STRANGER,
    _cfg,
    _dispatcher,
    _make_update,
    _message_count,
)
from tests.unit.test_engine_debouncer import FakeWorker

_CFG = Config.for_test(Path("/tmp"))


def _msg(
    chat_id: int, mid: int, *, private: bool = False, user: str | None = None
) -> ChatMessage:
    return ChatMessage(
        chat_id=chat_id,
        message_id=mid,
        user_id=chat_id,
        username=user,
        first_name="X",
        direction="in",
        timestamp=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
        text=f"m{mid}",
        private=private,
    )


def _spec(tmp_path: Path, **changes: Any) -> CcSpawnSpec:
    system = tmp_path / "system.md"
    system.write_text("System prompt.")
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}))
    schema = tmp_path / "schema.json"
    schema.write_text('{"type": "object"}')
    base = CcSpawnSpec(
        binary="claude",
        model="m",
        system_prompt_path=system,
        mcp_config_path=mcp,
        json_schema_path=schema,
    )
    return dataclasses.replace(base, **changes)


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# ── the private spawn profile ────────────────────────────────────────────────


def test_private_session_has_no_web_and_only_named_tools(tmp_path: Path) -> None:
    # Given a private spec granting one hamroh tool and denying another
    prompt = tmp_path / "private.md"
    prompt.write_text("You answer questions about the centre.")
    spec = _spec(
        tmp_path,
        private=True,
        hamroh_tool_names=("telegram_send_message",),
        denied_hamroh_tool_names=("memory_read", "database_query"),
        memory_index="# Memory\nsecret notes",
        private_prompt_path=prompt,
    )
    # When the argv is built
    argv = build_argv(spec)
    # Then web built-ins are gone, tools are granted by exact name, the rest denied
    tools = _flag(argv, "--tools").split(",")
    assert "WebFetch" not in tools and "WebSearch" not in tools, (
        f"web built-ins reachable: {tools}"
    )
    allowed = _flag(argv, "--allowedTools").split(",")
    assert allowed == ["mcp__hamroh__telegram_send_message"], (
        f"allowed must be exact names only: {allowed}"
    )
    denied = _flag(argv, "--disallowedTools").split(",")
    for name in (
        "WebFetch",
        "WebSearch",
        "Agent",
        "mcp__hamroh__memory_read",
        "mcp__hamroh__database_query",
        "Bash",
    ):
        assert name in denied, f"{name} must be denied in a private session: {denied}"
    prompt_text = _flag(argv, "--system-prompt")
    assert "# Private chat" in prompt_text, (
        "private rules missing from the system prompt"
    )
    assert "You answer questions about the centre." in prompt_text, (
        "operator's private prompt missing"
    )


def test_shared_session_is_unchanged(tmp_path: Path) -> None:
    # Given a normal (shared) spec, When the argv is built, Then the web tools stay
    argv = build_argv(_spec(tmp_path, hamroh_tool_names=("time_now",)))
    assert "WebFetch" in _flag(argv, "--tools"), "shared sessions keep web access"
    assert "mcp__hamroh" in _flag(argv, "--allowedTools").split(","), (
        "shared sessions keep the hamroh wildcard"
    )
    assert "# Private chat" not in _flag(argv, "--system-prompt"), (
        "shared prompt must not get private rules"
    )


# ── who may call what ────────────────────────────────────────────────────────


def test_audience_rules() -> None:
    # Given a private turn and a shared turn
    private, shared = TurnContext(), TurnContext()
    private.enter_private(chat_id=7, user_id=7, web_user=None)
    # Then shared tools are refused in private, private tools in shared, both runs anywhere
    assert call_refusal("shared", private) is not None, (
        "a shared tool ran in a private turn"
    )
    assert call_refusal("private", shared) is not None, (
        "a private tool ran in a shared turn"
    )
    assert (
        call_refusal("both", private) is None and call_refusal("both", shared) is None
    ), "'both' must run anywhere"
    assert reply_refusal(private, 8) is not None, "a private turn reached another chat"
    assert reply_refusal(private, 7) is None and reply_refusal(shared, 8) is None, (
        "legit replies refused"
    )


class _Args(BaseModel):
    chat_id: int = 0


class _SharedTool(BaseTool[_Args]):
    name = "shared_thing"
    description = "test"
    args_model: ClassVar[type[BaseModel]] = _Args

    async def run(self, args: _Args) -> ToolResult:
        return ToolResult(content="ran")


class _ReplyTool(_SharedTool):
    name = "reply_thing"
    audience = "both"


@pytest.mark.asyncio
async def test_mcp_gate_refuses_calls_across_the_wall() -> None:
    # Given a private turn for chat 7
    ctx = ToolContext()
    ctx.turn.enter_private(chat_id=7, user_id=7, web_user=None)
    # When a shared tool is called, or a 'both' tool targets another chat
    shared_result, _ = await _run_tool(_SharedTool(ctx), {})
    other_chat, _ = await _run_tool(_ReplyTool(ctx), {"chat_id": 8})
    own_chat, _ = await _run_tool(_ReplyTool(ctx), {"chat_id": 7})
    # Then the first two are refused in our process and the last one runs
    assert shared_result.is_error and "private chat" in shared_result.content, (
        shared_result.content
    )
    assert other_chat.is_error, "a reply escaped to another chat"
    assert not own_chat.is_error and own_chat.content == "ran", own_chat.content


# ── batching and sessions ────────────────────────────────────────────────────


def test_a_private_chat_never_shares_a_turn() -> None:
    # Given shared messages from two chats and a private chat's messages, interleaved
    pending = [
        _msg(1, 1),
        _msg(9, 2, private=True),
        _msg(2, 3),
        _msg(9, 4, private=True),
    ]
    # When the next batch is taken
    batch, rest = split_batch(pending)
    # Then the shared chats go together and the private chat waits for its own turn
    assert [m.message_id for m in batch] == [1, 3], f"shared batch wrong: {batch}"
    assert [m.message_id for m in rest] == [2, 4], (
        f"private messages lost or mixed: {rest}"
    )
    same, other = split_by_key(rest, "chat:9")
    assert len(same) == 2 and other == [], "private chat's messages must stay together"
    mine, left = split_keyed([("shared", "a"), ("chat:9", "b")], "chat:9")
    assert mine == ["b"] and left == [("shared", "a")], (
        "callbacks must follow their own session"
    )


def test_session_store_round_trip(tmp_path: Path) -> None:
    # Given a store with one chat's session
    path = tmp_path / "sessions.json"
    SessionStore(path).put("chat:9", "s-9")
    # Then a new store reads it back, and a corrupt file means no sessions
    assert SessionStore(path).get("chat:9") == "s-9", "session id not persisted"
    path.write_text("{not json")
    assert SessionStore(path).get("chat:9") is None, "corrupt file must not crash"


class _SwitchWorker:
    def __init__(self, spec: CcSpawnSpec) -> None:
        self.spec = spec
        self.session_id: str | None = spec.session_id
        self.switched: list[CcSpawnSpec] = []

    async def switch_session(self, spec: CcSpawnSpec) -> None:
        self.spec, self.session_id = spec, spec.session_id
        self.switched.append(spec)


@pytest.mark.asyncio
async def test_switcher_resumes_each_chats_own_session(tmp_path: Path) -> None:
    # Given a shared session "s-shared" and a stored session for web chat 9
    shared = _spec(tmp_path, session_id="s-shared")
    private = _spec(tmp_path, private=True)
    worker = _SwitchWorker(shared)
    store = SessionStore(tmp_path / "sessions.json")
    chat = web_chat_id("mkh:alice")
    store.put(f"chat:{chat}", "s-alice")
    turn = TurnContext()
    switcher = SessionSwitcher(worker, SessionSetup(shared, private, store, turn))
    # When alice's private message comes, then a shared one
    await switcher.enter([_msg(chat, 1, private=True, user="mkh:alice")])
    went_private = worker.switched[-1]
    alice_turn = (turn.private, turn.chat_id, turn.web_user)
    await switcher.enter([_msg(1, 2)])
    # Then her own session and the private profile were used, and the shared one came back
    assert went_private.private and went_private.session_id == "s-alice", went_private
    assert alice_turn == (True, chat, "mkh:alice"), (
        f"turn not bound to alice: {alice_turn}"
    )
    assert (
        worker.switched[-1].session_id == "s-shared" and not worker.switched[-1].private
    ), "shared session lost"
    assert turn.private is False and turn.chat_id is None, (
        "shared turn kept a private identity"
    )
    assert switcher.current == SHARED, switcher.current


@pytest.mark.asyncio
async def test_engine_runs_private_and_shared_chats_in_separate_turns(
    tmp_path: Path,
) -> None:
    # Given an engine with a switcher, and a shared and a private message arriving together
    worker = FakeWorker()
    sw = _SwitchWorker(_spec(tmp_path))
    setup = SessionSetup(
        sw.spec,
        _spec(tmp_path, private=True),
        SessionStore(tmp_path / "s.json"),
        TurnContext(),
    )
    switcher = SessionSwitcher(sw, setup)
    eng = Engine(worker, _CFG, EngineOptions(debounce_ms=20, switcher=switcher))
    await eng.start()
    try:
        await eng.submit(_msg(1, 1))
        await eng.submit(_msg(9, 2, private=True))
        await asyncio.sleep(0.1)
        # Then the first turn carries only the shared message
        assert (
            len(worker.sent) == 1
            and "m1" in worker.sent[0]
            and "m2" not in worker.sent[0]
        ), worker.sent
        # When more private text arrives mid-turn, Then it is not injected into the shared turn
        await eng.submit(_msg(9, 3, private=True))
        await asyncio.sleep(0.05)
        assert worker.injected == [], (
            f"private text injected into a shared turn: {worker.injected}"
        )
        # When the shared turn ends, Then the private chat gets its own turn, in a private session
        worker.feed_result(
            TurnResult(
                text_blocks=[],
                control=ControlAction(action="stop", reason="ok"),
                user_visible_action=True,
                dropped_text=False,
            )
        )
        await asyncio.sleep(0.1)
        assert (
            len(worker.sent) == 2 and "m2" in worker.sent[1] and "m3" in worker.sent[1]
        ), worker.sent
        assert sw.switched and sw.switched[-1].private, (
            "the private turn ran outside a private session"
        )
    finally:
        await eng.stop()


# ── entrances: web and Telegram ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_private_web_message_carries_the_web_user(tmp_path: Path) -> None:
    # Given the web channel in private mode
    cfg = Config.for_test(tmp_path)
    cfg.ensure_dirs()
    db = await Database.open(cfg.private_db_path)
    engine = AsyncMock()
    engine.prime_typing = lambda _chat: None
    inbound = WebInbound(
        WebInboundDeps(
            db=db, engine=engine, hub=WebHub(db), chat_titles={}, private=True
        )
    )
    # When a web user writes
    msg = await inbound.accept(
        WebMessageIn(user="mkh:alice", text="Сколько должников?")
    )
    await db.close()
    # Then the message is private and names the person for the tools
    assert msg.private and msg.username == "mkh:alice", msg


@pytest.mark.asyncio
async def test_vouched_telegram_user_skips_access_and_lands_in_private_db(
    tmp_path: Path,
) -> None:
    # Given an owner-only bot (strangers blocked) and an extension vouching for STRANGER
    cfg = _cfg(tmp_path)
    shared_db = await Database.open(cfg.db_path)
    private_db = await Database.open(cfg.private_db_path)
    dispatcher = _dispatcher(cfg, shared_db)
    dispatcher.private_db = private_db

    async def vouch(user_id: int) -> bool:
        return user_id == STRANGER

    dispatcher.set_private_user_check(vouch)
    # When STRANGER writes in a DM
    await dispatcher._on_message(
        _make_update(user_id=STRANGER, chat_id=STRANGER, chat_type="private"), None
    )  # type: ignore[arg-type]
    # Then the message reaches the engine as private and only the private DB has it
    submitted = dispatcher.engine.submit.await_args.args[0]  # type: ignore[union-attr]
    shared_count, private_count = (
        await _message_count(shared_db),
        await _message_count(private_db),
    )
    await shared_db.close()
    await private_db.close()
    assert submitted.private, "vouched user's DM was not marked private"
    assert (shared_count, private_count) == (0, 1), (
        f"row in the wrong DB: shared={shared_count} private={private_count}"
    )
    assert session_key(submitted) == f"chat:{STRANGER}", (
        "private DM must get its own session"
    )
