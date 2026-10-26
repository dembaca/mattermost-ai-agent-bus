"""Hook API: the turn hooks and the shell scripts ask the MCP server, not
Mattermost (RT-6.17 in docs/arc42/06-runtime-view.md).

The hooks (hooks/*.sh) and the scripts an agent runs (bin/mm-agent-listen.sh,
-post, -poll, -status) hold no bot token. They call this small HTTP API
instead, served next to the WebSocket inbox in the server's event loop:

    GET  /v1/me             {user_id, username, project, crown_until, channel}
    GET  /v1/pending        {count[, stall: 1][, channels: [{id, type, pending, fyi}]]}
                            (?detail=1: up to 5 channels, with fyi counts)
    GET  /v1/last-post      {at, message}: the own latest post in the work channel
    GET  /v1/posts          {channel, posts: [{id, user_id, root_id, create_at, type,
                            message, to, intent, fyi, gated}]}, work channel
                            (or ?channel=ID|NAME of the project or default
                            channel, else 403), newest first (?limit=N, at
                            most 200, or ?since=MS)
    GET  /v1/ask-reactions  {fresh}: reactions to open asks not handed over yet
    POST /v1/presence       {state: working|idle|listening|bus, label?, until?} → {ok}
                            (idle, listening: the turn ended, open permission
                            prompts close, RT-6.13)
    POST /v1/stat           {k: turn|turn_end|block|compact|wake|transcript, …} → {ok}
                            (one overhead counter, RT-6.9: the hooks of a keyless
                            session cannot reach the server's state dir, RT-6.18)
    POST /v1/post           {message, channel_id?, root_id?, to?, kind?} → what
                            post_message returns, a refusal included
    POST /v1/reply          {root_id, message, channel_id?, to?, kind?} → what
                            reply_in_thread returns (mm-agent-post.sh): the tools'
                            own code (server.py), not a second implementation
    GET  /v1/turn-end       {action: drain|signoff|idle, reason?, banner?, pending?,
                            listen_since?, listen_until?} (?drains=N&signoff_asked=0|1
                            &by_bus=0|1&push=0|1&listen_since=S): the Stop hook's
                            decision, words and listen window for a turn plugin (RT-6.20)
    GET  /v1/wake           {wake, reason: dm|stall|reaction|addressed|digest|'', count, text}
                            (?since=MS&until=S): the listener's decision inside the
                            listen window (RT-6.20)
    GET  /v1/handoff        {context}: the own latest posts after a compaction
                            (RT-6.7, RT-6.20)

HTTP/1.1 on 127.0.0.1 only, an ephemeral port, one random key per server: every
request carries ``Authorization: Bearer <key>``, compared in constant time, or
gets 401 (a malformed request: 400, before the key is looked at). 503 means
no bus session (no token yet), 502 that Mattermost failed. At most
MAX_CONNECTIONS connections at once and MAX_HEAVY_RUNS Mattermost-heavy requests.
The hooks find endpoint and key in MM_BUS_ENDPOINT / MM_BUS_KEY, else in the
discovery file ``endpoint-sid-<key>.env`` (0600) in the state dir, keyed like the
session file (session_file.keyed_path: the Claude Code session, else the
workspace), with the bot's id once known; it is removed when the server ends.
A file of another server that still runs is not overwritten: one MCP server
per workspace, and per Claude Code session id (a harness started from a Claude
Code shell inherits it; the file names each server's MCP client, warning()).
No file is keyed on /, the home directory or above it (session_file.unkeyed):
the server waits for the client's roots or a variable naming the workspace. The host process of a keyless corral session
(RT-6.18, hosted.py) serves it with the session's key from preStart and writes
no file: the sandbox has both in its environment.

The answers come from the server's own modules, no script runs behind them:
the status from lease.py, the pending count from unread.py.
"""

from __future__ import annotations

import asyncio
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from . import a2a, asks, channel, gate, lease, ownpost, session_file, stall, stats, turnend, unread
from .client import InvalidId, _pid_alive, mm_name

