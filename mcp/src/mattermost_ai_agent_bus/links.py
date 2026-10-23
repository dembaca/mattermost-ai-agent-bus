"""Clickable references in posts, only where the server can check them (RT-6.15).

The bus carries agents of different projects, customers, forges and ticket
systems, so the server links only what it can verify itself: a post id on this
bus (permalink), a commit that is in the agent's own checkout, a file tracked
there (`path/file.py:42`). The forge comes from the checkout's `origin`
(GitHub, else GitLab-style paths). PRs, issues and tickets the agent links
itself: only it knows which repo or tracker a number belongs to.

Links cost tokens in every agent that reads the post, so `plain` takes the
server's own links off again when a post is read: they can be rebuilt from the
bare reference. Links the agent wrote stay.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from . import session_file

_TTL = 60.0

# Text that is left alone: fenced code, existing links, URLs. Inline code spans
# are kept too, but linked as a whole when their content is one reference.
_SKIP = re.compile(r"```.*?(?:```|\Z)|\[[^\]\n]*\]\([^)\s]*\)|<?https?://[^\s>)]+>?|`[^`\n]+`",
                   re.S)
_REF = re.compile(
    r"(?P<file>(?<![\w/.@#-])(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?(?![\w/]))"
    r"|(?P<sha>(?<![@<#])\b[0-9a-f]{7,40}\b)"
    r"|(?P<post>(?<![@<#])\b[a-z0-9]{26}\b)"
)
_LINK = re.compile(r"\[(`?)([^\]`\n]+)\1\]\(([^)\s]+)\)")


@dataclass
class Repo:
    """The agent's git checkout and where its forge shows it."""

    root: str
    web: str          # https://host/owner/repo
    gitlab: bool
    _files: tuple[float, list[str]] = (0.0, [])

    def _git(self, *args: str) -> str:
        try:
            return subprocess.run(["git", "-C", self.root, *args], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def ref(self) -> str:
        """The pushed branch, else the default branch: a link that opens."""
        if self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return self._git("rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
        return "HEAD"

    def files(self) -> list[str]:
        at, files = self._files
        if time.monotonic() - at > _TTL:
            files = self._git("ls-files").splitlines()
            self._files = (time.monotonic(), files)
        return files

    def file(self, name: str) -> str | None:
        """The tracked path `name` means: exact, else the one path ending in it."""
        files = self.files()
        if name in files:
            return name
        hits = [f for f in files if f.endswith("/" + name)]
        return hits[0] if len(hits) == 1 else None

    def commit(self, sha: str) -> bool:
        return bool(self._git("rev-parse", "--quiet", "--verify", f"{sha}^{{commit}}"))

    def url(self, kind: str, what: str) -> str:
        return f"{self.web}{'/-/' if self.gitlab else '/'}{kind}/{what}"


def web_url(remote: str) -> str:
    """https URL of a git remote: git@host:o/r.git, ssh://, https://user@host/o/r."""
    m = re.match(r"^(?:\w+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", remote.strip())
    return f"https://{m.group(1)}/{m.group(2)}" if m else ""


_repo: tuple[str, Repo | None] | None = None


def repo() -> Repo | None:
    """The agent's checkout, once per working directory; None outside git."""
    global _repo
    cwd = session_file.project_dir()
    if _repo is None or _repo[0] != cwd:
        found = None
        try:
            root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
            remote = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            web = web_url(remote)
            if root and web:
                found = Repo(root, web, "github.com" not in web)
        except (OSError, subprocess.SubprocessError):
            pass
        _repo = (cwd, found)
    return _repo[1]


def _mixed(text: str, letters: str) -> bool:
    return bool(re.search(r"\d", text) and re.search(f"[{letters}]", text))


def _target(m: re.Match[str], r: Repo | None, post_url: str) -> str | None:
    kind, text = m.lastgroup, m.group(0)
    if kind == "post":
        return f"{post_url}{text}" if post_url and _mixed(text, "a-z") else None
    if r is None:
        return None
    if kind == "sha":
        return r.url("commit", text) if _mixed(text, "a-f") and r.commit(text) else None
    if kind == "file":
        name, _, lines = text.partition(":")
        path = r.file(name)
        if path is None:
            return None
        anchor = ""
        if lines:
            a, _, b = lines.partition("-")
            anchor = f"#L{a}" + (f"-{'' if r.gitlab else 'L'}{b}" if b else "")
        return r.url("blob", f"{r.ref()}/{path}{anchor}")
    return None


def _refs(text: str, r: Repo | None, post_url: str) -> str:
    def one(m: re.Match[str]) -> str:
        url = _target(m, r, post_url)
        return f"[{m.group(0)}]({url})" if url else m.group(0)
    return _REF.sub(one, text)


def linkify(message: str, *, post_url: str = "") -> str:
    """`message` with its checkable references as links; `post_url` + id is a permalink."""
    r = repo()
    out, pos = [], 0
    for m in _SKIP.finditer(message):
        out.append(_refs(message[pos:m.start()], r, post_url))
        span = m.group(0)
        if span.startswith("`") and not span.startswith("```"):
            ref = _REF.fullmatch(span[1:-1])
            url = _target(ref, r, post_url) if ref else None
            span = f"[{span}]({url})" if url else span
        out.append(span)
        pos = m.end()
    out.append(_refs(message[pos:], r, post_url))
    return "".join(out)


def _ours(label: str, url: str) -> bool:
    """A link `linkify` makes: a permalink to the post, or the forge's view of the
    commit or file. Any other link — a PR, an issue, a ticket — the agent wrote."""
    m = _REF.fullmatch(label)
    if not m:
        return False
    if m.lastgroup == "post":
        return url.endswith(f"/pl/{label}")
    if m.lastgroup == "sha":
        return url.endswith(f"/commit/{label}")
    return re.search(r"/blob/[^#]*" + re.escape(label.partition(":")[0]) + r"(#L[-\dL]+)?$",
                     url) is not None


def plain(message: str) -> str:
    """`message` with the links `linkify` makes read as their bare references."""
    def one(m: re.Match[str]) -> str:
        tick, label, url = m.groups()
        return f"{tick}{label}{tick}" if _ours(label, url) else m.group(0)
    return _LINK.sub(one, message)


def plain_post(post: dict[str, Any]) -> dict[str, Any]:
    if post.get("message"):
        post["message"] = plain(post["message"])
    return post
