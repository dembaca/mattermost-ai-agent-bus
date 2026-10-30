"""RT-6.15: post ids as permalinks; links that are not what they show, written out."""

import json

import httpx
import pytest

from mattermost_ai_agent_bus import asks, links, tasks
from mattermost_ai_agent_bus.client import ASK_READS, MattermostAdapter, attachment_props

PL = "https://mm.example/agents/pl/"
POST_ID = "if61sbeu1b88xf6wjpimzuh8uy"
RLO, ZWSP, CYR_I = chr(0x202E), chr(0x200B), chr(0x456)   # kept out of the source


def send(text):
    return links.send(text, post_url=PL)


def read(text):
    return links.read(text, post_url=PL)


def test_post_ids_become_permalinks_and_read_back_bare():
    """RT-6.15 step 2/3: the one reference that surely belongs to the bus."""
    assert send(f"see {POST_ID}.") == f"see [Post]({PL}{POST_ID})."
    assert read(send(f"see {POST_ID}.")) == f"see {POST_ID}."
    assert links.send(POST_ID, post_url="") == POST_ID


@pytest.mark.parametrize(("text", "sent"), [
    (f"ask {POST_ID}", f"ask [Post]({PL}{POST_ID})"),       # a bus word at the start stays
    (f"RESULT {POST_ID}", f"RESULT [Post]({PL}{POST_ID})"),
    (f"WAITING for approval: merge · ask {POST_ID}",
     f'WAITING for approval: merge · [Input]({PL}{POST_ID} "ask")'),
    (f"see Thread {POST_ID}, reply {POST_ID}", f"see [Thread]({PL}{POST_ID}), [reply]({PL}{POST_ID})"),
    (f"x [{POST_ID}]({PL}{POST_ID})", f"x [Post]({PL}{POST_ID})"),
    (f"x [`{POST_ID}`]({PL}{POST_ID})", f"x [Post]({PL}{POST_ID})"),
    (f"x post [{POST_ID}]({PL}{POST_ID}).", f"x [post]({PL}{POST_ID})."),
    (f"x task {POST_ID}", f"x task [Post]({PL}{POST_ID})"),
    (f"x unmask {POST_ID}", f"x unmask [Post]({PL}{POST_ID})"),
    ("x post [docs](https://h.example/d)", "x post [docs](https://h.example/d)"),
    (f"x `ask {POST_ID}` and\n```\n{POST_ID}\n```", f"x `ask {POST_ID}` and\n```\n{POST_ID}\n```"),
])
def test_post_ids_get_a_short_label_and_read_back_as_ids(text, sent):
    """RT-6.15 step 2/3: a human reads `Post` or the word before the id, not 26
    characters; the id stays in the URL, and an agent reads it back."""
    assert send(text) == sent
    back = read(sent)
    assert "[Post]" not in back and f"{PL}{POST_ID}" not in back
    assert (POST_ID in back) == (POST_ID in text)
    assert read(send(f"x ask {POST_ID}")) == f"x ask {POST_ID}"
    assert read(send(f"x [{POST_ID}]({PL}{POST_ID})")) == f"x {POST_ID}"


def test_a_short_label_elsewhere_is_no_bus_link():
    """RT-6.15 step 3/4: `Post` on another host's URL stays as written; an
    image of a permalink is no reference."""
    other = f"[Post](https://h.example/pl/{POST_ID})"
    assert read(other) == other and send(other) == other
    assert read(f"[ask]({PL}{POST_ID})") == f"ask {POST_ID}"


def test_everything_else_stays_as_written():
    """RT-6.15 step 1: PRs, tickets, files, commits may belong to any repo or tracker
    on the bus; inconspicuous links stay as they come."""
    text = ("PR #57, ABC-123, README.md:3, ae94af5, agent-mac-mattermos-baa85c, "
            f"abcdefghijklmnopqrstuvwxyz, @{POST_ID}, <@{POST_ID}>, {PL}{POST_ID}, "
            "[PR #57](https://github.com/o/r/pull/57), [docs](https://gitlab.example/g/r), "
            "[github.com/o/r](https://www.github.com/o/r), [x](/agents/channels/y), "
            "[README.md](https://gitlab.example/g/r/-/blob/main/README.md), "
            "[gitlab.example](https://gitlab.example), [v0.31.1](https://h.example/tag/v0.31.1), "
            "[mail](mailto:a@b.example), https://de.wikipedia.org/wiki/Bär, "
            f"`see {POST_ID}`\n```\n{POST_ID} [x](https://evil.example)\n```")
    assert send(text) == text
    assert read(text) == text


