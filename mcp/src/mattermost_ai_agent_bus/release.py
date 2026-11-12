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
STAMP_FILE = "release-check.at"
CHECK_SCRIPT = ROOT / "bin" / "mm-agent-release-check.sh"
CHECK_EVERY = 86400       # at most once a day (ADR-011 point 3)
CHECK_TIMEOUT = 30.0      # the child's bound; then TERM, and KILL after KILL_GRACE
KILL_GRACE = 2.0
FIRST_DELAY = 60.0        # not while the session starts
POLL = 3600.0
STATE_MAX = 65536
# What the child gets of this server's environment besides PATH and HOME:
# where the releases come from, the pinned keys, where src.git lies. Never a
# token, the registration secret or anything of the session.
CHILD_ENV = ("MM_RELEASE_URL", "MM_RELEASE_SIGNERS", "MM_BUS_HOST_DIR")

_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

# The Needs: grammar (ADR-011 point 3): variable names, versions and these
# words, a comma after a word, at most NEEDS_MAX characters. Kept alike in
# bin/mm-agent-lib.sh (MM_NEEDS_WORDS) and scripts/doc-check.py.
NEEDS_WORDS = frozenset(
    "alike all and before corral deployed dispatcher every first host hosts new on or "
    "registrar removed renamed restart server servers session sessions set same the to "
    "unset update".split())
NEEDS_MAX = 120
_NEEDS_CHARS = re.compile(r"^[A-Za-z0-9_., ]+$")
_NEEDS_VAR = re.compile(r"^MM_[A-Z0-9_]+$")
NOTE = ("a human installs a release on the host (mm-agent-host-install.sh --release); "
        "you never run the installer, and an update never runs by itself")


def needs_ok(text: str) -> bool:
    """A `Needs:` line's text in its grammar (RT-6.30)."""
    if not text or len(text) > NEEDS_MAX or not _NEEDS_CHARS.match(text):
        return False
    for word in text.split():
        word = word.removesuffix(",")
        if not (_NEEDS_VAR.match(word) or _VER.match(word) or word in NEEDS_WORDS):
            return False
    return True


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
    """What kind of tree this server runs (RT-6.29 step 7): ("signed", tag) for
    a release whose signature the installer verified, ("release", tag) for one
    installed without the keys, else ("unsigned", ""): `--from`, a clone, the
    plugin, a host copy from before 0.81.0. Like mm_release_kind."""
    text = _read((root or ROOT) / "SOURCE", 4096) or ""
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
    return {"signed": f"signed release {tag}",
            "release": f"release {tag} (signature not checked)"}.get(kind, "unsigned")


# The roster's version (ADR-011 point 6): one segment of the status text,
# `v0.83.0`, `v0.83.0 unchecked` or `v0.83.0 unsigned`; older readers skip a
# segment they do not know (usage.from_status, home_of, the status word).
_SOURCE_WORD = {"signed": "", "release": "unchecked", "unsigned": "unsigned"}
_SEGMENT = re.compile(r"^v(\d+\.\d+\.\d+)(?: (unchecked|unsigned))?$")


def segment() -> list[str]:
    """The status segment of this server's version; [] when it has none."""
    if not VERSION:
        return []
    word = _SOURCE_WORD[running()[0]]
    return [f"v{VERSION} {word}" if word else f"v{VERSION}"]


def from_status(text: str) -> dict[str, str]:
    """The version an agent's status text names: {version[, source]}, source
    "unchecked" or "unsigned" (none: a signed release); {} when it names none."""
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

def child_env() -> dict[str, str]:
    """The child's whole environment: PATH (the script keeps only entries no
    sandbox writes), HOME, LC_ALL=C and CHILD_ENV; for a host copy's release
    without MM_BUS_HOST_DIR, the layout it lies in (RT-6.28)."""
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(Path.home()), "LC_ALL": "C"}
    env.update({k: os.environ[k] for k in CHILD_ENV if k in os.environ})
    if "MM_BUS_HOST_DIR" not in env and ROOT.parent.name == "releases":
        env["MM_BUS_HOST_DIR"] = str(ROOT.parent.parent / "current")
    return env


def due(directory: Path | None = None, now: float | None = None) -> bool:
    """The last try (release-check.at) is a day old, or there was none."""
    text = _read((directory or state_dir()) / STAMP_FILE, 64) or ""
    last = int(text.strip()) if text.strip().isdigit() else 0
    return (time.time() if now is None else now) - last >= CHECK_EVERY


async def run_check(script: Path | None = None, *, directory: Path | None = None,
                    timeout: float = CHECK_TIMEOUT) -> int | None:
    """Run the check once as a child (ADR-011 point 3): its own process group,
    stdin closed, the environment of child_env() only, from /, `--if-due` a
    day; after `timeout` seconds its group gets TERM (git removes its lock
    files on it), KILL_GRACE later KILL. Its exit code; None when it did not
    run or was stopped. Never inside a corral sandbox."""
    script = script or CHECK_SCRIPT
    if os.environ.get("CORRAL_SANDBOX") or not script.is_file():
        return None
    bash = "/bin/bash" if Path("/bin/bash").exists() else "bash"
    proc = await asyncio.create_subprocess_exec(
        bash, str(script), "--state-dir", str(directory or state_dir()),
        "--if-due", str(CHECK_EVERY), env=child_env(), cwd="/",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _stop(proc)
        log.info("release check: no result within %.0f s, stopped (RT-6.30)", timeout)
        return None
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    text = out.decode("utf-8", "replace").strip()
    if text:
        log.info("release check: %s", text[-500:].replace("\n", " | "))
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
