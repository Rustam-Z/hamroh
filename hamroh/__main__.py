"""Entrypoint: ``python -m hamroh``.

Brings up the four components in order:

1. SQLite database (with migrations applied)
2. Local MCP server on a random localhost port
3. Claude Code subprocess via the CC worker
4. Engine + Telegram dispatcher
5. Web API, when ``HAMROH_WEB_PORT`` is set (see :mod:`hamroh.web`)

Then sleeps until interrupted, at which point everything is torn down.
"""

from __future__ import annotations

import asyncio
import logging

from .cc_worker import CcWorker, WorkerHooks
from .config import Config
from .db.database import Database
from .privacy.wiring import build_switcher
from .scheduler.reminder_scheduler import _reminder_loop
from .startup import (
    _acquire_instance_lock,
    _App,
    _attach_owner_log_notifier,
    _bootstrap_access,
    _build_cc_spec,
    _build_dispatcher_and_engine,
    _make_on_cc_giveup,
    _make_on_cc_stale_session,
    _open_db_and_stores,
    _register_extension_commands,
    _replay_unconsumed,
    _run_until_stopped,
    _seed_default_reminders,
    _start_mcp_server,
    web_db,
)
from .helpers.logging_setup import setup_logging
from .web.inbound import WebInboundDeps
from .web.server import start_web_server

__all__ = ["main", "_seed_default_reminders"]

log = logging.getLogger("hamroh")


async def _async_main() -> None:
    config = Config.from_env()
    config.ensure_dirs()
    setup_logging(config)
    lock = _acquire_instance_lock(config)  # refuse to boot twice on one data dir
    _bootstrap_access(config)

    db, plugins, stores = await _open_db_and_stores(config)
    private_db = await Database.open(config.private_db_path)
    app = _App(config=config, db=db, private_db=private_db, lock=lock)

    chat_titles: dict[int, str] = {}  # dispatcher writes, outbound tools read
    ctx, app.mcp = await _start_mcp_server(app, stores, plugins, chat_titles)
    app.browser_manager = ctx.browser_manager
    app.browser_session = ctx.browser_session
    # Warm Chromium in the background so even the first render is fast.
    if app.browser_manager is not None:
        app.warm_task = asyncio.create_task(
            app.browser_manager.warm(),
            name="hamroh-browser-warm",
        )
    spec = _build_cc_spec(config, plugins, app.mcp, stores)
    await _start_worker(app, ctx, spec)
    assert app.worker is not None
    app.sessions = build_switcher(app.worker, app.mcp, config, ctx.turn)
    await _start_engine_and_dispatcher(app, stores, ctx, plugins)
    log.info("hamroh is live")

    await _run_until_stopped(app)


async def _start_worker(app: _App, ctx, spec) -> None:
    """Spawn the CC worker wired to the supervisor callbacks, then supervise."""
    app.worker = CcWorker(
        spec,
        app.config,
        WorkerHooks(
            heartbeat=ctx.heartbeat,
            on_giveup=_make_on_cc_giveup(app),
            on_stale_session=_make_on_cc_stale_session(app),
        ),
    )
    await app.worker.start()
    await app.worker.supervise()


async def _start_engine_and_dispatcher(app: _App, stores, ctx, plugins) -> None:
    """Build + start the engine and dispatcher, replay any unconsumed inbound,
    and arm the reminder loop. The dispatcher and web API go live last."""
    app.dispatcher, app.engine = _build_dispatcher_and_engine(
        app, stores, ctx.chat_titles
    )
    _register_extension_commands(plugins.extensions, app.dispatcher)
    await app.engine.start()
    await _replay_unconsumed(app.db, app.engine)
    if app.private_db is not None:
        await _replay_unconsumed(app.private_db, app.engine, private=True)
    app.reminder_task = asyncio.create_task(
        _reminder_loop(app.db, app.engine),
        name="hamroh-reminders",
    )
    app.dispatcher.engine = app.engine
    assert app.bot is not None  # built together with the engine
    ctx.bot = app.bot
    ctx.on_chat_replied = app.engine.notify_chat_replied  # stops typing on reply
    _attach_owner_log_notifier(app)  # errors now DM the owner, not just the log
    await app.dispatcher.start()
    app.web_server = await start_web_server(
        app.config.web,
        WebInboundDeps(
            db=web_db(app),
            private=app.config.web.private,
            engine=app.engine,
            hub=app.bot.hub,
            chat_titles=ctx.chat_titles,
            rate_limiter=stores.rate_limiter,
        ),
    )


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
