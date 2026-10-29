"""RT-6.22 on the shell side: MM_PROJECT_CHANNEL is a list, its first entry
the home (mm_home_channel, mm_work_channel_id), the others guest channels that
mm-agent-channels.sh joins without failing the session start. One entry
behaves as before."""

from __future__ import annotations

import os
import subprocess

from conftest import GUEST, PROJECT, ROOT

STATUS = "bin/mm-agent-status.sh"
CHANNELS = "bin/mm-agent-channels.sh"


def _lib(value: str) -> list[str]:
    """mm_project_channels, then mm_home_channel, under a C locale (as in CI)."""
    p = subprocess.run(
        ["bash", "-c", 'source "$1"; mm_project_channels; mm_home_channel', "_",
         str(ROOT / "bin" / "mm-agent-lib.sh")],
        env={"PATH": os.environ["PATH"], "LC_ALL": "C", "LANG": "C",
             "MM_PROJECT_CHANNEL": value, "MM_CHANNEL": "agents"},
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout.split("\n")


def test_the_list_names_the_home_first():
    """The same names client.channel_list reads: trimmed, no ~, no repeats."""
    assert _lib(" ~proj-a , guest-b,,proj-a ") == ["proj-a", "guest-b", "proj-a"]
    assert _lib("") == ["agents"]  # no project channel: the lobby is the home


def test_guests_are_joined_and_a_failed_one_is_reported(mm, run):
    mm.missing.add("nope")
    r = run(CHANNELS, "resolve", MM_PROJECT_CHANNEL="proj-home, guest-a,nope")
    assert r.code == 0, r.stderr
    assert f"export MM_PROJECT_CHANNEL_ID={PROJECT}" in r.stdout
    assert "guest channel 'nope' not joined: does not exist" in r.stderr
    assert "home #proj-home, guests #guest-a" in r.stderr
    assert ("POST", f"/api/v4/channels/{GUEST}/members", {"user_id": "a" * 26}) in mm.writes


def test_a_private_guest_is_reported_the_home_stays_fatal(mm, run):
    mm.deny.add(f"/api/v4/channels/{GUEST}/members")
    r = run(CHANNELS, "resolve", MM_PROJECT_CHANNEL="proj-home,guest-a")
    assert r.code == 0 and "guest channel 'guest-a' not joined: private" in r.stderr
    mm.missing.add("proj-gone")
    assert run(CHANNELS, "resolve", MM_PROJECT_CHANNEL="proj-gone,guest-a").code == 1


def test_one_entry_resolves_as_before(mm, run):
    """Parity: one project channel, no guest joined, the same exports."""
    r = run(CHANNELS, "resolve", MM_PROJECT_CHANNEL="proj-home")
    assert r.code == 0, r.stderr
    assert "export MM_PROJECT_CHANNEL=proj-home\n" in r.stdout
    assert not [w for w in mm.writes if GUEST in w[1]] and "guest" not in r.stderr


def test_the_status_names_the_first_entry(mm, run):
    """RT-6.6 step 1: the home in the status is the list's first entry."""
    assert run(STATUS, "working", "x", fd_token=True, MM_PROJECT_CHANNEL="team-a,guest-b",
               LC_ALL="C", LANG="C").code == 0
    assert mm.custom_status()["text"].endswith(" · ~team-a")
