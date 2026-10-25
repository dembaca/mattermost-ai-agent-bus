"""Hook API: the turn hooks and the listener ask the MCP server, not Mattermost
(RT-6.17 in docs/arc42/06-runtime-view.md).

The hooks (hooks/*.sh) and the background listener (bin/mm-agent-listen.sh) hold
no bot token. They call this small HTTP API instead, served next to the
WebSocket inbox in the server's event loop:

    GET  /v1/me             {user_id, username, project, crown_until}
    GET  /v1/pending        {count[, channels: [{id, type, pending, fyi}]]}
                            (?detail=1: up to 5 channels, with fyi counts)
    GET  /v1/last-post      {at, message}: the own latest post in the work channel
    GET  /v1/posts          {posts: [{id, user_id, root_id, create_at, type,
                            message, to, intent, fyi, gated}]}, work channel,
                            newest first (?limit=N, at most 200, or ?since=MS)
    GET  /v1/ask-reactions  {fresh}: reactions to open asks not handed over yet
    POST /v1/presence       {state: working|idle|listening, label?, until?} → {ok}

HTTP/1.1 on 127.0.0.1 only, an ephemeral port, one random key per server: every
request carries ``Authorization: Bearer <key>``, compared in constant time, or
gets 401. 503 means no bus session (no token yet), 502 that Mattermost failed.
The hooks find endpoint and key in MM_BUS_ENDPOINT / MM_BUS_KEY, else in the
discovery file ``endpoint-sid-<key>.env`` (0600) in the state dir, keyed like the
session file (session_file.keyed_path); it is removed when the server ends.

What the shell CLI already does with the token stays there, run by the server:
the status (mm-agent-status.sh) and the unread counters (mm-agent-unread.sh).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from . import a2a, asks, gate, lease, ownpost, session_file

if TYPE_CHECKING:
    from .client import MattermostClient

log = logging.getLogger(__name__)

FILE_PREFIX = "endpoint"
MAX_BODY = 64 * 1024
MAX_HEADERS = 64
READ_TIMEOUT_SEC = 5.0
# A route may run a shell CLI script, which lease.py gives 60 s.
ROUTE_TIMEOUT_SEC = 90.0
PRESENCE = ("working", "idle", "listening")
DETAIL_CHANNELS = 5
MAX_POSTS = 200
_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
            413: "Payload Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}

Reply = tuple[int, dict[str, Any]]


class BadRequest(ValueError):
    pass


def file_path() -> Path:
    """Where the hooks find endpoint and key (mm_bus in bin/mm-agent-lib.sh)."""
    return session_file.keyed_path(FILE_PREFIX)


class HookAPI:
    """The server side of RT-6.17: one per MCP server."""

    def __init__(self, get_client: Callable[[], Awaitable[MattermostClient | None]]) -> None:
        self._get_client = get_client
        self.key = secrets.token_urlsafe(32)
        self.endpoint = ""
        self._server: asyncio.Server | None = None
        self._file: Path | None = None

    async def start(self, *, write_file: bool = True) -> str:
        """Listen on 127.0.0.1 and (by default) write the discovery file."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}"
        if write_file:
            self._write_file()
        return self.endpoint

    def _write_file(self) -> None:
        path = file_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"MM_BUS_ENDPOINT={self.endpoint}\nMM_BUS_KEY={self.key}\n"
                     f"MM_BUS_PID={os.getpid()}\n")
        os.replace(tmp, path)
        self._file = path

    def remove_file(self) -> None:
        """Remove the discovery file while it is still this server's: one that
        took the session over (`claude --continue`) may have written its own."""
        path, self._file = self._file, None
        if path is None:
            return
        try:
            if session_file.parse(path).get("MM_BUS_KEY") == self.key:
                path.unlink()
        except OSError:
            pass

    async def close(self) -> None:
        self.remove_file()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 1.0)
            except asyncio.TimeoutError:
                pass
            self._server = None

    # --- HTTP -------------------------------------------------------------------

    def _authorized(self, header: str) -> bool:
        return hmac.compare_digest(header.encode(), f"Bearer {self.key}".encode())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await self._respond(reader)
        except Exception as exc:  # noqa: BLE001 — a malformed request, never the key
            log.info("hook API: bad request (%s)", type(exc).__name__)
            status, body = 400, {"error": "bad request"}
        data = json.dumps(body).encode()
        head = (f"HTTP/1.1 {status} {_REASONS.get(status, '')}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n")
        try:
            writer.write(head.encode() + data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> Reply:
        method, target, headers = await asyncio.wait_for(_read_head(reader), READ_TIMEOUT_SEC)
        if not self._authorized(headers.get("authorization", "")):
            return 401, {"error": "unauthorized"}
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            return 413, {"error": "body too large"}
        raw = await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT_SEC) if length else b""
        url = urlsplit(target)
        route = _ROUTES.get((method, url.path))
        if route is None:
            return 404, {"error": "no such route"}
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return 400, {"error": "body must be a JSON object"}
        try:
            client = await self._get_client()
        except Exception:  # noqa: BLE001 — no bus configured
            client = None
        if client is None or not client.token:
            return 503, {"error": "no bus session"}
        try:
            return await asyncio.wait_for(route(client, query, payload), ROUTE_TIMEOUT_SEC)
        except BadRequest as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — Mattermost failed: the hook fails open
            log.info("hook API %s %s: %s", method, url.path, type(exc).__name__)
            return 502, {"error": "bus unreachable"}