@pytest.mark.parametrize("link,shown", [
    ("[https://github.com/o/r/pull/1](https://evil.example/x)",
     "https://github.com/o/r/pull/1 (https://evil.example/x)"),
    ("[github.com/o/r](https://evil.example/o/r)", "github.com/o/r (https://evil.example/o/r)"),
    ("[github.com](https://evil.example)", "github.com (https://evil.example)"),
    ("[README.md](https://evil.example/x)", "README.md (https://evil.example/x)"),
    ("[PR #57](https://github.com@evil.example/pull/57)",
     "PR #57 (https://github.com@evil.example/pull/57)"),
    (f"[PR #57](https://g{CYR_I}thub.com/o/r)", "PR #57 (https://xn--gthub-n2e.com/o/r)"),
    (f"[PR #57](https://github.com/o/r/{RLO}x)", "PR #57 (https://github.com/o/r/%E2%80%AEx)"),
    (f"[PR {RLO}#57](https://github.com/o/r)", "PR #57 (https://github.com/o/r)"),
    ("[run](javascript:alert(1)", "run (javascript:alert(1)"),
    ("[run](file:///etc/hosts)", "run (file:///etc/hosts)"),
    (f"[{POST_ID}](https://evil.example/pl/{POST_ID})",
     f"{POST_ID} (https://evil.example/pl/{POST_ID})"),
    ("![x](https://evil.example/x.png?d=1)", "![x](https://evil.example/x.png?d=1)"),
    (f"![x](https://g{CYR_I}thub.com/a.png)", "x (https://xn--gthub-n2e.com/a.png)"),
])
def test_odd_links_are_written_out(link, shown):
    """RT-6.15 step 4: a link that is not what it shows reads as label plus the
    full ASCII URL, for the human (send) and for the agent (read)."""
    assert send(link) == shown
    assert read(link) == shown


def test_odd_bare_urls_show_their_real_host():
    assert send(f"https://g{CYR_I}thub.com/o/r and <https://git{ZWSP}hub.com/x>") == \
        "https://xn--gthub-n2e.com/o/r and <https://github.com/x>"


def test_inline_code_in_a_label_does_not_hide_the_link():
    """RT-6.15: a label in backticks is still checked."""
    assert send("[`github.com/o/r`](https://evil.example)") == \
        "`github.com/o/r` (https://evil.example)"


def test_task_rows_read_linked_prs():
    """RT-6.15 + RT-6.14: a PR the agent links is still the task's PR."""
    assert tasks.a2a.pr("RESULT [MR !12](https://gl.example/g/r/-/merge_requests/12)") == 12


TABLE = "| a | b |\n|---|---|\n| 1 | 2 |"


@pytest.mark.parametrize("text,want", [
    (f"Stand:\n{TABLE}", f"Stand:\n\n{TABLE}"),
    (f"Stand:\n\n{TABLE}", f"Stand:\n\n{TABLE}"),
    (TABLE, TABLE),
    (f"```\nx\n{TABLE}\n```", f"```\nx\n{TABLE}\n```"),
    ("a | b\nno table | here", "a | b\nno table | here"),
    (f"one:\n{TABLE}\ntwo:\n{TABLE}", f"one:\n\n{TABLE}\ntwo:\n\n{TABLE}"),
])
def test_a_table_after_text_gets_a_blank_line(text, want):
    # RT-6.15: Mattermost renders a table only after a blank line.
    assert send(text) == want


