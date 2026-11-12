"""The release notice (ADR-011 phase 2, RT-6.30 in docs/arc42/06-runtime-view.md).

The host's release check, bin/mm-agent-release-check.sh, lists the newest
release tags, checks them as the installer does (RT-6.29: with the pinned keys
when the host sets MM_RELEASE_SIGNERS) and writes what the CHANGELOG of the
newest one that passed says into release.env in the host state directory. This
module only reads that file, and checks every field again: the server parses
nothing from the network itself. What reaches the agent is a version, the step
(major, minor, patch), `Breaking`, the `Needs:` lines in their grammar and
whether the signature was checked; never free text.

It also starts that check, at most once a day, as a child with a clean
environment and a timeout that writes only its state (check_loop). Nothing here
installs anything: a human installs a release on the host
(`mm-agent-host-install.sh --release v<x.y.z>`), never the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import session_file

log = logging.getLogger(__name__)

# The tree this server runs from: a checkout, the plugin, or releases/<name>/
# of the host copy (RT-6.28).
ROOT = Path(__file__).resolve().parents[3]
STATE_FILE = "release.env"
STAMP_FILE = "release-check.at"          # the last check that succeeded (the script writes it)
FAILED_FILE = "release-check.failed"     # the failed tries, one epoch a line (this module)
SCRIPT = Path("bin") / "mm-agent-release-check.sh"
HOST_DIR = Path.home() / ".local" / "share" / "mm-agent-bus" / "current"
CHECK_EVERY = 86400       # at most once a day (ADR-011 point 3)
TRIES = 3                 # failed tries a day, one per POLL
CHECK_TIMEOUT = 120.0     # the child's bound; then TERM, and KILL after KILL_GRACE
KILL_GRACE = 2.0
FIRST_DELAY = 60.0        # not while the session starts
POLL = 3600.0
STATE_MAX = 65536
# What the child gets of this server's environment besides PATH and HOME:
# where the releases come from, the pinned keys, where src.git lies, the
# session's workdir (which the script's own sandbox check counts). Never a
# token, the registration secret or anything else of the session.
CHILD_ENV = ("MM_RELEASE_URL", "MM_RELEASE_SIGNERS", "MM_BUS_HOST_DIR", "CORRAL_WORKDIR")

_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

# The Needs: grammar (ADR-011 point 3, CLAUDE.md "Releasing"): `MM_*` names,
# versions x.y.z and lowercase words of at most 15 letters, each with an
# optional `,` or `.`, one space between, at most NEEDS_MAX characters. The
# same text as MM_NEEDS_RE in bin/mm-agent-lib.sh; scripts/doc-check.py reads
# both from here.
NEEDS_PATTERN = r"^(MM_[A-Z0-9_]+|[0-9]+\.[0-9]+\.[0-9]+|[a-z]{1,15})[,.]?( (MM_[A-Z0-9_]+|[0-9]+\.[0-9]+\.[0-9]+|[a-z]{1,15})[,.]?)*$"
NEEDS_MAX = 120
_NEEDS = re.compile(NEEDS_PATTERN)
NOTE = ("a human installs a release on the host (mm-agent-host-install.sh --release); "
        "you never run the installer, and an update never runs by itself")


def needs_ok(text: str) -> bool:
    """A `Needs:` line's text in its grammar (RT-6.30)."""
    return 0 < len(text) <= NEEDS_MAX and _NEEDS.fullmatch(text) is not None


def _key(v: str) -> tuple[int, int, int]:
    m = _VER.match(v)
    return (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)


def _plugin_version() -> str:
    try:
        v = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return v if isinstance(v, str) and _VER.match(v) else ""


VERSION = _plugin_version()


