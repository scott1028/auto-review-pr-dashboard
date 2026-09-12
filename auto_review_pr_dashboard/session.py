"""Detached runner process and the local dashboard control connection."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import RunConfig
from .runner import LoopRunner
from .store import DEFAULT_BASE_DIR, Store

PROTOCOL_VERSION = 1
STARTUP_TIMEOUT_SEC = 5
RUNTIME_FILE_NAME = "daemon.json"
DAEMON_MODULE = "auto_review_pr_dashboard.session"
PROC_DIR = Path("/proc")
DAEMON_LOG_FILE_NAME = "daemon.log"
MAX_CLIENT_BUFFER_BYTES = 1024 * 1024


class SessionError(RuntimeError):
    """The daemon cannot be started or attached safely."""


@dataclass(frozen=True)
class RuntimeMetadata:
    protocol_version: int
    run_id: str
    pid: int
    cwd: str
    socket_path: str
    started_at: float
    config: dict

    @classmethod
    def from_dict(cls, payload: dict) -> RuntimeMetadata:
        try:
            return cls(
                protocol_version=int(payload["protocol_version"]),
                run_id=str(payload["run_id"]),
                pid=int(payload["pid"]),
                cwd=str(payload["cwd"]),
                socket_path=str(payload["socket_path"]),
                started_at=float(payload["started_at"]),
                config=dict(payload["config"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SessionError("invalid runtime metadata") from error

    def to_dict(self) -> dict:
        return asdict(self)


def get_socket_path(cwd: Path | str) -> Path:
    resolved_cwd = str(Path(cwd).resolve())
    cwd_hash = hashlib.sha256(resolved_cwd.encode("utf-8")).hexdigest()[:20]
    runtime_dir = (
        Path(tempfile.gettempdir()) / f"auto-review-pr-dashboard-{os.getuid()}"
    )
    return runtime_dir / f"{cwd_hash}.sock"


def get_runtime_path(store: Store) -> Path:
    return store.base_dir / RUNTIME_FILE_NAME


def load_runtime_metadata(store: Store) -> RuntimeMetadata | None:
    try:
        payload = json.loads(get_runtime_path(store).read_text(encoding="utf-8"))
        return RuntimeMetadata.from_dict(payload)
    except (
        KeyError,
        OSError,
        SessionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def get_resume_error(metadata: RuntimeMetadata, cwd: str) -> str:
    """Why a --resume from `cwd` would be refused, or "" when it would attach.

    Shared with the listing so `-l` can never advertise a daemon that --resume
    then rejects.
    """
    if metadata.protocol_version != PROTOCOL_VERSION:
        return (
            f"daemon protocol {metadata.protocol_version} is incompatible with "
            f"this client ({PROTOCOL_VERSION}); stop it before starting a new run"
        )
    if metadata.cwd != cwd or metadata.socket_path != str(get_socket_path(cwd)):
        return "daemon metadata does not belong to this working directory"
    if not _get_is_socket_live(Path(metadata.socket_path)):
        return (
            "background daemon is no longer running; --resume will not restart "
            "a possibly partial review"
        )
    try:
        RunConfig(**metadata.config)
    except TypeError as error:
        return f"daemon.json config is not usable by this version: {error}"
    return ""


def get_resume_metadata(store: Store) -> RuntimeMetadata:
    metadata = load_runtime_metadata(store)
    if metadata is None:
        raise SessionError(
            "no background daemon found in this working directory; "
            "--resume does not start a new run"
        )

    error = get_resume_error(metadata, str(Path.cwd().resolve()))
    if error:
        raise SessionError(error)
    return metadata


def get_live_workspaces() -> list[RuntimeMetadata]:
    """Working directories whose background daemon is still attachable.

    There is no cross-directory index - the socket file name is a one-way hash
    of the cwd. The daemons are the registry instead: each one chdir's into its
    working directory, so /proc/<pid>/cwd maps back, and that directory's
    daemon.json has to pass the same checks --resume would run there.
    """
    workspaces: list[RuntimeMetadata] = []
    for entry in PROC_DIR.glob("[0-9]*"):
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8").rstrip("\0")
            if argv.split("\0")[-2:] != ["-m", DAEMON_MODULE]:
                continue
            cwd = (entry / "cwd").resolve()
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        metadata = load_runtime_metadata(Store(cwd / DEFAULT_BASE_DIR))
        if (
            metadata is None
            or metadata.pid != int(entry.name)
            or get_resume_error(metadata, str(cwd))
        ):
            continue
        workspaces.append(metadata)
    return sorted(workspaces, key=lambda metadata: metadata.started_at)


def start_daemon(config: RunConfig, store: Store) -> RuntimeMetadata:
    """Start one detached daemon for the caller's working directory."""
    cwd = Path.cwd().resolve()
    base_dir = store.base_dir.resolve()
    socket_path = get_socket_path(cwd)
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(socket_path.parent, 0o700)
    store.prepare()

    lock_path = socket_path.with_suffix(".lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if _get_is_socket_live(socket_path):
            raise SessionError(
                "a daemon is already running in this working directory; "
                "use auto-review-pr-dashboard --resume"
            )

        socket_path.unlink(missing_ok=True)
        get_runtime_path(store).unlink(missing_ok=True)
        payload = json.dumps(
            {
                "config": asdict(config),
                "base_dir": str(base_dir),
                "cwd": str(cwd),
                "socket_path": str(socket_path),
                "run_id": uuid.uuid4().hex,
            },
            ensure_ascii=False,
        )
        log_path = store.base_dir / DAEMON_LOG_FILE_NAME
        child_env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[1])
        existing_python_path = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = (
            f"{package_root}{os.pathsep}{existing_python_path}"
            if existing_python_path
            else package_root
        )
        with log_path.open("ab") as daemon_log:
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", DAEMON_MODULE],
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=daemon_log,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                    start_new_session=True,
                    close_fds=True,
                )
                # The payload goes on stdin: argv is readable by other processes.
                # suppress has to cover write and close alike - close() flushes
                # whatever is still buffered, and that write can hit a dead pipe.
                with suppress(BrokenPipeError), process.stdin:
                    process.stdin.write(payload.encode("utf-8"))
                threading.Thread(
                    target=process.wait,
                    name="auto-review-pr-dashboard-daemon-wait",
                    daemon=True,
                ).start()
            except OSError as error:
                raise SessionError(
                    f"cannot start background daemon: {error}"
                ) from error

        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = _get_log_tail(log_path)
                suffix = f": {detail}" if detail else ""
                raise SessionError(
                    f"background daemon exited with {process.returncode}{suffix}"
                )
            metadata = load_runtime_metadata(store)
            if metadata is not None and _get_is_socket_live(socket_path):
                return metadata
            time.sleep(0.05)

        process.terminate()
        raise SessionError(
            f"background daemon did not become ready within {STARTUP_TIMEOUT_SEC}s; "
            f"see {log_path}"
        )


