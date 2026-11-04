"""Mandate for a role (RT-6.26, ADR-006 phase 1): a trusted human's pinned
`mandate: 👑 of ~<home>` makes the holder of that crown give tasks in the
home it is pinned in. Who may issue it, how the role resolves, its lifetime,
the markers on events, rows and bus_info, and what it never grants."""

import calendar
import json
import time
from typing import Any

import pytest

from mattermost_ai_agent_bus import askroute, asks, channel, gate, lease, mandate, server
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import Channel, ChannelUser
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

from fakes import BOT, WORK, FakeChat

HUB, GUEST = "g" * 26, "j" * 26
HUMAN, OTHER = "h" * 26, "o" * 26           # dembaca (trusted), a visitor
HOLDER, NEXT, FOREIGN = "k" * 26, "n" * 26, "f" * 26
MPOST, TPOST = "m" * 26, "t" * 26
MINE = "for @dembaca · trusts @dembaca · via agent-registrar"
DAY = mandate.DAY_MS
NOW = time.time()
NOW_MS = int(NOW * 1000)
NAMES = {HUMAN: "dembaca", OTHER: "visitor", BOT: "agent-me", HOLDER: "agent-hub",
         NEXT: "agent-next", FOREIGN: "agent-far"}
CHANNEL_NAMES = {WORK: "agents", HUB: "hub", GUEST: "proj-b"}


def _member(uid: str, home: str, emoji: str = "hammer_and_wrench", live: bool = True) -> ChannelUser:
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + (600 if live else -60)))
    return ChannelUser(uid, NAMES[uid], create_at=1, custom_status={
        "emoji": emoji, "text": lease.homed("coordinator" if emoji == "crown" else "working", home),
        "duration": "date_and_time", "expires_at": expires})


class _Homes(FakeChat):
    """Channels by id with their own members; HUB as the holder's home."""

    def __init__(self, **kw: Any) -> None:
        kw = {"uid": BOT, "username": "agent-me", "names": NAMES, "trusted": {HUMAN: "dembaca"},
              "agents": {BOT, HOLDER, NEXT, FOREIGN},
              "descriptions": {BOT: MINE, HOLDER: MINE, NEXT: MINE,
                               FOREIGN: "for @carol · trusts @carol · via agent-registrar"}} | kw
        super().__init__(**kw)
        self.channel_ids = {"hub": HUB, "proj-b": GUEST}
        self.presence = {u: "online" for u in NAMES}
        self.by_channel: dict[str, list[ChannelUser]] = {
            WORK: [_member(BOT, "agents"), _member(HOLDER, "hub", "crown")],
            HUB: [_member(HOLDER, "hub", "crown")]}
        self.pinned = [_mandate()]
        self.hub_readable = True

    async def get_channel(self, channel_id: str) -> Channel:
        return Channel(id=channel_id, name=CHANNEL_NAMES.get(channel_id, "x"),
                       type="D" if channel_id in self.dms else "O")

    async def channel_users(self, channel_id: str) -> list[ChannelUser]:
        if channel_id == HUB and not self.hub_readable:
            raise PermissionError("403 Forbidden")
        return list(self.by_channel.get(channel_id, []))

    def crown_to(self, uid: str | None) -> None:
        """The hub's crown moves to uid (None: it lapses)."""
        members = [_member(u, "hub", "crown" if u == uid else "hammer_and_wrench")
                   for u in (HOLDER, NEXT)]
        self.by_channel = {WORK: [_member(BOT, "agents"), *members], HUB: members}


def _mandate(text: str = "mandate: 👑 of ~hub", *, pid: str = MPOST, user: str = HUMAN,
             at: int = NOW_MS - 60_000, edit: int = 0, **over: Any) -> dict[str, Any]:
    return {"id": pid, "user_id": user, "channel_id": WORK, "message": text,
            "create_at": at, "edit_at": edit, "is_pinned": True} | over


def _post(user: str, text: str = "TASK · fix login · @agent-me", *, pid: str = TPOST,
          root: str = "", cid: str = WORK, at: int = NOW_MS) -> Any:
    return post_of({"id": pid, "user_id": user, "channel_id": cid, "root_id": root,
                    "message": text, "create_at": at})


