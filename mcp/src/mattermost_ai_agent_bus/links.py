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
the bus's own off again: the bare id says the same. A human reads a short
label instead of 26 characters: `Post`, or the word before the id
(`thread <id>` → `[thread](…/pl/<id>)`); an ask shows its headline
(`ask <id>` → `[<headline>](…/pl/<id> "ask")`, `Input` when the server does
not know it); the id stays in the URL (RT-6.15 step 2). An id alone in a code
span (`` `<id>` ``, after the word too) is linked as a bare one is; other code
stays as written. The adapter knows an own ask's headline from its asks file
and reads another agent's ask post for it once: `asks_in` names the ids it
looks up, so `send` itself makes no request.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

# Words a post id after them folds into its link's label (RT-6.15 step 2).
FOLD = ("ask", "thread", "post", "reply", "result")
_POST_ID = r"(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{26}"
# One pass, the leftmost match wins: a link (before the inline code its label
# may hold), an id, or an id alone in a code span (`tid`), each with the word
# before it (`pre`) to fold; other code is left alone (a fence ends only at a
# fence as long); then bare URLs. `tid` and code both start at a backtick, so
# `tid` comes first; an id in double backticks is code.
_TOKEN = re.compile(
    rf"(?P<pre>\b(?P<word>(?i:{'|'.join(FOLD)}))[ \t]+)?"
    r"(?:(?P<link>!?\[(?P<label>(?:\\[\\\[\]]|[^\]\n])*)\]\(\s*(?P<url>[^\s)]+)(?:\s+\"(?P<title>[^\"\n]*)\")?\s*\))"
    rf"|(?P<id>(?<![@<#/\w-]){_POST_ID}(?![\w-]))"
    rf"|(?<!`)`(?P<tid>{_POST_ID})`(?!`))"
    r"|(?P<code>(?P<fence>`{3,}).*?(?:(?P=fence)|\Z)|`[^`\n]+`)"
    r"|(?P<bare><?(?<![\w.])(?:https?://|www\.)[^\s<>]+>?)",
    re.S,
)
LABEL = "Post"
# An ask's link shows the ask's headline (its bold title, cut), or ASK_LABEL
# when the server does not know it; the link title "ask" marks it, so every
# reader takes it back as `ask <id>` (RT-6.15 steps 2-3, RT-6.11 step 1).
ASK = "ask"
ASK_LABEL = "Input"
HEADLINE = 60
_ID = re.compile(_POST_ID)
_HIDDEN = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_ADDRESS = re.compile(r"://|^www\.|^[\w-]+(?:\.[\w-]+)+/", re.I)
_NAME = re.compile(r"[\w-]+(?:\.[\w-]+)+")
_SAFE = "/?#[]@!$&'()*+,;=:%~"


def headline(text: str) -> str:
    """An ask's headline as its link shows it: one line, at most HEADLINE characters."""
    text = " ".join(text.split())
    return text if len(text) <= HEADLINE else text[:HEADLINE - 1].rstrip() + "…"


def _label(text: str) -> str:
    """Link text that cannot end the link early: \\, [ and ] escaped."""
    return re.sub(r"([\\\[\]])", r"\\\1", text)


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


def send(message: str, *, post_url: str,
         ask_title: Callable[[str], str] | None = None) -> str:
    """A post as the human sees it: post ids as permalinks to `post_url` + id,
    odd links written out, a blank line before a table. `ask_title` gives an
    ask's headline by its id ('' when unknown), asked only for `ask <id>`."""
    message = _tables(message)
    def one(m: re.Match[str]) -> str:
        if m["bare"]:
            return _bare(m["bare"])
        pre, label = m["pre"] or "", (m["label"] or "").strip().strip("`")
        if m["link"] and not (post_url and not m["link"].startswith("!")
                              and _ID.fullmatch(label) and m["url"] == post_url + label):
            return pre + (_written_out(m["label"], m["url"])
                          if _odd(m["label"], m["url"], post_url) else m["link"])
        pid = m["id"] or m["tid"] or label
        if not post_url:
            return m[0]
        # RT-6.15 step 2: a short label, the id in the URL. A word at the
        # post's start is its bus word (a2a.word): it stays outside the link.
        if pre and m.string[:m.start()].strip():
            if m["word"].lower() == ASK:
                title = headline(ask_title(pid) if ask_title else "") or ASK_LABEL
                return f'[{_label(title)}]({post_url}{pid} "{ASK}")'
            return f"[{m['word']}]({post_url}{pid})"
        return f"{pre}[{LABEL}]({post_url}{pid})"
    return _each(message, one)


def asks_in(message: str, *, post_url: str) -> list[str]:
    """The ids `send` links as asks (`ask <id>`, ``ask `<id>` ``), each once, in
    order: the ones whose headline the caller looks up first."""
    ids: list[str] = []

    def note(pid: str) -> str:
        ids.append(pid)
        return ""
    send(message, post_url=post_url, ask_title=note)
    return list(dict.fromkeys(ids))


def read(message: str, *, post_url: str) -> str:
    """A post as an agent reads it: the bus's permalinks as bare ids, odd links
    written out."""
    def one(m: re.Match[str]) -> str:
        if m["bare"]:
            return _bare(m["bare"])
        pre = m["pre"] or ""
        if m["id"] or m["tid"]:
            return m[0]
        label = m["label"].strip("`")
        pid = m["url"][len(post_url):] if post_url and m["url"].startswith(post_url) else ""
        if _ID.fullmatch(pid) and not m["link"].startswith("!"):
            # RT-6.15 step 3: the short labels of step 2 read as the id again.
            if label == pid:
                return pre + m["label"]
            if label == LABEL:
                return pre + pid
            if label.lower() in FOLD:
                return f"{pre}{label} {pid}"
            if m["title"] == ASK:
                return f"{pre}{ASK} {pid}"
        return pre + (_written_out(m["label"], m["url"]) if _odd(m["label"], m["url"], post_url)
                      else m["link"])
    return _each(message, one)


def read_post(post: dict[str, Any], post_url: str) -> dict[str, Any]:
    if post.get("message"):
        post["message"] = read(post["message"], post_url=post_url)
    return post
