"""Health-only ASGI entrypoint for the shared-Supabase CE profile.

This intentionally bypasses ``onyx.main``. Importing the native application
would load its router graph, Celery client, auth configuration, and normal
lifespan before an HTTP middleware could deny those surfaces.
"""

from __future__ import annotations

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from onyx.db.skybase_shared_supabase import is_shared_supabase_profile
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError
from onyx.db.skybase_shared_supabase import validate_shared_supabase_contract

_HEALTH_BODY = b'{"success":true,"message":"ok","data":null}'
_DISABLED_BODY = (
    b'{"detail":"This native Onyx surface is disabled in the '
    b'Skybase shared-Supabase profile."}'
)
_ALLOWED_HEALTH_PATHS = frozenset({"/health", "/health/"})
_ALLOWED_HEALTH_RAW_PATHS = frozenset({b"/health", b"/health/"})


async def _send_json(send: Send, *, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _handle_lifespan(receive: Receive, send: Send) -> None:
    """Acknowledge server lifecycle without native Onyx startup work."""

    while True:
        message: Message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def shared_supabase_health_app(
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    """Serve the one reviewed HTTP path and deny every other ASGI scope."""

    if scope["type"] == "lifespan":
        await _handle_lifespan(receive, send)
        return
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    if scope["type"] == "http":
        raw_path = scope.get("raw_path")
        query_string = scope.get("query_string", b"")
        if (
            scope["method"] != "GET"
            or scope["path"] not in _ALLOWED_HEALTH_PATHS
            or raw_path not in _ALLOWED_HEALTH_RAW_PATHS
            or query_string != b""
        ):
            await _send_json(send, status=503, body=_DISABLED_BODY)
        else:
            await _send_json(send, status=200, body=_HEALTH_BODY)


if not is_shared_supabase_profile():
    raise SharedSupabaseContractError(
        "The shared-Supabase health entrypoint requires SKYBASE_ONYX_SHARED_SUPABASE=true."
    )
validate_shared_supabase_contract()
app: ASGIApp = shared_supabase_health_app
