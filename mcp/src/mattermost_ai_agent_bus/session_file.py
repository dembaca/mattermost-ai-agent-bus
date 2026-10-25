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

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import SessionInfo

DEFAULT_AGENT_PREFIX = "agent-"


def agent_prefix(values: Mapping[str, str] | None = None) -> str:
    """The registrar's bot username prefix (its BOT_USERNAME_PREFIX, arc42 §7.1).

    MM_AGENT_PREFIX when set; else MM_BOT_USERNAME minus the name it was
    registered with (MM_REG_BOT_NAME); else "agent-". ``values`` is the
    environment by default, or a session file's keys. Must match
    mm_agent_prefix in bin/mm-agent-lib.sh.
    """
    v = os.environ if values is None else values
    if v.get("MM_AGENT_PREFIX"):
        return v["MM_AGENT_PREFIX"]
    user, name = v.get("MM_BOT_USERNAME", ""), v.get("MM_REG_BOT_NAME", "")
    if name and len(user) > len(name) and user.endswith(name):
        return user[: -len(name)]
    return DEFAULT_AGENT_PREFIX


# The state directory the host process chose for its session (RT-6.18): set,
# this process is the host side of a keyless corral session.
_hosted_dir: Path | None = None


def use_hosted_dir(path: Path) -> Path:
    """Make ``path`` this process's state directory for good (RT-6.18).

    The host process of a keyless session keeps its per-bot files there, never
    in a directory the sandbox names: HOME and XDG_STATE_HOME of the session
    are the agent's. Created 0700; refused unless it is a real directory of
    this user that nobody else may write.
    """
    global _hosted_dir
    if not path.is_absolute():
        raise ValueError(f"not an absolute path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_private_dir(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    ensure_private_dir(path)
    _hosted_dir = path
    return path


def hosted() -> bool:
    """This process is the host side of a keyless session (RT-6.18)."""
    return _hosted_dir is not None


def state_dir() -> Path:
    if _hosted_dir is not None:
        return _hosted_dir
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "mm-agent-bus"


def project_dir() -> str:
    if _hosted_dir is not None:
        # The sandbox's directory, from the relay's handshake: a name only,
        # never resolved, opened or entered on the host (RT-6.18).
        return os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or "/")
    return os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


# --- writing state files --------------------------------------------------------

def ensure_private_dir(path: Path) -> None:
    """A directory of this user, not a symlink, writable by nobody else."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, "not a real directory", str(path))
    if st.st_uid != os.geteuid():
        raise OSError(errno.EPERM, "not this user's directory", str(path))
    if st.st_mode & 0o022:
        raise OSError(errno.EPERM, "writable by others", str(path))


def _state_parent(path: Path) -> Path:
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.islink(d):
        raise OSError(errno.ELOOP, "state directory is a symlink", str(d))
    if os.path.islink(path):
        raise OSError(errno.ELOOP, "state file is a symlink", str(path))
    return d


def write_private(path: Path, text: str) -> None:
    """Replace a state file, 0600: a new file in its directory (mkstemp,
    exclusive), then renamed over it. Refuses a symlink in its place: nothing
    written here may land in the file it points to (RT-6.18)."""
    d = _state_parent(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_private(path: Path, text: str) -> None:
    """Append to a state file, 0600, never through a symlink (O_NOFOLLOW)."""
    _state_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(text)


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()


def keyed_path(prefix: str) -> Path:
    """A file of this session in the state dir: <prefix>-sid-<key>.env keyed on
    the session id, else <prefix>-<key>.env keyed on the project directory.
    Must match mm_hook_session_file in bin/mm-agent-lib.sh."""
    sid = session_id()
    if sid:
        return state_dir() / f"{prefix}-sid-{hashlib.sha1(sid.encode()).hexdigest()[:16]}.env"
    key = hashlib.sha1(project_dir().encode()).hexdigest()[:16]
    return state_dir() / f"{prefix}-{key}.env"


def session_file_path() -> Path:
    return keyed_path("mcp")


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
        f"MM_AGENT_PREFIX={info.prefix}",
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
