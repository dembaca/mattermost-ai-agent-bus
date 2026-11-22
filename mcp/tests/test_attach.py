"""RT-6.33: an agent attaches a file (ADR-012). The bytes come from inside the
sandbox (the hook API's POST /v1/files, or the attach tool's text), never
from a path the server opens; the server checks the type by its bytes, the
name, the channel, and lets a post carry only its own fresh uploads."""

import asyncio
import json

import httpx
import pytest
from fakes import BOT, WORK, HookClient

from mattermost_ai_agent_bus import attach, hookapi, server
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.model import Channel, NotFound, Refused

GUEST, DM, GROUP, OTHER, PEER, ROOT = "g" * 26, "d" * 26, "x" * 26, "o" * 26, "p" * 26, "r" * 26
GONE = "n" * 26  # a channel id that does not exist, or is none of the bot's
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(20)
JPEG = b"\xff\xd8\xff\xe0" + bytes(20)
GIF = b"GIF89a" + bytes(10)
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(10)
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
CSV = "a,b\n1,2\n".encode()


class Chat(HookClient):
    """HookClient with a guest channel, a DM with a peer agent, a group and
    a channel the bot is merely in; the DM with an agent by name; a person
    refused there as the adapter refuses one."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.guests = {GUEST}
        self.channel_ids = {"guest-x": GUEST, "elsewhere": OTHER}

    async def get_channel(self, channel_id: str) -> Channel:
        if channel_id == GONE:
            raise NotFound(f"no channel {channel_id}")
        if channel_id == DM:
            return Channel(id=DM, name=f"{BOT}__{PEER}", type="D")
        return Channel(id=channel_id, name="g-room", type="G" if channel_id == GROUP else "O")

    async def direct_channel_with_bot(self, username: str) -> str:
        if username == "dembaca":
            raise ValueError("dembaca is a person: ask people with ask_human")
        return DM

    async def post_channel_id(self, post_id: str) -> str:
        return GUEST if post_id == ROOT else WORK


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    attach._pending.clear()
    monkeypatch.setattr(server, "_inbox", None)
    yield
    attach._pending.clear()


@pytest.fixture
async def api():
    made = []

    async def serve(client):
        async def get():
            return client

        a = hookapi.HookAPI(get)
        await a.start(write_file=False)
        h = httpx.AsyncClient(base_url=a.endpoint, headers={"Authorization": f"Bearer {a.key}"})
        made.append((a, h))
        return a, h

    yield serve
    for a, h in made:
        await h.aclose()
        await a.close()


# --- what may go out (RT-6.33 step 3) ---------------------------------------------

@pytest.mark.parametrize("name,data,kind", [
    ("chart.png", PNG, "png"), ("photo.JPG", JPEG, "jpeg"), ("a.jpeg", JPEG, "jpeg"), ("x.gif", GIF, "gif"),
    ("x.webp", WEBP, "webp"), ("report.pdf", PDF, "pdf"), ("data.csv", CSV, "text"),
    ("notes.md", "# Hä\n".encode(), "text"), ("x.json", b"{}", "text"), ("run.log", b"ok\n", "text"),
    ("c.yaml", b"a: 1\n", "text"), ("t.tsv", b"a\tb\n", "text"), ("r.txt", b"plain", "text")])
def test_allowed_types_by_their_bytes(name, data, kind):
    """RT-6.33 step 3: the allowlist, by magic bytes; text only as UTF-8 without NUL."""
    assert attach.check(name, data) == (name, kind)


@pytest.mark.parametrize("name,data,why", [
    ("chart.png", b"ssh-ed25519 AAAA key", "named .png but its bytes are a text file"),
    ("id_rsa.txt", b"\x00\x01binary", "no allowed type"),
    ("notes.txt", b"\xff\xfe latin-1 \xe4", "no allowed type"),
    ("x.txt", PNG, "its bytes are a png file"),
    ("x.svg", b"<svg onload='x'/>", "SVG is refused"),
    ("app.exe", b"MZ", ".exe is not allowed"),
    ("x.html", b"<html>", ".html is not allowed"),
    ("x.png", b"", "empty"),
    ("noext", b"a", "a name with an extension"),
    (".png", PNG, "a name with an extension"),
    ("../", PNG, "a name with an extension")])
def test_refused_types_names_and_mismatches(name, data, why):
    """RT-6.33 step 3: a key named chart.png is refused; SVG, other types, an
    empty file and a name without extension too."""
    with pytest.raises(attach.NotAllowed, match=why):
        attach.check(name, data)


def test_the_name_is_a_plain_base_name():
    """RT-6.33 step 3: no directory, no control characters, at most NAME_MAX."""
    assert attach.clean_name("../../home/me/.ssh/chart.png") == "chart.png"
    assert attach.clean_name("C:\\Users\\x\\a.csv") == "a.csv"
    assert attach.clean_name("re\x00po\u202ert\n.md") == "report.md"
    long = attach.clean_name("a" * 300 + ".png")
    assert len(long) == attach.NAME_MAX and long.endswith(".png")


def test_over_the_size_cap_is_refused(monkeypatch):
    monkeypatch.setattr(attach, "FILE_MAX", 10)
    with pytest.raises(attach.NotAllowed, match="over"):
        attach.check("a.txt", b"x" * 11)


# --- POST /v1/files (RT-6.33 steps 1, 2, 4) -----------------------------------------

async def test_the_route_needs_the_key_and_uploads_into_the_home(api):
    """RT-6.33 steps 1 and 4, RT-6.17: the session's key; the body is the file,
    uploaded to the home, answered with its id; the id waits for a post."""
    c = Chat()
    c.upload_ids = ["f" * 26]
    a, h = await api(c)
    async with httpx.AsyncClient(base_url=a.endpoint) as anon:
        assert (await anon.post("/v1/files?name=chart.png", content=PNG)).status_code == 401
    assert c.uploads == []
    r = await h.post("/v1/files?name=chart.png", content=PNG)
    assert r.status_code == 200, r.text
    assert r.json() == {"file_id": "f" * 26, "name": "chart.png", "type": "png", "size": len(PNG),
                        "channel": WORK}
    assert c.uploads == [{"channel_id": WORK, "name": "chart.png", "size": len(PNG), "id": "f" * 26}]
    assert "f" * 26 in attach._pending


async def test_a_body_over_the_cap_is_refused_before_it_is_read(api, monkeypatch):
    """RT-6.33 step 1: Content-Length over FILE_MAX: 413 at once, nothing read;
    every other route keeps its 64 KiB."""
    _, h = await api(Chat())
    a = await api(Chat())
    host, port = a[0].endpoint.removeprefix("http://").split(":")
    r, w = await asyncio.open_connection(host, int(port))
    w.write((f"POST /v1/files?name=a.png HTTP/1.1\r\nAuthorization: Bearer {a[0].key}\r\n"
             f"Content-Length: {attach.FILE_MAX + 1}\r\n\r\n").encode())  # and no body at all
    await w.drain()
    head = await asyncio.wait_for(r.read(), 2)  # answered, not waiting for the bytes
    w.close()
    assert head.startswith(b"HTTP/1.1 413") and b"file over 10 MB" in head
    big = await h.post("/v1/post", content=b"x" * (hookapi.MAX_BODY + 1))
    assert big.status_code == 413


@pytest.mark.parametrize("query,cid", [
    ("", WORK), ("&channel=guest-x", GUEST), ("&channel=~guest-x", GUEST), (f"&channel={GUEST}", GUEST),
    (f"&channel={DM}", DM), ("&to=agent-peer", DM), (f"&root={ROOT}", GUEST)])
async def test_the_channels_a_post_goes_to(api, query, cid):
    """RT-6.33 step 2: the home, a guest channel, the lobby, a DM of the bot;
    by name, id, the thread's root or the agent of the DM."""
    c = Chat()
    _, h = await api(c)
    r = await h.post(f"/v1/files?name=data.csv{query}", content=CSV)
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == cid and c.uploads[-1]["channel_id"] == cid


