"""Claude Code channel: push bus events into the session (RT-6.12).

Research preview (code.claude.com/docs/en/channels-reference). A server that
declares the experimental `claude/channel` capability and is opted in with
`--channels` (or `--dangerously-load-development-channels`) may send
`notifications/claude/channel`; Claude Code wakes the session with it, or
queues it for the next turn while one runs. That replaces the background
listener and the Stop hook's listen window.

Claude Code never acknowledges such a notification and drops it silently when
the session did not opt the server in, so the server must not guess: the
human turns pushing on with MM_CHANNEL_PUSH=1 in the environment Claude Code
is started from, which the hooks see as well.

Two constraints of the preview shape the transport:
  - Claude Code does not register a channel on MCP revision 2026-07-28, which
    this SDK would negotiate, so the server answers on the handshake era only
    (an enveloped `server/discover` probe gets an error, the client falls back
    to `initialize`).
  - The push needs the connection outside any request, so the server keeps it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anyio
from mcp.server.connection import Connection
from mcp.server.runner import ServerRunner, _has_modern_envelope, aclose_shielded
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types import INVALID_REQUEST

log = logging.getLogger(__name__)

CAPABILITY = "claude/channel"
METHOD = "notifications/claude/channel"


def enabled() -> bool:
    return os.environ.get("MM_CHANNEL_PUSH", "").strip().lower() in ("1", "true", "yes", "on")


def notification(item: Mapping[str, Any]) -> dict[str, Any]:
    """One wait_for_events item as channel params: the message as content,
    everything to route an answer by as tag attributes (string values; keys
    are identifiers, others Claude Code drops)."""
    post = item.get("post") or {}
    meta: dict[str, str] = {"kind": str(item.get("kind", ""))}
    for key in ("id", "root_id", "channel", "author", "at"):
        if post.get(key):
            meta["post_id" if key == "id" else key] = str(post[key])
    meta["from_trusted_human"] = "true" if item.get("from_trusted_human") else "false"
    for key in ("trusted_human", "ask", "emoji", "answer"):
        if item.get(key):
            meta[key] = str(item[key])
    return {"content": str(post.get("message", "")), "meta": meta}


class Pusher:
    """Keeps the session's connection and feeds it bus events."""

    def __init__(self) -> None:
        self.connection: Connection | None = None

    async def send(self, params: dict[str, Any]) -> None:
        if self.connection is not None:
            await self.connection.notify(METHOD, params)

    async def run(
        self,
        wait: Callable[[], Awaitable[list[dict[str, Any]]]],
        *,
        retry_sec: float = 5.0,
    ) -> None:
        """Push what `wait` returns, forever. `wait` blocks until events arrive
        (or times out with []) and raises while the bus is not reachable yet."""
        conn = self.connection
        if conn is None:
            return
        await conn.initialized.wait()
        while True:
            try:
                items = await wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — no bus yet, or a dropped socket
                log.info("channel push waits for the bus: %s", exc)
                await asyncio.sleep(retry_sec)
                continue
            for item in items:
                await self.send(notification(item))


async def serve_handshake_only(
    server: Any,
    read_stream: Any,
    write_stream: Any,
    *,
    lifespan_state: Any,
    init_options: Any,
    pusher: Pusher,
    on_connected: Callable[[], Awaitable[None]],
) -> None:
    """The SDK's handshake-era loop, keeping the connection for the pusher.

    Mirrors mcp.server.runner._serve_legacy_stream; a request in the
    2026-07-28 envelope is refused, which makes the client fall back to
    `initialize` — the revision Claude Code can register a channel on.
    """
    dispatcher: JSONRPCDispatcher[Any] = JSONRPCDispatcher(
        read_stream, write_stream, inline_methods=frozenset({"initialize"})
    )
    connection = Connection.for_loop(dispatcher)
    pusher.connection = connection
    runner = ServerRunner(server, connection, lifespan_state, init_options=init_options)

    async def on_request(dctx: Any, method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        if method != "initialize" and _has_modern_envelope(params):
            raise MCPError(
                code=INVALID_REQUEST,
                message="channel server: handshake protocol era only",
            )
        return await runner.on_request(dctx, method, params)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(on_connected)
            await dispatcher.run(on_request, runner.on_notify)
            tg.cancel_scope.cancel()
    finally:
        pusher.connection = None
        await aclose_shielded(connection)
        await write_stream.aclose()
