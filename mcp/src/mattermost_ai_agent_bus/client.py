"""The Mattermost adapter (arc42 §5.1): MattermostAdapter implements the
chat port (port.ChatPort) over Mattermost's REST API, and holds the registrar
session helpers, which stay outside the port."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import httpx

from . import a2a, links, ownpost, profile, session_file, watermark
from .model import Channel, ChannelUser, Me, Post, Reaction, Thread, UnreadChannel

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)


def from_webhook(post: Mapping[str, Any]) -> bool:
    """Anyone with a hook's URL posts as the account that made the hook."""
    return str((post.get("props") or {}).get("from_webhook", "")) == "true"


def _int(value: object) -> int:
    if isinstance(value, int | float):
        return int(value)
    try:
        return int(str(value or 0))
    except ValueError:
        return 0


def post_of(raw: Mapping[str, Any]) -> Post:
    """The bus's Post from Mattermost's post JSON (arc42 §5.1): the adapter
    reads the keys and props here, nobody else."""
    props = raw.get("props")
    props = MappingProxyType(dict(props)) if isinstance(props, Mapping) else MappingProxyType({})
    meta = raw.get("metadata")
    reactions = meta.get("reactions") if isinstance(meta, Mapping) else None
    return Post(
        id=str(raw.get("id") or ""),
        channel_id=str(raw.get("channel_id") or ""),
        root_id=str(raw.get("root_id") or ""),
        user_id=str(raw.get("user_id") or ""),
        create_at=_int(raw.get("create_at")),
        edit_at=_int(raw.get("edit_at")),
        delete_at=_int(raw.get("delete_at")),
        message=str(raw.get("message") or ""),
        type=str(raw.get("type") or ""),
        reply_count=_int(raw.get("reply_count")),
        from_webhook=from_webhook(raw),
        kind=a2a.kind_of(raw),
        intent=a2a.intent_of(raw),
        to=a2a.ALL if a2a.to_all(raw) else "",
        pr=a2a.pr_of(raw),
        branch=a2a.branch_of(raw),
        crowned=a2a.crowned(raw),
        reactions=tuple(reaction_of(r) for r in reactions or () if isinstance(r, Mapping)),
        props=props,
        raw=MappingProxyType(dict(raw)),
    )


def reaction_of(raw: Mapping[str, Any]) -> Reaction:
    """The bus's Reaction from Mattermost's reaction JSON."""
    return Reaction(user_id=str(raw.get("user_id") or ""), post_id=str(raw.get("post_id") or ""),
                    emoji_name=str(raw.get("emoji_name") or ""), create_at=_int(raw.get("create_at")))


def me_of(raw: Mapping[str, Any]) -> Me:
    """The bus's Me from users/me."""
    return Me(id=str(raw.get("id") or ""), username=str(raw.get("username") or ""),
              raw=MappingProxyType(dict(raw)))


def channel_of(raw: Mapping[str, Any]) -> Channel:
    """The bus's Channel from Mattermost's channel JSON."""
    return Channel(id=str(raw.get("id") or ""), name=str(raw.get("name") or ""),
                   type=str(raw.get("type") or ""), raw=MappingProxyType(dict(raw)))


def _custom_status(user: Mapping[str, Any]) -> dict[str, Any]:
    """The custom status a user JSON carries in its props, {} without one."""
    props = user.get("props")
    raw = props.get("customStatus") if isinstance(props, Mapping) else None
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def channel_user_of(raw: Mapping[str, Any]) -> ChannelUser:
    """The bus's ChannelUser from Mattermost's user JSON. Deleted unless
    delete_at is 0, as the roster has always read it."""
    return ChannelUser(id=str(raw.get("id") or ""), username=str(raw.get("username") or ""),
                       create_at=_int(raw.get("create_at")), deleted=raw.get("delete_at") != 0,
                       custom_status=MappingProxyType(_custom_status(raw)))


def with_links(post: Post, post_url: str) -> Post:
    """The post with the bus's permalinks as bare ids and odd links written
    out (RT-6.15), as the client's read calls give it."""
    return post_of(read_links(dict(post.raw), post_url))


def read_links(post: dict[str, Any], post_url: str) -> dict[str, Any]:
    """links.read_post on a post's JSON, its attachments' text included (RT-6.15)."""
    post = links.read_post(post, post_url)
    if isinstance(post.get("props"), Mapping):
        post["props"] = attachment_texts(post["props"], lambda s: links.read(s, post_url=post_url))
    return post


def attachment_texts(props: Mapping[str, Any], edit: Callable[[str], str]) -> dict[str, Any]:
    """props with `edit` applied to what a human reads in its message
    attachments: pretext, text and the fields' values (RT-6.15). Titles and
    the fallback are the server's own plain text."""
    out = dict(props)
    cards = out.get("attachments")
    if not isinstance(cards, list):
        return out
    edited: list[Any] = []
    for card in cards:
        if isinstance(card, Mapping):
            card = dict(card)
            for key in ("pretext", "text"):
                if isinstance(card.get(key), str):
                    card[key] = edit(card[key])
            if isinstance(card.get("fields"), list):
                card["fields"] = [{**f, "value": edit(f["value"])}
                                  if isinstance(f, Mapping) and isinstance(f.get("value"), str) else f
                                  for f in card["fields"]]
        edited.append(card)
    out["attachments"] = edited
    return out


