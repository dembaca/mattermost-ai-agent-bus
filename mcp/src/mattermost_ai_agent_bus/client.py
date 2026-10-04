"""Mattermost REST client + registrar session helpers."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from . import session_file

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def env(name: str, default: str = "") -> str:
    """Read an env var, treating an unexpanded ``${VAR}`` as unset.

    A plugin manifest declares env as ``"MM_PROJECT_CHANNEL": "${MM_PROJECT_CHANNEL}"``.
    When the variable is not set in the environment, the placeholder can arrive
    verbatim — and a literal "${MM_PROJECT_CHANNEL}" is not a channel name.
    """
    value = os.environ.get(name, "")
    if not value or _PLACEHOLDER_RE.match(value):
        return default
    return value


def unique_name(base: str) -> str:
    """Append a registration suffix to a bot name.

    The registrar is not idempotent: a live name answers 409 and its token cannot
    be fetched again, and a name stays taken after the bot is removed. Without a
    suffix a second session on the host is locked out; with a suffix that only
    depends on the session, so is a restart within the same session. Hence: a
    few characters of the Claude Code session id (to trace it back) plus a random
    part (so every registration is new).
    """
    raw = env("CLAUDE_CODE_SESSION_ID") or env("CLAUDE_SESSION_ID")
    sid = re.sub(r"[^a-z0-9]", "", raw.lower())[:5]
    suffix = f"{sid}{secrets.token_hex(2)}"
    return f"{base[: 32 - 1 - len(suffix)].rstrip('-')}-{suffix}"


def ws_url_from_http(http_url: str) -> str:
    """Convert an HTTP(S) Mattermost base URL to a WebSocket API URL."""
    parsed = urlparse(http_url.strip().rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/api/v4"):
        ws_path = f"{path}/websocket"
    else:
        ws_path = f"{path}/api/v4/websocket"
    return urlunparse((scheme, parsed.netloc, ws_path, "", "", ""))


@dataclass
class SessionInfo:
    url: str
    name: str
    username: str
    user_id: str
    bot_token: str


@dataclass
class MattermostClient:
    """Thin async client for registrar + Mattermost API v4."""

    base_url: str
    token: str | None = None
    register_url: str | None = None
    reg_secret: str | None = None
    team_name: str = "agents"
    channel_name: str = "agents"
    project_channel_name: str = ""
    _client: httpx.AsyncClient | None = field(default=None, repr=False, init=False)
    _me: dict[str, Any] | None = field(default=None, repr=False, init=False)
    _team_id: str | None = field(default=None, repr=False, init=False)
    _channel_ids: dict[str, str] = field(
        default_factory=dict, repr=False, init=False
    )
    session: SessionInfo | None = field(default=None, repr=False, init=False)
    session_file_status: dict[str, Any] = field(
        default_factory=dict, repr=False, init=False
    )

    @property
    def work_channel_name(self) -> str:
        """Channel used for posting: the project channel when set, else the default."""
        return self.project_channel_name or self.channel_name

    @classmethod
    def from_env(cls) -> MattermostClient:
        base = (env("MM_CHAT_URL") or env("MATTERMOST_URL")).rstrip("/")
        if not base:
            raise RuntimeError("MM_CHAT_URL or MATTERMOST_URL is required")
        token = env("MM_BOT_TOKEN") or env("MATTERMOST_TOKEN") or None
        reg = env("MM_REGISTER_URL") or f"{base}/register/v1/agents"
        return cls(
            base_url=base,
            token=token,
            register_url=reg.rstrip("/"),
            reg_secret=env("MM_REG_SECRET") or None,
            team_name=env("MM_TEAM", "agents"),
            channel_name=env("MM_CHANNEL", "agents"),
            project_channel_name=env("MM_PROJECT_CHANNEL"),
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self, bearer: str | None = None) -> dict[str, str]:
        tok = bearer if bearer is not None else self.token
        if not tok:
            raise RuntimeError("bot token required (session_start or MM_BOT_TOKEN)")
        return {"Authorization": f"Bearer {tok}"}

    async def _api(
        self,
        method: str,
        path: str,
        *,
        bearer: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        resp = await self._http().request(
            method,
            url,
            headers=self._auth_headers(bearer),
            json=json,
            params=params,
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def session_start(
        self, name: str, display_name: str | None = None, *, unique: bool = True
    ) -> SessionInfo:
        """POST registrar → set token / session fields.

        With ``unique`` (default) a session suffix is appended to ``name``.
        """
        if not self.reg_secret:
            raise RuntimeError("MM_REG_SECRET is required for session_start")
        if not self.register_url:
            raise RuntimeError("MM_REGISTER_URL is required for session_start")
        name = name.strip().lower()
        if unique and _NAME_RE.match(name) and len(name) >= 3:
            name = unique_name(name)
        if not _NAME_RE.match(name) or not (3 <= len(name) <= 32):
            raise ValueError("name must be 3–32 lowercase alnum/hyphen")
        display = (display_name or name).strip()
        resp = await self._http().post(
            self.register_url,
            headers={
                "Authorization": f"Bearer {self.reg_secret}",
                "Content-Type": "application/json",
            },
            json={"name": name, "display_name": display},
        )
        resp.raise_for_status()
        data = resp.json()
        info = SessionInfo(
            url=(data.get("url") or self.base_url).rstrip("/"),
            name=name,
            username=data["username"],
            user_id=data["user_id"],
            bot_token=data["bot_token"],
        )
        self.base_url = info.url
        self.token = info.bot_token
        self.session = info
        self._me = None
        self._team_id = None
        self._channel_ids.clear()
        os.environ["MM_CHAT_URL"] = info.url
        os.environ["MATTERMOST_URL"] = info.url
        os.environ["MM_BOT_NAME"] = info.name
        os.environ["MM_BOT_USERNAME"] = info.username
        os.environ["MM_BOT_USER_ID"] = info.user_id
        os.environ["MM_BOT_TOKEN"] = info.bot_token
        os.environ["MATTERMOST_TOKEN"] = info.bot_token
        self.session_file_status: dict[str, Any]
        try:
            path = session_file.write(info, self.team_name, self.channel_name)
            self.session_file_status = {"path": str(path), "written": True}
        except OSError as exc:
            # The MCP tools do not need the file, but the hooks do — say so
            # instead of leaving them silently inert.
            self.session_file_status = {
                "path": str(session_file.session_file_path()),
                "written": False,
                "error": f"{type(exc).__name__}: {exc.strerror or exc}",
            }
        self.session_file_status["project_dir"] = session_file.project_dir()
        return info

    async def finish_session_setup(self) -> dict[str, Any]:
        """What session_start leaves undone: join the project channel, empty the inbox.

        Best effort — the bot is already registered, so a failure is reported,
        never raised. The registrar only makes the bot a member of MM_CHANNEL;
        without the join, posting to the project channel is a 403 even though
        reading it works.
        """
        result: dict[str, Any] = {}
        name = self.project_channel_name
        if name and self.session:
            try:
                cid = await self.get_channel_id(name)
                # No membership pre-check: a non-member cannot read the member
                # list (403, not 404). Joining twice is harmless.
                await self._api(
                    "POST",
                    f"/api/v4/channels/{cid}/members",
                    json={"user_id": self.session.user_id},
                )
                result["project_channel"] = {"name": name, "id": cid, "joined": True}
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                reason = {
                    404: f"channel '{name}' does not exist in team '{self.team_name}' "
                    "(check the name; this server never creates channels)",
                    403: f"cannot join '{name}': private channel (a human must add "
                    "the bot) or joining is denied on this instance",
                }.get(code, f"HTTP {code}")
                result["project_channel"] = {"name": name, "joined": False, "error": reason}
        # A fresh bot is born with a welcome DM from itself that nothing can
        # drain; mark everything read so the Stop hook starts from empty.
        try:
            for ch in await self.unread_channels():
                await self.view_channel(ch["channel_id"])
        except (httpx.HTTPError, KeyError):
            pass
        return result

    async def session_end(self, name: str | None = None) -> None:
        """DELETE registrar agent. Uses reg secret or bot token."""
        short = name or (self.session.name if self.session else os.environ.get("MM_BOT_NAME"))
        if not short:
            raise RuntimeError("no agent name; pass name or call session_start first")
        short = short.removeprefix("agent-")
        if not self.register_url:
            raise RuntimeError("MM_REGISTER_URL is required for session_end")
        bearer = self.reg_secret or self.token
        if not bearer:
            raise RuntimeError("MM_REG_SECRET or bot token required for session_end")
        resp = await self._http().delete(
            f"{self.register_url}/{short}",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
        self.session = None
        self.token = None
        self._me = None
        session_file.remove()
        for key in (
            "MM_BOT_NAME",
            "MM_BOT_USERNAME",
            "MM_BOT_USER_ID",
            "MM_BOT_TOKEN",
            "MATTERMOST_TOKEN",
        ):
            os.environ.pop(key, None)

    def shutdown_sync(self) -> str:
        """Unregister the bot this process is responsible for, synchronously.

        Runs from the process exit handler, where no event loop is available.
        Session bots are ephemeral by design, so this covers both our own
        ``session_start`` and a bot the host registered and handed over via
        MM_BOT_TOKEN; only ``agent-*`` bots are touched, never a human. Best
        effort, never raises; returns a short outcome for the log.
        """
        if not self.token or not self.register_url:
            return "no session"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            with httpx.Client(timeout=5.0) as http:
                if self.session:
                    short = self.session.name
                else:
                    me = http.get(f"{self.base_url}/api/v4/users/me", headers=headers)
                    me.raise_for_status()
                    user = me.json()
                    username = user.get("username", "")
                    if not username.startswith("agent-"):
                        return f"{username!r} is not an agent bot; left alone"
                    short = username.removeprefix("agent-")
                    # Gone before unregistered, so a failed delete does not
                    # leave a bot that looks available.
                    http.put(
                        f"{self.base_url}/api/v4/users/{user['id']}/status",
                        headers=headers,
                        json={"user_id": user["id"], "status": "offline"},
                    )
                resp = http.delete(
                    f"{self.register_url}/{short}",
                    headers={"Authorization": f"Bearer {self.reg_secret or self.token}"},
                )
                ok = resp.status_code in (200, 204, 404)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return f"unregister failed: {type(exc).__name__}"
        finally:
            self.session = None
            self.token = None
        if ok:
            session_file.remove()
            return f"unregistered {short}"
        return f"unregister failed: HTTP {resp.status_code}"

    async def get_me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = await self._api("GET", "/api/v4/users/me")
        return self._me

    async def get_team_id(self, team_name: str | None = None) -> str:
        name = team_name or self.team_name
        if self._team_id and name == self.team_name:
            return self._team_id
        data = await self._api("GET", f"/api/v4/teams/name/{name}")
        tid = data["id"]
        if name == self.team_name:
            self._team_id = tid
        return tid

    async def get_channel_id(
        self, channel_name: str | None = None, team_name: str | None = None
    ) -> str:
        """Resolve a channel name to its id.

        Defaults to the work channel (project channel when set, else MM_CHANNEL).
        Never creates a channel: a missing one surfaces as the API's 404.
        """
        cname = channel_name or self.work_channel_name
        key = f"{team_name or self.team_name}/{cname}"
        cached = self._channel_ids.get(key)
        if cached:
            return cached
        tid = await self.get_team_id(team_name)
        data = await self._api(
            "GET", f"/api/v4/teams/{tid}/channels/name/{cname}"
        )
        cid = data["id"]
        self._channel_ids[key] = cid
        return cid

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        return await self._api("GET", f"/api/v4/channels/{channel_id}")

    async def unread_channels(self) -> list[dict[str, Any]]:
        """Channels with something pending, each with the watermark to read from.

        Returns [{channel_id, type, last_viewed_at, pending}]. A DM counts
        unread messages (total minus the watermark); elsewhere only an explicit
        mention means someone wants this agent.

        Note `member.msg_count` is the watermark, not a pending count — unread
        is channel.total_msg_count - member.msg_count.
        """
        tid = await self.get_team_id()
        members = await self._api(
            "GET", f"/api/v4/users/me/teams/{tid}/channels/members"
        )
        channels = await self._api("GET", "/api/v4/users/me/channels")
        by_id = {c["id"]: c for c in (channels or [])}
        out: list[dict[str, Any]] = []
        for member in members or []:
            cid = member.get("channel_id")
            channel = by_id.get(cid) or {}
            ctype = channel.get("type") or "O"
            if ctype in ("D", "G"):
                pending = (channel.get("total_msg_count") or 0) - (
                    member.get("msg_count") or 0
                )
            else:
                pending = member.get("mention_count") or 0
            if pending > 0:
                out.append(
                    {
                        "channel_id": cid,
                        "type": ctype,
                        "last_viewed_at": member.get("last_viewed_at") or 0,
                        "pending": pending,
                    }
                )
        return out

    async def view_channel(self, channel_id: str) -> None:
        """Mark a channel read for this bot.

        WebSocket delivery does not clear Mattermost's unread counters, so
        without this the counts only ever grow — and anything that asks "am I
        being addressed?" by reading them would answer yes forever.
        """
        await self._api(
            "POST",
            "/api/v4/channels/members/me/view",
            json={"channel_id": channel_id},
        )

    async def post(
        self,
        message: str,
        *,
        channel_id: str | None = None,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        cid = channel_id or await self.get_channel_id()
        body: dict[str, Any] = {"channel_id": cid, "message": message}
        if root_id:
            body["root_id"] = root_id
        return await self._api("POST", "/api/v4/posts", json=body)

    async def thread(self, post_id: str) -> dict[str, Any]:
        """Return post + ordered replies for a thread root (or any post id)."""
        data = await self._api("GET", f"/api/v4/posts/{post_id}/thread")
        posts = data.get("posts") or {}
        order = data.get("order") or sorted(
            posts.keys(), key=lambda pid: posts[pid].get("create_at", 0)
        )
        return {
            "root_id": data.get("root_id") or post_id,
            "order": order,
            "posts": [posts[pid] for pid in order if pid in posts],
        }

    async def list_recent(
        self,
        *,
        channel_id: str | None = None,
        per_page: int = 20,
        since_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        cid = channel_id or await self.get_channel_id()
        params: dict[str, Any] = {"per_page": per_page}
        if since_ms is not None:
            params["since"] = since_ms
        data = await self._api(
            "GET", f"/api/v4/channels/{cid}/posts", params=params
        )
        posts = data.get("posts") or {}
        return sorted(posts.values(), key=lambda p: p.get("create_at", 0))
