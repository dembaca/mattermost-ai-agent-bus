"""RT-6.17 the hook API: turn hooks and listener ask the MCP server
(docs/arc42/06-runtime-view.md)."""

import asyncio
import json
import os
import stat
import time

import httpx
import pytest

from mattermost_ai_agent_bus import channel, hookapi, lease, ownpost, session_file, unread
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import UnreadChannel

from fakes import HookClient

UID = "u" * 26
OTHER = "o" * 26
PERSON = "q" * 26
ASK = "k" * 26


class FakeClient(HookClient):
    """HookClient whose bot lookup can fail."""

    def __init__(self, posts=(), reactions=None, agents=(), token="t", fail=False):
        super().__init__(uid=UID, posts=posts, reactions=reactions, agents=agents)
        self.token = token
        self.fail = fail

    async def get_me(self):
        if self.fail:
            raise httpx.ConnectError("down")
        return await super().get_me()


@pytest.fixture
async def api():
    """serve(client) → (HookAPI, httpx client with the key); closed afterwards."""
    made = []

    async def serve(client, *, write_file=False):
        async def get():
            return client

        a = hookapi.HookAPI(get)
        await a.start(write_file=write_file)
        h = httpx.AsyncClient(base_url=a.endpoint, headers={"Authorization": f"Bearer {a.key}"})
        made.append((a, h))
        return a, h

    yield serve
    for a, h in made:
        await h.aclose()
        await a.close()


def post(pid, message, user=OTHER, at=1000, root="", props=None, type_=""):
    return {"id": pid, "user_id": user, "message": message, "create_at": at,
            "root_id": root, "type": type_, "props": props or {}}


async def test_every_request_needs_the_key(api):
    """RT-6.17 step 1: 127.0.0.1 only; without the session's key, 401 —
    for unknown routes too, so the routes are not probed."""
    a, h = await api(FakeClient())
    assert a.endpoint.startswith("http://127.0.0.1:") and len(a.key) >= 43
    async with httpx.AsyncClient(base_url=a.endpoint) as anon:
        assert (await anon.get("/v1/me")).status_code == 401
        assert (await anon.get("/v1/me", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await anon.get("/v1/nothing")).status_code == 401
        assert (await anon.get("/v1/me", headers={"Authorization": a.key})).status_code == 401
    assert (await h.get("/v1/me")).status_code == 200
    assert (await h.get("/v1/nothing")).status_code == 404
    assert (await h.post("/v1/me")).status_code == 404


async def test_no_session_and_a_dead_bus_fail_the_hook_open(api):
    """RT-6.17 error handling: no token yet → 503, Mattermost down → 502; the
    hooks treat either as nothing to do (RT-6.4)."""
    _, h = await api(FakeClient(token=None))
    assert (await h.get("/v1/me")).status_code == 503
    _, h = await api(None)
    assert (await h.get("/v1/pending")).status_code == 503
    _, h = await api(FakeClient(fail=True))
    r = await h.get("/v1/me")
    assert r.status_code == 502 and r.json() == {"error": "bus unreachable"}


async def test_me_names_the_bot_its_project_and_crown(api, tmp_path, monkeypatch):
    """RT-6.17: who the bot is, and the crown deadline the Stop hook and the
    listener need (RT-6.6)."""
    client = FakeClient()
    _, h = await api(client)
    assert (await h.get("/v1/me")).json() == {
        "user_id": UID, "username": "agent-me", "project": False, "crown_until": 0,
        "channel": False}
    until = int(time.time()) + 600
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"coord-{UID}.env").write_text(f"CROWN_UNTIL={until}\n")
    client.project_channel_name = "proj-x"
    body = (await h.get("/v1/me")).json()
    assert body["project"] is True and body["crown_until"] == until
    client.project_channel_name = ""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", "p" * 26)
    assert (await h.get("/v1/me")).json()["project"] is True


