"""One-time: capture the tester account's Telethon session for the e2e suite.

Run in YOUR terminal — it logs you into Telegram (phone number, the code
Telegram sends you, and your 2FA password if set):

    .venv/bin/python tests/e2e/make_session.py

It reads E2E_TG_API_ID / E2E_TG_API_HASH from ``tests/e2e/.env.e2e`` (or
prompts if they're missing), logs in, then writes E2E_TG_SESSION and
E2E_OWNER_ID back into that file. Fill E2E_BOT_TOKEN / E2E_BOT_USERNAME /
E2E_GROUP_ID in by hand.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[1] / ".env.e2e"
_KEYS = (
    "E2E_TG_API_ID",
    "E2E_TG_API_HASH",
    "E2E_TG_SESSION",
    "E2E_OWNER_ID",
    "E2E_BOT_TOKEN",
    "E2E_BOT_USERNAME",
    "E2E_GROUP_ID",
)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write every known key (placeholders for ones not yet supplied)."""
    path.write_text("\n".join(f"{k}={values.get(k, '')}" for k in _KEYS) + "\n")


async def _login(api_id: int, api_hash: str) -> tuple[int, str]:
    """Interactive login; returns ``(user_id, session_string)``."""
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # prompts phone, code, 2FA via input()
    try:
        me = await client.get_me()
        return int(me.id), str(client.session.save())
    finally:
        await client.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    saved = _read_env_file(ENV_FILE)
    api_id_raw = os.environ.get("E2E_TG_API_ID") or saved.get("E2E_TG_API_ID")
    api_id = int(api_id_raw or input("api_id: "))
    api_hash = (
        os.environ.get("E2E_TG_API_HASH")
        or saved.get("E2E_TG_API_HASH")
        or input("api_hash: ")
    )
    log.info("Logging in — enter your phone number (e.g. +998901234567) when asked.")
    user_id, session = asyncio.run(_login(api_id, api_hash))

    saved.update(
        E2E_TG_API_ID=str(api_id),
        E2E_TG_API_HASH=api_hash,
        E2E_TG_SESSION=session,
        E2E_OWNER_ID=str(user_id),
    )
    _write_env_file(ENV_FILE, saved)
    log.info(
        "Wrote E2E_TG_SESSION + E2E_OWNER_ID (your id=%s) to %s", user_id, ENV_FILE
    )
    log.info("You can now run:  .venv/bin/python -m pytest tests/e2e -m e2e -v")


if __name__ == "__main__":
    main()
