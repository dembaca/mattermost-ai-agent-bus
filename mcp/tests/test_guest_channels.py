"""RT-6.22: guest channels (ADR-002, WP4 of #63). The home is the first
MM_PROJECT_CHANNEL entry; the further entries and every other team channel the
bot is a member of are guest channels, where only a mention, a DM or a reply in
the agent's own thread wakes it: no broadcast, no digest, no crown. Parity: one
entry and no further membership behave as before."""

import asyncio
import json
import time

import anyio
import httpx
import pytest
from fakes import BOT, WORK, FakeChat
from test_channel import Conn

from mattermost_ai_agent_bus import channel, gate, lease, ownpost, server, unread
from mattermost_ai_agent_bus.client import MattermostAdapter, SessionInfo, channel_list, post_of
from mattermost_ai_agent_bus.model import Channel, ChannelUser, UnreadChannel
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

GUEST = "g" * 26     # ~guest-a, a guest channel
LATER = "k" * 26     # ~guest-b, a channel the bot is added to at runtime
LOBBY = "l" * 26
HUMAN = "h" * 26
NAMES = {WORK: "agents", GUEST: "guest-a", LATER: "guest-b", LOBBY: "lobby"}


class GuestChat(FakeChat):
    """FakeChat with the adapter's guest channels (no port members): the
    team channels in `memberships` besides the home."""

    channel_name = "lobby"
    project_channel_name = ""
    trusted_source = ""
    guest_join_failures: dict[str, str] = {}

    def __init__(self, memberships=(GUEST,), **kw):
        super().__init__(**kw)
        self.memberships = set(memberships)
        self.joined: list[str] = []
        self.lobby = LOBBY  # MM_CHANNEL's id; WORK: the lobby is the home

    async def lobby_channel_id(self) -> str:
        return self.lobby

    async def get_channel_id(self, channel_name: str | None = None) -> str:
        """By name (MM_CHANNEL's by default): the lobby is watched."""
        name = channel_name or self.channel_name
        return self.lobby if name == "lobby" else next(i for i, n in NAMES.items() if n == name)

    async def guest_channel_ids(self) -> set[str]:
        return set(self.memberships) - {WORK, LOBBY}

    def forget_guests(self) -> None:
        pass

    async def get_channel(self, channel_id: str) -> Channel:
        return Channel(id=channel_id, name=NAMES.get(channel_id, "x"),
                       type="D" if channel_id in self.dms else "O")

    async def join_channel(self, channel_name: str) -> str:
        cid = next(i for i, n in NAMES.items() if n == channel_name)
        self.joined.append(channel_name)
        self.memberships.add(cid)
        return cid


class _Socket:
    """A connected WebSocket that feeds the given messages, then ends."""

    def __init__(self, *messages):
        self.messages = [json.dumps(m) for m in messages]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def send(self, msg):
        pass


def _posted(pid, message, where=GUEST, root="", props=None):
    post = {"id": pid, "user_id": HUMAN, "channel_id": where, "root_id": root,
            "message": message, "create_at": int(time.time() * 1000), "props": props or {}}
    return {"event": "posted", "data": {"post": json.dumps(post), "channel_type": "O"}}


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    lease._TYPES.clear()
    return GuestChat(username="agent-me")


async def _inbox(chat, *messages):
    """The inbox's reader over these WebSocket messages: what it queued."""
    inbox = WebSocketInbox(client=chat)  # type: ignore[arg-type]
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-me"
    await inbox.refresh_channels()
    inbox._ws = _Socket(*messages)
    await inbox._read_loop()
    return inbox, [inbox._queue.get_nowait() for _ in range(inbox._queue.qsize())]


# --- the list (client.channel_list, RT-6.22 step 1) -------------------------------------

def test_the_list_names_the_home_first_then_the_guests(monkeypatch):
    assert channel_list(" ~home , guest-a,,home, guest-b ") == ["home", "guest-a", "guest-b"]
    assert channel_list("") == []
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "home, guest-a,guest-b")
    c = MattermostAdapter.from_env()
    assert c.work_channel_name == "home" and c.project_channel_name == "home"
    assert c.guest_channel_names == ["guest-a", "guest-b"]
    assert c.project_channels == "home,guest-a,guest-b"


def test_one_entry_has_no_guests():
    """Parity: one entry is the home, as before."""
    c = MattermostAdapter(base_url="https://mm.example", project_channel_name="proj")
    assert (c.work_channel_name, c.guest_channel_names, c.project_channels) == ("proj", [], "proj")


