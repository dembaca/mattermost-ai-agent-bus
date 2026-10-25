"""Keyless host-managed session: the MCP server on the host, a relay in the
sandbox (RT-6.18 in docs/arc42/06-runtime-view.md).

In a corral session the bot token never enters the sandbox. The corral preStart
hook starts this package on the host (``--host``), isolated (``python -I``, from
the host install tree, a scrubbed environment), and hands it the per-session key
and the session's settings, token included, on stdin: nothing secret in its argv
or initial environment. The process keeps its files in a state directory the
host chose for the session, listens on 127.0.0.1 and serves

- the MCP stream to one relay connection at a time: the plugin's MCP entry point
  in the sandbox finds MM_BUS_MCP_ENDPOINT and MM_BUS_KEY and only relays its
  stdin and stdout, byte for byte, so channel notifications (RT-6.12) and the
  permission relay (RT-6.13) pass unchanged;
- the hook API (hookapi.py, RT-6.17) with the same key on a port of its own
  (MM_BUS_ENDPOINT), for the turn hooks and the listener, from the start of
  the session to its end, as do the lease heartbeat (RT-6.6).

A relay opens with one handshake line, ``{"key": …, "session": {…}}``. The
session values are names only (NAMING): the Claude Code session id, project
directory and working directory, the channel flag, CLAUDECODE. They are
checked for type and length and never opened, resolved or entered on the host;
HOME, XDG_STATE_HOME and anything else the relay sends are ignored. The host
answers ``{"ok": true}`` or ``{"ok": false, "error": …}``. A closed relay
connection does not end the session: an MCP reconnect may follow. SIGTERM
(corral's postEnd) or the watchdog finding corral gone does; then the server
does its exit work (BYE, stats, offline, unregister) and the state directory
goes.

Without the relay environment the entry point is the plain stdio server.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketAttribute
from anyio.streams.buffered import BufferedByteReceiveStream

from . import session_file

log = logging.getLogger(__name__)

ENDPOINT_ENV = "MM_BUS_MCP_ENDPOINT"
KEY_ENV = "MM_BUS_KEY"
LOOPBACK = "127.0.0.1"
MIN_KEY = 32
HANDSHAKE_MAX = 64 * 1024
MESSAGE_MAX = 64 * 1024 * 1024
HANDSHAKE_SEC = 5.0
# Connections still in their handshake at once; one more is closed unanswered.
MAX_PENDING = 8
WATCH_SEC = 5.0
READY_SEC = 20.0

# What a relay may tell the host: names only, with their longest length.
NAMING = {"CLAUDE_CODE_SESSION_ID": 256, "CLAUDE_PROJECT_DIR": 4096, "cwd": 4096,
          "MM_CHANNEL_PUSH": 1, "CLAUDECODE": 16}
_NAMING_PATHS = ("CLAUDE_PROJECT_DIR", "cwd")

# What preStart may hand the host process on stdin besides the key: the bus
# settings (MM_*, MATTERMOST_*), the corral session's names and the network
# settings that may carry credentials (proxies). Never the registration secret.
_HOST_ENV = re.compile(r"(MM|MATTERMOST)_[A-Z0-9_]{1,64}")
_HOST_ENV_EXTRA = frozenset({
    "CORRAL_WORKDIR", "CORRAL_SESSION_ID", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "no_proxy", "all_proxy",
})
_HOST_ENV_DENY = frozenset({
    "MM_REG_SECRET", "MM_REG_SECRET_GOPASS", "MM_AGENT_SESSION_FILE", "MM_REG_BOT_NAME",
    KEY_ENV, ENDPOINT_ENV, "MM_BUS_ENDPOINT", "MM_BUS_UNAVAILABLE",
})
_TOKEN_ENV = ("MM_BOT_TOKEN", "MATTERMOST_TOKEN")
MAX_VALUE = 8192

Serve = Callable[[Any, Any], Awaitable[None]]


def main(argv: list[str] | None = None) -> None:
    """The plugin's MCP entry point: host process, relay, or plain stdio server."""
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["--host"]:
        sys.exit(host_main(args[1:]))
    if os.environ.get(ENDPOINT_ENV, "").strip():
        sys.exit(relay(os.environ))
    from .server import main as serve_stdio

    serve_stdio()