async def _read_head(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]]:
    parts = (await reader.readline()).decode("latin-1").split()
    if len(parts) != 3:
        raise BadRequest("request line")
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            return parts[0], parts[1], headers
        name, sep, value = line.decode("latin-1").partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    raise BadRequest("too many headers")


# --- routes -------------------------------------------------------------------

async def _uid(client: MattermostClient) -> str:
    return str((await client.get_me()).get("id") or "")


async def me(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """Who the bot is, whether it works in a project channel, its crown (RT-6.6)."""
    who = await client.get_me()
    uid = str(who.get("id") or "")
    project = bool(os.environ.get("MM_PROJECT_CHANNEL_ID") or client.project_channel_name)
    return 200, {"user_id": uid, "username": str(who.get("username") or ""),
                 "project": project, "crown_until": lease.crown_until(uid)}


def _channel_row(line: str) -> dict[str, Any]:
    cid, _, rest = line.partition("\t")
    ctype, _, rest = rest.partition("\t")
    row: dict[str, Any] = {"id": cid, "type": ctype, "pending": 0, "fyi": 0}
    for pair in rest.split("\t"):
        k, _, v = pair.partition("=")
        if k in ("pending", "fyi") and v.isdigit():
            row[k] = int(v)
    return row


async def pending(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """Unread mentions, DMs and posts for all agents (RT-6.2 step 1), by the
    shell CLI's rules; with detail=1 per channel, with its fyi posts (RT-6.14)."""
    out = await lease.script(client, "mm-agent-unread.sh", "count")
    count = int(out) if out.isdigit() else 0
    result: dict[str, Any] = {"count": count}
    if count and query.get("detail") == "1":
        lines = [x for x in (await lease.script(client, "mm-agent-unread.sh", "detail")).splitlines() if x]
        result["channels"] = [_channel_row(x) for x in lines[:DETAIL_CHANNELS]]
    return 200, result


async def last_post(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """The own latest post in the work channel (RT-6.2 step 2): from
    last-<uid>.env (ownpost.py) without a request, else from the channel's last
    100 posts; at 0 when there is none."""
    uid = await _uid(client)
    rec = ownpost.read(uid)
    if rec.get("AT", "").isdigit() and rec.get("MSG"):
        return 200, {"at": int(rec["AT"]), "message": rec["MSG"]}
    for post in await client.channel_posts(await client.work_channel_id(), per_page=100):
        if post.get("user_id") == uid:
            message = str(post.get("message") or "").removeprefix("👑 ").replace("\n", " ").strip()
            return 200, {"at": int(post.get("create_at") or 0), "message": message}
    return 200, {"at": 0, "message": ""}


def _slim(post: Mapping[str, Any], agents: set[str], gated: set[str]) -> dict[str, Any]:
    intent = a2a.intent_of(post) or ""
    return {
        "id": str(post.get("id") or ""),
        "user_id": str(post.get("user_id") or ""),
        "root_id": str(post.get("root_id") or ""),
        "create_at": int(post.get("create_at") or 0),
        "type": str(post.get("type") or ""),
        "message": str(post.get("message") or ""),
        "to": a2a.ALL if a2a.to_all(post) else "",
        "intent": intent,
        # Only an agent's intent counts: anyone can write props (RT-6.14).
        "fyi": intent == a2a.FYI and post.get("user_id") in agents,
        "gated": post.get("id") in gated,
    }


async def posts(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """The work channel's posts, newest first: the last ?limit=N, or those since
    ?since=MS (created or edited). Each says whether it is for all agents, an
    agent's fyi post (RT-6.14) and gated by the server (RT-6.16)."""
    since, limit = query.get("since", ""), query.get("limit", "60")
    if not (since or limit).isdigit():
        raise BadRequest("since: epoch ms, limit: a count")
    uid = await _uid(client)
    cid = await client.work_channel_id()
    if since:
        raw = await client.channel_posts(cid, since_ms=int(since))
    else:
        raw = await client.channel_posts(cid, per_page=max(1, min(int(limit), MAX_POSTS)))
    authors = {str(p.get("user_id")) for p in raw if p.get("user_id") and a2a.intent_of(p) == a2a.FYI}
    try:
        agents = await client.agents(authors) if authors else set()
    except Exception as exc:  # noqa: BLE001 — nobody counts as an agent then
        log.info("hook API: fyi authors unknown: %s", type(exc).__name__)
        agents = set()
    gated = gate.gated(uid)
    return 200, {"posts": [_slim(p, agents, gated) for p in raw]}


async def ask_reactions(client: MattermostClient, query: Mapping[str, str],
                        body: Mapping[str, Any]) -> Reply:
    """Reactions to the agent's open asks it has not been handed yet (RT-6.11):
    a reaction raises no unread counter."""
    uid = await _uid(client)
    fresh = 0
    for pid in asks.open_asks(uid):
        try:
            reactions = await client.reactions(pid)
        except Exception:  # noqa: BLE001 — look again next time
            continue
        for r in reactions:
            who, emoji = str(r.get("user_id") or ""), str(r.get("emoji_name") or "")
            if who != uid and not asks.was_seen(uid, asks.seen_key(pid, who, emoji)):
                fresh += 1
    return 200, {"fresh": fresh}


async def presence(client: MattermostClient, query: Mapping[str, str],
                   body: Mapping[str, Any]) -> Reply:
    """Set the status for a turn (RT-6.2, RT-6.3, RT-6.6) through
    mm-agent-status.sh: working or idle with the project label, listening until
    an epoch."""
    state = body.get("state")
    if state not in PRESENCE:
        raise BadRequest("state: working, idle or listening")
    if state == "listening":
        until = body.get("until")
        if type(until) is not int or until <= 0:
            raise BadRequest("until: epoch seconds")
        arg = str(until)
    else:
        arg = str(body.get("label") or "")
    await lease.script(client, "mm-agent-status.sh", state, arg)
    return 200, {"ok": True}


_ROUTES: dict[tuple[str, str], Callable[[Any, Mapping[str, str], Mapping[str, Any]], Awaitable[Reply]]] = {
    ("GET", "/v1/me"): me,
    ("GET", "/v1/pending"): pending,
    ("GET", "/v1/last-post"): last_post,
    ("GET", "/v1/posts"): posts,
    ("GET", "/v1/ask-reactions"): ask_reactions,
    ("POST", "/v1/presence"): presence,
}
