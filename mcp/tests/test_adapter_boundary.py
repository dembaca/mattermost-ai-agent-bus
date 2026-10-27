"""State carriers stay behind the adapter (arc42 §5.1).

Post props, custom status and crown, unread counters and the profile the
registrar wrote are Mattermost's. Only the modules in ALLOWED read or write
them, and only client.py talks HTTP; the rest of the server gets bus types
(model.py) and plain posts.
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
    "last_viewed_at", "last_post_at", "msg_count", "mention_count", "total_msg_count",
    "bot_description",
}
# Roster keys, only as a read (x["crown"], x.get("crown")): a bus value may be
# named the same. A reaction's emoji is a bus value; the status emoji's word
# comes from lease.py.
READ_ONLY = {"crown", "presence", "reachable", "custom_status"}


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
    assert all((SRC / name).is_file() for name in ALLOWED)