def _event(post: Any, kind: str = "watched_channel", **kw: Any) -> ClassifiedEvent:
    return ClassifiedEvent(kind=kind, post=post, **kw)


@pytest.fixture
def chat(monkeypatch):
    c = _Homes()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(gate, "_coord", None)
    return c


async def _items(*events: ClassifiedEvent) -> list[dict[str, Any]]:
    return await server._event_items(list(events))


# --- what it marks -------------------------------------------------------------

async def test_the_holders_task_is_mandated_on_the_event_the_push_and_the_row(chat):
    """RT-6.26 steps 2, 4, 5: a trusted human's pin makes the hub's 👑 TASK in
    this home a task marked mandated, in wait_for_events and the push, and on
    its row; it stays from_agent, never from_trusted_human."""
    task = _post(HOLDER)
    [item] = await _items(_event(task, "mention"))
    assert item["mandated"] == MPOST and item["mandated_by"] == "dembaca"
    assert item["from_agent"] is True and item["from_trusted_human"] is False
    meta = channel.notification(item)["meta"]
    assert meta["mandated"] == MPOST and meta["mandated_by"] == "dembaca"
    assert meta["from_trusted_human"] == "false"
    [row] = await server._tasks(chat, [task], NAMES)
    assert row["task"] == TPOST and row["mandated"] == MPOST


async def test_a_reply_in_a_task_thread_is_mandated_too(chat):
    """RT-6.26 step 4: the holder's reply in a TASK's thread in the home (a
    priority, say); a reply in any other thread is not."""
    root = _post(HUMAN, "TASK · docs · @agent-me", pid="r" * 26, at=NOW_MS - 1000)
    plain = _post(HUMAN, "lunch?", pid="p" * 26, at=NOW_MS - 1000)
    chat.posts = [root, plain]
    a, b = await _items(_event(_post(HOLDER, "first this", pid="a" * 26, root=root.id)),
                        _event(_post(HOLDER, "and this", pid="b" * 26, root=plain.id)))
    assert a["mandated"] == MPOST and "mandated" not in b


async def test_rows_are_marked_in_the_home_also_with_scope_all(chat, monkeypatch):
    """RT-6.26 step 5: coordinator("tasks", scope="all") marks the home's
    rows; a guest channel's TASK by the same holder is that home's, unmarked."""
    async def channels(client):
        return [{"name": "agents", "id": WORK, "role": "home"},
                {"name": "proj-b", "id": GUEST, "role": "guest"}]

    async def after(since_ms, *, channel_id=None, max_pages=25):
        cid = channel_id or WORK
        return [_post(HOLDER, pid=("t" if cid == WORK else "u") * 26, cid=cid)]

    monkeypatch.setattr(server, "_own_channels", channels)
    monkeypatch.setattr(chat, "posts_after", after)
    out = json.loads(await server.coordinator("tasks", scope="all"))
    home, guest = out["channels"]
    assert home["tasks"][0]["mandated"] == MPOST
    assert "mandated" not in guest["tasks"][0]
    out = json.loads(await server.coordinator("tasks"))
    assert out["tasks"][0]["mandated"] == MPOST


async def test_a_dm_a_guest_channel_and_a_reaction_get_no_marker(chat):
    """RT-6.26 step 4: only posts in the receiving home; a DM, a post in
    another channel and a reaction carry nothing."""
    chat.dms = {"d" * 26}
    items = await _items(
        _event(_post(HOLDER, pid="1" * 26, cid="d" * 26), "dm"),
        _event(_post(HOLDER, pid="2" * 26, cid=GUEST), "mention", guest=True),
        _event(_post(HOLDER, ":+1:", pid=f"{TPOST}:{HOLDER}:+1", root=TPOST), "reaction",
               raw={"emoji": "+1", "ask": TPOST}))
    assert all("mandated" not in i for i in items)


async def test_without_a_pin_every_event_is_as_before(chat):
    """RT-6.26 step 2, parity: no pin, or no trusted humans: no marker, and
    the peer's TASK is a task as before (RT-6.16 step 5)."""
    chat.pinned = []
    [item] = await _items(_event(_post(HOLDER)))
    assert "mandated" not in item and item["from_agent"] is True
    [row] = await server._tasks(chat, [_post(HOLDER, pid="z" * 26)], NAMES)
    assert "mandated" not in row
    mandate._reads.clear()
    mandate._fetched.clear()
    chat.pinned = [_mandate()]
    chat.trusted = {}
    [item] = await _items(_event(_post(HOLDER, pid="y" * 26)))
    assert "mandated" not in item
    assert (await mandate.current(chat))[1] == "no trusted humans: no mandate counts"