# --- joining at start (RT-6.22 step 2) --------------------------------------------------

def _join_handler(statuses):
    """channels/name/<n> resolves to <n>'s id unless missing; joining answers statuses[n]."""
    ids = {"home": "h" * 26, "guest-a": GUEST, "private": "p" * 26}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/v4/teams/name/agents":
            return httpx.Response(200, json={"id": "t" * 26})
        if p.startswith("/api/v4/users/me/"):
            return httpx.Response(200, json=[])
        if "/channels/name/" in p:
            name = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": ids[name]}) if name in ids else httpx.Response(404)
        if p.endswith("/members"):
            name = next(n for n, i in ids.items() if i in p)
            return httpx.Response(statuses.get(name, 201), json={})
        return httpx.Response(404, json={})

    return handler


def _registered(project):
    c = MattermostAdapter(base_url="https://mm.example", token="t", project_channel_name=project)
    c.session = SessionInfo(url="https://mm.example", name="x", username="agent-x",
                            user_id="u" * 26, bot_token="tok")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(_join_handler({"private": 403})))
    return c


async def test_a_partial_join_is_reported_and_the_session_goes_on():
    """RT-6.22 step 2: a guest that is missing (404) or private (403) is
    reported once, the home and the other guests are joined."""
    c = _registered("home,guest-a,missing,private")
    out = await c.finish_session_setup()
    assert out["project_channel"]["joined"] is True
    assert [g["joined"] for g in out["guest_channels"]] == [True, False, False]
    assert "~missing: channel 'missing' does not exist" in out["guest_warning"]
    assert "~private: cannot join 'private': private channel" in out["guest_warning"]
    assert set(c.guest_join_failures) == {"missing", "private"}


async def test_one_entry_joins_as_before():
    """Parity: one entry, no guest_channels and no warning in the result."""
    out = await _registered("home").finish_session_setup()
    assert set(out) == {"project_channel"} and out["project_channel"]["joined"] is True


async def test_guests_are_the_memberships_besides_home_lobby_and_stats(monkeypatch):
    """RT-6.22 step 1: every team channel the bot is in; never a DM."""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", WORK)
    monkeypatch.setenv("MM_CHANNEL_ID", LOBBY)
    c = MattermostAdapter(base_url="https://mm.example", token="t", project_channel_name="agents")
    stats_id = "s" * 26

    async def member_channels():
        return [UnreadChannel(i, t) for i, t in
                ((WORK, "O"), (LOBBY, "O"), (stats_id, "O"), (GUEST, "P"), ("d" * 26, "D"))]

    async def get_channel_id(name=None, team_name=None):
        return {"agents-stats": stats_id}[name]

    monkeypatch.setattr(c, "member_channels", member_channels)
    monkeypatch.setattr(c, "get_channel_id", get_channel_id)
    assert await c.guest_channel_ids() == {GUEST}


async def test_team_default_channels_are_guests_only_when_named(monkeypatch):
    """RT-6.22 step 1: the bot is in town-square and off-topic only because it
    joined the team; they are guests when MM_PROJECT_CHANNEL names them."""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", WORK)
    monkeypatch.setenv("MM_CHANNEL_ID", LOBBY)
    square, off = "q" * 26, "o" * 26

    async def member_channels():
        return [UnreadChannel(WORK, name="agents"), UnreadChannel(GUEST, name="guest-a"),
                UnreadChannel(square, name="town-square"), UnreadChannel(off, name="off-topic")]

    async def get_channel_id(name=None, team_name=None):
        raise LookupError(name)

    for configured, want in (("agents,guest-a", {GUEST}),
                             ("agents,guest-a,~town-square", {GUEST, square})):
        c = MattermostAdapter(base_url="https://mm.example", token="t",
                              project_channel_name=configured)
        monkeypatch.setattr(c, "member_channels", member_channels)
        monkeypatch.setattr(c, "get_channel_id", get_channel_id)
        assert await c.guest_channel_ids() == want


# --- what wakes in a guest channel (RT-6.22 step 3) -------------------------------------

async def test_a_mention_wakes_a_plain_post_and_at_channel_do_not(chat):
    inbox, events = await _inbox(
        chat,
        _posted("m" * 26, "@agent-me can you look?"),
        _posted("p" * 26, "lunch anyone"),
        _posted("b" * 26, "@channel deploy at 5"),
        _posted("w" * 26, "plain post at home", where=WORK))
    assert [(e.kind, e.guest) for e in events] == [("mention", True), ("watched_channel", False)]
    item = {"kind": "mention", "guest": True, "post": {"message": "@agent-me can you look?"}}
    assert channel.wakes(item, coordinator=False, in_my_thread=False)
    assert channel.notification(item)["meta"]["guest"] == "true"


