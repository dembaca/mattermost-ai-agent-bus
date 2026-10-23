"""RT-6.15: what the server can check becomes a link for humans, plain text for agents."""

import subprocess

import pytest

from mattermost_ai_agent_bus import links, slim, tasks

PL = "https://mm.example/agents/pl/"
POST_ID = "if61sbeu1b88xf6wjpimzuh8uy"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def make(remote="git@github.com:dembaca/bus.git"):
        root = tmp_path / "repo"
        (root / "mcp/src").mkdir(parents=True)
        (root / "mcp/src/server.py").write_text("x\n")
        (root / "README.md").write_text("x\n")
        git(root, "init", "-q")
        git(root, "remote", "add", "origin", remote)
        git(root, "add", ".")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        monkeypatch.setattr(links, "_repo", None)
        return git(root, "rev-parse", "HEAD")
    return make


@pytest.mark.parametrize("remote,web", [
    ("git@github.com:o/r.git", "https://github.com/o/r"),
    ("https://github.com/o/r.git", "https://github.com/o/r"),
    ("ssh://git@gitlab.example:2222/g/sub/r.git", "https://gitlab.example/g/sub/r"),
    ("https://user@gitlab.example/g/r", "https://gitlab.example/g/r"),
])
def test_web_url_of_a_remote(remote, web):
    assert links.web_url(remote) == web


def test_files_commits_and_posts_are_linked(repo):
    """RT-6.15 step 2: files tracked in the checkout, its commits, posts on this bus."""
    sha = repo()
    gh = "https://github.com/dembaca/bus"
    out = links.linkify(f"see mcp/src/server.py:12-20, `server.py:7`, README.md, "
                        f"{sha[:9]} and {POST_ID}.", post_url=PL)
    assert f"[mcp/src/server.py:12-20]({gh}/blob/HEAD/mcp/src/server.py#L12-L20)" in out
    assert f"[`server.py:7`]({gh}/blob/HEAD/mcp/src/server.py#L7)" in out
    assert f"[README.md]({gh}/blob/HEAD/README.md)" in out
    assert f"[{sha[:9]}]({gh}/commit/{sha[:9]})" in out
    assert f"[{POST_ID}]({PL}{POST_ID})" in out


def test_gitlab_paths(repo):
    repo("git@gitlab.example:g/bus.git")
    assert links.linkify("README.md:3-5") == \
        "[README.md:3-5](https://gitlab.example/g/bus/-/blob/HEAD/README.md#L3-5)"


def test_what_the_server_cannot_check_stays_as_written(repo):
    """RT-6.15: PRs, issues and tickets may belong to any repo or tracker on the
    bus; unknown files and shas, versions, mentions and code stay too."""
    repo()
    text = ("PR #57, MR !12, #40, o/x#3, ABC-123, missing.py:3, 0.31.0, RT-6.15, e.g. "
            f"deadbee1, @agent-x, <@{POST_ID}>, https://github.com/o/r/pull/1, "
            "[#9](https://x), `#5 in code`\n```\nREADME.md\n```")
    assert links.linkify(text, post_url=PL) == text


def test_no_checkout_links_posts_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(links, "_repo", None)
    assert links.linkify(f"README.md {POST_ID}", post_url=PL) == \
        f"README.md [{POST_ID}]({PL}{POST_ID})"


def test_agents_read_the_servers_links_bare_and_keep_their_own(repo):
    """RT-6.15 step 3: what linkify adds, plain takes off; an agent's own links stay."""
    repo()
    own = "[PR #57](https://gitlab.example/g/r/-/merge_requests/57) · [ABC-1](https://jira.example/browse/ABC-1)"
    text = f"`server.py:7`, README.md, {POST_ID}; {own}; [README.md](https://elsewhere.example/x)"
    assert links.plain(links.linkify(text, post_url=PL)) == text
    lean = slim._one({"id": "p", "message": links.linkify(text, post_url=PL)}, {}, {})
    assert lean["message"] == text


def test_task_rows_read_linked_prs():
    """RT-6.15 + RT-6.14: a PR the agent links is still the task's PR."""
    assert set(tasks._PR.findall("RESULT [MR !12](https://gl.example/g/r/-/merge_requests/12)")) \
        == {"12"}