async def test_pending_counts_and_details_when_asked(api, monkeypatch):
    """RT-6.17, RT-6.2 step 1: the count over all channels (unread.py, with
    the server's token); per channel with fyi counts only when asked."""
    calls = []
    rows = [unread.Pending(UnreadChannel("dm", "D"), 3, []),
            unread.Pending(UnreadChannel("chan"), 1, [])]

    async def channels(client, watched):
        calls.append("channels")
        return list(rows)

    async def detail(client, got):
        calls.append(("detail", tuple(got)))
        return [{"id": "dm", "type": "D", "pending": 3, "fyi": 1}]

    monkeypatch.setattr(unread, "channels", channels)
    monkeypatch.setattr(unread, "detail", detail)
    _, h = await api(FakeClient())
    assert (await h.get("/v1/pending")).json() == {"count": 4}
    assert calls == ["channels"]
    body = (await h.get("/v1/pending?detail=1")).json()
    assert body == {"count": 4, "channels": [{"id": "dm", "type": "D", "pending": 3, "fyi": 1}]}
    assert calls[-1] == ("detail", tuple(rows))
    rows.clear()
    calls.clear()
    assert (await h.get("/v1/pending?detail=1")).json() == {"count": 0}
    assert calls == ["channels"]


async def test_last_post_from_the_file_else_the_channel(api):
    """RT-6.17, RT-6.2 step 2: last-<uid>.env first, without a request; else
    the channel's last 100 posts, without the crown; at 0 when none is own."""
    client = FakeClient(posts=[post("a", "hi", at=3000),
                               post("b", "👑 IDLE · done\nnext: x", user=UID, at=2000)])
    _, h = await api(client)
    assert (await h.get("/v1/last-post")).json() == {"at": 2000, "message": "IDLE · done next: x",
                                                     "signed_off": True}
    assert client.calls == [("posts", 100, None)]
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"last-{UID}.env").write_text("CHANNEL=c\nID=z\nAT=5000\nMSG=BYE · gone\n")
    client.calls.clear()
    assert (await h.get("/v1/last-post")).json() == {"at": 5000, "message": "BYE · gone",
                                                     "signed_off": True}
    assert client.calls == []
    (d / f"last-{UID}.env").unlink()
    client.posts = [post("a", "hi")]
    assert (await h.get("/v1/last-post")).json() == {"at": 0, "message": "", "signed_off": False}


async def test_signed_off_until_something_changes_on_the_bus(api):
    """RT-6.2 step 2 (0.65.1): signed off while the latest own post is an IDLE
    and no addressed event reached the agent after it; a delivery or a new own
    post ends it. A reminder the server sent is no own post."""
    reminder = {"a2a": {"v": 1, "kind": "reminder", "intent": "fyi"}}
    client = FakeClient(posts=[post("r", "🔔 @dembaca — reminder: **Merge?**", user=UID, at=3000,
                                    props=reminder),
                               post("b", "IDLE · done", user=UID, at=2000)])
    _, h = await api(client)
    assert (await h.get("/v1/last-post")).json()["signed_off"] is True
    ownpost.record(UID, "c", post_of({"id": "i" * 26, "message": "👑 IDLE · done", "create_at": 4000}))
    ownpost.record(UID, "c", post_of({"id": "r" * 26, "message": "🔔 reminder", "create_at": 5000,
                                      "props": reminder}))
    assert (await h.get("/v1/last-post")).json() == {"at": 4000, "message": "IDLE · done",
                                                     "signed_off": True}
    ownpost.delivered(UID)
    assert (await h.get("/v1/last-post")).json()["signed_off"] is False
    ownpost.record(UID, "c", post_of({"id": "j" * 26, "message": "IDLE · answered", "create_at": 6000}))
    assert (await h.get("/v1/last-post")).json()["signed_off"] is True
    ownpost.record(UID, "c", post_of({"id": "k" * 26, "message": "ACK · on it", "create_at": 7000}))
    assert (await h.get("/v1/last-post")).json()["signed_off"] is False