async def test_a_reply_in_my_thread_wakes_others_do_not(chat):
    root = "r" * 26
    ownpost.record_guest(BOT, "guest-a", post_of({"id": root, "message": "ACK · on it"}))
    _, events = await _inbox(chat, _posted("1" * 26, "thanks", root=root),
                             _posted("2" * 26, "thanks", root="o" * 26))
    assert [e.post.root_id for e in events] == [root]
    item = {"kind": "watched_channel", "guest": True, "post": {"message": "thanks"}}
    assert channel.wakes(item, coordinator=False, in_my_thread=True)
    assert not channel.wakes(item, coordinator=False, in_my_thread=False)


def test_no_broadcast_and_no_coordinator_wake_in_a_guest_channel():
    """Neither to="all", @channel nor the crown wakes a guest; at home they do."""
    for extra in ({"to": "all"}, {"post": {"message": "@channel all hands"}}):
        item = {"kind": "watched_channel", "post": {"message": "x"}} | extra
        assert channel.wakes(item, coordinator=False, in_my_thread=False)
        assert not channel.wakes(item | {"guest": True}, coordinator=True, in_my_thread=False)


@pytest.mark.anyio
async def test_a_guest_channel_goes_into_no_digest():
    """RT-6.22 step 3: what does not wake there is dropped, not held."""

    pusher = channel.Pusher(digest_sec=0.2)
    pusher.connection = Conn()  # type: ignore[assignment]
    pusher.confirm(pusher.code)
    fyi = {"kind": "watched_channel", "guest": True, "intent": "fyi", "from_trusted_human": False,
           "post": {"id": "p", "message": "ACK · noted"}}
    batches = [[fyi]]

    async def wait(timeout):
        if batches:
            return batches.pop(0)
        await asyncio.sleep(timeout)
        return []

    async def addressed(item):
        return channel.wakes(item, coordinator=False, in_my_thread=True)

    with anyio.move_on_after(0.5):
        await pusher.run(wait, addressed=addressed, wait_cap=0.1)
    assert pusher.connection.sent == [] and pusher.held == []


async def test_the_count_takes_only_mentions_and_my_threads_in_a_guest_channel(chat):
    """RT-6.22 step 3, RT-6.2 step 1: the Stop hook counts what the inbox delivers."""
    root, now = "r" * 26, int(time.time() * 1000)
    chat.channels = [UnreadChannel(GUEST, "O", last_post_at=now + 10)]
    chat.posts = [{"id": f"{n}" * 26, "user_id": HUMAN, "channel_id": GUEST, "create_at": now + n,
                   "message": m, "root_id": r}
                  for n, (m, r) in enumerate([("@agent-me look", ""), ("@channel hi", ""),
                                              ("all agents", ""), ("thanks", root),
                                              ("other", "o" * 26)], start=1)]
    chat.posts[2]["props"] = {"a2a": {"to": "all"}}
    rows = await unread.channels(chat, {GUEST}, guests={GUEST}, threads={root})
    assert [p.message for p in rows[0].posts] == ["@agent-me look", "thanks"]
    # The same channel as no guest: @channel and the post for all agents count, as before.
    rows = await unread.channels(chat, {GUEST})
    assert [p.message for p in rows[0].posts] == ["@agent-me look", "@channel hi", "all agents"]


# --- membership changes (RT-6.22 step 1) -------------------------------------------------

async def test_a_channel_the_bot_is_added_to_is_watched(chat):
    """user_added naming the bot: the watched and guest channels are read again."""
    inbox = WebSocketInbox(client=chat)  # type: ignore[arg-type]
    inbox._bot_user_id = BOT
    await inbox.refresh_channels()
    assert LATER not in inbox.watched_channel_ids
    chat.memberships.add(LATER)
    await inbox._on_membership({"event": "user_added", "data": {"user_id": "x" * 26},
                                "broadcast": {"channel_id": LATER}})
    assert LATER not in inbox.guest_channel_ids  # someone else was added
    await inbox._on_membership({"event": "user_added", "data": {"user_id": BOT},
                                "broadcast": {"channel_id": LATER}})
    assert {GUEST, LATER} <= inbox.watched_channel_ids and inbox.guest_channel_ids == {GUEST, LATER}
    chat.memberships.discard(LATER)
    await inbox._on_membership({"event": "user_removed", "data": {"channel_id": LATER},
                                "broadcast": {"user_id": BOT}})
    assert LATER not in inbox.watched_channel_ids


