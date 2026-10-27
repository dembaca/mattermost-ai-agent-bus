"""Links in posts: permalinks for post ids, odd links written out (RT-6.15).

The bus carries agents of many projects, repos, forges and ticket systems, so
the only reference the server links itself is one to the bus: a post id. PRs,
issues, tickets, files and commits the agent links; only it knows which repo
or tracker they belong to.

Anti-fooling, not a security scanner: a link that is not what it shows —
another host than its label names, a user@ before the host, a host in other
scripts, hidden characters, an unusual scheme — is written out as label plus
full ASCII URL, so the human sees where it goes and decides. Inconspicuous
links stay as they are. Both ways: what the agent posts (what the human sees)
and what it reads.

Permalinks cost tokens in every agent that reads the post, so `read` takes
the bus's own off again: the bare id says the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

# One pass, first match wins: code is left alone (a fence ends only at a fence
# as long), a link before the inline code its label may hold, then bare URLs, ids.
_TOKEN = re.compile(
    r"(?P<code>(?P<fence>`{3,}).*?(?:(?P=fence)|\Z)|`[^`\n]+`)"
    r"|(?P<link>!?\[(?P<label>[^\]\n]*)\]\(\s*(?P<url>[^\s)]+)(?:\s+\"[^\"\n]*\")?\s*\))"
    r"|(?P<bare><?(?<![\w.])(?:https?://|www\.)[^\s<>]+>?)"
    r"|(?P<id>(?<![@<#/\w-])(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{26}(?![\w-]))",
    re.S,
)
_ID = re.compile(r"(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{26}")
_HIDDEN = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_ADDRESS = re.compile(r"://|^www\.|^[\w-]+(?:\.[\w-]+)+/", re.I)
_NAME = re.compile(r"[\w-]+(?:\.[\w-]+)+")
_SAFE = "/?#[]@!$&'()*+,;=:%~"


def _host(url: str) -> str:
    try:
        return (urlsplit(url if "//" in url else "//" + url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _odd_host(url: str) -> bool:
    host = _host(url)
    return not host.isascii() or "xn--" in host or bool(_HIDDEN.search(url))


def _odd(label: str, url: str, own: str) -> bool:
    """A link that is not what it shows."""
    if _HIDDEN.search(label) or _odd_host(url):
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if parts.scheme.lower() not in ("", "http", "https", "mailto") or "@" in parts.netloc:
        return True
    label = label.strip().strip("`")
    if _ID.fullmatch(label):
        return url != own + label
    name = _NAME.fullmatch(label)
    if _ADDRESS.search(label) or name:
        shown, target = _host(label), _host(url)
        if target == shown or target.endswith("." + shown):
            return False
        # `README.md` is a file name, not a host, where the link ends in it.
        return not (name and parts.path.endswith(label))
    return False


def _ascii(url: str) -> str:
    """The URL as it really goes: punycode host, hidden and foreign characters %-encoded."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return quote(url, safe=_SAFE)
    user, at, hostport = parts.netloc.rpartition("@")
    host, colon, port = hostport.partition(":")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        host = quote(host, safe="")
    netloc = quote(user, safe=":%") + at + host + colon + quote(port, safe="")
    slashes = url[len(parts.scheme) + 1 if parts.scheme else 0:].startswith("//")
    head = (f"{parts.scheme}:" if parts.scheme else "") + (f"//{netloc}" if slashes else "")
    tail = parts.path + (f"?{parts.query}" if parts.query else "") + \
        (f"#{parts.fragment}" if parts.fragment else "")
    return head + quote(tail, safe=_SAFE)


def _written_out(label: str, url: str) -> str:
    label, full = _HIDDEN.sub("", label).strip(), _ascii(url)
    return f"{label} ({full})" if label and label != full else full


def _each(message: str, one: Callable[[re.Match[str]], str]) -> str:
    return _TOKEN.sub(lambda m: m["code"] or one(m), message)


def _bare(text: str) -> str:
    url = text.strip("<>")
    return text.replace(url, _ascii(url)) if _odd_host(url) else text


_FENCE = re.compile(r"\s*(`{3,}|~{3,})")
_ROW = re.compile(r"\s*\|")
_RULE = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*")


def _tables(message: str) -> str:
    """Mattermost renders a table only after a blank line: give it one."""
    lines, fence = message.split("\n"), ""
    out: list[str] = []
    for i, line in enumerate(lines):
        if m := _FENCE.match(line):
            mark = m[1]
            if not fence:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = ""
        elif (not fence and out and out[-1].strip() and not _ROW.match(out[-1])
              and _ROW.match(line) and i + 1 < len(lines) and _RULE.fullmatch(lines[i + 1])):
            out.append("")
        out.append(line)
    return "\n".join(out)


def send(message: str, *, post_url: str) -> str:
    """A post as the human sees it: post ids as permalinks to `post_url` + id,
    odd links written out, a blank line before a table."""
    message = _tables(message)
    def one(m: re.Match[str]) -> str:
        if m["id"]:
            return f"[{m['id']}]({post_url}{m['id']})" if post_url else m["id"]
        if m["bare"]:
            return _bare(m["bare"])
        return _written_out(m["label"], m["url"]) if _odd(m["label"], m["url"], post_url) \
            else m["link"]
    return _each(message, one)


def read(message: str, *, post_url: str) -> str:
    """A post as an agent reads it: the bus's permalinks as bare ids, odd links
    written out."""
    def one(m: re.Match[str]) -> str:
        if m["id"]:
            return m["id"]
        if m["bare"]:
            return _bare(m["bare"])
        label = m["label"].strip("`")
        if post_url and _ID.fullmatch(label) and m["url"] == post_url + label:
            return m["label"]
        return _written_out(m["label"], m["url"]) if _odd(m["label"], m["url"], post_url) \
            else m["link"]
    return _each(message, one)


def read_post(post: dict[str, Any], post_url: str) -> dict[str, Any]:
    if post.get("message"):
        post["message"] = read(post["message"], post_url=post_url)
    return post