async def test_posts_say_what_the_listener_needs(api):
    """RT-6.17, RT-6.2 step 4: slim posts, newest first, with `to` all
    (RT-6.11), an agent's fyi (RT-6.14: a person's props count for nothing)
    and gated (RT-6.16)."""
    fyi = {"a2a": {"v": 1, "kind": "done", "intent": "fyi"}}
    client = FakeClient(agents={OTHER}, posts=[
        post("p1", "DONE · merged", props=fyi),
        post("p2", "DONE · merged", user=PERSON, props=fyi),
        post("p3", "main is red", props={"a2a": {"v": 1, "to": "all"}}, root="r"),
        post("p4", "odd", props={"a2a": "all"}, type_="system_join_channel"),
    ])
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"gate-{UID}.env").write_text(f"GATED=p3 {int(time.time())}\n")
    _, h = await api(client)
    body = (await h.get("/v1/posts?limit=500")).json()
    assert body["channel"] == "agents"  # the work channel's name, for mm-agent-poll.sh
    got = body["posts"]
    assert client.calls == [("posts", 200, None)]
    assert [p["id"] for p in got] == ["p1", "p2", "p3", "p4"]
    assert [p["fyi"] for p in got] == [True, False, False, False]
    assert [p["to"] for p in got] == ["", "", "all", ""]
    assert [p["gated"] for p in got] == [False, False, True, False]
    assert got[2] == {"id": "p3", "user_id": OTHER, "root_id": "r", "create_at": 1000, "type": "",
                      "message": "main is red", "to": "all", "intent": "", "fyi": False, "gated": True}
    assert got[3]["type"] == "system_join_channel" and "props" not in got[0]
    await h.get("/v1/posts?since=1234")
    assert client.calls[-1] == ("posts", None, 1234)
    assert (await h.get("/v1/posts?since=yesterday")).status_code == 400


async def test_posts_name_their_files(api):
    """RT-6.23 step 1: a post with attachments names them as a slim post does,
    for mm-agent-poll.sh and the compaction hook; one without has no `files`."""
    fid = "f" * 26
    shot = {"id": fid, "name": "shot.png", "extension": "png", "size": 9, "mime_type": "image/png"}
    client = FakeClient(posts=[post("p1", "see this") | {"file_ids": [fid], "metadata": {"files": [shot]}},
                               post("p2", "no files")])
    _, h = await api(client)
    got = (await h.get("/v1/posts?limit=5")).json()["posts"]
    assert got[0]["files"] == [{"id": fid, "name": "shot.png", "type": "png", "size": 9}]
    assert "files" not in got[1]


async def test_ask_reactions_counts_only_what_was_not_handed_over(api):
    """RT-6.17, RT-6.11: a reaction raises no counter; the server counts those
    on open asks that are neither own nor SEEN."""
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"asks-{UID}.env").write_text(f"ASK={ASK} {int(time.time())}\nSEEN={ASK}:{PERSON}:eyes\n")
    client = FakeClient(reactions={ASK: [{"user_id": PERSON, "emoji_name": "eyes"},
                                         {"user_id": UID, "emoji_name": "white_check_mark"}]})
    _, h = await api(client)
    assert (await h.get("/v1/ask-reactions")).json() == {"fresh": 0}
    client.reacts[ASK].append({"user_id": PERSON, "emoji_name": "+1"})
    assert (await h.get("/v1/ask-reactions")).json() == {"fresh": 1}
    # RT-6.16 step 7: a reaction the server gated wakes nobody.
    (d / f"gate-{UID}.env").write_text(f"GATED={ASK}:{PERSON}:+1 {int(time.time())}\n")
    assert (await h.get("/v1/ask-reactions")).json() == {"fresh": 0}


async def test_presence_sets_the_status_lease(api, monkeypatch):
    """RT-6.17, RT-6.2, RT-6.3, RT-6.6: working / idle / bus with the caller's
    label, listening until an epoch, kept by lease.py; nothing else."""
    runs = []

    async def set_status(client, state, label=""):
        runs.append((state, label))

    monkeypatch.setattr(lease, "set_status", set_status)
    _, h = await api(FakeClient())
    assert (await h.post("/v1/presence", json={"state": "working", "label": "proj"})).json() == {"ok": True}
    await h.post("/v1/presence", json={"state": "idle"})
    until = int(time.time()) + 600
    await h.post("/v1/presence", json={"state": "listening", "until": until})
    await h.post("/v1/presence", json={"state": "bus", "label": "proj"})
    assert runs == [("working", "proj"), ("idle", ""), ("listening", str(until)), ("bus", "proj")]
    for body in ({"state": "offline"}, {"state": "listening"}, {"state": "listening", "until": "x"}):
        assert (await h.post("/v1/presence", json=body)).status_code == 400
    assert (await h.post("/v1/presence", content=b"[1]")).status_code == 400
    assert (await h.post("/v1/presence", content=b"{nope")).status_code == 400
    assert len(runs) == 4


