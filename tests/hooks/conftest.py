"""Run the shell hooks against an in-process fake Mattermost + registrar.

Each test gets a fresh server (random port), a private XDG state dir and a
minimal environment, so nothing from the developer's machine leaks in. The turn hooks and the scripts an agent runs (listen, post, poll, status) hold
no token: they ask the MCP server's hook API (RT-6.17), which runs here for each
of their runs, in-process, with the run's environment and a real client against
the fake. The server's module is loaded, as in the real process: its tools'
code answers POST /v1/post and /v1/reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Inside a corral session the temporary directory is corral's own
# (/private/tmp/corral-*), which the host scripts refuse as sandbox-writable
# (arc42 §7.1): the tests' trees and PATH entries go next to this checkout.
if os.path.realpath(tempfile.gettempdir()).lower().startswith("/private/tmp/corral-"):
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(ROOT / ".pytest-tmp"))
    (ROOT / ".pytest-tmp").mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / "mcp" / "src"))

from mattermost_ai_agent_bus import hookapi, server  # noqa: E402,F401
from mattermost_ai_agent_bus.client import MattermostAdapter  # noqa: E402

# Scripts that ask the hook API instead of Mattermost: no token in their environment.
API_USERS = ("hooks/", "bin/mm-agent-listen.sh", "bin/mm-agent-post.sh", "bin/mm-agent-poll.sh",
             "bin/mm-agent-status.sh")
ME = "a" * 26
TEAM = "t" * 26
CHANNEL = "c" * 26
PROJECT = "p" * 26
GUEST = "g" * 26
OWNER = "h" * 26          # the human the registration PAT belongs to
PAT = "pat-for-tests"


@dataclass
class FakeMattermost:
    posts: list[dict] = field(default_factory=list)  # newest first
    dm_posts: list[dict] = field(default_factory=list)  # unread_dm's
    members: list[dict] = field(default_factory=list)
    channels: list[dict] = field(default_factory=list)
    writes: list[tuple[str, str, dict]] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)  # channel members (roster)
    presence: dict[str, str] = field(default_factory=dict)
    deny: set[str] = field(default_factory=set)  # write paths answered with 403
    reactions: dict[str, list] = field(default_factory=dict)  # post id → reactions
    bot_description: str | None = None  # the bot's profile; None: not in its user object
    prefix: str = "agent-"  # the registrar's BOT_USERNAME_PREFIX (arc42 §7.1)
    register_reply: dict | None = None  # the registrar's answer instead of a bot
    missing: set[str] = field(default_factory=set)  # channel names answered with 404
    url: str = ""

    def agent(self, uid: str, username: str, create_at: int, emoji: str = "",
              expires_in: int | None = 600, presence: str = "online") -> None:
        """A bot in the work channel; expires_in=None: a status without expiry."""
        status: dict = {}
        if emoji:
            status = {"emoji": emoji, "text": emoji}
            if expires_in is not None:
                status |= {"duration": "date_and_time",
                           "expires_at": time.strftime(
                               "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))}
        self.users = [u for u in self.users if u["id"] != uid] + [{
            "id": uid, "username": username, "create_at": create_at, "delete_at": 0,
            "props": {"customStatus": json.dumps(status)} if status else {}}]
        self.presence[uid] = presence

    def own_post(self, message: str, create_at_ms: int | None = None) -> None:
        at = create_at_ms if create_at_ms is not None else int(time.time() * 1000)
        self.posts.insert(0, {"id": f"p{len(self.posts)}", "user_id": ME,
                              "create_at": at, "message": message})

    def unread_dm(self, n: int) -> None:
        """n posts by the owner in a DM, after the read watermark (RT-6.2)."""
        dm, at = "d" * 26, int(time.time() * 1000)
        self.members = [m for m in self.members if m["channel_id"] != dm]
        self.channels = [c for c in self.channels if c["id"] != dm]
        self.members.append({"channel_id": dm, "msg_count": 0, "mention_count": 0})
        self.channels.append({"id": dm, "type": "D", "total_msg_count": n,
                              "last_post_at": at + n})
        self.dm_posts = [{"id": f"dm{i}", "user_id": OWNER, "channel_id": dm,
                           "create_at": at + i + 1, "message": f"hi {i}"} for i in range(n)]

    def custom_status(self) -> dict | None:
        for method, path, body in reversed(self.writes):
            if path == "/api/v4/users/me/status/custom":
                return body if method == "PUT" else None
        return None


def _handler(fake: FakeMattermost):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/api/v4/users/me":
                if self.headers.get("Authorization") == f"Bearer {PAT}":
                    return self._send({"id": OWNER, "username": "dembaca", "is_bot": False})
                if self.headers.get("Authorization") != "Bearer bot-token-for-tests":
                    return self._send({"message": "invalid token"}, 401)
                return self._send({"id": ME, "username": "agent-test", "is_bot": True})
            if p == f"/api/v4/bots/{ME}":  # the registrar's admin owns the bot
                return self._send({"message": "no permission"}, 403)
            if p == f"/api/v4/users/{ME}":
                if fake.bot_description is None:
                    return self._send({"id": ME, "is_bot": True})
                return self._send({"id": ME, "is_bot": True, "bot_description": fake.bot_description})
            if p.startswith("/api/v4/teams/name/"):
                return self._send({"id": TEAM})
            if p.startswith(f"/api/v4/teams/{TEAM}/channels/name/"):
                name = p.rsplit("/", 1)[1]
                if name in fake.missing:
                    return self._send({"message": "not found"}, 404)
                if name.startswith("guest"):  # a guest channel (RT-6.22)
                    return self._send({"id": GUEST})
                return self._send({"id": PROJECT if name.startswith("proj") else CHANNEL})
            if p.startswith("/api/v4/posts/") and p.endswith("/reactions"):
                return self._send(fake.reactions.get(p.split("/")[4], []))
            if re.fullmatch(r"/api/v4/posts/[a-z0-9]{26}", p):  # a reply's root
                return self._send({"id": p.rsplit("/", 1)[1], "channel_id": CHANNEL})
            if p.endswith("/posts"):
                posts = fake.dm_posts if p == f"/api/v4/channels/{'d' * 26}/posts" else fake.posts
                return self._send({"order": [x["id"] for x in posts],
                                   "posts": {x["id"]: x for x in posts}})
            if p.endswith("/channels/members"):
                return self._send(fake.members)
            if p == "/api/v4/users/me/channels":
                return self._send(fake.channels)
            if p == "/api/v4/users":
                return self._send(fake.users)
            if p.startswith("/api/v4/users/username/"):
                known = {"dembaca": OWNER, "kollegin": "k" * 26}
                name = p.rsplit("/", 1)[1]
                if name in known:
                    return self._send({"id": known[name], "username": name, "is_bot": False})
                if name.startswith(fake.prefix):
                    return self._send({"id": "b" * 26, "username": name, "is_bot": True})
                return self._send({"message": "not found"}, 404)
            return self._send({}, 404)

        def _write(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            body = json.loads(raw) if raw else {}
            p = self.path.split("?")[0]
            fake.writes.append((self.command, p, body))
            if p in fake.deny:
                return self._send({"message": "no permission"}, 403)
            if p == "/api/v4/users/usernames":  # people, for the mention check (RT-6.11)
                return self._send([{"username": n, "is_bot": False} for n in body
                                   if n in ("dembaca", "kollegin")])
            if p == "/api/v4/users/ids":
                return self._send([u for u in fake.users if u["id"] in body])
            if p == "/api/v4/users/status/ids":
                return self._send([{"user_id": u, "status": fake.presence.get(u, "offline")}
                                   for u in body])
            if (p.startswith("/register/v1/agents/") and self.command == "DELETE"
                    and self.headers.get("Authorization") != "Bearer bot-token-for-tests"):
                return self._send({"message": "unauthorized"}, 401)
            if p == "/register/v1/agents" and self.command == "POST" and fake.register_reply:
                return self._send(fake.register_reply)
            if p == "/register/v1/agents" and self.command == "POST":
                return self._send({"username": f"{fake.prefix}{body['name']}",
                                   "bot_token": "bot-token-for-tests", "user_id": ME})
            if p == "/api/v4/posts" and self.command == "POST":
                return self._send({"id": "n" * 26, "user_id": ME, "create_at": 5000, **body})
            self._send({}, 201 if p.endswith("/members") else 200)

        do_PUT = do_POST = do_DELETE = _write

    return H


@pytest.fixture
def mm():
    fake = FakeMattermost()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield fake
    srv.shutdown()


@contextlib.contextmanager
def hook_api(env: dict[str, str], *, write_file: bool = False):
    """The MCP server's hook API (RT-6.17) as it runs under this environment:
    yields MM_BUS_ENDPOINT / MM_BUS_KEY for the hook. The server side reads
    os.environ (state dir, channel ids), so it is the run's for the duration."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        client = MattermostAdapter.from_env()
    except RuntimeError:
        client = None

    async def get_client():
        return client

    api = hookapi.HookAPI(get_client)
    try:
        asyncio.run_coroutine_threadsafe(api.start(write_file=write_file), loop).result(5)
        yield {"MM_BUS_ENDPOINT": api.endpoint, "MM_BUS_KEY": api.key}
    finally:
        asyncio.run_coroutine_threadsafe(api.close(), loop).result(5)
        if client is not None:
            asyncio.run_coroutine_threadsafe(client.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
        os.environ.clear()
        os.environ.update(saved)


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def decision(self) -> dict:
        """The hook's JSON output on stdout ({} when it lets the stop through)."""
        try:
            return json.loads(self.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    @property
    def blocked(self) -> bool:
        return self.code == 0 and self.decision.get("decision") == "block"

    @property
    def passed(self) -> bool:
        """Exited cleanly and let the stop through."""
        return self.code == 0 and not self.blocked

    @property
    def reason(self) -> str:
        return self.decision.get("reason", "")

    @property
    def banner(self) -> str:
        return self.decision.get("systemMessage", "")


@pytest.fixture
def run(mm, tmp_path):
    """run(script, *args, stdin=..., **env) → Result. Bot session env is set by default;
    pass key=None to drop a variable. Hooks and the agent's scripts get the hook
    API (RT-6.17) by env (bus="env"), by its file (bus="file") or none (bus=None).
    fd_token=True: the token on fd 3 instead (MM_BOT_TOKEN_FD), as the corral
    host hooks hand it to mm-agent-status.sh."""
    state = tmp_path / "state"

    def _run(script: str, *args: str, stdin: str = "{}", bus: str | None = "env",
             cwd: str | None = None, fd_token: bool = False, **env) -> Result:
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "XDG_STATE_HOME": str(state),
            "MM_CHAT_URL": mm.url,
            "MM_BOT_TOKEN": "bot-token-for-tests",
            "MM_BOT_USER_ID": ME,
            "MM_TEAM_ID": TEAM,
            "MM_CHANNEL_ID": CHANNEL,
        }
        base.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                base.pop(k, None)
        argv = ["bash", str(ROOT / script), *args]
        if fd_token:
            base["MM_TEST_FD_TOKEN"] = base.pop("MM_BOT_TOKEN", "")
            argv = ["bash", "-c", 'exec 3<<<"$MM_TEST_FD_TOKEN"; unset MM_TEST_FD_TOKEN; '
                    'MM_BOT_TOKEN_FD=3 exec bash "$@"', "_", *argv[1:]]
        with contextlib.ExitStack() as stack:
            if script.startswith(API_USERS) and not fd_token:
                if bus:
                    found = stack.enter_context(hook_api(base, write_file=bus == "file"))
                    if bus == "env":
                        base |= found
                # The token is the MCP server's alone: hooks and listener never see it.
                base = {k: v for k, v in base.items() if k not in ("MM_BOT_TOKEN", "MATTERMOST_TOKEN")}
            p = subprocess.run(argv, input=stdin, env=base,
                               capture_output=True, text=True, timeout=60, cwd=cwd)
        return Result(p.returncode, p.stdout, p.stderr)

    _run.state_dir = state / "mm-agent-bus"
    yield _run
    kill_host_processes(_run.state_dir)


def kill_host_processes(state_dir: Path) -> None:
    """The MCP servers corral's preStart started on the host (RT-6.18) end
    with the test: their watchdog would wait for pytest to end."""
    pids = set()
    for f in [*state_dir.glob("*.env"), *state_dir.glob("*.host.json")]:
        try:
            text = f.read_text()
        except OSError:
            continue
        pids |= {int(p) for p in re.findall(r"(?:MM_BUS_HOST_PID=|\"pid\": ?)(\d+)", text)}
    for pid in pids - {0, 1, os.getpid()}:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):  # a sandbox may deny ps
            cmd = "mattermost_ai_agent_bus --host"   # recorded by this test's own preStart
        if "mattermost_ai_agent_bus --host" in cmd:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