def _read(path: Path, limit: int) -> str | None:
    """A regular file's text, never through a symlink, at most `limit` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def running(root: Path | None = None) -> tuple[str, str]:
    """What kind of tree this server runs (RT-6.29 step 7, RT-6.30): ("signed",
    tag) for a release whose signature the installer verified, ("release", tag)
    for one installed without the keys, ("plugin", "") for a tree without a
    SOURCE file that is no host copy's release (the plugin, a checkout), else
    ("unsigned", ""): `--from`, a clone, a host copy from before 0.81.0. Like
    mm_release_kind, which tells host copies only."""
    root = root or ROOT
    text = _read(root / "SOURCE", 4096)
    if text is None:
        return ("unsigned", "") if root.parent.name == "releases" else ("plugin", "")
    lines = text.splitlines()
    tag = next((ln[4:] for ln in lines if ln.startswith("TAG=")), "")
    if _TAG.match(tag):
        if "SOURCE=signed" in lines:
            return "signed", tag
        if "SOURCE=release" in lines:
            return "release", tag
    return "unsigned", ""


def source_label(root: Path | None = None) -> str:
    kind, tag = running(root)
    return {"signed": f"signed release {tag}", "release": f"release {tag} (signature not checked)",
            "plugin": "plugin"}.get(kind, "unsigned")


# The roster's version (ADR-011 point 6): one segment of the status text,
# `v0.83.0`, `v0.83.0 unchecked`, `v0.83.0 unsigned` or `v0.83.0 plugin`; older
# readers skip a segment they do not know (usage.from_status, home_of, the
# status word). lease.status_text cuts it before the label.
_SOURCE_WORD = {"signed": "", "release": "unchecked", "unsigned": "unsigned", "plugin": "plugin"}
_SEGMENT = re.compile(r"^v(\d+\.\d+\.\d+)(?: (unchecked|unsigned|plugin))?$")


def segment() -> str:
    """The status segment of this server's version; '' when it has none."""
    if not VERSION:
        return ""
    word = _SOURCE_WORD[running()[0]]
    return f"v{VERSION} {word}" if word else f"v{VERSION}"


def from_status(text: str) -> dict[str, str]:
    """The version an agent's status text names: {version[, source]}, source
    "unchecked", "unsigned" or "plugin" (none: a signed release); {} when it
    names none."""
    for piece in str(text).split(" · "):
        m = _SEGMENT.match(piece.strip())
        if m:
            return {"version": m[1], **({"source": m[2]} if m[2] else {})}
    return {}


@dataclass
class State:
    """release.env, every field checked again."""
    newest: str
    verified: bool
    checked_at: int = 0
    breaking: set[str] = field(default_factory=set)
    needs: list[tuple[str, str]] = field(default_factory=list)
    withdrawn: set[str] = field(default_factory=set)


def state_dir() -> Path:
    return session_file.host_state_dir()


def read_state(directory: Path | None = None) -> State | None:
    """What the host's last check that passed found; None for no check yet or
    a file without its fields. A line outside its form is skipped."""
    text = _read((directory or state_dir()) / STATE_FILE, STATE_MAX)
    if text is None:
        return None
    fields: dict[str, str] = {}
    breaking: set[str] = set()
    needs: list[tuple[str, str]] = []
    withdrawn: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        ver, _, rest = value.partition(" ")
        if key in ("NEWEST", "VERIFIED", "CHECKED_AT"):
            fields[key] = value
        elif key == "ENTRY" and _VER.match(ver) and rest == "breaking":
            breaking.add(ver)
        elif key == "NEEDS" and _VER.match(ver) and needs_ok(rest) and (ver, rest) not in needs:
            needs.append((ver, rest))
        elif key == "WITHDRAWN" and _VER.match(value):
            withdrawn.add(value)
    newest, verified = fields.get("NEWEST", ""), fields.get("VERIFIED", "")
    if not _VER.match(newest) or verified not in ("yes", "no"):
        return None
    # A release withdraws only older ones.
    withdrawn = {v for v in withdrawn if _key(v) < _key(newest)}
    at = fields.get("CHECKED_AT", "")
    return State(newest=newest, verified=verified == "yes",
                 checked_at=int(at) if at.isdigit() else 0,
                 breaking=breaking, needs=needs, withdrawn=withdrawn)


def _between(st: State, have: str) -> tuple[bool, list[str]]:
    """Breaking and the Needs: lines of the entries after `have` up to the
    newest release: an update from `have` needs what each of them needs."""
    lo, hi = _key(have), _key(st.newest)
    breaking = any(lo < _key(v) <= hi for v in st.breaking)
    needs = list(dict.fromkeys(t for v, t in st.needs if lo < _key(v) <= hi))
    return breaking, needs


def _step(have: str, newest: str) -> str:
    a, b = _key(have), _key(newest)
    return "major" if a[0] != b[0] else "minor" if a[1] != b[1] else "patch"