@pytest.mark.parametrize("query,status,why", [
    (f"&channel={GROUP}", 403, "only the home, a guest channel, the lobby or a DM"),
    (f"&channel={OTHER}", 403, "only the home"),
    (f"&channel={GONE}", 403, "not a channel of the bot"),
    ("&channel=elsewhere", 400, "not the home, a guest channel or the lobby"),
    ("&to=dembaca", 400, "is a person"),
    (f"&to=agent-peer&channel={GUEST}", 400, "one of them"),
    ("&root=../x", 400, "invalid"),
    ("", 400, "named .csv but its bytes"),
])
async def test_refused_channels_and_files(api, query, status, why):
    """RT-6.33 steps 2 and 3: another channel, a group, a person's DM; a
    mismatch between name and bytes: nothing uploaded."""
    c = Chat()
    _, h = await api(c)
    body = PNG if not query else CSV
    r = await h.post(f"/v1/files?name=data.csv{query}", content=body)
    assert r.status_code == status and why in r.json()["error"], r.text
    assert c.uploads == []
    assert (await h.post("/v1/files", content=CSV)).status_code == 400  # no name


async def test_mattermosts_refusal_comes_back(api):
    """RT-6.33 step 4: over Mattermost's own MaxFileSize, attachments off: its
    refusal is the answer, nothing remembered."""
    c = Chat()

    async def upload_file(channel_id, name, data):
        raise Refused("Mattermost refused the file (413): file too large", status=413)
    c.upload_file = upload_file
    _, h = await api(c)
    r = await h.post("/v1/files?name=a.png", content=PNG)
    assert r.status_code == 422 and "file too large" in r.json()["error"]
    assert attach._pending == {}


