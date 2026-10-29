"""RT-6.15: post ids as permalinks; links that are not what they show, written out."""

import json

import httpx
import pytest

from mattermost_ai_agent_bus import asks, links, tasks
from mattermost_ai_agent_bus.client import MattermostAdapter, attachment_props

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
    sent = []
    bot = "b" * 26

    async def no_bookkeeping(self, cid, post):
        return None

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(201, json={"id": "n" * 26, **sent[-1]})

    monkeypatch.setattr(MattermostAdapter, "_remember", no_bookkeeping)
    monkeypatch.setenv("MM_BOT_USER_ID", bot)
    asks.add(bot, POST_ID, "dembaca", POST_ID, title="Merge #61?")
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    post = await c.post(f"🙋 ask {POST_ID}", channel_id="c" * 26)
    assert sent[0]["message"] == f'🙋 [Merge #61?]({PL}{POST_ID} "ask")'
    assert post.message == f"🙋 ask {POST_ID}"
    asks.resolve(bot, POST_ID)
    await c.post(f"IDLE\nopen: ask {POST_ID}", channel_id="c" * 26)
    assert sent[1]["message"] == f'IDLE\nopen: [Input]({PL}{POST_ID} "ask")'


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