# --- who may issue it -------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"user": OTHER},                                   # not trusted by this session
    {"props": {"from_webhook": "true"}},               # a webhook names the account
    {"root_id": "r" * 26},                             # a reply
    {"channel_id": GUEST},                             # pinned elsewhere
    {"type": "system_header_change"},                  # a system post
], ids=["not-trusted", "webhook", "reply", "other-channel", "system"])
async def test_a_mandate_counts_only_as_a_trusted_humans_top_level_post(chat, change):
    """RT-6.26 step 2: the author's account id against this session's trusted
    humans, never the pin; never a webhook's post, a reply or a system post."""
    chat.pinned = [_mandate(**change)]
    [item] = await _items(_event(_post(HOLDER)))
    assert "mandated" not in item
    assert "mandated" not in (await server._tasks(chat, [_post(HOLDER)], NAMES))[0]
    assert (await mandate.current(chat))[0] is None


async def test_a_bot_or_an_agent_listed_as_trusted_counts_nothing(chat):
    """RT-6.26 step 2: a bot (a CI account) or an agent in the trusted list
    issues no mandate, as for the ask mode (RT-6.25 step 2)."""
    chat.trusted = {HUMAN: "dembaca", HOLDER: "agent-hub"}
    chat.pinned = [_mandate(user=HOLDER)]
    assert (await mandate.current(chat))[0] is None
    mandate._fetched.clear()
    chat.pinned = [_mandate()]
    chat.bots = {"dembaca": ChannelUser(HUMAN, "dembaca")}
    assert (await mandate.current(chat))[0] is None


async def test_the_first_line_decides(chat):
    """RT-6.26 step 2: `mandate: 👑 of ~<home> [until <date>]`, any case, in
    backticks; further lines are a reason; anything else is none."""
    assert mandate.parse("Mandate: 👑 of ~Hub") == ("hub", "")
    assert mandate.parse("`mandate: :crown: of ~hub until 2026-12-01`\nso agents get work") == (
        "hub", "2026-12-01")
    for text in ("please: mandate: 👑 of ~hub", "mandate: 👑 of hub", "mandate 👑 of ~hub",
                 "reason first\nmandate: 👑 of ~hub"):
        assert mandate.parse(text) is None


async def test_the_newest_mandate_wins(chat):
    """RT-6.26 step 3: a newer qualifying pin in the home replaces the older;
    one by someone not trusted replaces nothing."""
    chat.pinned = [_mandate(pid="1" * 26, at=NOW_MS - 3000),
                   _mandate("mandate: 👑 of ~proj-b", pid="2" * 26, at=NOW_MS - 2000),
                   _mandate("mandate: 👑 of ~agents", pid="3" * 26, user=OTHER, at=NOW_MS - 1000)]
    m, _ = await mandate.current(chat)
    assert m is not None and (m.post, m.home, m.home_id) == ("2" * 26, "proj-b", GUEST)


# --- the holder -------------------------------------------------------------------

async def test_a_foreign_holder_is_never_mandated(chat, caplog):
    """RT-6.26 step 4: the crown taken by another owner's agent: its posts
    stay requests, logged once, and bus_info says so."""
    chat.by_channel[HUB] = [_member(FOREIGN, "hub", "crown")]
    with caplog.at_level("INFO"):
        [a] = await _items(_event(_post(FOREIGN, pid="1" * 26)))
        [b] = await _items(_event(_post(FOREIGN, pid="2" * 26)))
    assert "mandated" not in a and "mandated" not in b and a["foreign"] is True
    assert caplog.text.count("no peer of this session") == 1
    info = await mandate.info(chat)
    assert info["holder"] is None and "foreign" in info["why"]