def _get_is_socket_live(socket_path: Path) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.2)
    try:
        client.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        client.close()


def _get_log_tail(path: Path) -> str:
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, 2)
            log_file.seek(max(0, log_file.tell() - 4096))
            return log_file.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


class SessionClient:
    def __init__(self, metadata: RuntimeMetadata):
        self.metadata = metadata
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._closing = False
        self._ready = asyncio.Event()
        self._connect_error: Exception | None = None

    async def connect(self, on_message: Callable[[dict], None]) -> None:
        try:
            self.reader, self.writer = await asyncio.open_unix_connection(
                self.metadata.socket_path, limit=MAX_CLIENT_BUFFER_BYTES
            )
            await self._write(
                {
                    "type": "attach",
                    "protocol_version": PROTOCOL_VERSION,
                    "run_id": self.metadata.run_id,
                    "cwd": self.metadata.cwd,
                }
            )
            self._reader_task = asyncio.create_task(self._read_messages(on_message))
        except (ConnectionError, OSError, SessionError) as error:
            self._connect_error = error
            raise
        finally:
            self._ready.set()

    async def send_command(self, action: str, key: str | None = None) -> None:
        await self._ready.wait()
        if self._connect_error is not None:
            raise SessionError(str(self._connect_error)) from self._connect_error
        message = {"type": "command", "action": action}
        if key is not None:
            message["key"] = key
        await self._write(message)

    async def _write(self, message: dict) -> None:
        if self.writer is None or self.writer.is_closing():
            raise SessionError("daemon connection is closed")
        self.writer.write(_encode_message(message))
        await self.writer.drain()

    async def _read_messages(self, on_message: Callable[[dict], None]) -> None:
        disconnected_unexpectedly = True
        try:
            while self.reader is not None:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {
                        "type": "error",
                        "message": "daemon sent invalid JSON",
                        "fatal": True,
                    }
                if message.get("type") == "replaced":
                    disconnected_unexpectedly = False
                on_message(message)
        except (ConnectionError, OSError) as error:
            if not self._closing:
                on_message({"type": "error", "message": str(error), "fatal": True})
        finally:
            if disconnected_unexpectedly and not self._closing:
                on_message(
                    {
                        "type": "disconnected",
                        "message": "connection to background daemon closed",
                    }
                )

    def disconnect(self) -> None:
        self._closing = True
        self._ready.set()
        if self.writer is not None:
            self.writer.close()


