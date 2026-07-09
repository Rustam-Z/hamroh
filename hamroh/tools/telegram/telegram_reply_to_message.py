"""``telegram_reply_to_message`` — convenience wrapper that *requires* a reply target."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from .telegram_send_message import SendMessageArgs, TelegramSendMessageTool


class ReplyToMessageArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    reply_to_message_id: int = Field(
        description="Numeric id of the message to quote-reply within chat_id."
    )
    text: str = Field(
        description=(
            "Reply body. Markdown by default — auto-converted to Telegram HTML."
        )
    )


class TelegramReplyToMessageTool(BaseTool[ReplyToMessageArgs]):
    name = "telegram_reply_to_message"
    description = (
        "Reply to a specific message, threading to it so the parent is "
        "unambiguous (essential in busy group chats). This is the DEFAULT for "
        "every reply in an active conversation — thread to the inbound "
        "message you're answering. Use telegram_send_message only for an "
        "unthreaded, proactive message with no specific parent (e.g. a timer "
        "reminder). Sends immediately; long text auto-splits at paragraph "
        "boundaries."
    )
    args_model = ReplyToMessageArgs

    async def run(self, args: ReplyToMessageArgs) -> ToolResult:
        # Reuse the telegram_send_message tool's logic verbatim, including rate
        # limit and persistence.
        delegate = TelegramSendMessageTool(self.ctx)
        return await delegate.run(
            SendMessageArgs(
                chat_id=args.chat_id,
                text=args.text,
                reply_to_message_id=args.reply_to_message_id,
            )
        )
