"""Web channel: talk to the same hamroh agent over HTTP, next to Telegram.

A web user gets their own chat id (see :mod:`.chat_ids`). Inbound messages
go through the same engine as Telegram ones; the bot's replies reach the
browser through :class:`.hub.WebHub` instead of the Telegram API.
"""
