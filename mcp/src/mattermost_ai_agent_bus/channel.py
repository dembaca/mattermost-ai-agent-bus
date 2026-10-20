"""Claude Code channel: push bus events into the session (RT-6.12).

Research preview (code.claude.com/docs/en/channels-reference). A server that
declares the experimental `claude/channel` capability and is opted in with
`--channels` (or `--dangerously-load-development-channels`) may send
`notifications/claude/channel`; Claude Code wakes the session with it, or
queues it for the next turn while one runs. That replaces the background
listener and the Stop hook's listen window.

Claude Code never acknowledges such a notification and drops it silently when
the session did not opt the server in. So the server looks at how its Claude
Code was started (an ancestor process named claude with --channels or
--dangerously-load-development-channels naming this plugin; MM_CHANNEL_PUSH=1
or 0 overrides that), and it still checks: its first push asks the agent to call channel_ok with a
code only that push carries. Until the agent does, the server reads nothing
from the inbox (nothing is marked read unseen) and the hooks keep the
listener; channel-<uid>.env says it is confirmed.

Pushing is a wake-up, and every wake-up is a model turn: the same rules as the
background listener decide what wakes the agent at once. Everything else is
held and pushed as one digest once the oldest is MM_LISTEN_DIGEST_SEC old.

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
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import anyio
from mcp.server.connection import Connection
from mcp.server.runner import ServerRunner, _has_modern_envelope, aclose_shielded
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types import INVALID_REQUEST

from . import session_file

log = logging.getLogger(__name__)

CAPABILITY = "claude/channel"
METHOD = "notifications/claude/channel"
# A message that names everyone wakes everyone, as in the listener.
BROADCAST = ("@channel", "@all", "@here")


PLUGIN = "plugin:mattermost-ai-agent-bus@"
FLAGS = ("--channels", "--dangerously-load-development-channels")


def enabled() -> bool:
    """MM_CHANNEL_PUSH overrides; unset, Claude Code's own command line decides."""
    value = os.environ.get("MM_CHANNEL_PUSH", "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    try:
        return started_as_channel()
    except Exception as exc:  # noqa: BLE001 — unreadable: no channel, the listener stays
        log.info("could not read how Claude Code was started: %s", exc)
        return False


def names_this_plugin(argv: list[str]) -> bool:
    """argv opts this plugin in as a channel: `--channels plugin:…@…` (one or
    more entries, space or comma separated, or `--channels=…`)."""
    on = False
    for arg in argv[1:]:
        flag, eq, value = arg.partition("=")
        if flag in FLAGS:
            on = True
            if not eq:
                continue
            arg = value
        elif arg.startswith("-"):
            on = False
            continue
        if on and any(v.startswith(PLUGIN) for v in arg.replace(",", " ").split()):
            return True
    return False


def started_as_channel(
    pid: int | None = None,
    *,
    argv_of: Callable[[int], list[str]] | None = None,
    parent_of: Callable[[int], int] | None = None,
) -> bool:
    """The nearest ancestor named claude was started with this plugin as a channel."""
    argv_of = argv_of or _argv
    parent_of = parent_of or _parent
    pid = os.getppid() if pid is None else pid
    for _ in range(12):  # uv, python and shells sit between
        if pid <= 1:
            return False
        argv = argv_of(pid)
        if argv and os.path.basename(argv[0]) == "claude":
            return names_this_plugin(argv)
        pid = parent_of(pid)
    return False


def _argv(pid: int) -> list[str]:
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        return [a for a in proc.read_bytes().decode(errors="replace").split("\0") if a]
    import ctypes
    import struct

    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(262144)
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return []
    raw = buf.raw[: size.value]
    argc = struct.unpack("i", raw[:4])[0]
    # argc, the executable path, padding, then argv.
    parts = [x for x in raw[4:].split(b"\0") if x][1 : argc + 1]
    return [x.decode(errors="replace") for x in parts]


def _parent(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return int(stat.read_text().rsplit(")", 1)[1].split()[1])
    import ctypes

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    buf = ctypes.create_string_buffer(136)  # struct proc_bsdinfo
    if libproc.proc_pidinfo(pid, 3, 0, buf, 136) <= 0:  # PROC_PIDTBSDINFO
        return 0
    return int.from_bytes(buf.raw[16:20], "little")  # pbi_ppid


def digest_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("MM_LISTEN_DIGEST_SEC", "1800")))
    except ValueError:
        return 1800.0