def attachment_props(fields: list[tuple[str, str, bool]], *, fallback: str,
                     urgent: bool) -> dict[str, Any]:
    """Props for a message attachment: a card of (title, value, short) fields,
    red when urgent, else amber."""
    return {"attachments": [{
        "color": "#d24b4e" if urgent else "#e8a317",
        "fallback": fallback,
        "fields": [{"title": t, "value": v, "short": s} for t, v, s in fields],
    }]}


def _pid_alive(pid: str) -> bool:
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


_ID = re.compile(r"[a-z0-9]{26}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class InvalidId(ValueError):
    """An id or name that must not go into a Mattermost API path (#81)."""


class Refused(Exception):
    """Mattermost refused a post; the text says why, for the agent."""


def _refused(exc: httpx.HTTPStatusError) -> Refused:
    try:
        why = str(exc.response.json().get("message") or "")
    except ValueError:
        why = ""
    return Refused(f"Mattermost refused the post ({exc.response.status_code})" + (f": {why}" if why else ""))


def mm_id(value: object, what: str) -> str:
    """`value` if it is a Mattermost id (26 lowercase letters and digits), else
    InvalidId: nothing an agent or hook passes reaches an API path unchecked."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvalidId(f"invalid {what} id")
    return value


def mm_name(value: object, what: str) -> str:
    """`value` if it is a safe team, channel, user or agent name for an API
    path: letters, digits, '.', '_', '-', no '..', at most 64; else InvalidId.
    A DM channel's name "<id>__<id>" passes."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise InvalidId(f"invalid {what} name")
    return value


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


def _registration_fields(name: str, display: str) -> dict[str, Any]:
    """Registration body: who the agent answers to goes along (RT-6.8), so the
    registrar can put it into the bot's profile. Unknown fields are ignored."""
    trusted = [n.strip().lstrip("@") for n in
               (env("MM_TRUSTED_HUMANS") or env("MM_OWNER")).split(",") if n.strip()]
    responsible = env("MM_RESPONSIBLE_HUMAN").strip().lstrip("@") or (trusted[0] if trusted else "")
    body: dict[str, Any] = {
        "name": name, "display_name": display,
        "kind": "autonomous" if env("MM_AGENT_KIND") == "autonomous" else "session",
    }
    if responsible:
        body["responsible"] = responsible
    if trusted:
        body["trusted"] = trusted
    return body


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

    @property
    def prefix(self) -> str:
        """The registrar's username prefix, learned from this registration."""
        return session_file.agent_prefix(
            {"MM_BOT_USERNAME": self.username, "MM_REG_BOT_NAME": self.name})


@dataclass
class MattermostAdapter:
    """The chat port for Mattermost (API v4), plus the registrar session:
    registering, adopting and ending the bot, which is no port operation."""

    base_url: str
    token: str | None = None
    register_url: str | None = None
    reg_secret: str | None = None
    team_name: str = "agents"
    channel_name: str = "agents"
    project_channel_name: str = ""
    _client: httpx.AsyncClient | None = field(default=None, repr=False, init=False)
    _me: Me | None = field(default=None, repr=False, init=False)
    # Bumped whenever the identity changes (_take, session_end): a get_me that
    # started before must not cache the old bot's answer.
    _me_gen: int = field(default=0, repr=False, init=False)
    _team_id: str | None = field(default=None, repr=False, init=False)
    _channel_ids: dict[str, str] = field(
        default_factory=dict, repr=False, init=False
    )
    _channels: dict[str, Channel] = field(
        default_factory=dict, repr=False, init=False
    )
    _usernames: dict[str, str] = field(default_factory=dict, repr=False, init=False)
    _bots: set[str] = field(default_factory=set, repr=False, init=False)
    _is_human: dict[str, bool] = field(default_factory=dict, repr=False, init=False)
    session: SessionInfo | None = field(default=None, repr=False, init=False)
    _trusted: dict[str, str] | None = field(default=None, repr=False, init=False)
    profile_warning: str = field(default="", repr=False, init=False)
    trusted_source: str = field(default="", repr=False, init=False)
    session_file_status: dict[str, Any] = field(
        default_factory=dict, repr=False, init=False
    )
    # The bot was taken over from this session's file, not registered here.
    adopted: bool = field(default=False, repr=False, init=False)
    # Set by session_start: an existing bot was returned instead of a new one;
    # bots of long-ended sessions it unregistered first.
    reused: bool = field(default=False, repr=False, init=False)
    reaped: list[str] = field(default_factory=list, repr=False, init=False)

    @property
    def keeps_bot_on_exit(self) -> bool:
        """A bot of our own, kept for `claude --continue` (same session id) or,
        without one, for the workspace's next server unless the agent's last
        post signed off with BYE (RT-6.10)."""
        if self.session is None:
            return False
        if session_file.session_id():
            return True
        return (session_file.keyed_on_workspace()
                and bool(self.token)
                and session_file.parse(session_file.session_file_path()).get("MM_BOT_TOKEN")
                == self.token
                and a2a.word(ownpost.read(self.session.user_id).get("MSG", "")) != "BYE")

    @property
    def bot_id(self) -> str:
        """The bot's user id as far as it is known without a request."""
        if self.session:
            return self.session.user_id
        return (self._me.id if self._me else "") or env("MM_BOT_USER_ID")

    @property
    def work_channel_name(self) -> str:
        """Channel used for posting: the project channel when set, else the default."""
        return self.project_channel_name or self.channel_name

    @classmethod
    def from_env(cls) -> MattermostAdapter:
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
        json: dict[str, Any] | list[str] | None = None,
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
        # Already this session's bot (taken over after `claude --continue`, or a
        # second call): registering again would abandon it (RT-6.10).
        if self.session is not None and self.token:
            try:
                await self._api("GET", "/api/v4/users/me")  # uncached: still alive?
                self.reused = True
                return self.session
            except httpx.HTTPStatusError:
                pass  # deleted meanwhile: register a new one
        self.reused = False
        self.reaped = await self.reap_stale_sessions()
        name = name.strip().lower()
        if unique and _NAME_RE.match(name) and len(name) >= 3:
            name = unique_name(name)
        if not _NAME_RE.match(name) or not (3 <= len(name) <= 32):
            raise ValueError("name must be 3–32 lowercase alnum/hyphen")
        display = (display_name or name).strip()
        fields = _registration_fields(name, display)
        resp = await self._http().post(
            self.register_url,
            headers={
                "Authorization": f"Bearer {self.reg_secret}",
                "Content-Type": "application/json",
            },
            json=fields,
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
        self._take(info)
        self.profile_warning = await self._profile_warning(info, fields.get("trusted", []))
        return info

    async def _profile_warning(self, info: SessionInfo, sent: list[str]) -> str:
        """Does the profile the registrar wrote carry every trusted name sent
        (RT-6.8)? A bot's user object carries its description as
        `bot_description`; `/bots/<id>` is the second try, which its owner may
        read. '' when it does, or when neither can be read; never fails."""
        if not sent:
            return ""
        description: str | None = None
        try:
            mm_id(info.user_id, "user")
        except InvalidId:
            return ""
        for path, key in ((f"/api/v4/users/{info.user_id}", "bot_description"),
                          (f"/api/v4/bots/{info.user_id}", "description")):
            try:
                data = await self._api("GET", path)
            except Exception:  # noqa: BLE001 — not readable: try the next
                continue
            if isinstance(data, dict) and isinstance(data.get(key), str):
                description = data[key]
                break
        if description is None:
            log.info("profile not readable: trusted names not compared")
            return ""
        text = profile.warning(info.username, sent, description)
        if text:
            log.warning("trusted list vs profile: %s", text)
        return text

    def _take(self, info: SessionInfo) -> None:
        """Make ``info`` this process's bot and hand it to the hooks."""
        self.base_url = info.url
        self.token = info.bot_token
        self.session = info
        self._me = None
        self._me_gen += 1
        self._team_id = None
        self._channel_ids.clear()
        os.environ["MM_CHAT_URL"] = info.url
        os.environ["MATTERMOST_URL"] = info.url
        os.environ["MM_REG_BOT_NAME"] = info.name
        os.environ["MM_BOT_USERNAME"] = info.username
        os.environ["MM_AGENT_PREFIX"] = info.prefix
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

    def adopt_sync(self) -> SessionInfo | None:
        """Take over the bot of the server that ended (RT-6.10): this Claude
        Code session's after `claude --continue`, else this workspace's after
        a harness restarted its server (session_file.read).

        Taken over only while its token is still accepted; a rejected one
        means the bot is gone, so the file goes.
        """
        if self.token:
            return None
        data = session_file.read()
        if not data:
            return None
        url = (data.get("MM_CHAT_URL") or self.base_url).rstrip("/")
        try:
            with httpx.Client(timeout=5.0) as http:
                me = http.get(f"{url}/api/v4/users/me",
                              headers={"Authorization": f"Bearer {data['MM_BOT_TOKEN']}"})
            if me.status_code == 401:
                session_file.remove()
                return None
        except httpx.HTTPError:
            pass  # unreachable now; the token is most likely still good
        info = SessionInfo(url=url, name=data["MM_REG_BOT_NAME"],
                           username=data.get("MM_BOT_USERNAME", ""),
                           user_id=data.get("MM_BOT_USER_ID", ""),
                           bot_token=data["MM_BOT_TOKEN"])
        self._take(info)  # rewrites the file: MM_SESSION_PID is this server's parent now
        self.adopted = True
        return info

    async def reap_stale_sessions(self) -> list[str]:
        """Unregister bots whose MCP-registered session ended long ago (RT-6.10).

        A server keeps its bot when it ends, for `claude --continue` or the
        workspace's next server; nothing tells a resume from a real end. A
        session file whose Claude process is gone and that was untouched for
        MM_SESSION_KEEP_SEC (default 24 h) is taken as ended, and so is one
        whose server marked its end that long ago (its harness may live on):
        its bot is deleted with its own token, the file removed. So is the bot
        this workspace's ended server left, which a new registration here
        would otherwise orphan.
        """
        keep = session_file.keep_sec()
        reaped: list[str] = []
        now = time.time()
        own = session_file.session_file_path()
        paths = session_file.others()
        if session_file.keyed_on_workspace() and session_file.ENDED in session_file.parse(own):
            paths.append(own)
        for path in paths:
            data = session_file.parse(path)
            name, token = data.get("MM_REG_BOT_NAME"), data.get("MM_BOT_TOKEN")
            ended = session_file.ended_at(data)
            if not name or not token:
                continue
            if path != own and (now - ended < keep if ended is not None
                                else _pid_alive(data.get("MM_SESSION_PID", ""))):
                continue
            try:
                if path != own and ended is None and now - path.stat().st_mtime < keep:
                    continue
                short = mm_name(name.removeprefix(session_file.agent_prefix(data)), "agent")
                resp = await self._http().delete(
                    f"{self.register_url}/{short}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except (OSError, httpx.HTTPError, InvalidId) as exc:
                log.info("stale session %s left: %s", name, exc)
                continue
            # 401: the token is dead, so the bot is gone already.
            if resp.status_code in (200, 204, 401, 404):
                path.unlink(missing_ok=True)
                reaped.append(name)
        return reaped

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
                    f"/api/v4/channels/{mm_id(cid, 'channel')}/members",
                    json={"user_id": self.session.user_id},
                )
                result["project_channel"] = {"name": name, "id": cid, "joined": True}
            except InvalidId as exc:
                result["project_channel"] = {"name": name, "joined": False, "error": str(exc)}
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                reason = {
                    404: f"channel '{name}' does not exist in team '{self.team_name}' "
                    "(check the name; this server never creates channels)",
                    403: f"cannot join '{name}': private channel (a human must add "
                    "the bot) or joining is denied on this instance",
                }.get(code, f"HTTP {code}")
                result["project_channel"] = {"name": name, "joined": False, "error": reason}
        # A fresh bot is born with a welcome DM from itself, which Mattermost's
        # UI shows unread. The bus never counts it: an own post (RT-6.2).
        try:
            for ch in await self.member_channels():
                if ch.pending or ch.unread:
                    await self.view_channel(ch.channel_id)
        except (httpx.HTTPError, KeyError, InvalidId):
            pass
        return result

    async def session_end(self, name: str | None = None) -> None:
        """DELETE registrar agent. Uses reg secret or bot token."""
        short = name or (self.session.name if self.session else os.environ.get("MM_REG_BOT_NAME"))
        if not short:
            raise RuntimeError("no agent name; pass name or call session_start first")
        short = mm_name(short.removeprefix(session_file.agent_prefix()), "agent")
        uid = self.bot_id
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
        self._me_gen += 1
        session_file.remove()
        ownpost.remove(uid)
        watermark.remove(uid)
        for key in (
            "MM_REG_BOT_NAME",
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
        MM_BOT_TOKEN; only ``agent-*`` bots (the registrar's prefix,
        session_file.agent_prefix) are touched, never a human. Best
        effort, never raises; returns a short outcome for the log.
        """
        if not self.token or not self.register_url:
            return "no session"
        headers = {"Authorization": f"Bearer {self.token}"}
        uid = self.bot_id
        if self.keeps_bot_on_exit and self.session is not None and (
                session_file.session_id() or session_file.mark_ended(self.token)):
            # Kept for `claude --continue`, which reuses the session id, or for
            # the workspace's next server (the mark): it takes the bot over
            # (adopt_sync). The file's mtime starts the grace period after
            # which another session's start reaps it. Claude Code kills the
            # server ~0.5 s after asking it to stop, so only the quick, local
            # part is certain; going offline is best effort.
            session_file.touch()
            name = self.session.name
            how = "claude --continue" if session_file.session_id() else "the workspace's next server"
            try:
                with httpx.Client(timeout=0.3) as http:
                    http.put(f"{self.base_url}/api/v4/users/{mm_id(self.session.user_id, 'user')}/status",
                             headers=headers,
                             json={"user_id": self.session.user_id, "status": "offline"})
            except (httpx.HTTPError, InvalidId):
                pass
            self.session = None
            self.token = None
            return f"kept {name} for {how}"
        try:
            with httpx.Client(timeout=5.0) as http:
                if self.session:
                    short = self.session.name
                else:
                    me = http.get(f"{self.base_url}/api/v4/users/me", headers=headers)
                    me.raise_for_status()
                    user = me.json()
                    username = user.get("username", "")
                    prefix = session_file.agent_prefix()
                    if not username.startswith(prefix):
                        return f"{username!r} is not an agent bot; left alone"
                    short = username.removeprefix(prefix)
                    # Gone before unregistered, so a failed delete does not
                    # leave a bot that looks available.
                    http.put(
                        f"{self.base_url}/api/v4/users/{mm_id(user['id'], 'user')}/status",
                        headers=headers,
                        json={"user_id": user["id"], "status": "offline"},
                    )
                resp = http.delete(
                    f"{self.register_url}/{mm_name(short, 'agent')}",
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
            ownpost.remove(uid)
            watermark.remove(uid)
            return f"unregistered {short}"
        return f"unregister failed: HTTP {resp.status_code}"

    async def trusted_humans(self) -> dict[str, str]:
        """The humans whose posts may approve actions (RT-6.8): {user_id: username}.

        MM_TRUSTED_HUMAN_USER_IDS with MM_TRUSTED_HUMANS (set by host hooks), else
        the usernames in MM_TRUSTED_HUMANS looked up with the bot token. MM_OWNER
        and MM_OWNER_USER_ID (one human) are read as well. Nothing is inferred:
        MM_REG_SECRET is the registrar's shared secret, nobody's personal token.
        Cached; {} when none.
        """
        if self._trusted is not None:
            return self._trusted

        def names(var: str) -> list[str]:
            return [n.strip().lstrip("@") for n in env(var).split(",") if n.strip()]

        trusted: dict[str, str] = {}
        source = ""
        ids = names("MM_TRUSTED_HUMAN_USER_IDS") or names("MM_OWNER_USER_ID")
        wanted = names("MM_TRUSTED_HUMANS") or names("MM_OWNER")
        try:
            if ids:
                trusted = {i: (wanted[k] if k < len(wanted) else "") for k, i in enumerate(ids)}
                source = "host"
            elif wanted:
                for n in wanted:
                    try:
                        u = await self._api("GET", f"/api/v4/users/username/{mm_name(n, 'user')}")
                    except (httpx.HTTPError, InvalidId):
                        continue
                    if not u.get("is_bot"):
                        trusted[u["id"]] = n
                source = "MM_TRUSTED_HUMANS"
        except (httpx.HTTPError, KeyError, ValueError):
            trusted = {}
        self._trusted = trusted
        self.trusted_source = source if trusted else (
            "none: set MM_TRUSTED_HUMANS")
        return trusted

    @property
    def permalink_base(self) -> str:
        """A post's permalink is this plus its id (RT-6.15)."""
        return f"{self.base_url}/{self.team_name}/pl/"

    async def get_me(self) -> Me:
        while self._me is None:
            gen = self._me_gen
            me = me_of(await self._api("GET", "/api/v4/users/me"))
            if gen == self._me_gen:  # else the bot changed meanwhile: ask again as the new one
                self._me = me
                return me
        return self._me

    async def get_team_id(self, team_name: str | None = None) -> str:
        name = team_name or self.team_name
        if self._team_id and name == self.team_name:
            return self._team_id
        data = await self._api("GET", f"/api/v4/teams/name/{mm_name(name, 'team')}")
        tid: str = data["id"]
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
        cname = mm_name(channel_name or self.work_channel_name, "channel")
        key = f"{team_name or self.team_name}/{cname}"
        cached = self._channel_ids.get(key)
        if cached:
            return cached
        tid = await self.get_team_id(team_name)
        data = await self._api(
            "GET", f"/api/v4/teams/{mm_id(tid, 'team')}/channels/name/{cname}"
        )
        cid: str = data["id"]
        self._channel_ids[key] = cid
        return cid

    async def account_created(self) -> int:
        """When Mattermost created the bot's account, epoch ms; 0 unknown."""
        return _int((await self.get_me()).raw.get("create_at"))

    async def get_channel(self, channel_id: str) -> Channel:
        if channel_id not in self._channels:
            self._channels[channel_id] = channel_of(await self._api(
                "GET", f"/api/v4/channels/{mm_id(channel_id, 'channel')}"))
        return self._channels[channel_id]

    async def channel_header(self, channel_id: str) -> str:
        """The channel's header as it is now: read fresh, a human may have
        changed it (RT-6.6 step 7)."""
        raw = await self._api("GET", f"/api/v4/channels/{mm_id(channel_id, 'channel')}")
        self._channels[channel_id] = channel_of(raw)
        return str(raw.get("header") or "")

    async def set_channel_header(self, channel_id: str, header: str) -> None:
        """Set the channel's header; Mattermost posts a system message for it."""
        raw = await self._api("PUT", f"/api/v4/channels/{mm_id(channel_id, 'channel')}/patch",
                              json={"header": header})
        if isinstance(raw, Mapping) and raw.get("id"):
            self._channels[channel_id] = channel_of(raw)

    async def usernames(self, user_ids: set[str]) -> dict[str, str]:
        """{user_id: username}, cached; one request for all unknown ids."""
        missing = sorted(set(user_ids) - set(self._usernames))
        for i in missing:
            mm_id(i, "user")
        if missing:
            for u in await self._api("POST", "/api/v4/users/ids", json=missing):
                self._usernames[u["id"]] = u.get("username", "")
                if u.get("is_bot"):
                    self._bots.add(u["id"])
        return {i: self._usernames[i] for i in user_ids if i in self._usernames}

    async def username(self, user_id: str) -> str:
        """One account's username, uncached; raises when it cannot be read."""
        name: str = (await self._api("GET", f"/api/v4/users/{mm_id(user_id, 'user')}"))["username"]
        return name

    async def agents(self, user_ids: set[str]) -> set[str]:
        """The ids among ``user_ids`` of bus agents: bot accounts with the
        registrar's prefix, agent-* by default (RT-6.16, arc42 §7.1). A person
        may name their account agent-x; it stays a person."""
        names = await self.usernames(user_ids)
        prefix = session_file.agent_prefix()
        return {i for i, n in names.items() if i in self._bots and n.startswith(prefix)}

    async def direct_channel_with_bot(self, username: str) -> str:
        """The DM channel id between this bot and another bot. Refuses people
        (they are asked with ask_human) and unknown names."""
        name = mm_name(username.strip().lstrip("@").lower(), "user")
        found = await self._api("POST", "/api/v4/users/usernames", json=[name]) or []
        if not found:
            raise ValueError(f"no user {name}")
        if not found[0].get("is_bot", False):
            raise ValueError(f"{name} is a person: ask people with ask_human")
        return await self.direct_channel_with(found[0]["id"])

    async def direct_channel_with(self, user_id: str) -> str:
        """The DM channel id between this bot and a user id; no checks."""
        me = await self.get_me()
        ch = await self._api("POST", "/api/v4/channels/direct",
                             json=[mm_id(me.id, "user"), mm_id(user_id, "user")])
        cid: str = ch["id"]
        return cid

    async def humans_among(self, names: set[str]) -> set[str]:
        """The usernames among ``names`` that belong to people, not bots; cached.
        Unknown names count as no one."""
        missing = sorted({n.lower() for n in names} - set(self._is_human))
        if missing:
            found = await self._api("POST", "/api/v4/users/usernames", json=missing) or []
            for n in missing:
                self._is_human[n] = False
            for u in found:
                self._is_human[(u.get("username") or "").lower()] = not u.get("is_bot", False)
        return {n for n in names if self._is_human.get(n.lower())}

    async def member_channels(self) -> list[UnreadChannel]:
        """This bot's channels in its team, DMs and groups included, each with
        when its newest post came and Mattermost's counters. What is pending
        there is the read watermark's to say (unread.py, RT-6.2): the counters
        only tell about a channel whose time Mattermost does not report.

        A DM counts unread messages (total minus `member.msg_count`, the
        counter's own watermark, no count itself); elsewhere mentions.
        """
        tid = await self.get_team_id()
        members = await self._api(
            "GET", f"/api/v4/users/me/teams/{mm_id(tid, 'team')}/channels/members"
        )
        channels = await self._api("GET", "/api/v4/users/me/channels")
        by_id = {c["id"]: c for c in (channels or [])}
        out: list[UnreadChannel] = []
        for member in members or []:
            cid = member.get("channel_id")
            if not cid:
                continue
            channel = by_id.get(cid) or {}
            ctype = channel.get("type") or "O"
            unread = (channel.get("total_msg_count") or 0) - (member.get("msg_count") or 0)
            pending = unread if ctype in ("D", "G") else member.get("mention_count") or 0
            out.append(UnreadChannel(
                channel_id=cid, channel_type=ctype,
                last_post_at=int(channel.get("last_post_at") or 0),
                pending=max(pending, 0), unread=max(unread, 0)))
        return out

    async def view_channel(self, channel_id: str) -> None:
        """Mark a channel read in Mattermost, for its UI: best-effort cleanup.
        The bus counts what waits by its own watermark (watermark.py, RT-6.2)."""
        await self._api(
            "POST",
            "/api/v4/channels/members/me/view",
            json={"channel_id": mm_id(channel_id, "channel")},
        )

    async def post(
        self,
        message: str,
        *,
        channel_id: str | None = None,
        root_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> Post:
        """Post; Refused when Mattermost will not take it (a root it does not
        find, no permission): the MCP SDK would hide the error's text."""
        try:
            return await self._post(message, channel_id=channel_id, root_id=root_id, props=props)
        except httpx.HTTPStatusError as exc:
            raise _refused(exc) from exc

    async def _post(self, message: str, *, channel_id: str | None, root_id: str | None,
                    props: dict[str, Any] | None) -> Post:
        if root_id:
            # A reply belongs where its root is: a DM, or another channel. A
            # reply's id names its thread: Mattermost refuses it as root_id.
            root = await self._api("GET", f"/api/v4/posts/{mm_id(root_id, 'post')}")
            root_id = mm_id(root.get("root_id") or root_id, "post")
            channel_id = channel_id or root.get("channel_id") or None
        cid = mm_id(channel_id or await self.get_channel_id(), "channel")
        # RT-6.15: post ids as permalinks, odd links written out for the human.
        # The same for an attachment's text (an ask's card), so no caller forgets it.
        message = links.send(message, post_url=self.permalink_base)
        body: dict[str, Any] = {"channel_id": cid, "message": message}
        if root_id:
            body["root_id"] = root_id
        if props:
            body["props"] = attachment_texts(
                props, lambda s: links.send(s, post_url=self.permalink_base))
        post = post_of(read_links(await self._api("POST", "/api/v4/posts", json=body),
                                  self.permalink_base))
        await self._remember(cid, post)
        return post

    async def _remember(self, channel_id: str, post: Post) -> None:
        """Keep the bot's latest post in the work channel for the Stop hook and
        the exit handler (ownpost.py). Bookkeeping: never fails the post."""
        try:
            if self.bot_id and channel_id == await self.work_channel_id():
                ownpost.record(self.bot_id, channel_id, post)
        except Exception as exc:  # noqa: BLE001
            log.info("own post not remembered: %s", exc)

    def post_sync(self, message: str, *, channel_id: str, root_id: str = "",
                  props: dict[str, Any] | None = None, timeout: float = 5.0) -> Post:
        """Post without an event loop, for the exit handler: one bounded request,
        raises when it fails. The raw post: no permalinks, not remembered."""
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if props:
            body["props"] = props
        with httpx.Client(timeout=timeout) as http:
            resp = http.post(f"{self.base_url}/api/v4/posts", json=body,
                             headers=self._auth_headers())
            resp.raise_for_status()
            return post_of(resp.json())

    def post_by_name_sync(self, channel_name: str, message: str, *, join: str = "") -> bool:
        """Post to the team's channel `channel_name` without an event loop, for
        the exit handler, joining it as user `join` first: joining twice is
        harmless, a non-member cannot post. False when there is no such
        channel; raises when a request fails."""
        headers = self._auth_headers()
        api = f"{self.base_url}/api/v4"
        with httpx.Client(timeout=5.0) as http:
            team = http.get(f"{api}/teams/name/{mm_name(self.team_name, 'team')}", headers=headers)
            team.raise_for_status()
            tid = mm_id(team.json()["id"], "team")
            ch = http.get(f"{api}/teams/{tid}/channels/name/{mm_name(channel_name, 'channel')}",
                          headers=headers)
            if ch.status_code == 404:
                return False
            ch.raise_for_status()
            cid = mm_id(ch.json()["id"], "channel")
            if join:
                http.post(f"{api}/channels/{cid}/members", headers=headers,
                          json={"user_id": mm_id(join, "user")})
            resp = http.post(f"{api}/posts", headers=headers,
                             json={"channel_id": cid, "message": message})
            resp.raise_for_status()
        return True

    # --- presence: status, custom status, a channel's accounts (lease.py, RT-6.6)

    async def channel_users(self, channel_id: str) -> list[ChannelUser]:
        """The accounts in a channel, at most 200, in Mattermost's order."""
        rows = await self._api("GET", "/api/v4/users",
                               params={"in_channel": mm_id(channel_id, "channel"),
                                       "per_page": 200}) or []
        return [channel_user_of(u) for u in rows if isinstance(u, Mapping)]

    async def presences(self, user_ids: list[str]) -> dict[str, str]:
        """{user_id: online | away | dnd | offline}, in one request."""
        rows = await self._api("POST", "/api/v4/users/status/ids",
                               json=[mm_id(i, "user") for i in user_ids]) or []
        return {str(r["user_id"]): str(r.get("status") or "") for r in rows
                if isinstance(r, dict) and r.get("user_id")}

    async def set_presence(self, user_id: str, status: str) -> None:
        await self._api("PUT", f"/api/v4/users/{mm_id(user_id, 'user')}/status",
                        json={"user_id": user_id, "status": status})

    async def set_custom_status(self, emoji: str, text: str, expires_at: str) -> None:
        """The bot's own custom status, until an RFC 3339 time."""
        await self._api("PUT", "/api/v4/users/me/status/custom", json={
            "emoji": emoji, "text": text, "duration": "date_and_time", "expires_at": expires_at})

    async def clear_custom_status(self) -> None:
        await self._api("DELETE", "/api/v4/users/me/status/custom")

    async def add_reaction(self, post_id: str, emoji: str) -> None:
        me = await self.get_me()
        await self._api("POST", "/api/v4/reactions", json={
            "user_id": me.id, "post_id": mm_id(post_id, "post"), "emoji_name": emoji})

    async def reactions(self, post_id: str) -> list[Reaction]:
        rows = await self._api("GET", f"/api/v4/posts/{mm_id(post_id, 'post')}/reactions") or []
        return [reaction_of(r) for r in rows if isinstance(r, Mapping)]

    async def thread(self, post_id: str) -> Thread:
        """Return post + ordered replies for a thread root (or any post id)."""
        data = await self._api("GET", f"/api/v4/posts/{mm_id(post_id, 'post')}/thread")
        posts = data.get("posts") or {}
        order = data.get("order") or sorted(
            posts.keys(), key=lambda pid: posts[pid].get("create_at", 0)
        )
        return Thread(
            root_id=str(data.get("root_id") or post_id),
            order=tuple(order),
            posts=tuple(post_of(read_links(posts[pid], self.permalink_base))
                        for pid in order if pid in posts),
        )

    async def work_channel_id(self) -> str:
        """The work channel's id, resolved in the shell CLI's order
        (mm_work_channel_id): MM_PROJECT_CHANNEL_ID, the project channel's name,
        MM_CHANNEL_ID, the default channel's name."""
        if env("MM_PROJECT_CHANNEL_ID"):
            return env("MM_PROJECT_CHANNEL_ID")
        if not self.project_channel_name and env("MM_CHANNEL_ID"):
            return env("MM_CHANNEL_ID")
        return await self.get_channel_id()

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        """A channel's posts with their text as Mattermost returns it (no
        link rewriting, with_links), newest first: the last `per_page`, or
        those created or edited since `since_ms`."""
        params: dict[str, Any] = {}
        if per_page is not None:
            params["per_page"] = per_page
        if since_ms is not None:
            params["since"] = since_ms
        data = await self._api("GET", f"/api/v4/channels/{mm_id(channel_id, 'channel')}/posts",
                               params=params) or {}
        posts = data.get("posts") or {}
        order = [i for i in data.get("order") or [] if i in posts]
        listed = set(order)
        rest = sorted((i for i in posts if i not in listed),
                      key=lambda i: posts[i].get("create_at", 0), reverse=True)
        return [post_of(posts[i]) for i in order + rest]

    async def list_recent(
        self,
        *,
        channel_id: str | None = None,
        per_page: int = 20,
        since_ms: int | None = None,
    ) -> list[Post]:
        cid = channel_id or await self.get_channel_id()
        params: dict[str, Any] = {"per_page": per_page}
        if since_ms is not None:
            params["since"] = since_ms
        data = await self._api(
            "GET", f"/api/v4/channels/{mm_id(cid, 'channel')}/posts", params=params
        )
        posts = data.get("posts") or {}
        return sorted((post_of(read_links(p, self.permalink_base)) for p in posts.values()),
                      key=lambda p: p.create_at)

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]:
        """A channel's posts created since `since_ms`, oldest first, as
        list_recent gives them. Pages back from the newest: with `since`,
        Mattermost answers at most 1000 posts in no set order, and in a busy
        channel the newest are the ones missing."""
        cid = mm_id(channel_id or await self.get_channel_id(), "channel")
        found: dict[str, dict[str, Any]] = {}
        for page in range(max_pages):
            data = await self._api("GET", f"/api/v4/channels/{cid}/posts",
                                   params={"page": page, "per_page": 200}) or {}
            posts = data.get("posts") or {}
            found |= {i: p for i, p in posts.items() if int(p.get("create_at") or 0) >= since_ms}
            # The page is `order`; `posts` may add older thread roots besides.
            page_posts = [posts[i] for i in data.get("order") or posts if i in posts]
            if len(page_posts) < 200 or any(
                    int(p.get("create_at") or 0) < since_ms for p in page_posts):
                break
        return sorted((post_of(read_links(p, self.permalink_base)) for p in found.values()),
                      key=lambda p: p.create_at)


async def watched_channel_ids(client: MattermostAdapter) -> set[str]:
    """The channels the bus watches (RT-6.2 step 1): the work channel, the
    project channel and the default channel, by the ids the host passed
    (MM_PROJECT_CHANNEL_ID, MM_CHANNEL_ID) and by name. Never a DM or group
    channel. The one list for both sides: the inbox delivers their posts
    (websocket_inbox), the hook API counts and reads them (hookapi), so what
    the Stop hook counts is what wait_for_events can hand over. A channel that
    does not resolve is left out, the others still count."""
    ids = {v for v in (env("MM_PROJECT_CHANNEL_ID"), env("MM_CHANNEL_ID")) if _ID.fullmatch(v)}
    names = dict.fromkeys(n for n in (client.channel_name, client.project_channel_name) if n)
    for name in names:
        try:
            ids.add(await client.get_channel_id(name))
        except Exception as exc:  # noqa: BLE001 — a channel that is not there
            log.warning("could not resolve watched channel %r: %s", name, exc)
    try:
        ids.add(await client.work_channel_id())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve the work channel: %s", exc)
    return ids


if TYPE_CHECKING:
    def _implements_the_port(adapter: MattermostAdapter) -> ChatPort:
        """mypy checks that the adapter satisfies the port, structurally."""
        return adapter
