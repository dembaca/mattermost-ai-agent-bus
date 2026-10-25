"""RT-6.17 the hook API: turn hooks and listener ask the MCP server
(docs/arc42/06-runtime-view.md)."""

import asyncio
import json
import os
import stat
import time

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi, lease, session_file

UID = "u" * 26
OTHER = "o" * 26
PERSON = "q" * 26
ASK = "k" * 26


class FakeClient:
    """What the routes use of MattermostClient."""

    def __init__(self, posts=(), reactions=None, agents=(), token="t", fail=False):
        self.token = token
        self.project_channel_name = ""
        self.posts = list(posts)
        self._reactions = reactions or {}
        self._agents = set(agents)
        self.fail = fail
        self.calls = []

    async def get_me(self):
        if self.fail:
            raise httpx.ConnectError("down")
        return {"id": UID, "username": "agent-me"}

    async def work_channel_id(self):
        return "c" * 26

    async def channel_posts(self, cid, *, per_page=None, since_ms=None):
        self.calls.append(("posts", per_page, since_ms))
        return list(self.posts)

    async def agents(self, ids):
        return set(ids) & self._agents

    async def reactions(self, pid):
        return self._reactions.get(pid, [])


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
        "user_id": UID, "username": "agent-me", "project": False, "crown_until": 0}
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


async def test_pending_runs_the_unread_counters(api, monkeypatch):
    """RT-6.17, RT-6.2 step 1: the counters by the shell CLI's rules, run with
    the server's token; per channel with fyi counts only when asked."""
    runs = []

    async def script(client, name, *args):
        runs.append((name, *args))
        if args == ("count",):
            return "4"
        return ("d" * 26 + "\tD\tpending=3\tfyi=1\n" + "c" * 26 + "\tO\tpending=1\n"
                + "".join(f"{i}\tO\tpending=0\n" for i in range(6)))

    monkeypatch.setattr(lease, "script", script)
    _, h = await api(FakeClient())
    assert (await h.get("/v1/pending")).json() == {"count": 4}
    assert runs == [("mm-agent-unread.sh", "count")]
    body = (await h.get("/v1/pending?detail=1")).json()
    assert body["count"] == 4 and len(body["channels"]) == 5
    assert body["channels"][:2] == [{"id": "d" * 26, "type": "D", "pending": 3, "fyi": 1},
                                    {"id": "c" * 26, "type": "O", "pending": 1, "fyi": 0}]
    runs.clear()

    async def nothing(client, name, *args):
        runs.append(args)
        return "0"

    monkeypatch.setattr(lease, "script", nothing)
    assert (await h.get("/v1/pending?detail=1")).json() == {"count": 0}
    assert runs == [("count",)]


async def test_last_post_from_the_file_else_the_channel(api):
    """RT-6.17, RT-6.2 step 2: last-<uid>.env first, without a request; else
    the channel's last 100 posts, without the crown; at 0 when none is own."""
    client = FakeClient(posts=[post("a", "hi", at=3000),
                               post("b", "👑 IDLE · done\nnext: x", user=UID, at=2000)])
    _, h = await api(client)
    assert (await h.get("/v1/last-post")).json() == {"at": 2000, "message": "IDLE · done next: x"}
    assert client.calls == [("posts", 100, None)]
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"last-{UID}.env").write_text("CHANNEL=c\nID=z\nAT=5000\nMSG=BYE · gone\n")
    client.calls.clear()
    assert (await h.get("/v1/last-post")).json() == {"at": 5000, "message": "BYE · gone"}
    assert client.calls == []
    (d / f"last-{UID}.env").unlink()
    client.posts = [post("a", "hi")]
    assert (await h.get("/v1/last-post")).json() == {"at": 0, "message": ""}


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
    got = (await h.get("/v1/posts?limit=500")).json()["posts"]
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
    client._reactions[ASK].append({"user_id": PERSON, "emoji_name": "+1"})
    assert (await h.get("/v1/ask-reactions")).json() == {"fresh": 1}


async def test_presence_runs_the_status_script(api, monkeypatch):
    """RT-6.17, RT-6.2, RT-6.3: working / idle with the hook's label, listening
    until an epoch, through mm-agent-status.sh; nothing else."""
    runs = []

    async def script(client, name, *args):
        runs.append((name, *args))
        return ""

    monkeypatch.setattr(lease, "script", script)
    _, h = await api(FakeClient())
    assert (await h.post("/v1/presence", json={"state": "working", "label": "proj"})).json() == {"ok": True}
    await h.post("/v1/presence", json={"state": "idle"})
    until = int(time.time()) + 600
    await h.post("/v1/presence", json={"state": "listening", "until": until})
    assert runs == [("mm-agent-status.sh", "working", "proj"), ("mm-agent-status.sh", "idle", ""),
                    ("mm-agent-status.sh", "listening", str(until))]
    for body in ({"state": "offline"}, {"state": "listening"}, {"state": "listening", "until": "x"}):
        assert (await h.post("/v1/presence", json=body)).status_code == 400
    assert (await h.post("/v1/presence", content=b"[1]")).status_code == 400
    assert (await h.post("/v1/presence", content=b"{nope")).status_code == 400
    assert len(runs) == 3


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
    c, _ = await api(FakeClient(), write_file=True)
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
    `until` an epoch within a day, `state` one of three; anything else is 400
    and runs nothing."""
    runs = []

    async def script(client, name, *args):
        runs.append(args)
        return ""

    monkeypatch.setattr(lease, "script", script)
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


async def test_script_runs_are_limited(api, monkeypatch):
    """RT-6.17 step 4: requests that run a shell CLI script (pending,
    presence) run MAX_SCRIPT_RUNS at a time; the others are not held up."""
    running, peak, gate_ = 0, 0, asyncio.Event()

    async def script(client, name, *args):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate_.wait()
        running -= 1
        return "0"

    monkeypatch.setattr(lease, "script", script)
    _, h = await api(FakeClient())
    calls = [asyncio.create_task(h.get("/v1/pending")) for _ in range(3)]
    calls.append(asyncio.create_task(h.post("/v1/presence", json={"state": "idle"})))
    await asyncio.sleep(0.3)
    assert running == hookapi.MAX_SCRIPT_RUNS == 2
    assert (await h.get("/v1/me")).status_code == 200
    gate_.set()
    assert [r.status_code for r in await asyncio.gather(*calls)] == [200] * 4
    assert peak == 2