async def test_the_marker_follows_the_crown_and_drops_when_it_lapses(chat):
    """RT-6.26 step 4: a handover moves it to the next holder; a lapsed crown
    mandates nobody."""
    chat.crown_to(NEXT)
    old, new = await _items(_event(_post(HOLDER, pid="1" * 26)), _event(_post(NEXT, pid="2" * 26)))
    assert "mandated" not in old and new["mandated"] == MPOST
    chat.crown_to(None)
    [item] = await _items(_event(_post(NEXT, pid="3" * 26)))
    assert "mandated" not in item
    assert (await mandate.info(chat))["why"] == "the crown of ~hub is not held"


async def test_an_unreadable_hub_falls_back_to_the_holders_status_here(chat):
    """RT-6.26 step 4: where ~<home> cannot be read, the holder's own 👑
    status naming it, in this home's roster, counts; a missing home leaves no
    holder."""
    chat.hub_readable = False
    [item] = await _items(_event(_post(HOLDER, pid="1" * 26)))
    assert item["mandated"] == MPOST
    mandate._fetched.clear()
    chat.channel_ids = {}
    [item] = await _items(_event(_post(HOLDER, pid="2" * 26)))
    assert "mandated" not in item
    assert (await mandate.info(chat))["why"] == "~hub not found"


async def test_without_the_owner_split_nobody_is_a_peer(chat):
    """RT-6.26 step 4: unbound own bot (the split off): no holder can be shown
    to be a peer, so nobody is mandated."""
    chat.descriptions[BOT] = "Registered via agent-registrar"
    [item] = await _items(_event(_post(HOLDER)))
    assert "mandated" not in item and item["from_agent"] is True


# --- lifetime ---------------------------------------------------------------------

def test_expiry_default_until_and_the_cap():
    """RT-6.26 step 3: 7 days by default; `until` (the start of that day,
    UTC) within 30 days of the post's time, never later; no date: none."""
    at = 1_800_000_000_000
    assert mandate.until_ms(at, "") == at + 7 * DAY
    day = time.strftime("%Y-%m-%d", time.gmtime(at / 1000 + 3 * 86400))
    start = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
    assert mandate.until_ms(at, day) == start
    assert at + 2 * DAY < start <= at + 3 * DAY
    assert mandate.until_ms(at, "2099-01-01") == at + 30 * DAY
    assert mandate.until_ms(at, "2026-13-01") is None and mandate.until_ms(at, "soon") is None


async def test_an_expired_mandate_counts_as_none_and_is_logged_once(chat, caplog):
    """RT-6.26 step 3: past its expiry nothing is marked; pinned again later,
    an old post revives nothing."""
    chat.pinned = [_mandate(at=NOW_MS - 8 * DAY)]
    with caplog.at_level("INFO"):
        [item] = await _items(_event(_post(HOLDER, pid="1" * 26)))
        await mandate.current(chat)
    assert "mandated" not in item and caplog.text.count("expired at") == 1
    assert (await mandate.current(chat))[1].startswith(f"mandate post {MPOST} expired at")


async def test_until_shortens_and_an_edit_renews(chat):
    """RT-6.26 step 3: a past `until` ends it; an edit counts from its time."""
    yesterday = time.strftime("%Y-%m-%d", time.gmtime(NOW - 86400))
    chat.pinned = [_mandate(f"mandate: 👑 of ~hub until {yesterday}")]
    assert (await mandate.current(chat))[0] is None
    chat.pinned = [_mandate(at=NOW_MS - 20 * DAY, edit=NOW_MS - DAY)]
    await mandate.refresh(chat)
    m, _ = await mandate.current(chat)
    assert m is not None and m.until_ms == NOW_MS - DAY + 7 * DAY


async def test_revoke_by_delete_or_edit_and_unpin_pauses(chat):
    """RT-6.26 step 3: deleted or edited away it is gone at the next read;
    unpinned too, but pinned again within its expiry it is back (accepted:
    revocation is by delete or edit)."""
    assert (await mandate.current(chat))[0] is not None
    chat.pinned = [_mandate(delete_at=NOW_MS)]
    await mandate.refresh(chat)
    assert (await mandate.current(chat))[0] is None
    chat.pinned = [_mandate("we are done here", edit=NOW_MS)]
    await mandate.refresh(chat)
    assert (await mandate.current(chat))[0] is None
    chat.pinned = []                                   # unpinned
    await mandate.refresh(chat)
    assert (await mandate.current(chat))[1] == "none pinned in this home"
    chat.pinned = [_mandate()]                         # pinned again, within expiry
    await mandate.refresh(chat)
    assert (await mandate.current(chat))[0] is not None