# --- posts carry their own fresh uploads (RT-6.33 steps 4, 5) -------------------------

async def _upload(c, name="chart.png", data=PNG, channel=WORK) -> str:
    return (await attach.upload(c, name, data, channel))["file_id"]


async def test_a_post_carries_its_uploads_once(monkeypatch):
    """RT-6.33 step 5: post_message(files=…) posts them; the same id again is
    refused, nothing posted; an attachment-only post has no text."""
    c = Chat()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    fid = await _upload(c)
    out = json.loads(await server.post_message("", files=[fid]))
    assert out.get("posted") is not False, out
    assert c.posted[-1]["file_ids"] == [fid] and c.posted[-1]["message"] == ""
    assert out["files"] == [{"id": fid}]
    n = len(c.posted)
    again = json.loads(await server.post_message("again", files=[fid]))
    assert again["posted"] is False and "not an upload of this session" in again["refused"]
    assert len(c.posted) == n


async def test_foreign_expired_and_too_many_ids_are_refused(monkeypatch):
    """RT-6.33 step 5: an id this server did not upload (a human's file, another
    agent's), one older than PENDING_SEC, more than POST_MAX: refused, nothing
    posted, the good ids still usable."""
    c = Chat()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    mine = await _upload(c)
    for files, why in (([mine, "h" * 26], "not an upload of this session"),
                       (["../etc"], "not an upload of this session"),
                       ([mine] + [f"{i:0>26}" for i in range(1, 11)], "at most 10")):
        out = json.loads(await server.post_message("x", files=files))
        assert out["posted"] is False and why in out["refused"], out
    assert c.posted == [] and mine in attach._pending
    for _ in range(attach.PENDING_MAX):  # pushed out by newer uploads: the refusal says so
        await _upload(c)
    out = json.loads(await server.post_message("x", files=[mine]))
    assert out["posted"] is False and f"more than {attach.PENDING_MAX} uploads since" in out["refused"]
    mine = await _upload(c)
    now = attach._pending[mine].at
    monkeypatch.setattr(attach.time, "time", lambda: now + attach.PENDING_SEC + 1)
    out = json.loads(await server.post_message("x", files=[mine]))
    assert out["posted"] is False and c.posted == []


async def test_the_file_must_be_in_the_posts_channel(monkeypatch):
    """RT-6.33 step 5: Mattermost attaches only a file of the post's channel:
    one uploaded elsewhere is refused with the channel to upload to."""
    c = Chat()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    fid = await _upload(c)  # into the home
    out = json.loads(await server.reply_in_thread(ROOT, "here", files=[fid]))  # the root is in GUEST
    assert out["posted"] is False and f"upload it again with channel {GUEST}" in out["refused"]
    assert c.posted == [] and fid in attach._pending
    gid = await _upload(c, channel=GUEST)
    out = json.loads(await server.reply_in_thread(ROOT, "here", files=[gid]))
    assert out.get("posted") is not False and c.posted[-1]["file_ids"] == [gid]


async def test_a_refused_post_gives_its_files_back(monkeypatch):
    """RT-6.33 step 5: Mattermost refuses the post: the uploads stay usable."""
    c = Chat()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    fid = await _upload(c)

    async def post(message, **kw):
        raise Refused("Mattermost refused the post (403)", status=403)
    c.post = post
    out = json.loads(await server.post_message("x", files=[fid]))
    assert out["posted"] is False and fid in attach._pending