def notice(have: str | None = None, state: State | None = None,
           directory: Path | None = None) -> dict[str, Any]:
    """bus_info's `release` (RT-6.30): this server's version and source, and
    what the host's check found: the newest release that passed, the step to
    it, Breaking and the Needs: lines of the entries in between, whether its
    signature was checked, this version withdrawn, the newest older than this."""
    have = VERSION if have is None else have
    if not _VER.match(have):
        return {}  # this server's own version unknown: nothing to compare
    out: dict[str, Any] = {"this": {"version": have, "source": source_label()}}
    st = state if state is not None else read_state(directory)
    if st is None:
        out["check"] = "none yet: the host's release check has not found a release that passes"
        return out
    out["newest"] = st.newest
    out["signature"] = "checked" if st.verified else "not checked"
    if st.checked_at:
        out["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.checked_at))
    if _key(st.newest) > _key(have):
        breaking, needs = _between(st, have)
        out["step"] = _step(have, st.newest)
        out["breaking"] = breaking
        if needs:
            out["needs"] = needs
    if have in st.withdrawn:
        out["withdrawn"] = True
    if _key(st.newest) < _key(have):
        out["older"] = True
    text = line(have, st)
    if text:
        out["notice"] = text
        out["note"] = NOTE
    return out


def line(have: str | None = None, state: State | None = None,
         directory: Path | None = None) -> str:
    """One line for the human (the turn-end banner, RT-6.2 step 8); '' when
    there is nothing to say. The same text as mm_release_notice in
    bin/mm-agent-lib.sh."""
    have = VERSION if have is None else have
    st = state if state is not None else read_state(directory)
    if st is None or not _VER.match(have):
        return ""
    parts = []
    if have in st.withdrawn:
        parts.append(f"this host's {have} is withdrawn (CHANGELOG of v{st.newest})")
    if _key(st.newest) > _key(have):
        breaking, needs = _between(st, have)
        what = _step(have, st.newest)
        if breaking:
            what += ", Breaking"
        if needs:
            what += "; needs: " + "; ".join(needs)
        what += "; signature checked" if st.verified else "; signature not checked"
        parts.append(f"release v{st.newest} is out ({what})")
    elif _key(st.newest) < _key(have):
        parts.append(f"the newest release that passes the check is v{st.newest}, older than "
                     f"{have}: the repository may be frozen or rewound")
    return "; ".join(parts)


# --- the check, as a bounded child --------------------------------------------------

# corral's temporary directories (mm_sandbox_can_write in bin/mm-agent-lib.sh).
SANDBOX_TMP = ("/tmp/corral-", "/private/tmp/corral-")
_noted: set[str] = set()


def sandbox_can_write(path: str) -> bool:
    """A path a corral sandbox can write: every corral home, ~/.claude (the
    plugin cache among it), the session's workdir, corral's temporary
    directories; one that is not absolute or has a . or .. component counts too.
    Without regard to case. The twin of mm_sandbox_can_write."""
    p = path + "/"
    if not path.startswith("/") or "/../" in p or "/./" in p:
        return True
    low = p.lower()
    if low.startswith(SANDBOX_TMP):
        return True
    home = Path.home()
    roots = [str(home / ".cache" / "corral"), str(home / ".claude"), os.environ.get("CORRAL_WORKDIR", "")]
    tmp = os.environ.get("TMPDIR", "")
    if "corral" in tmp.lower():
        roots.append(tmp)
    for r in filter(None, roots):
        for c in {r, os.path.realpath(r)}:
            if low.startswith(c.rstrip("/").lower() + "/"):
                return True
    return False


def _note_once(msg: str) -> None:
    if msg not in _noted:
        _noted.add(msg)
        log.info("release check: %s (RT-6.30)", msg)


def check_script() -> tuple[Path, str] | None:
    """Which check script to run, and the MM_BUS_HOST_DIR to give it ('' keeps
    the environment's). This tree's own when no sandbox can write it (a host
    copy's release, a reviewed checkout); for a tree a sandbox can write (the
    plugin cache under ~/.claude) never that one, but the host copy's
    (MM_BUS_HOST_DIR, default ~/.local/share/mm-agent-bus/current) when it is
    there and out of every sandbox's reach; else none, said once in the log."""
    if not sandbox_can_write(str(ROOT)):
        return ROOT / SCRIPT, ""
    host = os.environ.get("MM_BUS_HOST_DIR") or str(HOST_DIR)
    real = os.path.realpath(host)
    if os.path.isabs(host) and not sandbox_can_write(real) and (Path(real) / SCRIPT).is_file():
        return Path(real) / SCRIPT, host
    _note_once(f"not run: this server's tree {ROOT} can be changed from inside a corral sandbox, "
               f"and the host copy {host} has no check script out of every sandbox's reach")
    return None


def child_env(host_dir: str = "") -> dict[str, str]:
    """The child's whole environment: PATH (the script keeps only entries no
    sandbox writes), HOME, LC_ALL=C and CHILD_ENV; MM_BUS_HOST_DIR the host
    copy whose script runs, or for a host copy's release without it the layout
    it lies in (RT-6.28)."""
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(Path.home()), "LC_ALL": "C"}
    env.update({k: os.environ[k] for k in CHILD_ENV if k in os.environ})
    if host_dir:
        env["MM_BUS_HOST_DIR"] = host_dir
    elif "MM_BUS_HOST_DIR" not in env and ROOT.parent.name == "releases":
        env["MM_BUS_HOST_DIR"] = str(ROOT.parent.parent / "current")
    return env


