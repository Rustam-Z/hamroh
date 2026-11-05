"""HTTP API for the web channel.

Three endpoints, all behind ``Authorization: Bearer <HAMROH_WEB_TOKEN>``:

- ``POST /api/messages`` — send a message as a web user.
- ``GET /api/events?user=…`` — Server-Sent Events stream of the bot's
  replies, reactions and "typing" signals for that user.
- ``GET /api/history?user=…&limit=…`` — saved messages of that user's chat.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..db.messages import RecentMessagesQuery, fetch_recent_messages
from ..rate_limiter import RateLimitExceeded
from .chat_ids import web_chat_id
from .hub import WebHub
from .inbound import WebInbound, WebInboundDeps, WebMessageIn

if TYPE_CHECKING:  # pragma: no cover
    from ..config import WebConfig
    from ..db.database import Database

log = logging.getLogger("hamroh.web")

#: Send a comment line this often so proxies don't close an idle stream.
KEEPALIVE_SECONDS = 15.0
#: Most messages one history request returns.
MAX_HISTORY = 200

Handler = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class WebApi:
    """Everything the endpoints need."""

    inbound: WebInbound
    hub: WebHub
    db: "Database"
    token: str


def build_app(api: WebApi, cors_origins: tuple[str, ...] = ()) -> Starlette:
    """The Starlette app with all routes, auth and optional CORS."""
    middleware = []
    if cors_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=list(cors_origins),
                allow_methods=["GET", "POST"],
                allow_headers=["Authorization", "Content-Type"],
            )
        )
    routes = [
        Route("/api/messages", _guard(api, _post_message), methods=["POST"]),
        Route("/api/events", _guard(api, _events), methods=["GET"]),
        Route("/api/history", _guard(api, _history), methods=["GET"]),
    ]
    return Starlette(routes=routes, middleware=middleware)


def _guard(
    api: WebApi, handler: Callable[[WebApi, Request], Awaitable[Response]]
) -> Handler:
    """Reject requests without the right bearer token."""

    async def guarded(request: Request) -> Response:
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(given.encode(), api.token.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await handler(api, request)

    return guarded


def _user_param(request: Request) -> str | None:
    """The ``user`` query parameter, or None when missing/blank."""
    user = request.query_params.get("user", "").strip()
    return user or None


async def _post_message(api: WebApi, request: Request) -> Response:
    """Accept one message from a web user and hand it to the agent."""
    try:
        incoming = WebMessageIn.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        return JSONResponse({"error": f"bad request: {exc}"}, status_code=400)
    try:
        msg = await api.inbound.accept(incoming)
    except RateLimitExceeded as exc:
        return JSONResponse(
            {"error": "rate limited", "retry_after": exc.retry_after_s},
            status_code=429,
        )
    return JSONResponse(
        {"chat_id": msg.chat_id, "message_id": msg.message_id}, status_code=202
    )


async def _events(api: WebApi, request: Request) -> Response:
    """Open a Server-Sent Events stream for one user's chat."""
    user = _user_param(request)
    if user is None:
        return JSONResponse({"error": "user is required"}, status_code=400)
    return StreamingResponse(
        _event_stream(api.hub, web_chat_id(user)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _event_stream(hub: WebHub, chat_id: int) -> AsyncIterator[str]:
    """Yield SSE frames until the client goes away."""
    queue = hub.subscribe(chat_id)
    try:
        yield f": connected chat={chat_id}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"event: {event.kind}\ndata: {event.to_json()}\n\n"
    finally:
        hub.unsubscribe(chat_id, queue)


async def _history(api: WebApi, request: Request) -> Response:
    """Return the last saved messages of one user's chat, oldest first."""
    user = _user_param(request)
    if user is None:
        return JSONResponse({"error": "user is required"}, status_code=400)
    try:
        limit = min(int(request.query_params.get("limit", "50")), MAX_HISTORY)
    except ValueError:
        return JSONResponse({"error": "limit must be a number"}, status_code=400)
    chat_id = web_chat_id(user)
    rows = await fetch_recent_messages(
        api.db,
        RecentMessagesQuery(limit=limit, include_unprocessed=True, chat_id=chat_id),
    )
    messages = [
        {
            "message_id": r["message_id"],
            "direction": r["direction"],
            "text": r["text"],
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]
    return JSONResponse({"chat_id": chat_id, "messages": messages})


class WebServer:
    """Runs the web API under uvicorn as a background task."""

    def __init__(self, app: Starlette, settings: "WebConfig") -> None:
        config = uvicorn.Config(
            app=app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            access_log=False,
            # Open event streams never end on their own; don't wait for them.
            timeout_graceful_shutdown=1,
        )
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None
        self._settings = settings

    async def start(self) -> None:
        """Start listening; returns once the port is bound."""
        self._task = asyncio.create_task(self._server.serve(), name="hamroh-web")
        while not self._server.started:
            if self._task.done():
                self._task.result()  # re-raise why uvicorn failed to start
            await asyncio.sleep(0.01)
        log.info(
            "web API listening on http://%s:%d",
            self._settings.host,
            self._settings.port,
        )

    async def stop(self) -> None:
        """Stop the server and wait for it to finish."""
        self._server.should_exit = True
        if self._task is not None:
            await self._task


async def start_web_server(
    settings: "WebConfig", deps: WebInboundDeps
) -> WebServer | None:
    """Start the web API when it's turned on in settings; None when it's off."""
    if not settings.enabled:
        return None
    api = WebApi(
        inbound=WebInbound(deps), hub=deps.hub, db=deps.db, token=settings.token
    )
    server = WebServer(build_app(api, settings.cors_origins), settings)
    await server.start()
    return server