if TYPE_CHECKING:
    from .client import MattermostClient

log = logging.getLogger(__name__)

FILE_PREFIX = "endpoint"
MAX_BODY = 64 * 1024
MAX_HEADERS = 64
READ_TIMEOUT_SEC = 5.0
# Writing the answer to a client that does not read it gives up after this.
WRITE_TIMEOUT_SEC = 5.0
# Connections served at once; one more is closed without an answer.
MAX_CONNECTIONS = 32
# Requests that make several Mattermost calls each (pending, presence) at once.
MAX_HEAVY_RUNS = 2
# The pending detail reads up to six channels' posts.
ROUTE_TIMEOUT_SEC = 90.0
PRESENCE = ("working", "idle", "listening", "bus")
MAX_LABEL = 64
# A listen window ends within Claude Code's 2 h limit; a day is ample.
MAX_UNTIL_AHEAD_SEC = 86400
MAX_POSTS = 200
_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            413: "Payload Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}

Reply = tuple[int, dict[str, Any]]
Route = Callable[[Any, Mapping[str, str], Mapping[str, Any]], Awaitable[Reply]]
_UID = re.compile(r"[a-z0-9]{26}")


class BadRequest(ValueError):
    pass


class Forbidden(ValueError):
    """A request the key does not cover: a 403."""


# How long another server's hook API may take to show it is alive.
PROBE_TIMEOUT_SEC = 1.0


def _answers(endpoint: str, key: str) -> bool:
    """The server at this loopback endpoint (http://127.0.0.1:<port>) answers
    an authenticated GET /v1/me within PROBE_TIMEOUT_SEC. Listening is not
    enough: an orphan whose event loop is stuck still accepts connections."""
    host, _, port = urlsplit(endpoint).netloc.rpartition(":")
    if host != "127.0.0.1" or not port.isdigit() or not key:
        return False
    conn = http.client.HTTPConnection(host, int(port), timeout=PROBE_TIMEOUT_SEC)
    try:
        conn.request("GET", "/v1/me", headers={"Authorization": f"Bearer {key}"})
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def file_path() -> Path:
    """Where the hooks find endpoint and key (mm_bus in bin/mm-agent-lib.sh)."""
    return session_file.keyed_path(FILE_PREFIX)


