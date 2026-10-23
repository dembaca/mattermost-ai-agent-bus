"""RT-6.15: post ids become permalinks for humans, bare ids for agents."""

from mattermost_ai_agent_bus import links, slim, tasks

PL = "https://mm.example/agents/pl/"
POST_ID = "if61sbeu1b88xf6wjpimzuh8uy"


def test_post_ids_become_permalinks():
    """RT-6.15 step 2: the one reference that surely belongs to the bus."""
    assert links.linkify(f"see {POST_ID}, `{POST_ID}`.", post_url=PL) == \
        f"see [{POST_ID}]({PL}{POST_ID}), [`{POST_ID}`]({PL}{POST_ID})."


def test_everything_else_stays_as_written():
    """RT-6.15 step 1: PRs, issues, tickets, files and commits may belong to any
    repo or tracker on the bus; the server does not guess."""
    text = ("PR #57, MR !12, #40, o/x#3, ABC-123, README.md:3, ae94af5, "
            f"agent-mac-mattermos-baa85c, abcdefghijklmnopqrstuvwxyz, @{POST_ID}, "
            f"<@{POST_ID}>, {PL}{POST_ID}, [x]({PL}{POST_ID}), `see {POST_ID}`\n"
            f"```\n{POST_ID}\n```")
    assert links.linkify(text, post_url=PL) == text
    assert links.linkify(POST_ID, post_url="") == POST_ID


def test_agents_read_bare_ids_and_keep_their_own_links():
    """RT-6.15 step 3: what linkify adds, plain takes off; an agent's links stay."""
    own = "[PR #57](https://gitlab.example/g/r/-/merge_requests/57) · [ABC-1](https://jira.example/browse/ABC-1)"
    text = f"{POST_ID}, `{POST_ID}`; {own}"
    assert links.plain(links.linkify(text, post_url=PL)) == text
    lean = slim._one({"id": "p", "message": links.linkify(text, post_url=PL)}, {}, {})
    assert lean["message"] == text


def test_task_rows_read_linked_prs():
    """RT-6.15 + RT-6.14: a PR the agent links is still the task's PR."""
    assert set(tasks._PR.findall("RESULT [MR !12](https://gl.example/g/r/-/merge_requests/12)")) \
        == {"12"}