def test_an_ask_shows_its_headline_and_reads_back_as_ask():
    """RT-6.15 step 2/3, RT-6.11 step 1: `ask <id>` shows the ask's headline
    (one line, cut at 60, brackets escaped so the link holds), `Input` when it
    is unknown; the link title `ask` marks it, so any reader, knowing the
    headline or not, takes it back as `ask <id>`."""
    head = "Merge [PR #61] now?\n" + "x" * 80
    sent = links.send(f"🙋 ask {POST_ID}", post_url=PL, ask_title=lambda pid: head)
    label = "Merge \\[PR #61\\] now? " + "x" * 39 + "…"
    assert sent == f'🙋 [{label}]({PL}{POST_ID} "ask")'
    assert len(links.headline(head)) == links.HEADLINE
    assert read(sent) == f"🙋 ask {POST_ID}"
    assert send(sent) == sent  # a second pass leaves the link alone
    assert send(f"open: ask {POST_ID}") == f'open: [Input]({PL}{POST_ID} "ask")'
    assert read(f"open: [Input]({PL}{POST_ID})") == f"open: [Input]({PL}{POST_ID})"
    # Another host's link titled `ask` is no ask of this bus.
    other = f'[Merge](https://h.example/pl/{POST_ID} "ask")'
    assert read(other) == other


async def test_an_own_ask_is_linked_by_its_headline_without_a_request(monkeypatch):
    """RT-6.15 step 2, RT-6.11 step 1: the adapter takes the headline from the
    bot's own asks file (asks-<uid>.env); an ask it does not know reads `Input`."""
    bot = "b" * 26
    c, sent, reads = _adapter(monkeypatch, {})
    asks.add(bot, POST_ID, "dembaca", POST_ID, title="Merge #61?")
    post = await c.post(f"🙋 ask {POST_ID}", channel_id="c" * 26)
    assert sent[0]["message"] == f'🙋 [Merge #61?]({PL}{POST_ID} "ask")'
    assert post.message == f"🙋 ask {POST_ID}"
    # Resolved, it is no open ask, and the IDLE right after still shows its headline.
    asks.resolve(bot, POST_ID)
    assert asks.open_asks(bot) == [] and asks.waiting(bot) == []
    await c.post(f"IDLE\ndone: ask {POST_ID} resolved", channel_id="c" * 26)
    assert sent[1]["message"] == f'IDLE\ndone: [Merge #61?]({PL}{POST_ID} "ask") resolved'
    assert reads == []
    # An ask it does not know is read once; not found, it reads `Input`.
    await c.post(f"x ask {'q' * 25}1", channel_id="c" * 26)
    assert sent[2]["message"] == f'x [Input]({PL}{"q" * 25}1 "ask")'
    assert reads == [f"{'q' * 25}1"]


def _adapter(monkeypatch, posts):
    """An adapter whose fake API takes posts and answers GET /api/v4/posts/<id>
    from `posts` (a post's JSON, a Response, or an exception to raise; 404
    otherwise). Returns it, the bodies posted and the ids read."""
    sent, reads = [], []

    async def no_bookkeeping(self, cid, post):
        return None

    def handler(req):
        if req.method == "GET":
            pid = req.url.path.rsplit("/", 1)[-1]
            reads.append(pid)
            got = posts.get(pid)
            if isinstance(got, Exception):
                raise got
            if isinstance(got, httpx.Response):
                return got
            return httpx.Response(200, json=got) if got else httpx.Response(404, json={})
        sent.append(json.loads(req.content))
        return httpx.Response(201, json={"id": "n" * 26, **sent[-1]})

    monkeypatch.setattr(MattermostAdapter, "_remember", no_bookkeeping)
    monkeypatch.setenv("MM_BOT_USER_ID", "b" * 26)
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, sent, reads