async def test_the_file_names_endpoint_and_key_and_goes_at_exit(api, monkeypatch):
    """RT-6.17 step 2: the discovery file is the session's (keyed like the
    session file, 0600) and removed when the server ends — unless a newer
    server of the same session wrote its own."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    a, _ = await api(FakeClient(), write_file=True)
    f = hookapi.file_path()
    assert f.name.startswith("endpoint-sid-") and f.parent == session_file.state_dir()
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert session_file.parse(f) == {"MM_BUS_ENDPOINT": a.endpoint, "MM_BUS_KEY": a.key,
                                     "MM_BUS_PID": str(os.getpid())}
    await a.close()
    assert not f.exists()
    b, _ = await api(FakeClient(), write_file=True)
    b._server.close()  # `claude --continue`: the old server is gone, its file not yet
    await b._server.wait_closed()
    c, _ = await api(FakeClient(), write_file=True)
    assert not c.conflict
    await b.close()
    assert session_file.parse(f)["MM_BUS_KEY"] == c.key
    await c.close()
    assert not f.exists()


async def test_a_malformed_request_is_refused(api):
    a, _ = await api(FakeClient())
    host, port = a.endpoint.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"nonsense\r\n\r\n")
    await writer.drain()
    head = (await reader.read()).decode()
    writer.close()
    assert head.startswith("HTTP/1.1 400") and json.loads(head.split("\r\n\r\n", 1)[1])


async def test_presence_takes_a_short_label_and_a_near_until(api, monkeypatch):
    """RT-6.17 step 3: the label is one line of at most 64 characters (stripped),
    `until` an epoch within a day, `state` one of four; anything else is 400
    and sets nothing."""
    runs = []

    async def set_status(client, state, label=""):
        runs.append((state, label))

    monkeypatch.setattr(lease, "set_status", set_status)
    _, h = await api(FakeClient())
    assert (await h.post("/v1/presence", json={"state": "idle", "label": "  " + "x" * 64 + " "})).status_code == 200
    assert runs == [("idle", "x" * 64)]
    now = int(time.time())
    for body in ({"state": "working", "label": "x" * 65}, {"state": "working", "label": "a\nb"},
                 {"state": "working", "label": "a\rb"}, {"state": "working", "label": "a\tb"},
                 {"state": "working", "label": 7}, {"state": "working", "label": ["x"]},
                 {"state": "listening", "until": now + 2 * 86400}, {"state": "listening", "until": -5},
                 {"state": "listening", "until": True}, {"state": "listening", "until": 1.5e9},
                 {"state": "Working"}, {"state": None}):
        assert (await h.post("/v1/presence", json=body)).status_code == 400, body
    assert len(runs) == 1


async def test_the_file_learns_the_bot_id(api, monkeypatch):
    """RT-6.17 step 2, error handling: the endpoint file names the bot's id
    (not secret) once the server knows it, so the prompt hook can clear the
    turn state and stop the listener without the server."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-uid")
    a, h = await api(FakeClient(), write_file=True)
    f = hookapi.file_path()
    assert "MM_BOT_USER_ID" not in session_file.parse(f)
    assert (await h.get("/v1/me")).status_code == 200
    assert session_file.parse(f)["MM_BOT_USER_ID"] == UID
    assert session_file.parse(f)["MM_BUS_KEY"] == a.key
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    monkeypatch.setenv("MM_BOT_USER_ID", OTHER)
    # a looks gone to b (`claude --continue`), so b takes the session's file over.
    monkeypatch.setattr(hookapi, "_answers", lambda endpoint, key: False)
    b, _ = await api(FakeClient(), write_file=True)
    assert session_file.parse(f)["MM_BOT_USER_ID"] == OTHER
    await h.get("/v1/me")   # a's file is b's now: a leaves it alone
    assert session_file.parse(f) == {"MM_BUS_ENDPOINT": b.endpoint, "MM_BUS_KEY": b.key,
                                     "MM_BUS_PID": str(os.getpid()), "MM_BOT_USER_ID": OTHER}


async def _raw(endpoint):
    host, port = endpoint.removeprefix("http://").split(":")
    return await asyncio.open_connection(host, int(port))