def _fail(message: str) -> int:
    print(f"mattermost-agent-bus: {message}", file=sys.stderr, flush=True)
    return 1


# --- relay (inside the sandbox) ----------------------------------------------

def parse_endpoint(value: str) -> tuple[str, int]:
    """``tcp://127.0.0.1:<port>``; loopback only, the key must not leave the machine."""
    rest = value.strip()
    if not rest.startswith("tcp://"):
        raise ValueError(value)
    host, _, port = rest[len("tcp://"):].rpartition(":")
    if host != LOOPBACK or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(value)
    return host, int(port)


def session_values(environ: Mapping[str, str], cwd: str | None = None) -> dict[str, str]:
    """What the relay tells the host about this session: names only (NAMING)."""
    from . import channel

    out = {k: environ[k] for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDECODE")
           if environ.get(k)}
    # Claude Code is among the relay's ancestors, not the host's (RT-6.12).
    out["MM_CHANNEL_PUSH"] = "1" if channel.enabled() else "0"
    out["cwd"] = cwd or os.getcwd()
    return out


def _read_line(sock: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\n" not in buf:
        if len(buf) > HANDSHAKE_MAX:
            raise ValueError("answer too long")
        data = sock.recv(4096)
        if not data:
            raise ValueError("connection closed")
        buf += data
    line, _, rest = buf.partition(b"\n")
    return line, rest


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def relay(environ: Mapping[str, str], *, stdin_fd: int = 0, stdout_fd: int = 1,
          timeout: float = HANDSHAKE_SEC) -> int:
    """Pipe stdio to the host process. Never starts a server here: the token
    stays on the host, and a token-less server would only pretend to be on
    the bus."""
    endpoint = environ.get(ENDPOINT_ENV, "").strip()
    key = environ.get(KEY_ENV, "")
    if not key:
        return _fail(f"{ENDPOINT_ENV} is set but {KEY_ENV} is not; restart the corral session")
    try:
        host, port = parse_endpoint(endpoint)
    except ValueError:
        return _fail(f"{ENDPOINT_ENV}={endpoint!r} is not tcp://127.0.0.1:<port>")
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return _fail(f"the bus's MCP server on the host ({endpoint}) is unreachable: {exc}. "
                     "The bot token stays on the host, so no server starts here; "
                     "restart the corral session (RT-6.18)")
    with sock:
        hello = {"key": key, "session": session_values(environ)}
        try:
            sock.sendall(json.dumps(hello).encode() + b"\n")
            line, rest = _read_line(sock)
            answer = json.loads(line)
        except (OSError, ValueError) as exc:
            return _fail(f"no answer from the bus's MCP server on the host ({endpoint}): {exc}")
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            why = answer.get("error") if isinstance(answer, dict) else None
            return _fail(f"the bus's MCP server on the host refused the relay: {why or 'no reason'}")
        sock.settimeout(None)
        return _pipe(sock, stdin_fd, stdout_fd, rest)


def _pipe(sock: socket.socket, stdin_fd: int, stdout_fd: int, pending: bytes) -> int:
    stdin_closed = threading.Event()

    def upstream() -> None:
        try:
            while data := os.read(stdin_fd, 65536):
                sock.sendall(data)
        except OSError:
            pass
        finally:
            stdin_closed.set()
            with suppress(OSError):
                sock.shutdown(socket.SHUT_WR)

    threading.Thread(target=upstream, name="relay-upstream", daemon=True).start()
    try:
        if pending:
            _write_all(stdout_fd, pending)
        while data := sock.recv(65536):
            _write_all(stdout_fd, data)
    except OSError as exc:
        return 0 if stdin_closed.is_set() else _fail(f"relay connection lost: {exc}")
    return 0 if stdin_closed.is_set() else _fail("the host closed the relay connection")


# --- host process: the handshake --------------------------------------------------

def check_session(values: Any) -> dict[str, str]:
    """The handshake's session values, checked: names only (RT-6.18).

    Keys outside NAMING (HOME, XDG_STATE_HOME, anything) are dropped. A known
    key must be a string within its length, on one line; the directories
    absolute; the channel flag 0 or 1. Raises ValueError otherwise.
    """
    if not isinstance(values, dict):
        raise ValueError("session: a JSON object")
    out: dict[str, str] = {}
    for name, limit in NAMING.items():
        v = values.get(name)
        if v is None or v == "":
            continue
        if not isinstance(v, str) or len(v) > limit or any(c in v for c in "\0\r\n"):
            raise ValueError(f"session.{name}: one line of at most {limit} characters")
        if name in _NAMING_PATHS and not os.path.isabs(v):
            raise ValueError(f"session.{name}: an absolute path")
        if name == "MM_CHANNEL_PUSH" and v not in ("0", "1"):
            raise ValueError("session.MM_CHANNEL_PUSH: 0 or 1")
        out[name] = v
    return out


def apply_session(values: Mapping[str, str], *,
                  environ: MutableMapping[str, str] | None = None) -> None:
    """The checked session values as this process's names (RT-6.18): the
    session id and project directory key the server's file names, the flag
    says whether it serves as a channel. Never opened, resolved or entered:
    the host's working directory stays its own."""
    env = os.environ if environ is None else environ
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDECODE"):
        env.pop(name, None)
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "MM_CHANNEL_PUSH"):
        if values.get(name):
            env[name] = values[name]
    project = values.get("CLAUDE_PROJECT_DIR") or values.get("cwd")
    if project:
        env["CLAUDE_PROJECT_DIR"] = os.path.normpath(project)