def test_an_id_alone_in_a_code_span_is_linked_like_a_bare_one():
    """RT-6.15 step 2/3: `` `<id>` `` is linked as the bare id is, the word
    before it folded (any case), `Ask` as the ask's headline; other code stays
    as written; reading gives the bare id back (without the backticks)."""
    tick = f"`{POST_ID}`"
    assert send(f"see {tick}") == f"see [Post]({PL}{POST_ID})"
    assert send(tick) == f"[Post]({PL}{POST_ID})"
    assert send(f"x THREAD {tick}") == f"x [THREAD]({PL}{POST_ID})"
    assert send(f"Ask {tick} first") == f"Ask [Post]({PL}{POST_ID}) first"  # the bus word
    sent = links.send(f"open: waits on dembacas Ask {tick}", post_url=PL,
                      ask_title=lambda pid: "Merge PR #136?" if pid == POST_ID else "")
    assert sent == f'open: waits on dembacas [Merge PR #136?]({PL}{POST_ID} "ask")'
    assert send(f"open: ask {tick}") == f'open: [Input]({PL}{POST_ID} "ask")'
    assert read(sent) == f"open: waits on dembacas ask {POST_ID}"
    assert read(send(f"see {tick}")) == f"see {POST_ID}"
    assert read(f"see {tick}") == f"see {tick}"  # what an agent reads stays
    for code in (f"`x {POST_ID}`", f"`ask {POST_ID}`", f"``{POST_ID}``",
                 f"```\nask {tick}\n```", f"`{POST_ID}x`", "`ask`"):
        assert send(code) == code
    other = "a1" * 13
    assert links.asks_in(f"x ask {POST_ID}, Ask `{other}`, ask {POST_ID}, `{'c3' * 13}`",
                         post_url=PL) == [POST_ID, other]


async def test_another_agents_ask_is_linked_by_its_headline_read_once(monkeypatch):
    """RT-6.15 step 2: an ask the bot did not post is read once for its
    headline, the first bold text of an ask (props a2a kind `ask`, or `🙋 @`
    after an optional 👑), in the message and in a card's text alike; the
    adapter remembers it, so the next post makes no request."""
    other, crowned, own = "a1" * 13, "c3" * 13, POST_ID
    c, sent, reads = _adapter(monkeypatch, {
        other: {"id": other, "message": "🙋 @dembaca — **Merge PR #136 now?** see below",
                "props": {"a2a": {"kind": "ask"}}},
        crowned: {"id": crowned, "message": "👑 🙋 @dembaca — **Deploy?**"},
    })
    asks.add("b" * 26, own, "dembaca", own, title="Own one?")
    props = attachment_props([("Context", f"waits on ask {other}", False)],
                             fallback="x", urgent=False)
    await c.post(f"IDLE\nopen: waits on dembacas Ask `{other}`, ask {crowned}, ask {own}",
                 channel_id="c" * 26, props=props)
    assert sent[0]["message"] == (
        f'IDLE\nopen: waits on dembacas [Merge PR #136 now?]({PL}{other} "ask"), '
        f'[Deploy?]({PL}{crowned} "ask"), [Own one?]({PL}{own} "ask")')
    card = sent[0]["props"]["attachments"][0]
    assert card["fields"][0]["value"] == f'waits on [Merge PR #136 now?]({PL}{other} "ask")'
    assert reads == [other, crowned]  # each once; the own ask needs no request
    await c.post(f"still ask {other}", channel_id="c" * 26)
    assert sent[1]["message"] == f'still [Merge PR #136 now?]({PL}{other} "ask")'
    assert reads == [other, crowned]


async def test_a_post_that_is_no_ask_or_cannot_be_read_reads_input(monkeypatch):
    """RT-6.15 step 2, fail open: a post that is no ask, an ask without a bold
    title, one not visible or not found, a server error, no connection or bad
    JSON give `Input`, and the post still goes. A definite answer is
    remembered; an error that may pass (500, connection, bad JSON) is read
    again in the next post."""
    ids = [f"{i}" + "z" * 25 for i in range(7)]
    c, sent, reads = _adapter(monkeypatch, {
        ids[0]: {"id": ids[0], "message": "**Merge?** said someone"},
        ids[1]: {"id": ids[1], "message": "🙋 @dembaca — merge?"},
        ids[2]: httpx.Response(403, json={}),
        # ids[3]: not found
        ids[4]: httpx.Response(500, text="oops"),
        ids[5]: httpx.ConnectError("down"),
        ids[6]: httpx.Response(200, text="not json"),
    })
    definite, passing = ids[:4], ids[4:]
    for part in (definite, passing, definite, passing):
        await c.post(" ".join(f"x ask {p}" for p in part), channel_id="c" * 26)
        assert sent[-1]["message"] == " ".join(f'x [Input]({PL}{p} "ask")' for p in part)
    assert reads == definite + passing + passing


