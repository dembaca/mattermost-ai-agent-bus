"""RT-6.15: post ids as permalinks; links that are not what they show, written out."""

import pytest

from mattermost_ai_agent_bus import links, tasks

PL = "https://mm.example/agents/pl/"
POST_ID = "if61sbeu1b88xf6wjpimzuh8uy"
RLO, ZWSP, CYR_I = chr(0x202E), chr(0x200B), chr(0x456)   # kept out of the source


def send(text):
    return links.send(text, post_url=PL)


def read(text):
    return links.read(text, post_url=PL)


def test_post_ids_become_permalinks_and_read_back_bare():
    """RT-6.15 step 2/3: the one reference that surely belongs to the bus."""
    assert send(f"see {POST_ID}.") == f"see [{POST_ID}]({PL}{POST_ID})."
    assert read(send(f"see {POST_ID}.")) == f"see {POST_ID}."
    assert links.send(POST_ID, post_url="") == POST_ID


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
