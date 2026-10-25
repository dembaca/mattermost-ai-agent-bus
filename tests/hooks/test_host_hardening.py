"""Host scripts against a sandbox's reach (arc42 §7.1, §7.3; RT-6.18): the
installer ignores what a clone's git config could run, the corral hooks, the
installer and the sweep find no command in the workdir, the location check
sees through case and `..`, credentials stay out of every environment and
message, and a FIFO cannot stall preStart."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest
from conftest import ROOT
from test_host_install import INSTALL, START, STOP, UV, VERSION, _refused, clone, git, registry
from test_keyless_session import HOST, PY, TOKEN, _shims

SWEEP = "bin/mm-agent-sweep.sh"
BASH = shutil.which("bash")
# What a planted command would be found as: anything the scripts run.
PLANTED = ("bash", "curl", "jq", "git", "tar", "dirname", "readlink", "head", "sed", "env",
           "mktemp", "find", "chmod", "mv", "rm", "cat", "stat", "date", "sleep", "kill",
           "basename", "hostname", "tr", "uv", "ps", "od", "grep", "sort", "cut", "python3")


# --- 1. the installer runs nothing a clone's git config names -----------------

@pytest.mark.parametrize("how", ["filter", "include", "attributes"])
def test_installer_refuses_a_clone_whose_git_config_can_run_commands(run, tmp_path, how):
    """arc42 §7.1: filter drivers (git status, git archive), includes and
    info/attributes are refused before git could run any of them."""
    c = clone(tmp_path)
    marker = tmp_path / "ran"
    cmd = f"sh -c 'echo ran >>{marker}; cat'"
    if how == "filter":
        git(c, "config", "filter.x.clean", cmd)
        git(c, "config", "filter.x.smudge", cmd)
    elif how == "include":
        inc = tmp_path / "inc.cfg"
        inc.write_text(f'[filter "x"]\n\tclean = {cmd}\n\tsmudge = {cmd}\n')
        git(c, "config", "include.path", str(inc))
    if how in ("filter", "attributes"):
        (c / ".git" / "info").mkdir(exist_ok=True)
        (c / ".git" / "info" / "attributes").write_text("* filter=x\n")
    (c / "bin" / "mm-agent-lib.sh").touch()  # stat-dirty: git status would clean it
    dest = tmp_path / "current"
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "refused" in r.stderr, r.stderr
    assert not marker.exists() and not dest.exists()


def test_installer_names_the_commit_it_installs(run, tmp_path):
    c = clone(tmp_path)
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(tmp_path / "current"))
    assert r.code == 0, r.stderr
    assert f"commit: {git(c, 'rev-parse', 'HEAD').strip()} reviewed" in r.stdout


def test_installer_refuses_a_source_by_case_or_under_corral_tmp(run, tmp_path):
    """macOS volumes ignore case: ~/.CLAUDE is ~/.claude (arc42 §7.1)."""
    src = tmp_path / ".CLAUDE" / "clone"
    shutil.copytree(clone(tmp_path), src, symlinks=True)
    r = run(INSTALL, "--from", str(src), **UV, MM_BUS_HOST_DIR=str(tmp_path / "current"))
    assert r.code != 0 and "can be changed from inside a corral sandbox" in r.stderr


# --- 2. no command from the workdir -------------------------------------------

def _plant(at, marker):
    at.mkdir(parents=True, exist_ok=True)
    for name in PLANTED:
        (at / name).write_text(f'#!/bin/sh\necho {name} >>"{marker}"\nexit 1\n')
        (at / name).chmod(0o755)


def _workdir(tmp_path, *, absolute=True):
    """A sandbox's workdir with its own commands, and a PATH that would find
    them: relative entries, and the workdir by name where it is known
    (CORRAL_WORKDIR, in the hooks)."""
    wd = tmp_path / "work"
    marker = tmp_path / "planted.ran"
    _plant(wd, marker)
    _plant(wd / "relbin", marker)
    path = os.pathsep.join([".", "", "relbin", *([str(wd)] if absolute else []),
                            os.environ["PATH"]])
    return wd, marker, path


def _direct(script, args, *, cwd, env):
    """Started the way corral does, with an absolute bash: the test's own
    lookup must not find the planted one."""
    p = subprocess.run([BASH, str(ROOT / script), *args], cwd=cwd, env=env, input="{}",
                       capture_output=True, text=True, timeout=60)
    return p


@pytest.mark.skipif(not PY.exists(), reason="needs mcp/.venv: uv run --directory mcp")
def test_hooks_run_no_command_from_the_workdir(mm, run, tmp_path):
    """arc42 §7.3: `.`, empty and relative PATH entries and the workdir are
    dropped, and the hooks work from /; the session still gets its bus (RT-6.18)."""
    wd, marker, path = _workdir(tmp_path)
    env = {"PATH": path, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "MM_CHAT_URL": mm.url, "CORRAL_WORKDIR": str(wd), "CORRAL_SESSION_ID": "sess-path",
           **{k: v for k, v in HOST.items() if v is not None}}
    p = _direct(START, [], cwd=wd, env=env)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert "MM_BUS_UNAVAILABLE" not in out["env"], p.stderr
    assert out["env"]["MM_BOT_USERNAME"].startswith("agent-")
    p = _direct(STOP, [], cwd=wd, env={**env, "MM_REG_SECRET": ""})
    assert p.returncode == 0, p.stderr
    assert any(m == "DELETE" for m, _, _ in mm.writes)
    assert not marker.exists(), marker.read_text()


def test_sweep_and_installer_run_no_command_from_the_workdir(mm, run, tmp_path):
    wd, marker, path = _workdir(tmp_path, absolute=False)
    state = tmp_path / "state" / "mm-agent-bus"
    state.mkdir(parents=True)
    (state / "gone.env").write_text(f"MM_REG_BOT_NAME=gone\nMM_BOT_USERNAME=agent-gone\n"
                                    f"MM_BOT_TOKEN={TOKEN}\nMM_SESSION_PID=999999\n")
    env = {"PATH": path, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "MM_CHAT_URL": mm.url}
    p = _direct(SWEEP, ["--apply"], cwd=wd, env=env)
    assert p.returncode == 0 and "unregistered gone" in p.stderr, p.stderr
    p = _direct(INSTALL, ["--from", str(clone(tmp_path))], cwd=wd,
                env={**env, **UV, "MM_BUS_HOST_DIR": str(tmp_path / "current")})
    assert p.returncode == 0, p.stderr
    assert not marker.exists(), marker.read_text()


# --- 3. the sweep keeps every credential out of what it starts ----------------

def test_sweep_puts_no_credential_into_any_environment_or_argv(mm, run, tmp_path):
    """arc42 §7.3: the session's bot token and the operator's token and secret
    reach the helpers on fd 3 and curl on stdin only."""
    path, dump = _shims(tmp_path)
    state = run.state_dir
    state.mkdir(parents=True)
    (state / "gone.env").write_text(f"MM_REG_BOT_NAME=gone\nMM_BOT_USERNAME=agent-gone\n"
                                    f"MM_BOT_TOKEN={TOKEN}\nMM_SESSION_PID=999999\n")
    mm.agent("o" * 26, "agent-orphan", 1000)
    r = run(SWEEP, "--apply", PATH=path, MM_BOT_TOKEN="operator-token", MM_REG_SECRET="op-secret")
    assert "unregistered gone" in r.stderr, r.stderr
    dels = [p for m, p, _ in mm.writes if m == "DELETE"]
    assert "/register/v1/agents/gone" in dels and "/register/v1/agents/orphan" in dels
    text = dump.read_text()
    assert "== bash " in text and "== curl " in text
    for v in (TOKEN, "operator-token", "op-secret", "Bearer"):
        assert v not in text, v


# --- 4. the location check sees through case and `..` -------------------------

ENV = dict(MM_BOT_TOKEN=None, MM_TEAM="team", MM_REG_SECRET="test-secret",
           MM_PROJECT_CHANNEL="proj-x", CORRAL_SESSION_ID="sess-1")


def test_hooks_refuse_claude_in_another_case(mm, run, tmp_path):
    d = tmp_path / ".CLAUDE" / "plugins" / "marketplaces" / "mattermost-ai-agent-bus"
    d.parent.mkdir(parents=True)
    d.symlink_to(ROOT)
    _refused(run(str(d / START), **ENV))
    r = run(str(d / STOP), **ENV)
    assert r.code == 0 and "nothing run from it" in r.stderr
    assert mm.writes == []


def test_hooks_refuse_dotdot_through_a_symlink(mm, run, tmp_path):
    """/safe/x/l/../hook.sh with l → ~/.claude/plug is ~/.claude/hook.sh, a
    link a sandbox can point anywhere; read as written it looked like /safe/x."""
    plug = tmp_path / ".claude" / "plug"
    plug.mkdir(parents=True)
    for script in (START, STOP):
        (tmp_path / ".claude" / os.path.basename(script)).symlink_to(ROOT / script)
    x = tmp_path / "safe" / "x"
    x.mkdir(parents=True)
    (x / "l").symlink_to(plug)
    _refused(run(f"{x}/l/../{os.path.basename(START)}", **ENV))
    r = run(f"{x}/l/../{os.path.basename(STOP)}", **ENV)
    assert r.code == 0 and "nothing run from it" in r.stderr
    assert mm.writes == []


# --- 5. a failed registration prints no token ---------------------------------

def test_register_failure_shows_the_registrar_message_not_its_answer(mm, run):
    mm.register_reply = {"bot_token": "leaked-token-x", "message": "username taken"}
    r = run("bin/mm-agent-register.sh", "--exports", "xyz", MM_BOT_TOKEN=None, MM_REG_SECRET="s")
    assert r.code != 0 and "username taken" in r.stderr
    assert "leaked-token-x" not in r.stderr + r.stdout


# --- 6. a FIFO does not stall preStart ----------------------------------------

def test_a_fifo_in_a_corral_home_does_not_stall_prestart(mm, run, tmp_path):
    """arc42 §7.1: the version notice reads installed_plugins.json and the
    plugin.json it names, both sandbox-writable, as regular files only and
    for a bounded time."""
    home = tmp_path / ".cache" / "corral" / "home-f"
    odd = home / "mattermost-ai-agent-bus"
    (odd / ".claude-plugin").mkdir(parents=True)
    os.mkfifo(odd / ".claude-plugin" / "plugin.json")
    registry(home, odd)
    other = tmp_path / ".cache" / "corral" / "home-g" / ".claude" / "plugins"
    other.mkdir(parents=True)
    os.mkfifo(other / "installed_plugins.json")
    t = time.monotonic()
    r = run(START, MM_BOT_TOKEN=None, MM_TEAM="team", CORRAL_SESSION_ID="sess-1")
    assert time.monotonic() - t < 20, r.stderr
    assert r.code == 0 and VERSION
