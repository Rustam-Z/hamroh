"""Support machinery for the e2e suite, split by responsibility.

config (env + E2EConfig) · harness (bot subprocess + access) · state (read-only
DB/file inspection) · models (Conversation, Reply, eval Scenario dataset) ·
timeouts (latency budgets) · data (sentinels/prompts) · client (Telethon
send/wait) · assertions · waits (generic measure/poll) · eval (latency eval) ·
make_session (one-time login). Not collected as tests.
"""