class HookAPI:
    """The server side of RT-6.17: one per MCP server."""

    def __init__(self, get_client: Callable[[], Awaitable[MattermostClient | None]],
                 key: str | None = None) -> None:
        self._get_client = get_client
        self.key = key or secrets.token_urlsafe(32)
        self.endpoint = ""
        self._server: asyncio.Server | None = None
        self._file: Path | None = None
        self._uid = ""
        self._open = 0
        self._heavy = asyncio.Semaphore(MAX_HEAVY_RUNS)
        self._writes = False
        # Another live server owns this workspace's file (RT-6.17 error handling).
        self.conflict: dict[str, Any] | None = None
        # The directory no file is keyed on (/, home or above), while it is the key.
        self.unkeyed = ""
        # The MCP client's name (clientInfo), written into the file.
        self.client = ""

    async def start(self, *, write_file: bool = True) -> str:
        """Listen on 127.0.0.1 and (by default) write the discovery file."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}"
        self._writes = write_file
        if write_file:
            self._write_file()
        return self.endpoint

    def rekey(self) -> None:
        """The workspace is known now (the client's roots arrived): the file
        moves to its name, if another live server does not own that one."""
        if not self._writes or self._server is None or self._file == file_path():
            return
        # The new file first, then the old one goes: a shell never finds neither.
        old, self._file = self._file, None
        try:
            self._write_file(self._uid or None)
        except OSError as exc:
            log.warning("hook API: endpoint file not written: %s", exc)
        if old is not None and old != self._file:
            self._unlink_own(old)

    def note_client(self, name: str) -> None:
        """The MCP client's name from the handshake: into the file, while it is
        this server's, so a second server can say whose file it is."""
        self.client = " ".join(str(name).split())[:64]
        if self._file is None:
            return
        try:
            if session_file.parse(self._file).get("MM_BUS_KEY") == self.key:
                self._write_file(self._uid or None)
        except OSError as exc:
            log.info("hook API: endpoint file not updated: %s", exc)

    def warning(self) -> str:
        """Why the hooks and shell scripts here do not reach this server, or ''."""
        if self.unkeyed:
            return (f"no endpoint file: the workspace would be {self.unkeyed}, which is / or "
                    "the home directory (or above it), so the hooks and shell scripts here "
                    "find no server until the MCP client names its workspace (roots). Set "
                    "MM_WORKSPACE in the server's env or start it in the project (RT-6.17)")
        if not self.conflict:
            return ""
        other = self.conflict.get("client", "")
        who = f"pid {self.conflict['pid']}" + (f", client {other}" if other else "")
        text = (f"another MCP server ({who}) owns {self.conflict['file']}: the hooks and "
                "shell scripts here reach that one, not this one. ")
        if not Path(self.conflict["file"]).name.startswith(f"{FILE_PREFIX}-sid-"):
            return text + ("Run one MCP server per workspace: close the other window or "
                           "instance, then restart this one (RT-6.17)")
        mine = f" (this server's client is {self.client})" if self.client and self.client != other else ""
        return text + (f"It has the same CLAUDE_CODE_SESSION_ID{mine}: a harness started from "
                       "a Claude Code shell inherits it. Start that harness with "
                       "`env -u CLAUDE_CODE_SESSION_ID …` (RT-6.17)")

    def _foreign(self, path: Path) -> dict[str, Any] | None:
        """The file of another server that still runs: a second server in the
        same workspace, or a harness started from a Claude Code shell with its
        session id, must not take the first one's hooks and scripts. A newer
        server of the same session (`claude --continue`) takes the file over:
        the old one is gone by then."""
        data = session_file.parse(path)
        pid = data.get("MM_BUS_PID", "")
        if not data or data.get("MM_BUS_KEY") == self.key or not _pid_alive(pid):
            return None
        if not _answers(data.get("MM_BUS_ENDPOINT", ""), data.get("MM_BUS_KEY", "")):
            return None  # a crash or a stuck orphan left it behind; its pid may be reused
        return {"file": str(path), "pid": int(pid), "client": data.get("MM_BUS_CLIENT", "")}

    def _write_file(self, uid: str | None = None) -> None:
        # The bot's id too (not secret): the prompt hook stops the listener and
        # clears the turn state by it, server or not (RT-6.17 error handling).
        uid = os.environ.get("MM_BOT_USER_ID", "") if uid is None else uid
        self._uid = uid if _UID.fullmatch(uid) else ""
        path = file_path()
        self.unkeyed = session_file.unkeyed()
        if self.unkeyed:
            self.conflict = None
            log.warning("hook API: %s", self.warning())
            return
        self.conflict = self._foreign(path)
        if self.conflict:
            log.warning("hook API: %s", self.warning())
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"MM_BUS_ENDPOINT={self.endpoint}\nMM_BUS_KEY={self.key}\n"
                     f"MM_BUS_PID={os.getpid()}\n"
                     + (f"MM_BOT_USER_ID={self._uid}\n" if self._uid else "")
                     + (f"MM_BUS_CLIENT={self.client}\n" if self.client else ""))
        os.replace(tmp, path)
        self._file = path

    def _note_uid(self, uid: str) -> None:
        """Keep the file's bot id current (a bot registered or taken over after
        start), while the file is still this server's."""
        if self._file is None or uid == self._uid or not _UID.fullmatch(uid):
            return
        try:
            if session_file.parse(self._file).get("MM_BUS_KEY") != self.key:
                return
            self._write_file(uid)
        except OSError as exc:
            log.info("hook API: endpoint file not updated: %s", exc)

    def remove_file(self) -> None:
        """Remove the discovery file while it is still this server's: one that
        took the session over (`claude --continue`) may have written its own."""
        path, self._file = self._file, None
        if path is not None:
            self._unlink_own(path)

    def _unlink_own(self, path: Path) -> None:
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
        if self._open >= MAX_CONNECTIONS:
            writer.close()  # too many at once: no answer, no work
            return
        self._open += 1
        try:
            await self._serve(reader, writer)
        finally:
            self._open -= 1

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
            await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            writer.transport.abort()  # a client that does not read holds nothing
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
            reply = await asyncio.wait_for(self._run(route, client, query, payload), ROUTE_TIMEOUT_SEC)
        except (BadRequest, InvalidId) as exc:  # InvalidId: refused before any request (#81)
            return 400, {"error": str(exc)}
        except Forbidden as exc:
            return 403, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — Mattermost failed: the hook fails open
            log.info("hook API %s %s: %s", method, url.path, type(exc).__name__)
            return 502, {"error": "bus unreachable"}
        if route is me and reply[0] == 200:
            self._note_uid(str(reply[1].get("user_id") or ""))
        return reply

    async def _run(self, route: Route, client: MattermostClient,
                   query: Mapping[str, str], payload: Mapping[str, Any]) -> Reply:
        if route not in _HEAVY_ROUTES:
            return await route(client, query, payload)
        async with self._heavy:  # each makes several Mattermost calls: a few at a time
            return await route(client, query, payload)


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