@asynccontextmanager
async def byte_transport(receive: BufferedByteReceiveStream, send: Any) -> AsyncIterator[tuple[Any, Any]]:
    """MCP message streams over a byte stream: newline-delimited JSON-RPC, as stdio."""
    import mcp_types as types
    from mcp.shared._context_streams import create_context_streams
    from mcp.shared.message import SessionMessage

    read_writer, read_stream = create_context_streams[SessionMessage | Exception](0)
    write_stream, write_reader = create_context_streams[SessionMessage](0)

    async def reader() -> None:
        async with read_writer:
            while True:
                try:
                    line = await receive.receive_until(b"\n", MESSAGE_MAX)
                except (anyio.IncompleteRead, anyio.DelimiterNotFound, anyio.BrokenResourceError,
                        anyio.ClosedResourceError, OSError):
                    return
                if not line.strip():
                    continue
                try:
                    message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                except Exception as exc:  # noqa: BLE001 — handed to the session, as stdio does
                    await read_writer.send(exc)
                    continue
                await read_writer.send(SessionMessage(message))

    async def writer() -> None:
        async with write_reader:
            async for item in write_reader:
                data = item.message.model_dump_json(by_alias=True, exclude_unset=True)
                try:
                    await send.send(data.encode() + b"\n")
                except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                    return

    async with anyio.create_task_group() as tg:
        tg.start_soon(reader)
        tg.start_soon(writer)
        try:
            yield read_stream, write_stream
        finally:
            tg.cancel_scope.cancel()


def _load_server(client: Any) -> Serve:
    from . import server

    if client is not None:
        server._client = client  # one client for hook API, heartbeat and tools
    server._hide_lifecycle_tools()
    return server.serve


async def _answer(stream: Any, ok: bool, error: str = "") -> None:
    body: dict[str, Any] = {"ok": True} if ok else {"ok": False, "error": error}
    with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        await stream.send(json.dumps(body).encode() + b"\n")


