"""The request id: every HTTP request gets one (a harmless X-Request-ID from the caller, else a new one), it is bound for the whole request so every log line made while it is
served carries it (also in the threads FastAPI runs the routes in), and it is returned in the X-Request-ID response header so a person can quote it when something looks wrong.

Plain ASGI, no framework import: the outermost layer, so even a refusal by the layers inside it carries the id."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from .. import obs

HEADER = b"x-request-id"


class RequestContext:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[None]]) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        sent = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == HEADER), "")
        request_id = obs.clean_request_id(sent) or obs.new_request_id()

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != HEADER]
                message = {**message, "headers": [*headers, (HEADER, request_id.encode("ascii"))]}
            await send(message)

        with obs.bind(request_id=request_id):
            await self.app(scope, receive, send_with_id)
