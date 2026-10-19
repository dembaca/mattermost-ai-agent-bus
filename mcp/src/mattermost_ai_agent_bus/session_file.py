"""Session file: hands the bot token from the MCP server to the hooks.

Under Claude Code the MCP server and the hooks are sibling processes. A token
that session_start puts into the server's own ``os.environ`` never reaches the
hooks, so they stayed inert and a Stop hook could not tell anyone was waiting.
The session file is the hand-over: written by session_start, read by the hooks
(``mm_load_hook_session`` in bin/mm-agent-lib.sh), removed by session_end.

It lives next to the corral session files, so ``mm-agent-sweep.sh`` sees it too:
it records MM_SESSION_PID, which the sweep uses to tell a live session from an
abandoned one.

Server and hooks have to derive the same file name, and two sessions must never
share one. Both key it on the Claude Code session (CLAUDE_CODE_SESSION_ID), which
Claude Code puts into the environment of the MCP server, the hooks and every
command the agent starts. Only without it do they fall back to the project
directory (CLAUDE_PROJECT_DIR, else the working directory). Keyed on the project
alone, a second session in the same checkout overwrote the first one's file:
the first agent's hooks and listener then ran as the second bot and never saw
the messages for its own (RT-6.10).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import SessionInfo


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "mm-agent-bus"


def project_dir() -> str:
    return os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()


def session_file_path() -> Path:
    # Must match mm_hook_session_file in bin/mm-agent-lib.sh.
    sid = session_id()
    if sid:
        return state_dir() / f"mcp-sid-{hashlib.sha1(sid.encode()).hexdigest()[:16]}.env"
    key = hashlib.sha1(project_dir().encode()).hexdigest()[:16]
    return state_dir() / f"mcp-{key}.env"


def write(info: SessionInfo, team: str, channel: str) -> Path:
    """Write the session file, 0600 in a 0700 directory. Returns its path."""
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [
        "# ephemeral session — do not commit",
        f"MM_CHAT_URL={info.url}",
        f"MATTERMOST_URL={info.url}",
        f"MM_REG_BOT_NAME={info.name}",
        f"MM_BOT_USERNAME={info.username}",
        f"MM_BOT_USER_ID={info.user_id}",
        f"MM_BOT_TOKEN={info.bot_token}",
        f"MATTERMOST_TOKEN={info.bot_token}",
        f"MM_TEAM={team}",
        f"MM_CHANNEL={channel}",
        f"MM_SESSION_PID={os.getppid()}",
    ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def parse(path: Path) -> dict[str, str]:
    """KEY=value lines of a session file; {} when unreadable."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def read() -> dict[str, str]:
    """This Claude Code session's own file, for taking its bot over after a
    restart (RT-6.10). Only a file named after the session id counts: a
    project-keyed one may belong to another session."""
    if not session_id():
        return {}
    data = parse(session_file_path())
    return data if data.get("MM_BOT_TOKEN") and data.get("MM_REG_BOT_NAME") else {}


def touch() -> None:
    """Mark the moment the session's MCP server ended: the start of the grace
    period after which another session's start removes the bot."""
    try:
        os.utime(session_file_path())
    except OSError:
        pass


def others() -> list[Path]:
    """Session files of other MCP-registered sessions on this host."""
    own = session_file_path()
    return [p for p in sorted(state_dir().glob("mcp-*.env")) if p != own]


def remove() -> None:
    try:
        session_file_path().unlink()
    except FileNotFoundError:
        pass