class Host:
    """Takes relay connections: each after a handshake with the session key,
    one at a time. The server is loaded on the first one, after its session
    values are applied; later ones keep those."""

    def __init__(self, key: str, *, client: Any = None, serve: Serve | None = None,
                 apply: Callable[[Mapping[str, str]], None] = apply_session) -> None:
        self._key = key.encode()
        self._serve = serve
        self._apply = apply
        self.client = client
        self.session: dict[str, str] | None = None
        self.active = False
        self.served = 0
        self._pending = 0

    def _load(self, session: dict[str, str]) -> Serve:
        if self.session is None:
            self._apply(session)
            self.session = session
        elif session != self.session:
            log.info("relay: its session values differ from the first relay's; the first stay")
        if self._serve is None:
            self._serve = _load_server(self.client)
        return self._serve

    async def handle(self, stream: Any) -> None:
        if self._pending >= MAX_PENDING:
            await stream.aclose()
            return
        self._pending += 1
        async with stream:
            try:
                buffered = BufferedByteReceiveStream(stream)
                try:
                    with anyio.fail_after(HANDSHAKE_SEC):
                        hello = json.loads(await buffered.receive_until(b"\n", HANDSHAKE_MAX))
                except (TimeoutError, anyio.IncompleteRead, anyio.DelimiterNotFound,
                        anyio.BrokenResourceError, OSError, ValueError):
                    log.warning("relay refused: no handshake")
                    return
            finally:
                self._pending -= 1
            key = hello.get("key") if isinstance(hello, dict) else None
            if not isinstance(key, str) or not hmac.compare_digest(key.encode(), self._key):
                log.warning("relay refused: wrong key")
                await _answer(stream, False, "wrong key")
                return
            try:
                session = check_session(hello.get("session", {}))
            except ValueError as exc:
                log.warning("relay refused: %s", exc)
                await _answer(stream, False, str(exc))
                return
            if self.active:
                log.warning("relay refused: another relay is connected")
                await _answer(stream, False, "another relay is connected")
                return
            self.active = True
            try:
                serve = self._load(session)
                await _answer(stream, True)
                self.served += 1
                log.info("relay %d connected", self.served)
                async with byte_transport(buffered, stream) as (read_stream, write_stream):
                    await serve(read_stream, write_stream)
            finally:
                self.active = False
                log.info("relay disconnected; the session goes on until postEnd or the watchdog")


# --- host process: lifetime -------------------------------------------------------

def proc_start(pid: int) -> str | None:
    """A process's start time, comparable only with another call; None when
    there is no such process or no way to tell (no subprocess: /proc, sysctl)."""
    if sys.platform.startswith("linux"):
        try:
            return "proc:" + Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
        except (OSError, IndexError):
            return None
    if sys.platform == "darwin":
        import ctypes
        import struct

        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value == 0:
            return None
        sec, usec = struct.unpack_from("qi", buf.raw, 0)  # kinfo_proc.kp_proc.p_starttime
        return f"sysctl:{sec}.{usec:06d}"
    return None


def watcher(pid: int, start: str | None) -> Callable[[], bool]:
    """Is the watched process (corral) still the one we started under?"""
    def alive() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        if start is None:
            return True
        now = proc_start(pid)
        return now is None or now == start  # same pid, other start: reused

    return alive


def write_ready(path: Path, pid: int, port: int, hook: str) -> None:
    session_file.write_private(path, json.dumps({"pid": pid, "port": port, "hook": hook}))


@asynccontextmanager
async def services(key: str, client: Any) -> AsyncIterator[str]:
    """Hook API (same key, own port) and lease heartbeat for the whole session
    (RT-6.17, RT-6.6): the hooks call before any relay connects and after one
    closed. Yields the hook API's endpoint."""
    from . import hookapi, lease

    async def get_client() -> Any:
        return client

    api = hookapi.HookAPI(get_client, key=key)
    await api.start(write_file=False)  # the sandbox has it in its environment
    task = asyncio.create_task(lease.heartbeat(get_client))
    try:
        yield api.endpoint
    finally:
        task.cancel()
        await api.close()