async def test_connections_beyond_the_cap_are_closed(api, monkeypatch):
    """RT-6.17 step 1: at most MAX_CONNECTIONS at once; one more is closed
    without an answer and without work."""
    monkeypatch.setattr(hookapi, "MAX_CONNECTIONS", 2)
    a, h = await api(FakeClient())
    held = [await _raw(a.endpoint) for _ in range(2)]   # say nothing: held open
    await asyncio.sleep(0.1)
    reader, writer = await _raw(a.endpoint)
    writer.write(f"GET /v1/me HTTP/1.1\r\nAuthorization: Bearer {a.key}\r\n\r\n".encode())
    try:
        answer = await asyncio.wait_for(reader.read(), 2)
    except ConnectionResetError:   # closed with the request unread
        answer = b""
    assert answer == b""
    writer.close()
    for _, w in held:
        w.close()
    await asyncio.sleep(0.1)
    assert (await h.get("/v1/me")).status_code == 200


async def test_a_client_that_does_not_read_is_dropped(api, monkeypatch):
    """RT-6.17 step 1: writing the answer gives up after WRITE_TIMEOUT_SEC."""
    monkeypatch.setattr(hookapi, "WRITE_TIMEOUT_SEC", 0.1)
    a, _ = await api(FakeClient())

    class Stuck:
        closed = aborted = False

        def __init__(self):
            self.transport = self

        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(3600)

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    reader = asyncio.StreamReader()
    reader.feed_data(f"GET /v1/me HTTP/1.1\r\nAuthorization: Bearer {a.key}\r\n\r\n".encode())
    reader.feed_eof()
    w = Stuck()
    await asyncio.wait_for(a._handle(reader, w), 2)
    assert w.aborted and w.closed


async def test_heavy_routes_are_limited(api, monkeypatch):
    """RT-6.17 step 4: requests to the Mattermost-heavy routes (pending,
    presence) run MAX_HEAVY_RUNS at a time; the others are not held up."""
    running, peak, gate_ = 0, 0, asyncio.Event()

    async def hold():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate_.wait()
        running -= 1

    async def channels(client, watched):
        await hold()
        return []

    async def set_status(client, state, label=""):
        await hold()

    monkeypatch.setattr(unread, "channels", channels)
    monkeypatch.setattr(lease, "set_status", set_status)
    _, h = await api(FakeClient())
    calls = [asyncio.create_task(h.get("/v1/pending")) for _ in range(3)]
    calls.append(asyncio.create_task(h.post("/v1/presence", json={"state": "idle"})))
    await asyncio.sleep(0.3)
    assert running == hookapi.MAX_HEAVY_RUNS == 2
    assert (await h.get("/v1/me")).status_code == 200
    gate_.set()
    assert [r.status_code for r in await asyncio.gather(*calls)] == [200] * 4
    assert peak == 2


async def test_stat_counts_into_the_servers_own_file(api):
    """RT-6.18 (RT-6.9): the hooks of a keyless session cannot reach the
    server's state dir; they count through the API, names and values only."""
    _, h = await api(FakeClient())
    assert (await h.post("/v1/stat", json={"k": "turn", "by": "bus"})).json() == {"ok": True}
    assert (await h.post("/v1/stat", json={"k": "transcript", "path": "/x/t.jsonl", "t": 5})).status_code == 200
    rows = [json.loads(line) for line in
            (session_file.state_dir() / f"stats-{UID}.jsonl").read_text().splitlines()]
    assert [(e["k"], e.get("by") or e.get("path")) for e in rows] == [
        ("turn", "bus"), ("transcript", "/x/t.jsonl")]
    assert rows[1]["t"] != 5  # the server's own time
    for bad in ({"k": "evil"}, {"k": "turn", "Bad": 1}, {"k": "turn", "a": [1]},
                {"k": "turn", "a": 1, "b": 2, "c": 3, "d": 4, "e": 5}, {"k": "wake", "a": "x" * 5000}):
        assert (await h.post("/v1/stat", json=bad)).status_code == 400


async def test_me_says_whether_the_channel_is_confirmed(api):
    """RT-6.18 (RT-6.12): the hooks of a keyless session ask, they cannot see the file."""
    _, h = await api(FakeClient())
    assert (await h.get("/v1/me")).json()["channel"] is False
    session_file.write_private(channel.confirmed_file(UID), "CONFIRMED=1\n")
    assert (await h.get("/v1/me")).json()["channel"] is True


