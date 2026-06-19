"""Generators for unique, collision-free test data."""

from __future__ import annotations

import uuid

#: Warns the bot a burst is coming so each message gets a fast, reply-only
#: answer (no memory/render/skill tools to slow the turn down). Sent and
#: acknowledged once before the timed burst begins.
BURST_PRIMER = (
    "I'm about to send you several separate messages, one right after another. "
    "Reply to each one as fast as you can, and use no tools other than your "
    "reply — just answer each message directly. Reply OK when you're ready."
)


def new_sentinel(prefix: str) -> str:
    """A unique, exactly-matchable token so each test asserts on its own
    reply (e.g. ``BANANA-1a2b3c4d``)."""
    return f"TEST-{prefix}-{uuid.uuid4().hex[:8]}"


def recall_prompt() -> tuple[str, str]:
    """A natural "I told you X, what is X?" prompt and its unique token.

    Preferred over a bare "echo this token" instruction: the bot's system
    prompt rejects blind echo-on-command as a prompt-injection pattern, but
    happily answers a normal question that recalls a value from the same
    message. Returns ``(question, token)``.
    """
    token = new_sentinel("REF")
    return f"My reference number is {token}. What is my reference number?", token


def split_message_prompt() -> tuple[str, tuple[str, str, str]]:
    """A prompt that explicitly asks for three *separate* Telegram messages,
    each carrying its own unique token, plus those three tokens to look for.

    Used to prove the bot can deliver more than one message per request.
    Returns ``(question, tokens)``.
    """
    tokens = (new_sentinel("PART1"), new_sentinel("PART2"), new_sentinel("PART3"))
    question = (
        "Send me three separate Telegram messages, one after another — "
        "do not combine them into a single message. "
        f"The first message must say {tokens[0]}, "
        f"the second must say {tokens[1]}, "
        f"and the third must say {tokens[2]}."
    )
    return question, tokens