async def run_host(host: Host, *, ready_file: Path, key: str = "", with_services: bool = True,
                   watch: Callable[[], bool] | None = None, interval: float = WATCH_SEC,
                   signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)) -> str:
    """Serve relay connections and the hook API until a signal or the watchdog
    ends the session. Returns why it ended."""
    stop = anyio.Event()
    why = ["stopped"]

    def end(reason: str) -> None:
        if not stop.is_set():
            why[0] = reason
            stop.set()

    async def on_signals() -> None:
        with anyio.open_signal_receiver(*signals) as received:
            async for num in received:
                end(f"signal {signal.Signals(num).name}")
                return

    async def watchdog() -> None:
        assert watch is not None
        while True:
            await anyio.sleep(interval)
            if not await anyio.to_thread.run_sync(watch):
                end("watchdog: the corral process is gone")
                return

    listener = await anyio.create_tcp_listener(local_host=LOOPBACK, local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    async with AsyncExitStack() as stack:
        hook = await stack.enter_async_context(services(key, host.client)) if with_services else ""
        async with listener, anyio.create_task_group() as tg:
            tg.start_soon(listener.serve, host.handle)
            if signals:
                tg.start_soon(on_signals)
            if watch is not None:
                tg.start_soon(watchdog)
            write_ready(ready_file, os.getpid(), port, hook)
            log.info("listening on %s:%d (MCP), %s (hook API)", LOOPBACK, port, hook or "-")
            await stop.wait()
            tg.cancel_scope.cancel()
    return why[0]


def exit_work(client: Any) -> None:
    """BYE, stats, offline, unregister: when the session ends, never when a
    relay disconnects; also when no relay ever connected."""
    if client is None:
        return
    from . import server

    if server._client is None:
        server._client = client
    server._cleanup_on_exit()


def serve_host(host: Host, *, ready_file: Path, key: str = "", with_services: bool = True,
               watch: Callable[[], bool] | None = None, interval: float = WATCH_SEC,
               signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)) -> str:
    """run_host, then the exit work; the readiness file and the session's
    state directory go with the process."""
    why = "failed"
    try:
        why = anyio.run(lambda: run_host(host, ready_file=ready_file, key=key,
                                         with_services=with_services, watch=watch,
                                         interval=interval, signals=signals))
    finally:
        log.info("session over (%s): exit work", why)
        try:
            exit_work(host.client)
        finally:
            ready_file.unlink(missing_ok=True)
            if session_file.hosted():
                shutil.rmtree(session_file.state_dir(), ignore_errors=True)
    return why


# --- host process: start ------------------------------------------------------------

def read_start(stream: Any) -> tuple[str, dict[str, str]]:
    """The first line on stdin: ``{"key": …, "env": {…}}`` from preStart.

    The key must be MIN_KEY characters at least. Of ``env`` only bus settings
    (MM_*, MATTERMOST_*), the corral names and network settings are taken,
    never the registration secret; values are strings on one line.
    """
    line = stream.readline(HANDSHAKE_MAX + 1)
    if not line.endswith(b"\n") or len(line) > HANDSHAKE_MAX:
        raise ValueError("no start line on stdin")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("start line: a JSON object")
    key = data.get("key")
    if not isinstance(key, str) or len(key) < MIN_KEY:
        raise ValueError(f"start line: a key of at least {MIN_KEY} characters")
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ValueError("start line: env must be an object")
    out: dict[str, str] = {}
    for name, value in env.items():
        if not (_HOST_ENV.fullmatch(name) or name in _HOST_ENV_EXTRA) or name in _HOST_ENV_DENY:
            continue
        if isinstance(value, str) and len(value) <= MAX_VALUE and not any(c in value for c in "\0\r\n"):
            out[name] = value
    return key, out


