"""Session file: hands the bot token from the MCP server to the hooks.

Under Claude Code the MCP server and the hooks are sibling processes. A token
that session_start puts into the server's own ``os.environ`` never reaches the
hooks, so they stayed inert and a Stop hook could not tell anyone was waiting.
The session file is the hand-over: written by session_start, read by the hooks
(``mm_load_hook_session`` in bin/mm-agent-lib.sh), removed by session_end.

It lives next to the corral session files, so ``mm-agent-sweep.sh`` sees it too:
it records MM_SESSION_PID, which the sweep uses to tell a live session from an
abandoned one.

Server and hooks have to derive the same file name. Both key it on the project
directory (CLAUDE_PROJECT_DIR, else the working directory) — the one thing the
two processes share without being told.
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


def session_file_path() -> Path:
    # Must match mm_hook_session_file in bin/mm-agent-lib.sh.
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
        f"MM_BOT_NAME={info.name}",
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


def remove() -> None:
    try:
        session_file_path().unlink()
    except FileNotFoundError:
        pass