async def test_a_failed_read_keeps_the_last_good_one_until_its_expiry(chat):
    """RT-6.26 step 1: one read per refresh; a failure keeps what was read,
    which still ends at its expiry."""
    assert (await mandate.current(chat, NOW))[0] is not None
    chat.pinned = RuntimeError("502 Bad Gateway")
    assert await mandate.refresh(chat, NOW + 200) is False
    m, _ = await mandate.current(chat, NOW + 200)
    assert m is not None
    assert (await mandate.current(chat, m.until_ms / 1000 + 1))[0] is None


async def test_the_heartbeat_reads_every_120_s(chat):
    """RT-6.26 step 1: the first look reads; the heartbeat (stall.watch)
    again once REFRESH_SEC have passed, and brings no beat forward."""
    assert await mandate.tick(chat, BOT, NOW) is None
    await mandate.current(chat, NOW + 10)
    await mandate.tick(chat, BOT, NOW + 60)
    assert chat.pinned_reads == 1
    await mandate.tick(chat, BOT, NOW + mandate.REFRESH_SEC)
    assert chat.pinned_reads == 2


async def test_the_row_is_judged_once_at_first_sight(chat):
    """RT-6.26 step 3: a task taken under a mandate stays marked when the
    mandate ends; a TASK posted before the mandate is not marked."""
    task = _post(HOLDER)
    older = _post(HOLDER, pid="e" * 26, at=NOW_MS - 120_000)
    [item] = await _items(_event(task))
    assert item["mandated"] == MPOST
    chat.pinned = []
    await mandate.refresh(chat)
    rows = await server._tasks(chat, [task, older], NAMES)
    assert [r.get("mandated") for r in rows] == [None, MPOST]


# --- what it never grants -----------------------------------------------------------

async def test_a_mandated_post_never_approves(chat):
    """RT-6.26 step 7, RT-6.11: the holder's 👍 on an ask and its APPROVE in
    the thread stay from_agent; the ask stays open, and the approval floor
    classifies a mandated merge as before (RT-6.25 step 1)."""
    ask = "q" * 26
    asks.add(BOT, ask, "dembaca", title="Merge PR #12?")
    chat.posts = [_post(HOLDER, "TASK · merge PR #12 · @agent-me", pid=ask, at=NOW_MS - 1000)]
    yes = _event(post_of({"id": f"{ask}:{HOLDER}:+1", "root_id": ask, "user_id": HOLDER,
                          "channel_id": WORK, "message": ":+1:", "create_at": NOW_MS}),
                 "reaction", raw={"emoji": "+1", "ask": ask})
    reply = _event(_post(HOLDER, "APPROVE: merge it", pid="w" * 26, root=ask))
    a, b = await _items(yes, reply)
    assert a["from_trusted_human"] is False and not server._ask_answer(yes, a)
    assert b["mandated"] == MPOST and b["from_trusted_human"] is False
    assert asks.is_open(BOT, ask)
    assert askroute.classify("question", "merge PR #12") == (askroute.APPROVAL, "merge")


async def test_bus_info_shows_the_mandate(chat, monkeypatch):
    """RT-6.26 step 6: role, by, until, holder, post; why when none holds."""
    class _Info(_Homes):
        trusted_source = "MM_TRUSTED_HUMANS"
        channel_name = "agents"

        async def username(self, uid):
            return self.names.get(uid, "")

    async def none(client):
        return []
    c = _Info()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_guest_names", none)
    monkeypatch.setattr(server, "_own_channels", none)
    monkeypatch.setattr(server, "_newer_installed", lambda: None)
    c.bots = {"agent-hub": ChannelUser(HOLDER, "agent-hub")}
    got = json.loads(await server.bus_info())["mandate"]
    assert got == {"role": "👑 of ~hub", "by": "dembaca",
                   "until": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime((NOW_MS - 60_000 + 7 * DAY) / 1000)),
                   "holder": "agent-hub", "post": MPOST}
    mandate._fetched.clear()
    c.pinned = []
    assert json.loads(await server.bus_info())["mandate"] == {"why": "none pinned in this home"}
