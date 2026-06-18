"""Entrypoint: ``python -m pyclaudir``.

Brings up the four components in order:

1. SQLite database (with migrations applied)
2. Local MCP server on a random localhost port
3. Claude Code subprocess via the CC worker
4. Engine + Telegram dispatcher

Then sleeps until interrupted, at which point everything is torn down.
"""

from __future__ import annotations

import asyncio
import logging

from .cc_worker import CcWorker
from .config import Config
from .reminder_scheduler import _reminder_loop
from .startup import (
    _acquire_instance_lock,
    _App,
    _bootstrap_access,
    _build_cc_spec,
    _build_dispatcher_and_engine,
    _make_on_cc_crash,
    _make_on_cc_giveup,
    _make_on_cc_stale_session,
    _open_db_and_stores,
    _replay_unconsumed,
    _run_until_stopped,
    _seed_default_reminders,
    _setup_logging,
    _start_mcp_server,
)

__all__ = ["main", "_seed_default_reminders"]

log = logging.getLogger("pyclaudir")


async def _async_main() -> None:
    _setup_logging()
    config = Config.from_env()
    config.ensure_dirs()
    lock = _acquire_instance_lock(config)  # refuse to boot twice on one data dir
    _bootstrap_access(config)

    db, plugins, stores = await _open_db_and_stores(config)
    app = _App(config=config, db=db, lock=lock)

    chat_titles: dict[int, str] = {}  # dispatcher writes, outbound tools read
    ctx, app.mcp = await _start_mcp_server(db, stores, plugins, chat_titles)
    spec = _build_cc_spec(config, plugins, app.mcp)

    app.worker = CcWorker(
        spec, config,
        heartbeat=ctx.heartbeat,
        on_crash=_make_on_cc_crash(app),
        on_giveup=_make_on_cc_giveup(app),
        on_stale_session=_make_on_cc_stale_session(app),
    )
    await app.worker.start()
    await app.worker.supervise()

    app.dispatcher, app.engine = _build_dispatcher_and_engine(
        app, stores, chat_titles,
    )
    await app.engine.start()
    await _replay_unconsumed(db, app.engine)
    app.reminder_task = asyncio.create_task(
        _reminder_loop(db, app.engine), name="pyclaudir-reminders",
    )

    app.dispatcher.engine = app.engine
    ctx.bot = app.dispatcher.bot
    ctx.on_chat_replied = app.engine.notify_chat_replied  # stops typing on reply
    await app.dispatcher.start()
    log.info("pyclaudir is live")

    await _run_until_stopped(app)


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
