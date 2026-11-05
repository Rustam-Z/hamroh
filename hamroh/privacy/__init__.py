"""Private chats: one person, own Claude session, own database, narrow tool set.

A chat is *private* when the web channel runs in private mode
(``HAMROH_WEB_PRIVATE``) or an extension vouches for a Telegram DM's sender
(see ``TelegramDispatcher.set_private_user_check``). Everything said there —
messages, tool results, the model's context — stays with that person:

- the engine never puts a private chat in a turn with any other chat;
- each private chat resumes its own Claude session (``sessions.py``);
- the session runs with the private tool profile (no web, memory, database,
  browser or external MCPs) and the MCP server re-checks every call (``turn.py``);
- rows go to ``private.db``, which no tool can query.
"""
