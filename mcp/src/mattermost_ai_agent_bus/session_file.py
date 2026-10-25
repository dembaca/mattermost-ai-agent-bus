"""Session file: the bot a session registered itself, for `claude --continue`.

session_start writes it (0600), session_end removes it; the server's exit
touches it, and the next server of the same Claude Code session takes the bot
over from it (RT-6.10). It lives next to the corral session files, so
``mm-agent-sweep.sh`` sees it too: it records MM_SESSION_PID, which the sweep
uses to tell a live session from an abandoned one. No shell reads its token:
hooks and scripts ask the server's hook API (RT-6.17), whose endpoint file is
keyed the same way (keyed_path).

Two sessions must never share a key. Under Claude Code it is the session
(CLAUDE_CODE_SESSION_ID), which Claude Code puts into the environment of the
MCP server, the hooks and every command the agent starts. Elsewhere (Cursor,
OpenCode, Codex) nothing names a session, so it is the workspace (workspace()):
one MCP server per workspace, one bot per workspace. Keyed on the project
alone, a second Claude Code session in the same checkout overwrote the first
one's file: the first agent's hooks and listener then ran as the second bot
and never saw the messages for its own (RT-6.10).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

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


# A transcript larger than this is not read at all (RT-6.18, RT-6.9).
TRANSCRIPT_MAX = 64 << 20


def claude_projects() -> str:
    """The real path of ~/.claude/projects, where Claude Code keeps transcripts."""
    return os.path.realpath(Path.home() / ".claude" / "projects")


def _fd_path(fd: int) -> str | None:
    """The path an open fd really names, or None where it cannot be told."""
    try:
        if sys.platform == "darwin":
            buf = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
            return os.fsdecode(buf.split(b"\0", 1)[0]) or None
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError):
        return None


def read_transcript(path: str | os.PathLike[str]) -> str | None:
    """A transcript's text, or None when it may not or cannot be read (RT-6.18).

    The path came from the agent's side. Never through a symlink in its last
    part, never a FIFO or device (opened non-blocking, a regular file only),
    nothing over TRANSCRIPT_MAX. In the host process of a keyless session the
    opened file must still lie under the host's ~/.claude/projects/: a
    directory swapped for a symlink after a path check is caught here.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > TRANSCRIPT_MAX:
            return None
        if hosted():
            real = _fd_path(fd)
            if not real or not real.startswith(claude_projects() + os.sep):
                return None
        chunks: list[bytes] = []
        size = 0
        while size <= TRANSCRIPT_MAX:
            chunk = os.read(fd, min(1 << 20, TRANSCRIPT_MAX + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > TRANSCRIPT_MAX:
            return None
        return b"".join(chunks).decode("utf-8")
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def state_dir() -> Path:
    if _hosted_dir is not None:
        return _hosted_dir
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "mm-agent-bus"


# The workspace the client named in roots/list, when the environment names none.
_roots_dir: str | None = None
# Where the workspace may come from, before roots and cwd (RT-6.17 step 2).
WORKSPACE_VARS = ("CLAUDE_PROJECT_DIR", "MM_WORKSPACE")


def workspace() -> tuple[str, str]:
    """The directory a session without a session id is keyed on, and its source:
    CLAUDE_PROJECT_DIR, MM_WORKSPACE (Cursor: ``${workspaceFolder}``), the
    first file:// root the client named (roots), else the server's working
    directory (cwd; OpenCode starts its servers there). Resolved, so a shell
    that walks up from its real directory meets the same name. Must match
    mm_bus_file in bin/mm-agent-lib.sh (RT-6.17 step 2)."""
    if _hosted_dir is not None:
        # The sandbox's directory, from the relay's handshake: a name only,
        # never resolved, opened or entered on the host (RT-6.18).
        return os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or "/"), "hosted"
    for var in WORKSPACE_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return os.path.realpath(value), var
    if _roots_dir:
        return _roots_dir, "roots"
    return os.path.realpath(os.getcwd()), "cwd"


def project_dir() -> str:
    return workspace()[0]


def root_path(uri: str) -> str | None:
    """A client's file:// root as the real path of a directory, else None."""
    parts = urlsplit(uri)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        return None
    path = unquote(parts.path)
    if not os.path.isabs(path) or not os.path.isdir(path):
        return None
    return os.path.realpath(path)


def set_roots_dir(path: str) -> bool:
    """Key on the client's first root from now on, where neither a session id
    nor the environment names the workspace. True when the key changed."""
    global _roots_dir
    if session_id() or workspace()[1] not in ("cwd", "roots"):
        return False
    before = keyed_path("mcp")
    _roots_dir = path
    return keyed_path("mcp") != before


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
    # The host process keeps its files where the host hook chose: nothing from
    # a sandbox may redirect them (RT-6.18). Elsewhere the state dir is the
    # user's own and may be a symlink (a dotfiles setup).
    if hosted() and os.path.islink(d):
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
    the session id, else <prefix>-<key>.env keyed on the workspace.
    Must match mm_bus_file in bin/mm-agent-lib.sh."""
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