def confirmed_file(user_id: str) -> Path:
    """Exists while this bot's session confirmed the channel (the hooks read it)."""
    return session_file.state_dir() / f"channel-{user_id}.env"


def check(code: str) -> dict[str, Any]:
    """The first push: proves the session receives what the server sends."""
    return {
        "content": (
            "Bus channel check. Call channel_ok(code=\"" + code + "\") now, then finish "
            "your turn without another word. From then on bus messages arrive here "
            "by themselves; until then the turn hooks deliver them as before."
        ),
        "meta": {"kind": "channel_check"},
    }


def wakes(item: Mapping[str, Any], *, coordinator: bool, in_my_thread: bool) -> bool:
    """The listener's rules (RT-6.2): what is for this agent is pushed at once."""
    if item.get("kind") in ("mention", "dm", "reaction") or coordinator or in_my_thread:
        return True
    if item.get("from_trusted_human"):
        return True
    words = str((item.get("post") or {}).get("message", "")).lower().split()
    return any(w.rstrip(".,:;!?") in BROADCAST for w in words)


def digest(items: list[Mapping[str, Any]], since: float) -> dict[str, Any]:
    """Held posts as one push: each cut short, with what an answer needs."""
    lines = []
    for item in items:
        post = item.get("post") or {}
        text = " ".join(str(post.get("message", "")).split())
        where = f"post {post.get('id', '')}" + (f", thread {post['root_id']}" if post.get("root_id") else "")
        lines.append(f"- [{post.get('at', '')}] {post.get('author', '')} ({where}): {text[:300]}")
    head = (f"Digest: {len(items)} post(s) on the bus since {time.strftime('%H:%M', time.localtime(since))}, "
            "none addressed to you. Act only on what needs you, then finish your turn "
            "without another word.")
    return {"content": "\n".join([head, *lines]), "meta": {"kind": "digest", "count": str(len(items))}}


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

    def __init__(self, *, digest_sec: float = 1800.0) -> None:
        self.connection: Connection | None = None
        self.code = secrets.token_hex(4)
        self.confirmed = asyncio.Event()
        self.digest_sec = digest_sec
        self.held: list[dict[str, Any]] = []
        self.held_since = 0.0

    def confirm(self, code: str) -> bool:
        if code.strip() != self.code:
            return False
        self.confirmed.set()
        return True

    async def send(self, params: dict[str, Any]) -> None:
        if self.connection is not None:
            await self.connection.notify(METHOD, params)

    def _timeout(self, cap: float) -> float:
        if not self.held:
            return cap
        return max(0.0, min(cap, self.held_since + self.digest_sec - time.time()))

    async def run(
        self,
        wait: Callable[[float], Awaitable[list[dict[str, Any]]]],
        *,
        prepare: Callable[[], Awaitable[None]] | None = None,
        addressed: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
        retry_sec: float = 5.0,
        wait_cap: float = 300.0,
    ) -> None:
        """Check the channel, then push what `wait(timeout)` returns, forever.

        `prepare` (forget an older confirmation) and `wait` raise while the bus
        is not reachable yet; both are retried. `addressed` says whether an
        event wakes the agent now; the rest goes into the next digest.
        """
        conn = self.connection
        if conn is None:
            return
        await conn.initialized.wait()
        while prepare is not None:
            try:
                await prepare()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — no bus yet
                log.info("channel check waits for the bus: %s", exc)
                await asyncio.sleep(retry_sec)
        await self.send(check(self.code))
        # Unconfirmed, the session may not see a single push: leave the inbox
        # to wait_for_events and the listener, which mark read what they hand over.
        await self.confirmed.wait()
        while True:
            try:
                items = await wait(self._timeout(wait_cap))
                for item in items:
                    if addressed is None or await addressed(item):
                        await self.send(notification(item))
                    else:
                        if not self.held:
                            self.held_since = time.time()
                        self.held.append(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — no bus yet, or a dropped socket
                log.info("channel push waits for the bus: %s", exc)
                await asyncio.sleep(retry_sec)
                continue
            if self.held and time.time() - self.held_since >= self.digest_sec:
                held, self.held = self.held, []
                await self.send(digest(held, self.held_since))


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
