"""Daemon lifecycle: runtime metadata, detached start, resume checks, orphans.

One daemon per working directory. Its socket path is a hash of the cwd, and
`daemon.json` under the store records the pid, run id and live agent groups,
so `--resume`, `-l` and the next start after a crash all find what is there.
"""

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
from contextlib import suppress
from dataclasses import asdict, dataclass
from io import TextIOWrapper
from pathlib import Path

from .config import RunConfig
from .runner import TERMINATE_GRACE_SEC
from .store import DEFAULT_BASE_DIR, Store

PROTOCOL_VERSION = 1
STARTUP_TIMEOUT_SEC = 5
RUNTIME_FILE_NAME = "daemon.json"
DAEMON_MODULE = "auto_review_pr_dashboard.daemon"
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
    # The agent groups a running daemon owns, as (pgid, start ticks), so the next
    # start can kill what a crash left behind. Absent from a daemon.json written
    # before this existed.
    agent_groups: tuple[tuple[int, int | None], ...] = ()

    @classmethod
    def from_dict(cls, payload: dict) -> RuntimeMetadata:
        try:
            agent_groups = payload.get("agent_groups")
            if agent_groups is None and payload.get("agent_pgid") is not None:
                # Written by a daemon that ran one agent at a time.
                agent_groups = [[payload["agent_pgid"], payload.get("agent_start_ticks")]]
            return cls(
                protocol_version=int(payload["protocol_version"]),
                run_id=str(payload["run_id"]),
                pid=int(payload["pid"]),
                cwd=str(payload["cwd"]),
                socket_path=str(payload["socket_path"]),
                started_at=float(payload["started_at"]),
                config=dict(payload["config"]),
                agent_groups=tuple(
                    (int(pgid), None if start_ticks is None else int(start_ticks))
                    for pgid, start_ticks in agent_groups or ()
                ),
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


def start_daemon(
    config: RunConfig, store: Store, handoff: dict | None = None
) -> RuntimeMetadata:
    """Start one detached daemon for the caller's working directory."""
    socket_path = get_socket_path(Path.cwd().resolve())
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(socket_path.parent, 0o700)

    with _take_start_lock(socket_path) as lock_file:
        _remove_stale_runtime_files(socket_path.parent)
        return _start_daemon_locked(config, store, handoff)


def _start_daemon_locked(
    config: RunConfig, store: Store, handoff: dict | None
) -> RuntimeMetadata:
    """Start the daemon; the caller already holds this working directory's lock.

    A recycling daemon holds that lock and calls this directly: flock is per file
    descriptor, so taking it again in the same process would deadlock.
    """
    cwd = Path.cwd().resolve()
    base_dir = store.base_dir.resolve()
    socket_path = get_socket_path(cwd)
    store.prepare()

    if _get_is_socket_live(socket_path):
        raise SessionError(
            "a daemon is already running in this working directory; "
            "use auto-review-pr-dashboard --resume"
        )

    # Nothing cleans up after a SIGKILL, an OOM or a crash: the agent outlives the
    # daemon in its own session, and its pid is only in the leftover daemon.json.
    leftover = load_runtime_metadata(store)
    if leftover is not None:
        _kill_orphan_agents(leftover)

    socket_path.unlink(missing_ok=True)
    get_runtime_path(store).unlink(missing_ok=True)
    payload = json.dumps(
        {
            "config": asdict(config),
            "base_dir": str(base_dir),
            "cwd": str(cwd),
            "socket_path": str(socket_path),
            "run_id": uuid.uuid4().hex,
            "handoff": handoff,
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
            raise SessionError(f"cannot start background daemon: {error}") from error

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


def get_process_start_ticks(pid: int) -> int | None:
    """The pid's starttime in clock ticks, which never survives a pid being reused."""
    try:
        stat = (PROC_DIR / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm is parenthesised and may hold spaces and ')', so the fixed fields are
    # counted back from the last ')' rather than forward from the first.
    fields = stat.rsplit(")", 1)[1].split()
    try:
        return int(fields[19])
    except IndexError:
        return None


def _kill_orphan_agents(metadata: RuntimeMetadata) -> None:
    """Kill the agent groups a dead daemon left running, if still the same ones."""
    pgids = []
    for pgid, agent_start_ticks in metadata.agent_groups:
        # A pid can be handed to a new process: the recorded starttime says whether
        # this group is still the one we knew. A group whose leader is gone keeps its
        # pgid while any member lives, so a missing starttime is no reason to skip.
        if agent_start_ticks is not None:
            start_ticks = get_process_start_ticks(pgid)
            if start_ticks is not None and start_ticks != agent_start_ticks:
                continue
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        pgids.append(pgid)

    # One grace period shared by every group, not one each.
    deadline = time.monotonic() + TERMINATE_GRACE_SEC
    while True:
        alive_pgids = []
        for pgid in pgids:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, 0)
                alive_pgids.append(pgid)
        pgids = alive_pgids
        if not pgids or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    for pgid in pgids:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)


def _take_start_lock(socket_path: Path) -> TextIOWrapper:
    """Take this working directory's startup lock and hand back the open file.

    Runs off the event loop: the lock can be held by a launch that is still
    starting a daemon. The caller keeps the file referenced until it exits.

    flock locks an inode, not a name, and the stale sweep unlinks locks out
    from under waiters: after waking, only a path still naming this fd's file
    means the lock is real, so verify that and start over on the replacement.
    """
    lock_path = socket_path.with_suffix(".lock")
    while True:
        lock_file = lock_path.open("a", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                path_stat = os.stat(lock_path)
            except FileNotFoundError:
                path_stat = None
            fd_stat = os.stat(lock_file.fileno())
        except OSError:
            # Close before re-raising: only a verified mismatch may loop on.
            lock_file.close()
            raise
        if (
            path_stat is not None
            and (path_stat.st_dev, path_stat.st_ino)
            == (fd_stat.st_dev, fd_stat.st_ino)
        ):
            return lock_file
        lock_file.close()


def _remove_stale_runtime_files(runtime_dir: Path) -> None:
    """Unlink `.lock`/`.sock` pairs no launch holds and no daemon serves.

    A launch or recycle holds its own lock, and LOCK_NB skips it: a second fd on
    the same inode conflicts even within one process. A stopping daemon holds
    none, so its own pair is swept as well. Every failure is swallowed per
    pair, so the sweep never blocks or fails a launch or a recycle.
    """
    for lock_path in runtime_dir.glob("*.lock"):
        try:
            # "r", not "a": the sweep must not recreate a lock it just raced.
            with lock_path.open("r", encoding="utf-8") as lock_file:
                # BlockingIOError here means a live launch or recycle owns the
                # pair.
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path_stat = os.stat(lock_path)
                fd_stat = os.stat(lock_file.fileno())
                if (path_stat.st_dev, path_stat.st_ino) != (
                    fd_stat.st_dev,
                    fd_stat.st_ino,
                ):
                    continue
                socket_path = lock_path.with_suffix(".sock")
                if _get_is_socket_live(socket_path):
                    continue
                socket_path.unlink(missing_ok=True)
                lock_path.unlink(missing_ok=True)
        except OSError:
            # FileNotFoundError (raced deletion), BlockingIOError (held lock)
            # or anything else: skip this pair and keep sweeping.
            continue


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


def encode_message(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def _run_daemon(payload_text: str) -> int:
    # Imported here: server imports this module for the shared constants.
    from .server import SessionServer

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
        handoff=payload.get("handoff"),
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