async def test_post_and_reply_run_the_tools_code(api, monkeypatch):
    """RT-6.17 step 3: POST /v1/post and /v1/reply hand the fields to the
    server's post_as_bot / reply_as_bot (the tools' code) and answer what they
    return, a refusal included; anything but the post fields as strings is a 400."""
    calls = []

    async def post_as_bot(client, **kw):
        calls.append(("post", kw))
        return {"posted": False, "refused": "kind must be one of …"} if kw["kind"] else {"id": "n"}

    async def reply_as_bot(client, **kw):
        calls.append(("reply", kw))
        return {"id": "r"}

    monkeypatch.setattr(hookapi, "_tools", lambda: type("M", (), {
        "post_as_bot": staticmethod(post_as_bot), "reply_as_bot": staticmethod(reply_as_bot)}))
    _, h = await api(FakeClient())
    r = await h.post("/v1/post", json={"message": "hi", "to": "all"})
    assert r.status_code == 200 and r.json() == {"id": "n"}
    assert calls[-1] == ("post", {"message": "hi", "channel_id": "", "root_id": "",
                                  "to": "all", "kind": ""})
    assert (await h.post("/v1/post", json={"message": "x", "kind": "SHOUT"})).json()["posted"] is False
    assert (await h.post("/v1/reply", json={"root_id": ASK, "message": "ok"})).json() == {"id": "r"}
    for bad in ({"message": "x", "token": "t"}, {"message": 1}, {"message": ""}):
        assert (await h.post("/v1/post", json=bad)).status_code == 400
    assert (await h.post("/v1/reply", json={"message": "x"})).status_code == 400
    monkeypatch.setattr(hookapi, "_tools", lambda: None)  # a host process before its first relay
    assert (await h.post("/v1/post", json={"message": "x"})).status_code == 503


async def test_post_by_channel_name_only_in_the_bots_channels(api, monkeypatch):
    """RT-6.22 step 4, RT-6.17: mm-agent-post.sh --channel NAME posts in the
    home, a guest channel or the lobby by name; a channel of the team the bot
    is not in, or none of that name, is a 400 and nothing is posted or joined."""
    lobby, guest = "l" * 26, "g" * 26
    monkeypatch.setenv("MM_CHANNEL_ID", lobby)
    monkeypatch.delenv("MM_PROJECT_CHANNEL_ID", raising=False)
    calls = []

    async def post_as_bot(client, **kw):
        calls.append(kw)
        return {"id": "n"}

    monkeypatch.setattr(hookapi, "_tools", lambda: type("M", (), {
        "post_as_bot": staticmethod(post_as_bot), "reply_as_bot": staticmethod(post_as_bot)}))
    c = FakeClient()
    home = await c.work_channel_id()
    names = {"home-x": home, "agents": lobby, "guest-x": guest, "elsewhere": "e" * 26}

    async def get_channel_id(name=None):
        if name not in names:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "http://x"),
                                        response=httpx.Response(404))
        return names[name]

    async def guest_channel_ids():
        return {guest}

    c.get_channel_id, c.guest_channel_ids = get_channel_id, guest_channel_ids
    _, h = await api(c)
    for name, cid in (("home-x", home), ("~guest-x", guest), ("agents", lobby)):
        r = await h.post("/v1/post", json={"message": "hi", "channel": name})
        assert r.status_code == 200, r.text
        assert calls[-1]["channel_id"] == cid and "channel" not in calls[-1]
    n = len(calls)
    r = await h.post("/v1/post", json={"message": "hi", "channel": "elsewhere"})
    assert r.status_code == 400 and "not the home, a guest channel or the lobby" in r.json()["error"]
    r = await h.post("/v1/post", json={"message": "hi", "channel": "nope"})
    assert r.status_code == 400 and "no channel nope" in r.json()["error"]
    for bad in ({"channel_id": guest}, {"to": "agent-b"}):
        r = await h.post("/v1/post", json={"message": "hi", "channel": "guest-x", **bad})
        assert r.status_code == 400
    r = await h.post("/v1/reply", json={"root_id": "r" * 26, "message": "x", "channel": "guest-x"})
    assert r.status_code == 400
    assert len(calls) == n