def _failures(directory: Path, now: float) -> list[int]:
    text = _read(directory / FAILED_FILE, 4096) or ""
    return [int(x) for x in text.split() if x.isdigit() and now - int(x) < CHECK_EVERY]


def _record_failure(directory: Path, now: float) -> None:
    """One failed try more in release-check.failed (the last TRIES kept)."""
    kept = [*_failures(directory, now), int(now)][-TRIES:]
    try:
        session_file.write_private(directory / FAILED_FILE, "".join(f"{x}\n" for x in kept))
    except OSError as exc:
        log.debug("release check: %s", exc)


def due(directory: Path | None = None, now: float | None = None) -> bool:
    """The last check that succeeded (release-check.at) is a day old, or there
    was none, and fewer than TRIES tries failed in the last day: a failure is
    tried again at the next hourly look, at most TRIES times a day."""
    d = directory or state_dir()
    now = time.time() if now is None else now
    text = (_read(d / STAMP_FILE, 64) or "").strip()
    if now - (int(text) if text.isdigit() else 0) < CHECK_EVERY:
        return False
    return len(_failures(d, now)) < TRIES


async def run_check(script: Path | None = None, *, directory: Path | None = None,
                    timeout: float = CHECK_TIMEOUT) -> int | None:
    """Run the check once as a child (ADR-011 point 3): the script of
    check_script() unless one is given, its own process group, stdin closed,
    the environment of child_env() only, from /, `--if-due` a day; after
    `timeout` seconds its group gets TERM (git removes its lock files on it),
    KILL_GRACE later KILL. A failure or a stop counts in release-check.failed;
    the script stamps a success. Its exit code; None when it did not run or
    was stopped. Never inside a corral sandbox."""
    if os.environ.get("CORRAL_SANDBOX"):
        return None
    host_dir = ""
    if script is None:
        found = check_script()
        if found is None:
            return None
        script, host_dir = found
    if not script.is_file():
        return None
    d = directory or state_dir()
    bash = "/bin/bash" if Path("/bin/bash").exists() else "bash"
    proc = await asyncio.create_subprocess_exec(
        bash, str(script), "--state-dir", str(d),
        "--if-due", str(CHECK_EVERY), env=child_env(host_dir), cwd="/",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _stop(proc)
        _record_failure(d, time.time())
        log.info("release check: no result within %.0f s, stopped (RT-6.30)", timeout)
        return None
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    text = out.decode("utf-8", "replace").strip()
    if text:
        log.info("release check: %s", text[-500:].replace("\n", " | "))
    if proc.returncode != 0:
        _record_failure(d, time.time())
    return proc.returncode


async def _stop(proc: asyncio.subprocess.Process) -> None:
    for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, KILL_GRACE)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except TimeoutError:
            continue


async def check_loop(first_delay: float = FIRST_DELAY) -> None:
    """Started with the server: the check when it is due, looked at hourly."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            if due():
                await run_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a notice, never in the way
            log.debug("release check: %s", exc)
        await asyncio.sleep(POLL)