def _project(client: MattermostClient) -> bool:
    return bool(os.environ.get("MM_PROJECT_CHANNEL_ID") or client.project_channel_name)


async def me(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """Who the bot is, whether it works in a project channel, its crown (RT-6.6)."""
    who = await client.get_me()
    uid = str(who.get("id") or "")
    project = _project(client)
    # channel: the agent confirmed the channel check (RT-6.12); the hooks of a
    # keyless session cannot see the file that says so (RT-6.18).
    return 200, {"user_id": uid, "username": str(who.get("username") or ""),
                 "project": project, "crown_until": lease.crown_until(uid),
                 "channel": bool(_UID.fullmatch(uid)) and channel.confirmed_file(uid).exists()}


async def pending(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """Unread mentions, DMs and posts for all agents (RT-6.2 step 1, unread.py),
    plus a stall event still to collect (RT-6.19); with detail=1 per channel,
    with its fyi posts (RT-6.14)."""
    rows = await unread.channels(client)
    # A stall the server noticed waits in the inbox like a message (RT-6.19).
    waits = 1 if stall.waiting() else 0
    result: dict[str, Any] = {"count": sum(n for _, n in rows) + waits}
    if waits:
        result["stall"] = waits
    if rows and query.get("detail") == "1":
        result["channels"] = await unread.detail(client, rows)
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


async def watched(client: MattermostClient) -> set[str]:
    """The channels the bus watches: the work channel, the project channel and
    the default channel. Never a DM or group channel."""
    ids = {await client.work_channel_id()}
    ids |= {v for v in (os.environ.get("MM_PROJECT_CHANNEL_ID", ""),
                        os.environ.get("MM_CHANNEL_ID", "")) if _UID.fullmatch(v)}
    for name in {getattr(client, "project_channel_name", ""), getattr(client, "channel_name", "")}:
        if name:
            try:
                ids.add(await client.get_channel_id(name))
            except Exception as exc:  # noqa: BLE001 — a channel that is not there
                log.info("hook API: channel %s not resolved: %s", name, type(exc).__name__)
    return ids


async def _channel(client: MattermostClient, chan: str) -> str:
    """The work channel, or the one named (an id, or a name in the bot's team)
    when the bus watches it (watched()): the key reads no DM, no group and no
    other channel the bot is in (arc42 §7.3)."""
    if not chan:
        return await client.work_channel_id()
    if _UID.fullmatch(chan):
        cid = chan
    else:
        name = mm_name(chan, "channel")  # InvalidId: a 400, before any request
        try:
            cid = await client.get_channel_id(name)
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                raise BadRequest(f"no channel {chan} in the team") from exc
            raise
    if cid not in await watched(client):
        raise Forbidden("channel: only the work, project and default channel")
    return cid


async def posts(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """The work channel's posts (or ?channel=), newest first: the last
    ?limit=N, or those since ?since=MS (created or edited). Each says whether
    it is for all agents, an agent's fyi post (RT-6.14) and gated by the
    server (RT-6.16). mm-agent-poll.sh reads them too."""
    since, limit = query.get("since", ""), query.get("limit", "60")
    if not (since or limit).isdigit():
        raise BadRequest("since: epoch ms, limit: a count")
    uid = await _uid(client)
    cid = await _channel(client, query.get("channel", ""))
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
    # Which channel this is, for mm-agent-poll.sh to say: as asked, else the work channel's name.
    return 200, {"channel": query.get("channel", "") or client.work_channel_name,
                 "posts": [_slim(p, agents, gated) for p in raw]}


async def ask_reactions(client: MattermostClient, query: Mapping[str, str],
                        body: Mapping[str, Any]) -> Reply:
    """Reactions to the agent's open asks it has not been handed yet (RT-6.11):
    a reaction raises no unread counter. One the server gated wakes nobody (RT-6.16)."""
    uid = await _uid(client)
    gated = gate.gated(uid)
    fresh = 0
    for pid in asks.open_asks(uid):
        try:
            reactions = await client.reactions(pid)
        except Exception:  # noqa: BLE001 — look again next time
            continue
        for r in reactions:
            who, emoji = str(r.get("user_id") or ""), str(r.get("emoji_name") or "")
            key = asks.seen_key(pid, who, emoji)
            if who != uid and key not in gated and not asks.was_seen(uid, key):
                fresh += 1
    return 200, {"fresh": fresh}


async def presence(client: MattermostClient, query: Mapping[str, str],
                   body: Mapping[str, Any]) -> Reply:
    """Set the status for a turn (RT-6.2, RT-6.3, RT-6.6, lease.py): working,
    idle or bus with the project label, listening until an epoch."""
    state = body.get("state")
    if state not in PRESENCE:
        raise BadRequest("state: working, idle, listening or bus")
    if state == "listening":
        until = body.get("until")
        if type(until) is not int or not 0 < until <= time.time() + MAX_UNTIL_AHEAD_SEC:
            raise BadRequest("until: epoch seconds, within a day")
        arg = str(until)
    else:
        label = body.get("label") or ""
        if not isinstance(label, str):
            raise BadRequest("label: a string")
        arg = label.strip()
        if len(arg) > MAX_LABEL or any(c < " " or c == "\x7f" for c in arg):
            raise BadRequest(f"label: one line, at most {MAX_LABEL} characters")
    await lease.set_status(client, state, arg)
    tools = _tools()
    if state in ("idle", "listening") and tools is not None:
        await tools.permissions_obsolete(client)  # the turn ended (RT-6.13 step 5)
    return 200, {"ok": True}


# The counters the hooks and the listener note (bin/mm-agent-lib.sh mm_stat).
STAT_KINDS = frozenset({"turn", "turn_end", "block", "compact", "wake", "transcript"})
MAX_STAT_FIELDS = 4
MAX_STAT_VALUE = 4096
_FIELD = re.compile(r"[a-z][a-z_]{0,15}")


async def stat(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """One overhead counter for this session (RT-6.9) into the server's own
    stats file. Names and values only, as mm_stat writes them; a transcript
    path is read at session end only where stats.transcript allows."""
    kind = body.get("k")
    if kind not in STAT_KINDS:
        raise BadRequest(f"k: one of {', '.join(sorted(STAT_KINDS))}")
    values = {k: v for k, v in body.items() if k not in ("k", "t")}
    if len(values) > MAX_STAT_FIELDS:
        raise BadRequest(f"at most {MAX_STAT_FIELDS} fields")
    for k, v in values.items():
        if not _FIELD.fullmatch(k) or not (
                (type(v) is int and abs(v) < 2**53)
                or (isinstance(v, str) and len(v) <= MAX_STAT_VALUE and "\x00" not in v)):
            raise BadRequest("fields: short lowercase names, numbers or strings")
    stats.set_user(await _uid(client))
    stats.record(kind, **values)
    return 200, {"ok": True}


POST_FIELDS = ("message", "channel_id", "root_id", "to", "kind")


def _fields(body: Mapping[str, Any], required: str) -> dict[str, str]:
    """The post fields as strings: an unknown field or another type is a 400."""
    unknown = sorted(set(body) - set(POST_FIELDS))
    if unknown:
        raise BadRequest(f"unknown field {unknown[0]}: {', '.join(POST_FIELDS)}")
    out: dict[str, str] = {}
    for name in POST_FIELDS:
        value = body.get(name, "")
        if not isinstance(value, str):
            raise BadRequest(f"{name}: a string")
        out[name] = value
    if not out[required].strip():
        raise BadRequest(f"{required}: required")
    if not (out["message"].strip() or out["kind"].strip()):
        raise BadRequest("message: required (or a kind alone, e.g. IDLE)")
    return out


def _tools() -> Any:
    """The MCP server's module, whose tools post: loaded by the stdio server,
    and by the host process of a keyless session at the first relay
    handshake (RT-6.18), never imported from here."""
    return sys.modules.get(f"{__package__}.server")


async def post_route(client: MattermostClient, query: Mapping[str, str],
                     body: Mapping[str, Any]) -> Reply:
    """post_message for the shell (mm-agent-post.sh): the tool's own code,
    its result, refusals included (RT-6.17)."""
    tools = _tools()
    if tools is None:
        return 503, {"error": "the MCP server is not loaded yet"}
    return 200, await tools.post_as_bot(client, **_fields(body, "message"))


async def reply_route(client: MattermostClient, query: Mapping[str, str],
                      body: Mapping[str, Any]) -> Reply:
    """reply_in_thread for the shell (mm-agent-post.sh --root)."""
    tools = _tools()
    if tools is None:
        return 503, {"error": "the MCP server is not loaded yet"}
    return 200, await tools.reply_as_bot(client, **_fields(body, "root_id"))


def _flag(query: Mapping[str, str], name: str) -> bool:
    value = query.get(name, "0")
    if value not in ("0", "1"):
        raise BadRequest(f"{name}: 0 or 1")
    return value == "1"


async def turn_end(client: MattermostClient, query: Mapping[str, str],
                   body: Mapping[str, Any]) -> Reply:
    """The Stop hook's checks for a turn plugin (RT-6.20; RT-6.2 steps 1–2):
    drain while messages wait (at most turnend.MAX_DRAIN rounds a turn, the
    caller counts them), then a sign-off once a turn, else idle. The caller
    keeps the turn's state; push=1: the plugin wakes the agent between turns,
    so a turn the bus started needs no sign-off. On idle, the listen window
    (step 3) the plugin wakes the agent in: listen_since/listen_until in epoch
    s, absent when there is none; ?listen_since= is the running one's start."""
    drains, since = query.get("drains", "0"), query.get("listen_since", "0")
    if not drains.isdigit():
        raise BadRequest("drains: a count")
    if not since.isdigit():
        raise BadRequest("listen_since: epoch s")
    asked, by_bus, push = (_flag(query, n) for n in ("signoff_asked", "by_bus", "push"))
    if int(drains) < turnend.MAX_DRAIN:
        rows = await unread.channels(client)
        count = sum(n for _, n in rows) + (1 if stall.waiting() else 0)  # as /v1/pending
        if count > 0:
            return 200, turnend.drain(count, await unread.detail(client, rows) if rows else [])
    _, last = await last_post(client, query, body)
    message = str(last.get("message") or "")
    if turnend.needs_signoff(message, asked=asked, by_bus=by_bus, push=push):
        return 200, turnend.signoff(push=push)
    uid = await _uid(client)
    start, until = turnend.listen_window(message, int(last.get("at") or 0), project=_project(client),
                                         coordinator=lease.crown_until(uid) > time.time(),
                                         since=int(since))
    return 200, {"action": "idle", **({"listen_since": start, "listen_until": until} if until else {})}


# The agent's own recent posts (the threads it is in), per client and bot,
# with the own latest post they were read at: an idle session's wake checks
# read them again only once the agent posted or OWN_TTL_SEC passed.
OWN_TTL_SEC = 300
_own_cache: dict[tuple[int, str], tuple[str, float, list[dict[str, Any]]]] = {}


async def _own_posts(client: MattermostClient, uid: str, body: Mapping[str, Any]) -> list[dict[str, Any]]:
    key, mark = (id(client), uid), ownpost.read(uid).get("ID", "")
    hit = _own_cache.get(key)
    if hit and hit[0] == mark and time.monotonic() - hit[1] < OWN_TTL_SEC:
        return hit[2]
    _, recent = await posts(client, {"limit": str(MAX_POSTS)}, body)
    own = [p for p in recent["posts"] if p.get("user_id") == uid]
    _own_cache[key] = (mark, time.monotonic(), own)
    return own


async def wake(client: MattermostClient, query: Mapping[str, str], body: Mapping[str, Any]) -> Reply:
    """The listener's checks for a turn plugin (RT-6.20; RT-6.2 step 4): is
    anything on the bus for the agent since ?since=MS, or a stall event
    (RT-6.19)? Only inside the listen window ending at ?until=S and while its
    gates hold (RT-6.2 step 3). Only looks: the agent collects with
    wait_for_events, which marks read."""
    since, until = query.get("since", ""), query.get("until", "")
    if not since.isdigit():
        raise BadRequest("since: epoch ms")
    if not until.isdigit():
        raise BadRequest("until: epoch s")
    _, last = await last_post(client, query, body)
    if int(until) <= time.time() or not turnend.listens(str(last.get("message") or ""),
                                                         project=_project(client)):
        return 200, turnend.no_wake()
    count = sum(n for _, n in await unread.channels(client))
    waits = 1 if stall.waiting() else 0  # a stall event for the coordinator (RT-6.19)
    if count > 0:
        return 200, turnend.wake_pending(count + waits)
    if waits:
        return 200, turnend.wake_stall()
    _, fresh = await ask_reactions(client, query, body)
    if fresh["fresh"] > 0:
        return 200, turnend.wake_reactions(fresh["fresh"])
    who = await client.get_me()
    uid = str(who.get("id") or "")
    own = await _own_posts(client, uid, body)
    _, new = await posts(client, {"since": since}, body)
    return 200, turnend.wake(new["posts"], own, me=uid,
                             username=str(who.get("username") or ""), since_ms=int(since),
                             coordinator=lease.crown_until(uid) > time.time(),
                             digest_sec=channel.digest_sec())


async def handoff(client: MattermostClient, query: Mapping[str, str],
                  body: Mapping[str, Any]) -> Reply:
    """After a compaction (RT-6.7, RT-6.20): the compaction hook's context."""
    uid = await _uid(client)
    _, got = await posts(client, {"limit": str(MAX_POSTS)}, body)
    return 200, {"context": turnend.handoff(got["posts"], uid)}


_ROUTES: dict[tuple[str, str], Route] = {
    ("GET", "/v1/me"): me,
    ("GET", "/v1/pending"): pending,
    ("GET", "/v1/last-post"): last_post,
    ("GET", "/v1/posts"): posts,
    ("GET", "/v1/ask-reactions"): ask_reactions,
    ("POST", "/v1/presence"): presence,
    ("POST", "/v1/stat"): stat,
    ("POST", "/v1/post"): post_route,
    ("POST", "/v1/reply"): reply_route,
    ("GET", "/v1/turn-end"): turn_end,
    ("GET", "/v1/wake"): wake,
    ("GET", "/v1/handoff"): handoff,
}
# Routes that make several Mattermost calls: at most MAX_HEAVY_RUNS at once.
_HEAVY_ROUTES = frozenset({pending, presence, post_route, reply_route, turn_end, wake, handoff})
