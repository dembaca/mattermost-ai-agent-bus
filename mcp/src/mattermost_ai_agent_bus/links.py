"""Clickable references in posts, at no token cost (RT-6.15).

Agents write `PR #57`, `!12`, `#40`, `owner/repo#3`, `ABC-123`, a commit sha,
`path/file.py:42` or a post id. A human reading Mattermost wants to click them,
so `linkify` turns them into markdown links when a post is sent: the project's
git remote gives the forge (GitHub, else GitLab-style paths), `MM_TICKET_URL`
the ticket tracker, the bus itself the permalinks. Only references that exist
are linked: files must be tracked, shas must be commits in the repo.

Links cost tokens in every agent that reads the post, so `plain` takes them
off again when a post is read: a link whose label is such a reference reads
as the bare reference.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from . import session_file

_TTL = 60.0
_STOP = {"UTF", "SHA", "ISO", "RFC", "CVE", "RT", "IF", "MD", "HTTP", "TLS"}

# Text that is left alone: fenced code, existing links, URLs. Inline code spans
# are kept too, but linked as a whole when their content is one reference.
_SKIP = re.compile(r"```.*?(?:```|\Z)|\[[^\]\n]*\]\([^)\s]*\)|<?https?://[^\s>)]+>?|`[^`\n]+`",
                   re.S)
_REF = re.compile(
    r"(?P<pr>\b(?:PR|MR) [#!]\d+\b)"
    r"|(?P<xrepo>\b[\w.-]+/[\w.-]+[#!]\d+\b)"
    r"|(?P<num>(?<![\w&/#!])[#!]\d+\b)"
    r"|(?P<file>(?<![\w/.@-])(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?(?![\w/]))"
    r"|(?P<ticket>\b[A-Z][A-Z0-9]{1,9}-\d+\b(?![.-]\d))"
    r"|(?P<sha>\b[0-9a-f]{7,40}\b)"
    r"|(?P<post>(?<![@<])\b[a-z0-9]{26}\b)"
)
_LINK = re.compile(r"\[(`?)([^\]`\n]+)\1\]\([^)\s]+\)")


@dataclass
class Repo:
    """The project's git checkout and where its forge shows it."""

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
        sep = "/-/" if self.gitlab else "/"
        paths = {"pull": "merge_requests" if self.gitlab else "pull",
                 "issue": "issues", "commit": "commit", "blob": "blob"}
        return f"{self.web}{sep}{paths[kind]}/{what}"


def web_url(remote: str) -> str:
    """https URL of a git remote: git@host:o/r.git, ssh://, https://user@host/o/r."""
    m = re.match(r"^(?:\w+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", remote.strip())
    return f"https://{m.group(1)}/{m.group(2)}" if m else ""


_repo: tuple[str, Repo | None] | None = None


def repo() -> Repo | None:
    """The project's repo, once per working directory; None outside git."""
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


def _target(m: re.Match[str], r: Repo | None, post_url: str) -> str | None:
    kind, text = m.lastgroup, m.group(0)
    if kind == "post":
        return f"{post_url}{text}" if post_url and re.search(r"\d", text) \
            and re.search(r"[a-z]", text) else None
    if kind == "ticket":
        base = os.environ.get("MM_TICKET_URL", "").strip()
        if not base or text.split("-")[0] in _STOP:
            return None
        return base.replace("{id}", text) if "{id}" in base else base.rstrip("/") + "/" + text
    if r is None:
        return None
    if kind == "pr":
        return r.url("pull", text.split()[-1][1:])
    if kind == "num":
        return r.url("pull" if text[0] == "!" else "issue", text[1:])
    if kind == "xrepo":
        name, n = re.split(r"[#!]", text)
        other = Repo(r.root, r.web.rsplit("/", 2)[0] + "/" + name, r.gitlab)
        return other.url("pull" if "!" in text else "issue", n)
    if kind == "sha":
        return r.url("commit", text) if re.search(r"[a-f]", text) and re.search(r"\d", text) \
            and r.commit(text) else None
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
    """`message` with its references as markdown links; `post_url` + id is a permalink."""
    r = repo()
    out, pos = [], 0
    for m in _SKIP.finditer(message):
        out.append(_refs(message[pos:m.start()], r, post_url))
        span = m.group(0)
        if span.startswith("`") and not span.startswith("```"):
            inner = span[1:-1]
            ref = _REF.fullmatch(inner)
            url = _target(ref, r, post_url) if ref else None
            span = f"[{span}]({url})" if url else span
        out.append(span)
        pos = m.end()
    out.append(_refs(message[pos:], r, post_url))
    return "".join(out)


def plain(message: str) -> str:
    """`message` with the links `linkify` makes read as their bare references."""
    def one(m: re.Match[str]) -> str:
        tick, label = m.group(1), m.group(2)
        return f"{tick}{label}{tick}" if _REF.fullmatch(label) else m.group(0)
    return _LINK.sub(one, message)


def plain_post(post: dict[str, Any]) -> dict[str, Any]:
    if post.get("message"):
        post["message"] = plain(post["message"])
    return post