async def test_the_shell_routes_pass_files(api, monkeypatch):
    """RT-6.33 step 5, RT-6.17: POST /v1/post and /v1/reply take `files`, a
    list of ids; a file alone is a post; another type is a 400."""
    calls = []

    async def as_bot(client, **kw):
        calls.append(kw)
        return {"id": "n"}

    monkeypatch.setattr(hookapi, "_tools", lambda: type("M", (), {
        "post_as_bot": staticmethod(as_bot), "reply_as_bot": staticmethod(as_bot)}))
    _, h = await api(Chat())
    assert (await h.post("/v1/post", json={"message": "", "files": ["f" * 26]})).status_code == 200
    assert calls[-1]["files"] == ["f" * 26] and calls[-1]["message"] == ""
    r = await h.post("/v1/reply", json={"root_id": ROOT, "message": "x", "files": ["f" * 26]})
    assert r.status_code == 200 and calls[-1]["files"] == ["f" * 26]
    for bad in ({"message": "x", "files": "f"}, {"message": "x", "files": [1]},
                {"message": "x", "files": ["f"] * 11}):
        assert (await h.post("/v1/post", json=bad)).status_code == 400
    assert (await h.post("/v1/post", json={"message": "x"})).status_code == 200
    assert "files" not in calls[-1]


# --- the attach tool (RT-6.33 step 6) ------------------------------------------------

async def test_attach_uploads_a_small_text(monkeypatch):
    """RT-6.33 step 6: a text file without a shell, checked as the route checks."""
    c = Chat()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.attach_text("data.csv", "a,b\n1,2\n"))
    assert out["name"] == "data.csv" and out["type"] == "text" and out["channel"] == WORK
    assert out["file_id"] in attach._pending
    out = json.loads(await server.attach_text("t.md", "# x", channel="~guest-a"))
    assert out["posted"] is False and 'unknown channel "guest-a"' in out["refused"]
    out = json.loads(await server.attach_text("t.md", "# x", channel=DM))
    assert out["channel"] == DM
    out = json.loads(await server.attach_text("t.md", "# x", to="agent-peer"))  # as --to
    assert out["channel"] == DM
    for kw, why in (({"to": "dembaca"}, "is a person"), ({"to": "agent-peer", "channel": DM}, "one of them"),
                    ({"channel": GONE}, "not a channel of the bot")):
        out = json.loads(await server.attach_text("t.md", "# x", **kw))
        assert out["posted"] is False and why in out["refused"], out
    for name, text, why in (("chart.png", "not a png", "named .png"),
                            ("x.svg", "<svg/>", "SVG is refused"),
                            ("big.txt", "x" * (attach.TEXT_MAX + 1), "over 100 KB")):
        out = json.loads(await server.attach_text(name, text))
        assert out["posted"] is False and why in out["refused"], out
    out = json.loads(await server.attach_text("a.txt", "x", channel=GROUP))
    assert out["posted"] is False and "only the home" in out["refused"]
    assert len(c.uploads) == 3


# --- the adapter (RT-6.33 step 4) ------------------------------------------------------

async def test_the_adapter_uploads_multipart_and_posts_the_ids():
    """RT-6.33 step 4: POST /api/v4/files with channel_id and the file; the
    post names the ids in file_ids."""
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers.get("content-type", ""), req.content))
        if req.url.path == "/api/v4/files":
            return httpx.Response(201, json={"file_infos": [
                {"id": "f" * 26, "name": "chart.png", "extension": "png", "size": len(PNG)}]})
        if req.url.path == "/api/v4/posts":
            return httpx.Response(201, json={"id": "n" * 26, **json.loads(req.content)})
        return httpx.Response(200, json={})

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c._me = {"id": BOT, "username": "agent-me"}
    info = await c.upload_file(WORK, "chart.png", PNG)
    assert info.id == "f" * 26 and info.name == "chart.png"
    method, path, ctype, body = seen[0]
    assert (method, path) == ("POST", "/api/v4/files") and ctype.startswith("multipart/form-data")
    assert b'name="channel_id"' in body and WORK.encode() in body and PNG in body
    assert b'filename="chart.png"' in body
    await c.post("see", channel_id=WORK, file_ids=["f" * 26])
    assert json.loads(seen[-1][3])["file_ids"] == ["f" * 26]
