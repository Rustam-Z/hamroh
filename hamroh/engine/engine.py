"""The engine — debouncer, queue, inject channel, control loop.

This is the heart of hamroh. The dispatcher calls :meth:`Engine.submit`
for every allowed inbound message. The engine batches them with a 1-second
debounce, formats them as XML the same way Claudir does, and ships them to
the CC worker. While CC is processing a turn, additional messages are
shovelled through the inject channel so they land in the same turn rather
than triggering a new one.

The engine itself owns the asyncio coordination — debounce timer, batch
buffer, processing flag, control loop. It does *not* own the CC worker's
lifecycle (the run loop in ``__main__`` does), nor the database
(persistence happens in the dispatcher before the engine ever sees a
message).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from ..cc_worker.cc_failure_classifier import (
    CcFailureClassification,
    classify_cc_failure,
)
from ..config import Config
from ..db.messages import mark_messages_consumed, mark_messages_processed
from ..utils.formatting import chunk_text
from ..models import ChatMessage
from .format import format_messages_with_context
from .restore import build_restored_context
from .typing_indicator import TypingAction, TypingIndicatorMixin, TypingState

#: Async callable shape: ``await error_notify(chat_id, text, reply_to_message_id)``
#: sends a message directly via the bot, bypassing the MCP layer (which is
#: dead when we need this). Engine doesn't import telegram. The third
#: argument is optional (implementations default it to ``None``) — pass a
#: message id to thread the send as a reply, or ``None`` for a plain send.
ErrorNotify = Callable[[int, str, "int | None"], Awaitable[None]]

#: A per-turn success/failure hook: ``await hook()``. Aliased so signatures
#: stay readable (and so naive comma-counting param linters don't trip on the
#: ``Callable[[], ...]`` brackets).
AsyncHook = Callable[[], Awaitable[None]]

if TYPE_CHECKING:  # pragma: no cover
    from ..cc_worker import CcWorker, TurnResult
    from ..db.database import Database

log = logging.getLogger("hamroh.engine")

#: Cap on consecutive non-terminal ``heartbeat`` continuations in one
#: logical turn. Past this we finalize the turn like a clean stop, so a
#: model that always returns ``heartbeat`` can't spin the control loop
#: forever.
MAX_HEARTBEAT_CONTINUATIONS = 10

#: Nudge sent to resume CC after a ``heartbeat`` when no new user messages
#: are waiting, so the model picks its own task back up. The model has
#: already posted its status via ``telegram_send_message``; this only
#: re-engages the session.
HEARTBEAT_CONTINUE_NUDGE = (
    "<system>Continue the task you just reported on. "
    'Return action "stop" only once it is actually done.</system>'
)

#: Corrective nudge sent once when a turn that consumed a human message (DM
#: or group) ended ``stop`` without delivering anything (no
#: ``telegram_send_message`` call, no text block). ``stop`` promises a reply
#: was sent, so an empty one is almost always the model ending the turn
#: prematurely — its intent narrated only in the control ``reason``, which
#: the user never sees. Deliberate silence has its own action (``skip``) and
#: never triggers this; the nudge offers that exit, so it can't badger the
#: model into replying to group chatter. Bounded to one retry via
#: ``TurnState.silent_stop_retried`` so a persistently silent model can't loop.
SILENT_STOP_NUDGE = (
    '<system>You returned action "stop" but sent nothing — no '
    "telegram_send_message or telegram_reply_to_message call. The reason field "
    "is internal and never shown to the user. If the user is owed a reply, "
    "send it now via telegram_send_message or telegram_reply_to_message. If "
    "staying silent was intentional (e.g. the user asked for no reply), return "
    'action "skip" instead.</system>'
)


def _batch_awaits_reply(batch: list[ChatMessage]) -> bool:
    """True when the batch contains any human message — DM or group. Only
    the model can tell whether such a message deserved a reply, so a silent
    ``stop`` on one gets the corrective nudge and the model resolves it
    (reply, or confirm silence with ``skip``). Synthetic reminders
    (``message_id == 0``) never count."""
    return any(m.message_id > 0 for m in batch)


def _trim_snippet(text: str, limit: int = 400) -> str:
    """Trim diagnostic text to fit comfortably in a Telegram message."""
    snippet = text.strip()
    if len(snippet) > limit:
        snippet = snippet[:limit].rstrip() + "…"
    return snippet


def _classified_failure_message(classification: CcFailureClassification) -> str:
    """User-facing message when dropped text is actually a known failure
    (bad model name, quota, …), with the matched diagnostic snippet so the
    user sees what went wrong rather than a generic apology."""
    msg = classification.user_message
    if classification.matched_source:
        msg = f"{msg}\n\nDetails:\n{classification.matched_source}"
    return msg


@dataclass
class TurnState:
    """Per-turn user-facing state. Cleared on each new turn in ``_kick``,
    consulted by the dropped-text handler and crash-notification path."""

    #: Chats from the most recent batch waiting on a reply. Synthetic
    #: reminders (``message_id == 0``) are excluded so reminder-only
    #: turns produce no turn-start typing indicator.
    active_chats: set[int] = field(default_factory=set)
    #: Per active chat, the most recent human message in the batch. The
    #: error-notify path references these so a failed turn points the owner at
    #: the message that kicked it — a deep link for supergroups, or the sender
    #: and quoted text for DMs — instead of floating free.
    reply_targets: dict[int, ChatMessage] = field(default_factory=dict)
    #: ``time.monotonic()`` when the current turn started in ``_kick``.
    #: Read by :attr:`Engine.turn_elapsed_s` for the /health readout.
    started_monotonic: float = 0.0
    #: ``(chat_id, message_id)`` keys consumed by this turn (kick plus
    #: mid-turn injects). Committed via ``mark_messages_processed`` only
    #: when the turn finishes cleanly — failed/aborted turns leave the
    #: rows untrusted, which bars them from restored-context digests.
    consumed_keys: list[tuple[int, int]] = field(default_factory=list)
    #: True when this turn's opening send carried a ``<restored_context>``
    #: digest. One-shot poison guard: if such a turn fails with an
    #: ``api_error``, the next reset must NOT rebuild the digest.
    had_restored_context: bool = False
    #: How many times this logical turn has continued via a non-terminal
    #: ``heartbeat`` action. Reset on each ``_kick``; capped in
    #: ``_handle_turn_result`` so a model that always returns ``heartbeat``
    #: can't spin the control loop forever.
    heartbeat_count: int = 0
    #: True once this turn has been re-engaged for a silent ``stop`` (ended
    #: with no delivered message and no text while a DM user waited). Reset on
    #: each ``_kick``; bounds the corrective nudge to a single retry so a
    #: persistently silent model can't loop.
    silent_stop_retried: bool = False
    #: True when this turn consumed any human message (DM or group). Set
    #: from the kick batch and OR-ed in by mid-turn injects; gates the
    #: silent-stop nudge — reminder-only turns never nudge.
    awaiting_reply: bool = False


@dataclass
class TurnCallbacks:
    """A submit's deferred hooks, kept as a pair so success and failure
    can't desync.

    ``on_success`` runs after the turn that consumed the message ends with
    a result from CC; ``on_failure`` runs when that turn is discarded
    mid-flight (subprocess crash, owner session reset). The reminder loop
    hangs advance/close on success and revert on failure off these — see
    #22 and the claim model in ``db/reminders.py``.
    """

    on_success: AsyncHook
    on_failure: AsyncHook | None = None


@dataclass(frozen=True)
class EngineOptions:
    """Optional wiring + tuning for :class:`Engine`, all with safe defaults so
    a test can build one from just a worker and config.

    ``typing_action`` shows the "typing…" indicator (wired by ``__main__`` to
    ``bot.send_chat_action``); ``error_notify`` sends a message straight via the
    bot when the MCP path is dead.
    """

    debounce_ms: int = 1000
    db: "Database | None" = None
    typing_action: TypingAction | None = None
    error_notify: ErrorNotify | None = None


class Engine(TypingIndicatorMixin):
    def __init__(
        self,
        worker: "CcWorker",
        config: Config,
        options: EngineOptions = EngineOptions(),
    ) -> None:
        self._worker = worker
        self._debounce = options.debounce_ms / 1000.0
        self._db = options.db
        self._typing_action = options.typing_action
        self._error_notify = options.error_notify
        #: Per-turn user state — see :class:`TurnState`.
        self._turn = TurnState()
        #: ``<restored_context>`` digest set via :meth:`stash_restore_context`
        #: on the three session-reset paths; consumed once by the next
        #: ``_kick`` so the fresh session's first turn carries it.
        self._restore_context: str | None = None
        #: Typing-indicator state — see :class:`TypingState`.
        self._typing = TypingState()
        self._pending: list[ChatMessage] = []
        #: Per-submit success+failure hooks queued alongside ``_pending``.
        #: Transferred to ``_turn_callbacks`` when the buffer drains into
        #: a turn (``_kick`` / ``_maybe_inject``). The reminder loop hangs
        #: advance/close (success) and revert (failure) off these so a
        #: subprocess crash mid-turn doesn't lose the reminder — see #22.
        self._pending_callbacks: list[TurnCallbacks] = []
        #: Hooks bound to the in-flight turn. ``on_success`` fires in
        #: ``_fire_turn_callbacks`` once the turn ends cleanly; ``on_failure``
        #: fires in ``_fail_turn_callbacks`` when the turn is discarded
        #: mid-flight (worker crash, session reset) so the caller re-arms
        #: the reminder for the next 60s tick.
        self._turn_callbacks: list[TurnCallbacks] = []
        self._lock = asyncio.Lock()
        self._is_processing = asyncio.Event()
        self._debounce_task: asyncio.Task[None] | None = None
        self._control_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._control_task = asyncio.create_task(
            self._control_loop(), name="hamroh-engine-loop"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._control_task and not self._control_task.done():
            self._control_task.cancel()
            try:
                await self._control_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._stop_typing()
        # Drop any queued reminder callbacks. A claimed reminder is left
        # ``processing`` in the DB; the next startup's reset_stuck_reminders
        # re-arms it to ``pending`` so the reminder loop re-fires it — the
        # right behaviour for a clean shutdown.
        self._pending_callbacks = []
        self._turn_callbacks = []

    async def reset_session(self) -> None:
        """Owner-requested fresh CC session — delegates to the worker.

        If a turn is in flight, the worker queues a sentinel result that
        unblocks the control loop; ``_handle_turn_result`` cleans up the
        engine-side turn state when it arrives.
        """
        await self._worker.reset_session()

    async def stash_restore_context(self, reason: str) -> None:
        """Build a ``<restored_context>`` digest now; the next ``_kick``
        prepends it to the fresh session's first turn. Stays ``None``
        (plain turn) when there is no DB or no trusted history."""
        self._restore_context = await build_restored_context(self._db, reason=reason)

    # ------------------------------------------------------------------
    # Introspection (read-only, used by /health)
    # ------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """Number of buffered messages waiting for the next turn."""
        return len(self._pending)

    @property
    def turn_elapsed_s(self) -> float | None:
        """Seconds the current turn has been running, or None when idle."""
        if not self._is_processing.is_set():
            return None
        return time.monotonic() - self._turn.started_monotonic

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def submit(
        self,
        msg: ChatMessage,
        *,
        on_success: AsyncHook | None = None,
        on_failure: AsyncHook | None = None,
    ) -> None:
        """Add an inbound message to the pending buffer.

        - If the engine is *not* currently processing a turn we (re)start the
          debounce timer; once it fires we drain the buffer and start a turn.
        - If the engine *is* processing a turn we still buffer here, but the
          control loop will drain whatever's in the buffer between turns. The
          inject path is used for *immediate* mid-turn delivery only when
          we're sure CC is mid-stream — see :meth:`_maybe_inject`.

        ``on_success`` / ``on_failure``: optional async hooks for the turn
        that consumes this message. ``on_success`` runs once the turn ends
        with a result from CC; ``on_failure`` runs when the turn is
        discarded before CC consumed it (subprocess crash, owner session
        reset). The reminder loop uses them to advance/close vs revert its
        claimed DB row, so a crash mid-turn doesn't lose the reminder and a
        long turn doesn't re-fire it (see #22).
        """
        async with self._lock:
            self._pending.append(msg)
            if on_success is not None:
                self._pending_callbacks.append(
                    TurnCallbacks(on_success=on_success, on_failure=on_failure)
                )

        if self._is_processing.is_set():
            await self._maybe_inject()
            return

        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce_then_kick())

    async def _debounce_then_kick(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        await self._kick()

    async def _kick(self) -> None:
        async with self._lock:
            if not self._pending or self._is_processing.is_set():
                return
            batch = self._pending
            self._pending = []
            self._turn_callbacks.extend(self._pending_callbacks)
            self._pending_callbacks = []
            self._is_processing.set()
        # Skip synthetic reminders (mid=0) — no human waiting on them, so
        # the turn-start typing indicator should be silent for
        # reminder-only turns.
        self._turn.active_chats = {m.chat_id for m in batch if m.message_id > 0}
        self._turn.reply_targets = {m.chat_id: m for m in batch if m.message_id > 0}
        self._turn.started_monotonic = time.monotonic()
        self._turn.consumed_keys = []
        self._turn.heartbeat_count = 0
        self._turn.silent_stop_retried = False
        self._turn.awaiting_reply = _batch_awaits_reply(batch)
        restore = self._restore_context
        self._restore_context = None
        self._turn.had_restored_context = restore is not None
        xml = await format_messages_with_context(batch, self._db)
        if restore is not None:
            xml = f"{restore}\n{xml}"
        log.info("starting turn with %d msgs", len(batch))
        # Show "typing..." in every chat involved in this batch.
        await self._start_typing(set(self._turn.active_chats))
        self._log_hot_path("worker-send", batch, self._turn.active_chats)
        await self._worker.send(xml)
        await self._mark_consumed(batch)

    def _log_hot_path(
        self, stage: str, batch: list[ChatMessage], chats: set[int]
    ) -> None:
        """Log inbound→worker latency for the batch, keyed on the oldest
        message's receipt time (synthetic messages without one are ignored)."""
        now = time.monotonic()
        oldest_receipt = min(
            (
                m.received_at_monotonic
                for m in batch
                if m.received_at_monotonic is not None
            ),
            default=now,
        )
        log.info(
            "hot-path stage=%s chats=%s msgs=%d t_ms=%d",
            stage,
            sorted(chats),
            len(batch),
            int((now - oldest_receipt) * 1000),
        )

    async def _mark_consumed(self, batch: list[ChatMessage]) -> None:
        """Flag the drained batch as handed to CC.

        Called AFTER the send/inject — CC already has the messages, so
        this one small UPDATE per turn never delays the response. A crash
        in the window between send and this write replays the batch on
        the next boot (at-least-once, mirroring the reminder semantics
        of #22). Synthetic reminders (``message_id == 0``) are skipped —
        they re-fire via their own ``pending`` status.
        """
        keys = [(m.chat_id, m.message_id) for m in batch if m.message_id > 0]
        self._turn.consumed_keys.extend(keys)
        if self._db is None or not keys:
            return
        await mark_messages_consumed(self._db, keys)

    async def _maybe_inject(self) -> None:
        """Write pending messages to CC's stdin mid-turn.

        Called from :meth:`submit` whenever a new message arrives while a
        turn is already running. The worker's ``inject`` is event-driven
        (direct stdin write), not polled, so the follow-up lands at CC's
        next message boundary — typically the next reasoning step. The
        dispatcher's ``_on_message`` awaits this, so we must not do slow
        work here: the only I/O is the DB reply-chain lookup and the
        stdin drain, both ~microseconds for normal payloads.
        """
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
            self._turn_callbacks.extend(self._pending_callbacks)
            self._pending_callbacks = []
        self._turn.awaiting_reply = self._turn.awaiting_reply or _batch_awaits_reply(
            batch
        )
        xml = await format_messages_with_context(batch, self._db)
        await self._worker.inject(xml)
        await self._mark_consumed(batch)
        self._log_hot_path("inject", batch, {m.chat_id for m in batch})
        await self._rearm_typing({m.chat_id for m in batch})

    async def _rearm_typing(self, new_chats: set[int]) -> None:
        """Re-show "typing…" for chats whose follow-up was injected mid-turn.

        Two cases: if the typing loop is still running (model hasn't replied
        yet) we just widen its chat set; if it already exited (the model sent
        its first reply and ``notify_chat_replied`` stopped it) we restart it
        from scratch — same path as a fresh turn.
        """
        if self._typing.task is not None and not self._typing.task.done():
            self._typing.chats.update(new_chats)
        else:
            await self._start_typing(new_chats)

    # ------------------------------------------------------------------
    # Error notification
    # ------------------------------------------------------------------

    async def _notify_error_to_chats(self, text: str) -> None:
        """Send an error message directly via the bot to every chat that
        was waiting for a response. Bypasses the MCP layer (which is dead
        when we need this). Failures are swallowed — this is best-effort.
        """
        if self._error_notify is None:
            return
        for chat_id in self._turn.active_chats:
            try:
                await self._error_notify(chat_id, text, None)
                log.info("sent error notification to chat %s", chat_id)
            except Exception as exc:
                log.warning("failed to send error notification to %s: %s", chat_id, exc)

    def _alert_classified(self, classification: CcFailureClassification) -> None:
        """Log a classified CC failure at ERROR so the owner is DM'd.

        Owner delivery — and the link to the triggering message — is the
        root ``OwnerLogHandler``'s job, so every operator alert travels the
        one path: ``log.error`` → owner DM. See ``owner_log_notifier``.
        """
        log.error("%s", _classified_failure_message(classification))

    async def _handle_dropped_text(self, result: "TurnResult") -> None:
        """Deliver text the model produced but never sent via ``telegram_send_message``.

        The model occasionally writes its reply as a plain text content
        block and then stops, instead of calling ``telegram_send_message`` /
        ``telegram_reply_to_message`` — those blocks are invisible to the user.
        Rather than burn a whole retry turn nagging it to resend, deliver
        the text it already produced. ``skip`` turns never reach here — the
        caller discards their leftover text as internal narration.

        Exception: when that text is actually a technical error (bad model
        name, quota, …) we surface the classified user-facing message
        instead of echoing the raw diagnostic. ``classify_cc_failure``
        draws the line; thinking blocks never reach ``text_blocks`` (they're
        logged, not collected), so what remains is genuine reply prose.
        """
        classification = classify_cc_failure(result.text_blocks)
        if classification is not None:
            self._alert_classified(classification)
        else:
            await self._deliver_text_to_chats("\n\n".join(result.text_blocks))
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()

    async def _deliver_text_to_chats(self, text: str) -> None:
        """Deliver a model reply that landed as a text block to every chat
        waiting on this turn.

        Reuses the error-notify bot channel (the engine never imports
        telegram) and the shared ``chunk_text`` splitter so a long answer
        breaks at paragraph boundaries instead of hitting Telegram's
        length limit. Best-effort: a failed send is logged, not raised.

        Threaded when possible: if ``reply_targets`` has the human message
        that kicked this turn for a chat, every chunk replies to it instead
        of landing as a free-floating message — the model never got to call
        ``telegram_reply_to_message`` itself (that's the whole reason this
        path exists), so this is the engine doing that threading on its
        behalf rather than losing it.
        """
        if self._error_notify is None or not text.strip():
            return
        chunks = chunk_text(text)
        for chat_id in self._turn.active_chats:
            target = self._turn.reply_targets.get(chat_id)
            reply_to_message_id = target.message_id if target is not None else None
            for chunk in chunks:
                try:
                    await self._error_notify(chat_id, chunk, reply_to_message_id)
                except Exception as exc:
                    log.warning("dropped-text delivery to %s failed: %s", chat_id, exc)
            log.info(
                "delivered dropped text to chat %s (%d chunk(s))",
                chat_id,
                len(chunks),
            )

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _control_loop(self) -> None:
        """Wait for each turn to finish and decide what to do next.

        NOTE: ``_run_one_turn`` blocks the engine until the current turn
        completes. Messages arriving from other chats during a
        long-running turn (e.g. code review) queue in ``_pending`` and
        are dispatched only after the turn returns. See README "Known
        limitations — Single-turn blocking".
        """
        try:
            while not self._stop.is_set():
                if not self._is_processing.is_set():
                    await asyncio.sleep(0.05)
                    continue
                await self._run_one_turn()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("engine control loop crashed")

    async def _run_one_turn(self) -> None:
        """Wait for the worker's result and dispatch on the outcome.
        The outer loop just iterates."""
        try:
            result: TurnResult = await self._worker.wait_for_result()
        except Exception as exc:
            await self._handle_worker_failure(exc)
            return
        await self._handle_turn_result(result)

    async def _handle_worker_failure(self, exc: Exception) -> None:
        """CC subprocess died mid-turn. The worker's supervisor handles
        respawning; our job is to alert the owner — the crash is theirs to
        handle, so the waiting chat stays silent and only the owner is told,
        with a link to the message that was in flight.

        Queued ``on_failure`` hooks fire so the caller (reminder loop)
        reverts its claimed row to ``pending`` and retries — without this,
        a reminder injected into a turn that crashed before CC consumed it
        would be silently lost (#22).
        """
        log.error("⚠️ Claude Code crashed mid-turn and is restarting: %s", exc)
        self._is_processing.clear()
        await self._stop_typing()
        if self._turn_callbacks:
            log.info(
                "reverting %d turn callback(s) on worker failure — caller will retry",
                len(self._turn_callbacks),
            )
            await self._fail_turn_callbacks()
        self._turn.active_chats.clear()

    async def _fire_turn_callbacks(self) -> None:
        """Run every ``on_success`` hook queued for the just-ended turn.

        Called from ``_handle_turn_result`` once the turn definitively
        ends — clean stop, dropped-text delivery, tool-error-limit abort,
        or api-error. CC saw the messages, so reminders advance/close.
        Each hook is independent; one failing doesn't suppress the rest.
        See #22.
        """
        callbacks = self._turn_callbacks
        self._turn_callbacks = []
        for cb in callbacks:
            try:
                await cb.on_success()
            except Exception:
                log.exception("turn-success callback failed")

    async def _fail_turn_callbacks(self) -> None:
        """Run every ``on_failure`` hook for a turn discarded before CC
        consumed it (subprocess crash, owner session reset).

        Mirrors :meth:`_fire_turn_callbacks`. The reminder loop hangs a
        ``revert_reminder`` here so a claimed reminder returns to
        ``pending`` and re-fires on the next loop tick — the #22
        at-least-once guarantee, preserved now that the row is claimed.
        Each hook is independent; one failing doesn't suppress the rest.
        """
        callbacks = self._turn_callbacks
        self._turn_callbacks = []
        for cb in callbacks:
            if cb.on_failure is None:
                continue
            try:
                await cb.on_failure()
            except Exception:
                log.exception("turn-failure callback failed")

    async def _handle_aborted_turn(self, result: "TurnResult") -> None:
        """Handle a short-circuited turn (``aborted_reason`` set).

        - ``session-reset``: owner asked for a fresh session mid-turn.
          CC never finished the turn, so ``on_failure`` fires — reminders
          revert to ``pending`` and re-fire into the fresh session on the
          next loop tick (#22).
        - ``tool-error-limit``: an *intentional* abort, so the worker's
          supervisor respawns without going through crash recovery —
          ``_on_cc_crash`` never fires. We must notify the waiting chats
          ourselves, flushing any partial text the model wrote before the
          breaker killed the turn. CC saw the messages before the abort, so
          success callbacks fire — reminders advance and don't loop on a
          poisoned state.
        Neither path kicks ``_pending`` — the subprocess is mid-respawn.
        """
        if result.aborted_reason == "session-reset":
            log.info("turn aborted: session reset requested by owner")
            await self._fail_turn_callbacks()
            self._turn.active_chats.clear()
            return
        log.error(
            "⚠️ Hit an internal error and had to restart mid-task (%s). "
            "Please resend the last message.",
            result.aborted_reason,
        )
        if result.text_blocks:
            await self._deliver_text_to_chats("\n\n".join(result.text_blocks))
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()

    async def _handle_api_error_turn(self, result: "TurnResult") -> None:
        """Handle a turn the API itself rejected (``api_error`` set).

        Skips the dropped-text retry loop — re-sending into the same
        session just re-triggers the same refusal. A classified failure
        (rate-limit, auth, quota…) keeps the session: a reset wouldn't
        fix it and would lose context. Anything else (usage-policy
        refusal, context overflow) has poisoned the session history —
        every resumed turn replays the rejected content — so we notify
        the user and respawn CC fresh. Callbacks fire either way: CC saw
        the messages and retrying identical content fails
        deterministically (tool-error-limit precedent). ``_pending`` is
        not kicked — the subprocess is mid-respawn.
        """
        classification = classify_cc_failure(
            [result.api_error or "", *result.text_blocks]
        )
        if classification is not None:
            self._alert_classified(classification)
            self._turn.active_chats.clear()
            await self._fire_turn_callbacks()
            return

        log.error("turn failed with API error: %s", result.api_error)
        notice = await self._prepare_api_error_reset()
        detail = _trim_snippet(result.api_error or "")
        await self._notify_error_to_chats(f"{notice}\n\nDetails:\n{detail}")
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()
        await self.reset_session()

    async def _prepare_api_error_reset(self) -> str:
        """One-shot poison guard + digest stash for the api-error reset.

        If the failed turn itself opened with a restored digest, the
        digest is the prime poison suspect: skip the rebuild so the next
        session starts plain (no refusal loop). Otherwise stash a digest
        — the failed turn's own batch was never committed
        (``processed=0``), so the digest can't contain it. Returns the
        matching user-facing notice.
        """
        if self._turn.had_restored_context:
            log.warning(
                "restored digest preceded this api_error; next session starts plain"
            )
            return (
                "⚠️ Claude rejected that request and the turn failed. "
                "I've started a completely fresh session — the recent recap "
                "could not be carried over (it may itself have caused the "
                "failure). Please rephrase and resend."
            )
        await self.stash_restore_context("api-error")
        return (
            "⚠️ Claude rejected that request and the turn failed. "
            "I've started a fresh session and will carry a short recap of "
            "the recent conversation into it. Please rephrase and resend."
        )

    async def _handle_turn_result(self, result: "TurnResult") -> None:
        """Process a successfully-returned :class:`TurnResult`.

        Five outcome paths: short-circuited turn (``aborted_reason``),
        API-rejected turn (``api_error``), stderr-classified failure
        (rate-limit/auth/quota), dropped-text (no ``telegram_send_message``;
        never on ``skip`` — deliberate silence discards leftover text),
        or a clean turn (see :meth:`_finish_clean_turn`).
        """
        self._is_processing.clear()
        await self._stop_typing()

        if result.aborted_reason is not None:
            await self._handle_aborted_turn(result)
            return

        if result.api_error:
            await self._handle_api_error_turn(result)
            return

        action = result.control.action if result.control else None
        log.info(
            "turn done (action=%s, dropped_text=%s, text_blocks=%d)",
            action,
            result.dropped_text,
            len(result.text_blocks),
        )
        await self._notify_stderr_failure(result)

        if result.dropped_text and action != "skip":
            await self._handle_dropped_text(result)
            return

        if action == "heartbeat" and (
            self._turn.heartbeat_count < MAX_HEARTBEAT_CONTINUATIONS
        ):
            await self._continue_after_heartbeat()
            return

        await self._finish_or_retry_stop(result, action)

    async def _finish_or_retry_stop(
        self, result: "TurnResult", action: str | None
    ) -> None:
        """Finish a terminal turn — but re-engage once when a DM ``stop``
        delivered nothing. ``skip`` (deliberate silence) always finishes
        clean — any text it left behind is internal narration per the
        system-prompt contract, so it is logged and never delivered. A
        still-silent stop after the single retry is logged and finished
        clean too."""
        if action == "skip" and result.dropped_text:
            log.warning(
                "skip turn produced undelivered text — discarded as internal "
                "narration: %r",
                result.text_blocks,
            )
        if self._is_silent_stop(result, action):
            await self._retry_silent_stop()
            return
        if (
            self._turn.silent_stop_retried
            and action == "stop"
            and (not result.user_visible_action)
        ):
            log.warning("silent stop persisted after re-engagement — user got no reply")
        await self._finish_clean_turn(result)

    def _is_silent_stop(self, result: "TurnResult", action: str | None) -> bool:
        """A ``stop`` that delivered nothing while someone awaited a reply.

        ``stop`` promises a reply was already sent this turn, so an empty one
        is almost always premature — the model narrated its intent in the
        control ``reason`` (invisible to the user) and ended the turn.
        Deliberate silence must arrive as ``skip``, which never lands here —
        the nudge offers that exit, so group chatter is never forced into a
        reply. Fires for any turn that consumed a human message (DM or
        group; reminder-only turns excluded) and only once per turn.
        """
        return (
            action == "stop"
            and not result.dropped_text
            and not result.text_blocks
            and not result.user_visible_action
            and not self._turn.silent_stop_retried
            and self._turn.awaiting_reply
        )

    async def _retry_silent_stop(self) -> None:
        """Re-engage CC once to resolve a silent ``stop``.

        Mirrors :meth:`_continue_after_heartbeat`'s minimal-nudge path: keep
        the turn alive, resume typing, and hand the model a corrective nudge
        that lets it either deliver the reply or confirm the silence with
        ``skip``. Bounded to a single retry via ``silent_stop_retried``; if
        the model stops silently again the turn finishes clean (a warning is
        logged in ``_finish_or_retry_stop``), with no user-facing apology
        since nothing failed.
        """
        self._turn.silent_stop_retried = True
        log.warning(
            "silent stop with a DM waiting — re-engaging CC to reply or confirm skip"
        )
        self._is_processing.set()
        await self._start_typing(set(self._turn.active_chats))
        await self._worker.send(SILENT_STOP_NUDGE)

    async def _notify_stderr_failure(self, result: "TurnResult") -> None:
        """Surface a targeted message if stderr names the failure mode
        (rate-limit, auth, quota…). Orthogonal to dropped-text handling — a
        turn can be both rate-limited AND dropped_text, but we notify once."""
        classification = classify_cc_failure(result.stderr_tail)
        if classification is not None:
            self._alert_classified(classification)

    async def _continue_after_heartbeat(self) -> None:
        """Resume CC after a non-terminal ``heartbeat`` — keep the turn alive.

        The model posted a status update and signalled it isn't done, so
        rather than ending the turn we re-engage the same session to keep
        it working. Messages that landed in the brief window since the
        result event are folded into the continuation; otherwise a minimal
        nudge resumes the task. The original batch's success callbacks and
        ``processed`` commit stay deferred to the final clean stop, so a
        crash mid-continuation still replays the work.
        """
        self._turn.heartbeat_count += 1
        async with self._lock:
            batch = self._pending
            self._pending = []
            self._turn_callbacks.extend(self._pending_callbacks)
            self._pending_callbacks = []
        if batch:
            self._turn.awaiting_reply = (
                self._turn.awaiting_reply or _batch_awaits_reply(batch)
            )
            xml = await format_messages_with_context(batch, self._db)
        else:
            xml = HEARTBEAT_CONTINUE_NUDGE
        self._is_processing.set()
        await self._start_typing(set(self._turn.active_chats))
        await self._worker.send(xml)
        if batch:
            await self._mark_consumed(batch)

    async def _finish_clean_turn(self, result: "TurnResult") -> None:
        """Wrap up a successful turn: commit its messages as trusted, fire
        callbacks, and kick any messages that queued up while the turn was
        running.

        The commit is the ONLY place rows gain ``processed=1`` — failed,
        aborted, and crashed turns never reach it, so their messages stay
        barred from restored-context digests.
        """
        if self._db is not None and self._turn.consumed_keys:
            await mark_messages_processed(self._db, self._turn.consumed_keys)
        self._turn.active_chats.clear()
        await self._fire_turn_callbacks()

        async with self._lock:
            has_pending = bool(self._pending)
        if has_pending:
            await self._kick()