class SessionServer:
    def __init__(
        self,
        config: RunConfig,
        store: Store,
        cwd: str,
        socket_path: Path,
        run_id: str,
    ):
        self.config = config
        self.store = store
        self.cwd = cwd
        self.socket_path = socket_path
        self.run_id = run_id
        self.started_at = time.time()
        self.runner = LoopRunner(config, store, on_event=self.emit_runner_event)
        self.status = "starting"
        self.phase_started_at = self.started_at
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._client_writer: asyncio.StreamWriter | None = None

    async def run(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o600)
        self._write_runtime_metadata()
        self._install_signal_handlers()
        try:
            await self.runner.run_forever()
        finally:
            await self._shutdown()

    def _install_signal_handlers(self) -> None:
        event_loop = asyncio.get_running_loop()
        for caught_signal in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                event_loop.add_signal_handler(caught_signal, self.runner.stop)

    def _write_runtime_metadata(self) -> None:
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id=self.run_id,
            pid=os.getpid(),
            cwd=self.cwd,
            socket_path=str(self.socket_path),
            started_at=self.started_at,
            config=asdict(self.config),
        )
        runtime_path = get_runtime_path(self.store)
        temporary_path = runtime_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(metadata.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(runtime_path)

    def emit_runner_event(self, name: str, *payload) -> None:
        if self._event_loop is None:
            return
        self._event_loop.call_soon_threadsafe(self._handle_runner_event, name, payload)

    def _handle_runner_event(self, name: str, payload: tuple) -> None:
        if name == "log":
            self._send_to_client({"type": "log", "key": payload[0], "line": payload[1]})
            return

        if name == "loop_start":
            self.status = f"Loop #{payload[0]} · discovering"
            self.phase_started_at = time.time()
        elif name == "discovery_progress":
            done, total, _item = payload
            self.status = f"Loop #{self.runner.loop_index} · discovering {done}/{total}"
        elif name == "items_ready":
            self.status = f"Loop #{self.runner.loop_index} · running"
            self.phase_started_at = time.time()
        elif name == "loop_end":
            self.status = f"Loop #{self.runner.loop_index} · finished"
        elif name == "error":
            self._send_to_client(
                {"type": "error", "message": str(payload[0]), "fatal": False}
            )

        if name in ("idle_tick", "blocked_tick"):
            # A full snapshot per second would swamp a slow client, and an
            # unpaused countdown needs none: the client counts down by itself.
            # While paused the runner pushes the deadline out, so send that.
            if self.runner.paused:
                self._send_to_client(
                    {
                        "type": "tick",
                        "next_run_at": self.runner.next_run_at,
                        "blocked_until": self.runner.blocked_until,
                    }
                )
            return

        self._send_snapshot()

    def _get_snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "config": asdict(self.config),
            "record": None
            if self.runner.record is None
            else self.runner.record.to_dict(),
            "status": self.status,
            "paused": self.runner.paused,
            "next_run_at": self.runner.next_run_at,
            "blocked_until": self.runner.blocked_until,
            "block_reason": self.runner.block_reason,
            "current_key": self.runner._current_key,
            "phase_started_at": self.phase_started_at,
        }

    def _send_snapshot(self) -> None:
        self._send_to_client(self._get_snapshot())

    def _send_to_client(self, message: dict) -> None:
        writer = self._client_writer
        if writer is None or writer.is_closing():
            return
        if writer.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER_BYTES:
            writer.close()
            if self._client_writer is writer:
                self._client_writer = None
            return
        try:
            writer.write(_encode_message(message))
        except (ConnectionError, OSError):
            writer.close()
            if self._client_writer is writer:
                self._client_writer = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(line)
            error = self._get_attach_error(request)
            if error:
                writer.write(
                    _encode_message({"type": "error", "message": error, "fatal": True})
                )
                await writer.drain()
                return

            previous_writer = self._client_writer
            if previous_writer is not None and previous_writer is not writer:
                if not previous_writer.is_closing():
                    previous_writer.write(
                        _encode_message(
                            {
                                "type": "replaced",
                                "message": "this dashboard was replaced by a newer --resume",
                            }
                        )
                    )
                    with suppress(asyncio.TimeoutError, ConnectionError, OSError):
                        await asyncio.wait_for(previous_writer.drain(), timeout=0.5)
                previous_writer.close()

            self._client_writer = writer
            writer.write(_encode_message(self._get_snapshot()))
            await writer.drain()

            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._send_notice("invalid command JSON", "error")
                    continue
                self._handle_command(message)
        except (asyncio.TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            if self._client_writer is writer:
                self._client_writer = None
            writer.close()

    def _get_attach_error(self, request: dict) -> str:
        if request.get("type") != "attach":
            return "first message must be attach"
        if request.get("protocol_version") != PROTOCOL_VERSION:
            return "client protocol version does not match daemon"
        if request.get("run_id") != self.run_id:
            return "client run id does not match daemon"
        if request.get("cwd") != self.cwd:
            return "client working directory does not match daemon"
        return ""

    def _handle_command(self, message: dict) -> None:
        if message.get("type") != "command":
            self._send_notice("unknown session message", "error")
            return

        action = message.get("action")
        key = message.get("key")
        if action == "cancel":
            if not isinstance(key, str) or not self.runner.cancel(key):
                self._send_notice(f"{key or 'PR'} is not queued or running", "warning")
        elif action == "resume":
            if not isinstance(key, str) or not self.runner.resume(key):
                self._send_notice(f"{key or 'PR'} is not cancelled", "warning")
        elif action == "run_now":
            self.runner.run_now()
            self._send_snapshot()
        elif action == "toggle_pause":
            self.runner.toggle_pause()
        elif action == "stop":
            self._send_notice("stopping background daemon", "information")
            self.runner.stop()
        else:
            self._send_notice(f"unknown command: {action}", "error")

    def _send_notice(self, message: str, severity: str) -> None:
        self._send_to_client(
            {"type": "notice", "message": message, "severity": severity}
        )

    async def _shutdown(self) -> None:
        if self._client_writer is not None:
            self._client_writer.close()
            with suppress(ConnectionError, OSError):
                await self._client_writer.wait_closed()
            self._client_writer = None
        # Unlink while this daemon still owns the socket: closing the listener
        # first lets a successor bind the same path, and this unlink would then
        # delete its socket.
        self.socket_path.unlink(missing_ok=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        metadata = load_runtime_metadata(self.store)
        if metadata is not None and metadata.run_id == self.run_id:
            get_runtime_path(self.store).unlink(missing_ok=True)


def _encode_message(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def _run_daemon(payload_text: str) -> int:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise SessionError("invalid daemon payload") from error
    os.chdir(payload["cwd"])
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    server = SessionServer(
        RunConfig(**payload["config"]),
        Store(payload["base_dir"]),
        payload["cwd"],
        Path(payload["socket_path"]),
        payload["run_id"],
    )
    asyncio.run(server.run())
    return 0


if __name__ == "__main__":
    try:
        # The launcher writes the payload on stdin for the same reason it is kept
        # out of argv, then closes the pipe.
        sys.exit(_run_daemon(sys.stdin.read()))
    except SessionError as error:
        print(f"auto-review-pr-dashboard: {error}", file=sys.stderr)
        sys.exit(1)
