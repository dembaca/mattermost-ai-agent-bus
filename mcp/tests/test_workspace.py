"""RT-6.17 step 2: which workspace a session without a session id is keyed on
(Cursor, OpenCode, Codex), and the state directory outside a corral host
process (docs/arc42/06-runtime-view.md)."""

import asyncio
import errno
from types import SimpleNamespace

import pytest

from mattermost_ai_agent_bus import server, session_file
from mattermost_ai_agent_bus.client import SessionInfo

INFO = SessionInfo(url="https://mm.example", name="cur-1", username="agent-cur-1",
                   user_id="u" * 26, bot_token="tok-secret")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "MM_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session_file, "_roots_dir", None)
    for d in ("ws", "other", "cwd"):
        (tmp_path / d).mkdir()
    monkeypatch.chdir(tmp_path / "cwd")


def test_the_workspace_comes_from_the_environment_then_roots_then_cwd(tmp_path, monkeypatch):
    real = tmp_path.resolve()
    assert session_file.workspace() == (str(real / "cwd"), "cwd")
    assert session_file.set_roots_dir(str(real / "other"))
    assert session_file.workspace() == (str(real / "other"), "roots")
    monkeypatch.setenv("MM_WORKSPACE", str(tmp_path / "ws"))
    assert session_file.workspace() == (str(real / "ws"), "MM_WORKSPACE")
    assert not session_file.set_roots_dir(str(real / "cwd"))  # the environment names it
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "other"))
    assert session_file.workspace()[1] == "CLAUDE_PROJECT_DIR"


def test_a_session_id_keys_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert not session_file.set_roots_dir(str(tmp_path / "ws"))
    assert session_file.keyed_path("endpoint").name.startswith("endpoint-sid-")


def test_only_a_local_directory_is_a_root(tmp_path):
    assert session_file.root_path((tmp_path / "ws").as_uri()) == str((tmp_path / "ws").resolve())
    assert session_file.root_path("file://host/x") is None
    assert session_file.root_path((tmp_path / "gone").as_uri()) is None
    assert session_file.root_path("https://example.com/ws") is None


async def test_the_clients_roots_move_the_files(tmp_path, monkeypatch):
    """A bot registered before the roots came keeps its file: it moves along."""
    monkeypatch.setenv("MM_BOT_TOKEN", "tok-secret")
    old = session_file.write(INFO, "agents", "agents")
    moved = []
    monkeypatch.setattr(server, "_hook_api", SimpleNamespace(rekey=lambda: moved.append(1)))

    async def list_roots():
        return SimpleNamespace(roots=[SimpleNamespace(uri="https://x"),
                                      SimpleNamespace(uri=(tmp_path / "ws").as_uri())])

    await server._learn_roots(SimpleNamespace(list_roots=list_roots))
    new = session_file.session_file_path()
    assert new != old and not old.exists() and "tok-secret" in new.read_text()
    assert moved == [1]


async def test_roots_are_asked_once_initialized(tmp_path, monkeypatch):
    asked = []

    async def list_roots():
        asked.append(1)
        return SimpleNamespace(roots=[SimpleNamespace(uri=(tmp_path / "ws").as_uri())])

    async def call_next(ctx):
        return None

    with_roots = SimpleNamespace(client_capabilities=SimpleNamespace(roots={}), list_roots=list_roots)
    without = SimpleNamespace(client_capabilities=SimpleNamespace(roots=None), list_roots=list_roots)
    monkeypatch.setattr(server, "_hook_api", None)
    await server._note_client(SimpleNamespace(method="notifications/initialized", session=without,
                                              params={}), call_next)
    await server._note_client(SimpleNamespace(method="notifications/initialized", session=with_roots,
                                              params={}), call_next)
    await asyncio.sleep(0.05)
    assert asked == [1] and session_file.workspace()[1] == "roots"


def test_a_symlinked_state_dir_is_fine_off_the_host(tmp_path, monkeypatch):
    """#93 kept the host process's files out of symlinked directories; a
    developer's own state dir may be one (RT-6.18 applies to the host only)."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "link" / "x"))
    (real / "x").mkdir()
    (real / "x" / "mm-agent-bus").symlink_to(real)
    session_file.write_private(session_file.state_dir() / "a.env", "A=1\n")
    assert (real / "a.env").read_text() == "A=1\n"
    monkeypatch.setattr(session_file, "_hosted_dir", session_file.state_dir())
    with pytest.raises(OSError) as exc:
        session_file.write_private(session_file.state_dir() / "b.env", "B=1\n")
    assert exc.value.errno == errno.ELOOP


async def test_no_take_over_before_the_roots_name_the_workspace(monkeypatch):
    """RT-6.10 step 7: keyed on the working directory until the client's roots
    answer, a server could take over the wrong workspace's bot: it waits."""
    adopted = []
    client = SimpleNamespace(adopt_sync=lambda: adopted.append(1))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_client_named", True)
    monkeypatch.setattr(server, "_adopt_tried", False)
    monkeypatch.setattr(server, "_roots_pending", True)
    assert server._get_client() is client and adopted == []

    async def list_roots():
        return SimpleNamespace(roots=[])

    await server._learn_roots(SimpleNamespace(list_roots=list_roots))
    assert not server._roots_pending
    server._get_client()
    assert adopted == [1]
