"""Post ids as permalinks, at no token cost (RT-6.15).

The bus carries agents of many projects, repos, forges and ticket systems, so
the only reference the server can link without guessing is one to the bus
itself: a post id. PRs, issues, tickets, files and commits the agent links;
only it knows which repo or tracker they belong to.

Permalinks cost tokens in every agent that reads the post, so `plain` takes
them off again when a post is read: the bare id says the same.
"""

from __future__ import annotations

import re
from typing import Any

# Text that is left alone: fenced code, existing links, URLs. An inline code
# span that is one post id is linked as a whole.
_SKIP = re.compile(r"```.*?(?:```|\Z)|\[[^\]\n]*\]\([^)\s]*\)|<?https?://[^\s>)]+>?|`[^`\n]+`",
                   re.S)
_ID = re.compile(r"(?<![@<#/\w-])(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{26}(?![\w-])")
_PERMALINK = re.compile(r"\[(`?)([a-z0-9]{26})\1\]\([^)\s]*/pl/\2\)")


def linkify(message: str, *, post_url: str) -> str:
    """`message` with its post ids as links to `post_url` + id."""
    if not post_url:
        return message
    out, pos = [], 0
    for m in _SKIP.finditer(message):
        out.append(_ID.sub(lambda i: f"[{i[0]}]({post_url}{i[0]})", message[pos:m.start()]))
        span = m.group(0)
        if _ID.fullmatch(span[1:-1]) and span[0] == "`" and span[1] != "`":
            span = f"[{span}]({post_url}{span[1:-1]})"
        out.append(span)
        pos = m.end()
    out.append(_ID.sub(lambda i: f"[{i[0]}]({post_url}{i[0]})", message[pos:]))
    return "".join(out)


def plain(message: str) -> str:
    """`message` with its permalinks read as the bare post ids."""
    return _PERMALINK.sub(lambda m: f"{m[1]}{m[2]}{m[1]}", message)


def plain_post(post: dict[str, Any]) -> dict[str, Any]:
    if post.get("message"):
        post["message"] = plain(post["message"])
    return post