def _detach(log_file: Path | None) -> None:
    """Own session, stdin from /dev/null, stdout and stderr to the log: corral
    waits until the hook's own stdout and stderr close."""
    os.setsid()
    null = os.open(os.devnull, os.O_RDONLY)
    if log_file is not None:
        out = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    else:
        out = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.closerange(3, min(os.sysconf("SC_OPEN_MAX"), 65536))


def _daemon(args: argparse.Namespace, key: str) -> int:
    from .client import MattermostClient

    logging.basicConfig(level=logging.INFO, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    watch = None
    if args.watch_pid:
        watch = watcher(args.watch_pid, proc_start(args.watch_pid))
        if not watch():
            log.warning("corral (pid %d) is gone already", args.watch_pid)
            return 1
    try:
        client = MattermostClient.from_env()
    except RuntimeError as exc:
        log.warning("no bus: %s", exc)
        return 1
    # Read once into the client; nothing else in this process needs it.
    for name in _TOKEN_ENV:
        os.environ.pop(name, None)
    serve_host(Host(key, client=client), ready_file=args.ready_file, key=key, watch=watch)
    return 0


def _await_ready(ready_file: Path, pid: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with suppress(OSError, ValueError, AttributeError):
            if json.loads(ready_file.read_text()).get("pid") == pid:
                return 0
        with suppress(ChildProcessError):
            if os.waitpid(pid, os.WNOHANG)[0]:
                return _fail("the host process ended before it was ready")
        time.sleep(0.05)
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    return _fail(f"the host process was not ready within {timeout:g} s")


def host_main(argv: list[str]) -> int:
    """``--host``: start the session's MCP server as a detached host process.

    Returns once it listens (0) or failed to (1); the process itself lives on
    until SIGTERM or until the watched corral process is gone. Key and settings
    come as one JSON line on stdin (read_start), never from the command line,
    which other users can read, nor from the environment it started with.
    """
    p = argparse.ArgumentParser(prog="mattermost-ai-agent-bus-mcp --host")
    p.add_argument("--ready-file", required=True, type=Path,
                   help="written as {pid, port, hook} once listening (0600)")
    p.add_argument("--state-dir", required=True, type=Path,
                   help="the session's own state directory on the host (created 0700)")
    p.add_argument("--log-file", type=Path, help="stdout, stderr and the log of the host process")
    p.add_argument("--watch-pid", type=int, help="corral's pid: its end ends the session")
    p.add_argument("--ready-timeout", type=float, default=READY_SEC)
    p.add_argument("--foreground", action="store_true", help="do not detach (tests)")
    args = p.parse_args(argv)
    try:
        key, env = read_start(sys.stdin.buffer)
    except ValueError as exc:
        return _fail(str(exc))
    for path in (args.ready_file, args.state_dir, args.log_file):
        if path is not None and not path.is_absolute():
            return _fail(f"not an absolute path: {path}")
    os.environ.update(env)
    os.environ.pop(KEY_ENV, None)
    try:
        session_file.use_hosted_dir(args.state_dir)
    except (OSError, ValueError) as exc:
        return _fail(f"no state directory for the session: {exc}")
    os.chdir("/")  # nothing of the session's directories, ever
    args.ready_file.unlink(missing_ok=True)
    if args.foreground:
        return _daemon(args, key)
    pid = os.fork()
    if pid == 0:  # pragma: no cover — the detached host process
        code = 1
        try:
            _detach(args.log_file)
            code = _daemon(args, key)
        except BaseException:  # noqa: BLE001 — logged, then the process ends
            log.exception("host process failed")
        finally:
            logging.shutdown()
            os._exit(code)
    return _await_ready(args.ready_file, pid, args.ready_timeout)