async def test_another_agents_headline_mentions_nobody(monkeypatch):
    """RT-6.15 step 2, RT-6.11 step 1: a read headline comes from someone
    else's post and goes into ours after its mentions were checked, so its @
    are dropped and it notifies nobody."""
    other = "a1" * 13
    c, sent, _ = _adapter(monkeypatch, {other: {
        "id": other, "message": "🙋 @dembaca — **Tell @dembaca and @channel, @all?**",
        "props": {"a2a": {"kind": "ask"}}}})
    await c.post(f"x ask {other}", channel_id="c" * 26)
    assert sent[0]["message"] == f'x [Tell dembaca and channel, all?]({PL}{other} "ask")'


async def test_at_most_five_asks_are_read_per_post(monkeypatch):
    """RT-6.15 step 2: at most client.ASK_READS reads per post; the asks past
    them read `Input` this time and are read in a later post."""
    assert ASK_READS == 5
    ids = [f"{i}" + "y" * 25 for i in range(7)]
    c, sent, reads = _adapter(monkeypatch, {
        p: {"id": p, "message": f"🙋 @dembaca — **Q{p[0]}?**"} for p in ids})
    await c.post(" ".join(f"x ask {p}" for p in ids), channel_id="c" * 26)
    assert reads == ids[:5]
    assert sent[0]["message"] == " ".join(
        f'x [{f"Q{p[0]}?" if i < 5 else "Input"}]({PL}{p} "ask")' for i, p in enumerate(ids))
    await c.post(" ".join(f"x ask {p}" for p in ids), channel_id="c" * 26)
    assert reads == ids  # the five remembered, the other two read now
    assert sent[1]["message"] == " ".join(f'x [Q{p[0]}?]({PL}{p} "ask")' for p in ids)


def test_ask_titles_outlive_resolve_capped_at_the_newest():
    """RT-6.15 step 2: headlines stay after resolve_ask, the newest asks.TITLES."""
    bot = "b" * 26
    ids = [f"{i:02d}" + "a" * 24 for i in range(asks.TITLES + 2)]
    for pid in ids:
        asks.add(bot, pid, "dembaca", pid, title=f"q {pid[:2]}")
        asks.resolve(bot, pid)
    got = asks.titles(bot)
    assert list(got) == ids[2:] and got[ids[-1]] == f"q {ids[-1][:2]}"
    assert asks.open_asks(bot) == []


async def test_an_asks_card_links_post_ids_and_reads_back_bare(monkeypatch):
    """RT-6.15 step 6: the adapter treats an attachment's text like the
    message, so a post id in an ask's context opens with one click; reading
    gives the agent the bare id again. Titles and the fallback stay plain."""
    sent = []

    async def no_bookkeeping(self, cid, post):
        return None

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(201, json={"id": "n" * 26, **sent[-1]})

    monkeypatch.setattr(MattermostAdapter, "_remember", no_bookkeeping)
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pr = "[PR #7](https://forge.example/pull/7)"
    props = attachment_props([("context", f"see {POST_ID}", False), ("where", pr, True)],
                             fallback=f"ask {POST_ID}", urgent=False)
    props["attachments"][0]["text"] = f"also {POST_ID}"
    post = await c.post("🙋 ask", channel_id="c" * 26, props=props)
    card = sent[0]["props"]["attachments"][0]
    assert card["fields"][0] == {"title": "context", "value": f"see [Post]({PL}{POST_ID})",
                                 "short": False}
    assert card["fields"][1]["value"] == pr
    assert card["text"] == f"also [Post]({PL}{POST_ID})" and card["fallback"] == f"ask {POST_ID}"
    back = post.props["attachments"][0]
    assert back["fields"][0]["value"] == f"see {POST_ID}" and back["text"] == f"also {POST_ID}"
    assert props["attachments"][0]["fields"][0]["value"] == f"see {POST_ID}"  # the caller's stays
