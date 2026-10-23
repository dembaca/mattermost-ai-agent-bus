"""RT-6.15: references in posts become links for humans, plain text for agents."""

import subprocess

import pytest

from mattermost_ai_agent_bus import links, slim

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


def test_github_references(repo):
    """RT-6.15: PRs, issues, files with lines and commits link to the forge."""
    sha = repo()
    gh = "https://github.com/dembaca/bus"
    out = links.linkify(f"PR #57 fixes #40 and o/x#3; see mcp/src/server.py:12-20, "
                        f"`server.py:7`, README.md and {sha[:9]}.", post_url=PL)
    assert f"[PR #57]({gh}/pull/57)" in out
    assert f"[#40]({gh}/issues/40)" in out
    assert "[o/x#3](https://github.com/o/x/issues/3)" in out
    assert f"[mcp/src/server.py:12-20]({gh}/blob/HEAD/mcp/src/server.py#L12-L20)" in out
    assert f"[`server.py:7`]({gh}/blob/HEAD/mcp/src/server.py#L7)" in out
    assert f"[README.md]({gh}/blob/HEAD/README.md)" in out
    assert f"[{sha[:9]}]({gh}/commit/{sha[:9]})" in out


def test_gitlab_paths(repo):
    repo("git@gitlab.example:g/bus.git")
    out = links.linkify("MR !12, #4, README.md:3-5")
    assert "[MR !12](https://gitlab.example/g/bus/-/merge_requests/12)" in out
    assert "[#4](https://gitlab.example/g/bus/-/issues/4)" in out
    assert "(https://gitlab.example/g/bus/-/blob/HEAD/README.md#L3-5)" in out


def test_only_what_exists_is_linked(repo):
    """RT-6.15: untracked files, unknown shas, versions and code stay as written."""
    repo()
    text = ("missing.py:3, 0.31.0, RT-6.15, e.g. deadbee1, @agent-x, "
            "https://github.com/o/r/pull/1, [#9](https://x), `#5 in code`\n"
            "```\n#7 README.md\n```")
    assert links.linkify(text) == text


def test_tickets_and_permalinks(repo, monkeypatch):
    repo()
    assert links.linkify("ABC-123") == "ABC-123"            # no tracker configured
    monkeypatch.setenv("MM_TICKET_URL", "https://jira.example/browse/")
    out = links.linkify(f"ABC-123 per {POST_ID}, not UTF-8 or <@{POST_ID}>", post_url=PL)
    assert "[ABC-123](https://jira.example/browse/ABC-123)" in out
    assert f"[{POST_ID}]({PL}{POST_ID})" in out
    assert "UTF-8" in out and f"<@{POST_ID}>" in out


def test_no_repo_links_nothing_of_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(links, "_repo", None)
    assert links.linkify("PR #57 README.md") == "PR #57 README.md"


def test_agents_read_the_bare_references(repo):
    """RT-6.15: what linkify adds, plain takes off — no extra tokens on read."""
    repo()
    text = "PR #57: `server.py:7`, README.md, ticket ABC-1; [docs](https://x.example)"
    assert links.plain(links.linkify(text, post_url=PL)) == text
    lean = slim._one({"id": "p", "message": links.linkify(text)}, {}, {})
    assert lean["message"] == text
