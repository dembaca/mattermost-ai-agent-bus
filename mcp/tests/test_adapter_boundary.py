"""State carriers stay behind the adapter (arc42 §5.1).

Post props, custom status and crown, unread counters and the profile the
registrar wrote are Mattermost's. Only the modules in ALLOWED read or write
them, and only client.py talks HTTP; the rest of the server gets bus types
(model.py), a post as a Post that client.post_of built.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mattermost_ai_agent_bus"

ALLOWED = {
    "client.py": "the Mattermost adapter: REST, unread counters, profile, Mattermost's own props",
    "a2a.py": "the bus's props: agent_card and a2a, written and read back",
    "lease.py": "custom status, crown and roster: the lease, read and written through client.py",
}
# Keys that are a carrier wherever they appear.
CARRIERS = {
    "props", "a2a", "agent_card", "attachments", "from_webhook",
    "last_viewed_at", "msg_count", "mention_count", "total_msg_count", "bot_description",
}
# Roster keys, only as a read (x["crown"], x.get("crown")): a bus value may be
# named the same. A reaction's emoji is a bus value; the status emoji's word
# comes from lease.py.
READ_ONLY = {"crown", "presence", "reachable", "custom_status"}
# Mattermost's post JSON keys, as a read (p["message"], p.get("root_id")): the
# rest of the server reads a Post's fields instead. id, user_id and type stay
# out: users, reactions and channels have them too.
POST_KEYS = {"message", "root_id", "channel_id", "create_at", "delete_at", "edit_at",
             "reply_count", "metadata"}
# Who still reads them, and why. Shrink it: issue #82, P3b follow-up.
POST_KEYS_ALLOWED = {
    "links.py": "rewrites the post JSON's links before client.post_of reads it (RT-6.15)",
    "slim.py": "its own output: the slim posts in wait_for_events items",
    "channel.py": "wait_for_events items, whose posts are slim (slim.py)",
    "overhead.py": "Claude Code transcript entries, no posts",
    "websocket_inbox.py": "the WebSocket envelope and reaction JSON; posts go through client.post_of",
    "hookapi.py": "follow-up #82 P3b: the turn hooks' posts, raw from client.channel_posts",
    "turnend.py": "follow-up #82 P3b: hookapi's raw posts",
    "unread.py": "follow-up #82 P3b (bus-owned read watermark): raw from client.channel_posts",
}


def _key(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def leaks(source: str) -> list[str]:
    """What in `source` touches a state carrier or the REST API, by line."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if (k := _key(node)) in CARRIERS:
            out.append(f"{node.lineno}: {k!r}")
        elif isinstance(node, ast.Subscript) and _key(node.slice) in READ_ONLY:
            out.append(f"{node.lineno}: [{_key(node.slice)!r}]")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and node.args and _key(node.args[0]) in READ_ONLY):
            out.append(f"{node.lineno}: .get({_key(node.args[0])!r})")
        elif isinstance(node, ast.Import) and any(a.name.split(".")[0] == "httpx" for a in node.names):
            out.append(f"{node.lineno}: import httpx")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "httpx":
            out.append(f"{node.lineno}: from httpx")
        elif isinstance(node, ast.Attribute) and node.attr == "_api":
            out.append(f"{node.lineno}: ._api")
    return out


def post_key_reads(source: str) -> list[str]:
    """What in `source` reads a key of Mattermost's post JSON, by line."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and _key(node.slice) in POST_KEYS):
            out.append(f"{node.lineno}: [{_key(node.slice)!r}]")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and node.args and _key(node.args[0]) in POST_KEYS):
            out.append(f"{node.lineno}: .get({_key(node.args[0])!r})")
    return out


def test_posts_are_read_as_posts():
    skip = set(ALLOWED) | set(POST_KEYS_ALLOWED)
    found = {f.name: post_key_reads(f.read_text()) for f in sorted(SRC.glob("*.py")) if f.name not in skip}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_the_post_guard_sees_a_read():
    source = ('a = post["message"]\n'
              'b = str(post.get("root_id") or post.get("id"))\n'
              'post["message"] = short  # a write: slim output\n'
              'c = post.message\n')
    assert sorted(post_key_reads(source)) == ["1: ['message']", "2: .get('root_id')"]


def test_state_carriers_only_behind_the_adapter():
    found = {f.name: leaks(f.read_text()) for f in sorted(SRC.glob("*.py")) if f.name not in ALLOWED}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_the_guard_sees_a_leak():
    """The forms it checks, so a silent pass means none of them is there."""
    source = ('import httpx\n'
              'w = str((post.get("props") or {}).get("from_webhook", ""))\n'
              'c = next(r for r in rows if r.get("crown"))\n'
              'n = await client._api("GET", "/api/v4/users/me")\n'
              'out = {"crown": True}  # a bus value of the same name\n')
    assert sorted(leaks(source)) == ["1: import httpx", "2: 'from_webhook'", "2: 'props'",
                                     "3: .get('crown')", "4: ._api"]


def test_every_allowed_module_exists():
    assert all((SRC / name).is_file() for name in ALLOWED | POST_KEYS_ALLOWED)
