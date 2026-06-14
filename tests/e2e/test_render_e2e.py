"""E2E: the bot renders a diagram and sends it as a photo — DM and group.

given  a request to render content as an image
when    the tester asks
then    a photo arrives AND a PNG lands in data/renders/.

The render-tool duration and the turn latency are logged — render is the
slowest tool path, so this is where the "diagram + speed" numbers come from.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from telethon import TelegramClient  # type: ignore[import-untyped]

from tests.e2e.support.harness import Sut, new_png_files, tool_calls_since
from tests.e2e.support.helpers import (
    MAX_RENDER_REPLY_S,
    Conversation,
    assert_reply_within,
    new_sentinel,
    send_and_wait,
)

log = logging.getLogger(__name__)


async def _assert_renders(
    sut: Sut, client: TelegramClient, convo: Conversation
) -> None:
    token = new_sentinel("DIAG")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    before = time.time()

    # when we ask the bot to render a table as a photo
    reply = await send_and_wait(
        client,
        convo,
        f"Render a small HTML table containing the text {token} and send it to me "
        "as a photo.",
        timeout=180,
    )

    render_ms = next(
        (
            r["duration_ms"]
            for r in tool_calls_since(sut.db_path, since)
            if r["tool_name"] == "render_html"
        ),
        None,
    )
    log.info("render: turn=%.2fs render_html=%sms", reply.t_complete_s, render_ms)

    # then a photo came back ...
    assert reply.media_kind == "photo", (
        f"expected a photo, got media_kind={reply.media_kind!r}; text {reply.text!r}"
    )
    # ... a PNG landed on disk ...
    pngs = new_png_files(sut.renders_dir, before)
    assert pngs, f"no new PNG appeared in {sut.renders_dir}"
    # ... and the photo arrived within the render limit
    assert_reply_within(reply, MAX_RENDER_REPLY_S, "render")


async def test_bot_renders_and_sends_photo_dm(
    pyclaudir_sut: Sut, tester_client: TelegramClient, dm: Conversation
) -> None:
    await _assert_renders(pyclaudir_sut, tester_client, dm)


async def test_bot_renders_and_sends_photo_group(
    pyclaudir_sut: Sut, tester_client: TelegramClient, group: Conversation
) -> None:
    await _assert_renders(pyclaudir_sut, tester_client, group)