# --- posting (RT-6.22 step 4) ------------------------------------------------------------

async def test_post_message_takes_a_channel_name_and_marks_the_home(chat, monkeypatch):
    monkeypatch.setattr(server, "_inbox", None)
    chat.memberships = set()
    await server.post_as_bot(chat, "STATUS · looking", channel_name="~guest-b")
    post = chat.posted[-1]
    assert chat.joined == ["guest-b"] and post["channel_id"] == LATER
    assert post["props"]["a2a"]["home"] == "agents"
    assert list(ownpost.guest_threads(BOT)) == [chat.post_id]
    await server.post_as_bot(chat, "STATUS · at home")
    assert "home" not in (chat.posted[-1]["props"] or {}).get("a2a", {})


async def test_bye_lists_the_open_guest_threads(chat, monkeypatch):
    monkeypatch.setattr(server, "_inbox", None)
    a, b = "a" * 26, "b" * 26
    ownpost.record_guest(BOT, "guest-a", post_of({"id": a, "message": "ACK · on it"}))
    ownpost.record_guest(BOT, "guest-a", post_of({"id": "x" * 26, "root_id": b, "message": "DONE · fixed"}))
    await server.post_as_bot(chat, "BYE · signing off")
    assert chat.posted[-1]["message"].endswith(f"\nOpen in guest channels: ~guest-a {a}")
    assert ownpost.with_guest_threads(ownpost.BYE, "nobody") == ownpost.BYE


# --- the gate's reply names the guest channel's coordinator (RT-6.22 step 5) -----------

def _member(uid, name, home):
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    return ChannelUser(uid, name, 1, custom_status={
        "emoji": "crown", "text": f"coordinator · working · x · ~{home}",
        "duration": "date_and_time", "expires_at": until})


async def test_the_gate_reply_names_the_guest_channels_coordinator(chat, monkeypatch):
    monkeypatch.delenv("MM_AGENT_PREFIX", raising=False)
    monkeypatch.setenv("MM_BOT_USERNAME", "agent-me")
    chat.trusted = {HUMAN: "dembaca"}
    chat.members = [_member("c" * 26, "agent-mine", "agents"), _member("o" * 26, "agent-theirs", "guest-a")]
    chat.presence = {"c" * 26: "online", "o" * 26: "online"}
    text = await gate.reply_text(chat, "mallory", dm=False, guest=GUEST)
    assert text == "Not woken: mallory is not on my trusted list. agent-theirs (👑) will look at it."
    chat.members = chat.members[:1]  # only a guest's crown of its own home: none here
    text = await gate.reply_text(chat, "mallory", dm=False, guest=GUEST)
    assert text.endswith("no agent coordinates ~guest-a. Ask dembaca.")


