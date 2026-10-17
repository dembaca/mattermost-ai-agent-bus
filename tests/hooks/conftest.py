"""Run the shell hooks against an in-process fake Mattermost + registrar.

Each test gets a fresh server (random port), a private XDG state dir and a
minimal environment, so nothing from the developer's machine leaks in.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ME = "a" * 26
TEAM = "t" * 26
CHANNEL = "c" * 26
PROJECT = "p" * 26


@dataclass
class FakeMattermost:
    posts: list[dict] = field(default_factory=list)  # newest first
    members: list[dict] = field(default_factory=list)
    channels: list[dict] = field(default_factory=list)
    writes: list[tuple[str, str, dict]] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)  # channel members (roster)
    presence: dict[str, str] = field(default_factory=dict)
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
        self.members.append({"channel_id": "d" * 26, "msg_count": 0, "mention_count": 0})
        self.channels.append({"id": "d" * 26, "type": "D", "total_msg_count": n})

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
                return self._send({"id": ME, "username": "agent-test"})
            if p.startswith("/api/v4/teams/name/"):
                return self._send({"id": TEAM})
            if p.startswith(f"/api/v4/teams/{TEAM}/channels/name/"):
                name = p.rsplit("/", 1)[1]
                return self._send({"id": PROJECT if name.startswith("proj") else CHANNEL})
            if p.endswith("/posts"):
                return self._send({"order": [x["id"] for x in fake.posts],
                                   "posts": {x["id"]: x for x in fake.posts}})
            if p.endswith("/channels/members"):
                return self._send(fake.members)
            if p == "/api/v4/users/me/channels":
                return self._send(fake.channels)
            if p == "/api/v4/users":
                return self._send(fake.users)
            return self._send({}, 404)

        def _write(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            body = json.loads(raw) if raw else {}
            p = self.path.split("?")[0]
            fake.writes.append((self.command, p, body))
            if p == "/api/v4/users/status/ids":
                return self._send([{"user_id": u, "status": fake.presence.get(u, "offline")}
                                   for u in body])
            if p == "/register/v1/agents" and self.command == "POST":
                return self._send({"username": f"agent-{body['name']}",
                                   "bot_token": "bot-token-for-tests", "user_id": ME})
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
    pass key=None to drop a variable."""
    state = tmp_path / "state"

    def _run(script: str, *args: str, stdin: str = "{}", **env) -> Result:
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
        p = subprocess.run(["bash", str(ROOT / script), *args], input=stdin, env=base,
                           capture_output=True, text=True, timeout=30)
        return Result(p.returncode, p.stdout, p.stderr)

    _run.state_dir = state / "mm-agent-bus"
    return _run