async def test_posts_only_of_the_channels_the_bus_watches(api, monkeypatch):
    """RT-6.17, arc42 §7.3: mm-agent-poll.sh --channel reads the work, the
    project or the default channel, by id or name; a DM, a group channel or
    another channel the bot is in is a 403, read from nowhere."""
    proj, default = "p" * 26, "d" * 26
    monkeypatch.setenv("MM_CHANNEL_ID", default)
    monkeypatch.delenv("MM_PROJECT_CHANNEL_ID", raising=False)
    c = FakeClient(posts=[post("p1", "hi")])
    c.project_channel_name, c.channel_name = "proj-x", "agents"
    names = {"proj-x": proj, "agents": default, "elsewhere": "e" * 26}
    seen = []

    async def get_channel_id(name=None):
        return names[name]

    async def channel_posts(cid, *, per_page=None, since_ms=None):
        seen.append(cid)
        return [post_of(p) for p in c.posts]

    c.get_channel_id, c.channel_posts = get_channel_id, channel_posts
    _, h = await api(c)
    for chan in (proj, default, "proj-x", "c" * 26):
        assert (await h.get(f"/v1/posts?channel={chan}&since=1")).status_code == 200
    assert seen == [proj, default, proj, "c" * 26]
    dm, group = "x" * 26, "g" * 26
    for chan in (dm, group, "elsewhere"):
        r = await h.get(f"/v1/posts?channel={chan}&since=1")
        assert r.status_code == 403 and "work, project, default and guest" in r.json()["error"]
    assert (await h.get("/v1/posts?channel=../x")).status_code == 400
    assert len(seen) == 4


async def test_a_harness_with_an_inherited_session_id_leaves_the_file(api, monkeypatch):
    """RT-6.17 error handling: a harness started from a Claude Code shell
    (codex exec, opencode) inherits CLAUDE_CODE_SESSION_ID. Its server leaves
    the live server's file alone and names both clients in its warning."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    a, _ = await api(FakeClient(), write_file=True)
    a.note_client("claude-code")
    f = hookapi.file_path()
    assert session_file.parse(f)["MM_BUS_CLIENT"] == "claude-code"
    # Both serve on this test's one loop: b's probe of a (GET /v1/me within
    # 1 s) cannot be answered here, so it is taken as answered.
    monkeypatch.setattr(hookapi, "_answers", lambda endpoint, key: key == a.key)
    b, _ = await api(FakeClient(), write_file=True)
    b.note_client("codex-mcp-client")
    assert session_file.parse(f)["MM_BUS_KEY"] == a.key
    w = b.warning()
    assert "client claude-code" in w and "codex-mcp-client" in w
    assert "env -u CLAUDE_CODE_SESSION_ID" in w and not a.warning()


async def test_rekey_writes_the_new_file_before_the_old_one_goes(api, monkeypatch, tmp_path):
    """RT-6.17 step 2: when the roots name the workspace, the file moves; a
    shell looking meanwhile finds the old file or the new one, never neither."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "MM_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session_file, "_roots_dir", None)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    monkeypatch.chdir(one)
    a, _ = await api(FakeClient(), write_file=True)
    old = hookapi.file_path()
    assert old.exists()
    order, real = [], os.replace

    def replace(src, dst):
        real(src, dst)
        order.append(("new written, old still there", old.exists()))

    monkeypatch.setattr(os, "replace", replace)
    assert session_file.set_roots_dir(str(two.resolve()))
    a.rekey()
    assert order == [("new written, old still there", True)]
    assert not old.exists() and session_file.parse(hookapi.file_path())["MM_BUS_KEY"] == a.key


async def test_no_file_keyed_on_home_or_above(api, monkeypatch, tmp_path):
    """RT-6.17 step 2: a server started in the home directory (or /) keys no
    endpoint file there, where a shell anywhere below would find it; it waits
    for the client's roots and says so (bus_info's warning)."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "MM_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session_file, "_roots_dir", None)
    home, proj = tmp_path / "home", tmp_path / "home" / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)
    a, _ = await api(FakeClient(), write_file=True)
    assert not hookapi.file_path().exists() and not list(session_file.state_dir().glob("endpoint-*"))
    assert "MM_WORKSPACE" in a.warning() and str(home.resolve()) in a.warning()
    monkeypatch.chdir("/")
    assert session_file.unkeyed() == "/"
    monkeypatch.chdir(home)
    assert session_file.set_roots_dir(str(proj.resolve()))
    a.rekey()
    assert hookapi.file_path().exists() and not a.warning()
    monkeypatch.setenv("MM_WORKSPACE", str(home))
    assert session_file.unkeyed() == ""  # a variable that names it is taken