async def test_bus_info_lists_home_guests_and_lobby(chat, monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    info = json.loads(await server.bus_info())
    assert (info["home"], info["guests"], info["lobby"]) == ("agents", ["guest-a"], "lobby")


# --- the lobby (RT-6.22 step 3, WP5) ----------------------------------------------------

TO_ALL = {"a2a": {"v": 1, "to": "all"}}


async def _mine(item, uid):
    """in_my_thread as the server answers it: the agent posted in thread r…r."""
    return (item.get("post") or {}).get("root_id") == "r" * 26


async def _lobby_inbox(chat, *messages):
    inbox = WebSocketInbox(client=chat, in_my_thread=_mine)  # type: ignore[arg-type]
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-me"
    await inbox.refresh_channels()
    inbox._ws = _Socket(*messages)
    await inbox._read_loop()
    return inbox, [inbox._queue.get_nowait() for _ in range(inbox._queue.qsize())]


async def test_the_lobby_queues_mentions_broadcasts_and_my_threads(chat):
    """RT-6.22 step 3: in the lobby, not the home, a plain post and a reply in
    a thread the agent never posted in are dropped (no event, no digest)."""
    inbox, events = await _lobby_inbox(
        chat,
        _posted("m" * 26, "@agent-me can you look?", where=LOBBY),
        _posted("p" * 26, "lunch anyone", where=LOBBY),
        _posted("b" * 26, "@channel deploy at 5", where=LOBBY),
        _posted("a" * 26, "main is red", where=LOBBY, props=TO_ALL),
        _posted("1" * 26, "thanks", where=LOBBY, root="r" * 26),
        _posted("2" * 26, "thanks", where=LOBBY, root="o" * 26),
        _posted("w" * 26, "plain post at home", where=WORK))
    assert inbox.lobby_channel_ids == {LOBBY}
    assert [(e.post.id[0], e.lobby) for e in events] == [
        ("m", True), ("b", True), ("a", True), ("1", True), ("w", False)]


def test_the_lobby_wakes_for_broadcasts_never_for_the_crown():
    """RT-6.22 step 3: unlike a guest channel, @channel and to="all" wake in
    the lobby; a plain post wakes not even the crown holder of another home."""
    for extra in ({"to": "all"}, {"post": {"message": "@channel all hands"}}):
        item = {"kind": "watched_channel", "lobby": True, "post": {"message": "x"}} | extra
        assert channel.wakes(item, coordinator=False, in_my_thread=False)
        assert not channel.wakes(item | {"lobby": False, "guest": True},
                                 coordinator=False, in_my_thread=False)
    plain = {"kind": "watched_channel", "lobby": True, "post": {"message": "lunch"}}
    assert not channel.wakes(plain, coordinator=True, in_my_thread=False)
    assert channel.wakes(plain, coordinator=False, in_my_thread=True)
    assert channel.notification(plain)["meta"]["lobby"] == "true"


@pytest.mark.anyio
async def test_the_lobby_goes_into_no_digest():
    """RT-6.22 step 3: a lobby post that does not wake (here one the gate held
    back) is dropped, not held for a digest."""
    pusher = channel.Pusher(digest_sec=0.2)
    pusher.connection = Conn()  # type: ignore[assignment]
    pusher.confirm(pusher.code)
    gated = {"kind": "watched_channel", "lobby": True, "gated": "broadcast",
             "post": {"id": "p", "message": "@channel buy now"}}
    batches = [[gated]]

    async def wait(timeout):
        if batches:
            return batches.pop(0)
        await asyncio.sleep(timeout)
        return []

    async def addressed(item):
        return channel.wakes(item, coordinator=False, in_my_thread=False)

    with anyio.move_on_after(0.5):
        await pusher.run(wait, addressed=addressed, wait_cap=0.1)
    assert pusher.connection.sent == [] and pusher.held == []


async def test_an_untrusted_broadcast_in_the_lobby_stays_gated(chat):
    """RT-6.16, RT-6.22 step 3: with trusted humans set, an @channel there by
    someone not trusted is queued gated (data only); a trusted one's is not."""
    chat.trusted = {"t" * 26: "dembaca"}
    _, events = await _lobby_inbox(chat, _posted("b" * 26, "@channel buy now", where=LOBBY))
    assert [(e.lobby, bool(e.gated)) for e in events] == [(True, True)]
    chat.trusted = {HUMAN: "dembaca"}
    _, events = await _lobby_inbox(chat, _posted("c" * 26, "@channel deploy at 5", where=LOBBY))
    assert [(e.lobby, bool(e.gated)) for e in events] == [(True, False)]


async def test_the_lobby_counts_what_wakes_there(chat):
    """RT-6.2 step 1: the Stop hook's count in the lobby takes a mention, an
    @channel and a post for all agents, never a plain post."""
    now = int(time.time() * 1000)
    chat.channels = [UnreadChannel(LOBBY, "O", last_post_at=now + 10)]
    chat.posts = [{"id": f"{n}" * 26, "user_id": HUMAN, "channel_id": LOBBY, "create_at": now + n,
                   "message": m} for n, m in enumerate(
                       ["@agent-me look", "@channel hi", "all agents", "lunch anyone"], start=1)]
    chat.posts[2]["props"] = TO_ALL
    rows = await unread.channels(chat, {LOBBY})
    assert [p.message for p in rows[0].posts] == ["@agent-me look", "@channel hi", "all agents"]


async def test_the_lobby_as_home_behaves_as_before(chat):
    """Parity: no project channel, MM_CHANNEL is the home: no lobby rules, a
    plain post is queued and wakes its crown holder."""
    chat.lobby = WORK
    inbox, events = await _lobby_inbox(chat, _posted("p" * 26, "lunch anyone", where=WORK))
    assert inbox.lobby_channel_ids == set()
    assert [(e.kind, e.lobby) for e in events] == [("watched_channel", False)]
    item = {"kind": "watched_channel", channel.CHANNEL_ID: WORK, "post": {"message": "lunch"}}
    assert channel.wakes(item, coordinator=True, in_my_thread=False, routes={WORK})
