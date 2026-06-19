"""``telegram_send_message`` — The agent's primary way to talk to humans."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ..formatting import chunk_text, markdown_to_telegram_html
from ..transcript import log_outbound
from .base import BaseTool, ToolResult, notify_chat_replied, record_outbound

log = logging.getLogger(__name__)


class SendMessageArgs(BaseModel):
    chat_id: int = Field(description="Telegram chat id.")
    text: str = Field(description="Message body. Plain text by default.")
    reply_to_message_id: int | None = Field(default=None)
    parse_mode: Literal["HTML", "MarkdownV2", None] = Field(default=None)


class TelegramSendMessageTool(BaseTool):
    name = "telegram_send_message"
    description = (
        "Send a text message to a Telegram chat. Long replies are split "
        "automatically at paragraph boundaries. Returns the first new "
        "message_id plus the full list in ``message_ids``."
    )
    args_model = SendMessageArgs

    async def run(self, args: SendMessageArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)

        # Chunk the RAW text before markdown conversion so each chunk's HTML
        # is self-contained (no mid-tag splits across chunk boundaries).
        raw_chunks = chunk_text(args.text)
        parse_mode = args.parse_mode
        if parse_mode is None:
            bodies = [markdown_to_telegram_html(c) for c in raw_chunks]
            parse_mode = "HTML"
        else:
            # Caller owns formatting; trust them but still chunk on whitespace.
            bodies = list(raw_chunks)

        message_ids: list[int] = []
        for i, body in enumerate(bodies):
            reply_to = args.reply_to_message_id if i == 0 else None
            sent = await self.ctx.bot.send_message(
                chat_id=args.chat_id,
                text=body,
                reply_to_message_id=reply_to,
                parse_mode=parse_mode,
            )
            message_ids.append(sent.message_id)
            log.info(
                "hot-path stage=delivered chat=%s msg=%s chunk=%d/%d",
                args.chat_id, sent.message_id, i + 1, len(bodies),
            )

            # Stop typing after the FIRST chunk lands — user has visible
            # content. Subsequent chunks stream in without the indicator.
            if i == 0:
                notify_chat_replied(self.ctx, args.chat_id)

        first_id = message_ids[0]
        log_outbound(
            chat_id=args.chat_id,
            chat_titles=self.ctx.chat_titles,
            message_id=first_id,
            reply_to_id=args.reply_to_message_id,
            text=args.text,
        )

        # Persist each delivered chunk as its own row. ``record_outbound``
        # internally handles the bot-identity lookup; PTB caches the
        # ``get_me`` result after the first call, so the N-1 follow-ups
        # cost a dict read.
        for i, (mid, raw_chunk) in enumerate(zip(message_ids, raw_chunks)):
            await record_outbound(
                self.ctx,
                chat_id=args.chat_id,
                message_id=mid,
                text=raw_chunk,
                reply_to_id=args.reply_to_message_id if i == 0 else None,
            )

        content = (
            f"sent message_id={first_id}"
            if len(message_ids) == 1
            else f"sent {len(message_ids)} chunks: message_ids={message_ids}"
        )
        return ToolResult(
            content=content,
            data={
                "message_id": first_id,
                "message_ids": message_ids,
                "chat_id": args.chat_id,
            },
        )
