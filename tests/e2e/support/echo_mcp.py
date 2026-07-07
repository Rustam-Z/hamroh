"""Throwaway stdio MCP server for the e2e suite.

Speaks MCP over stdio with a single ``echo`` tool that prepends the secret
from the ``E2E_MCP_SECRET`` env var to the caller's text. The secret is
generated fresh per run and reaches the model only through a real tool call
— so a bot reply carrying it proves the MCP connection in ``plugins.json``
actually spawned this server and routed the call end to end.

Launched by Claude Code itself (via the SUT's ``--mcp-config``), never by
pytest — see the ``plugins_sut`` fixture in ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("e2e-echo")


@server.tool()
def echo(text: str) -> str:
    """Return this server's secret followed by ``text``."""
    return f"{os.environ['E2E_MCP_SECRET']} {text}"


if __name__ == "__main__":
    server.run("stdio")
