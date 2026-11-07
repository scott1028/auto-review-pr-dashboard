"""Daemon lifecycle: runtime metadata, detached start, resume, orphans, the -l listing."""

import asyncio
import fcntl
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import asdict, replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import daemon
from auto_review_pr_dashboard import store as store_module
from auto_review_pr_dashboard.__main__ import main, print_workspaces
from auto_review_pr_dashboard.client import SessionClient
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.daemon import (
    DAEMON_LOG_FILE_NAME,
    MAX_CLIENT_BUFFER_BYTES,
    PROTOCOL_VERSION,
    RuntimeMetadata,
    SessionError,
    get_live_workspaces,
    get_resume_metadata,
    get_runtime_path,
    get_socket_path,
    load_runtime_metadata,
    start_daemon,
)
from auto_review_pr_dashboard.server import RECYCLE_EVERY_LOOPS, SessionServer
from auto_review_pr_dashboard.store import Store
from tests import runtime_isolation
from tests.runtime_isolation import kill_agent


def setUpModule() -> None:
    runtime_isolation.setUpModule()


def tearDownModule() -> None:
    runtime_isolation.tearDownModule()


# Defined here, not imported: unittest runs a test under the hooks of the module
# its class comes from, so this fails if this module's setUpModule stops isolating.
class RuntimeDirIsolationTestCase(runtime_isolation.RuntimeDirIsolationTestCase):
    pass


class RuntimeMetadataTestCase(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = Store(Path(temporary_directory.name) / "state")
        self.store.prepare()

    def test_runtime_metadata_round_trip_and_socket_path_is_stable_and_short(self):
        config = RunConfig("codex", "review", repos=["o/r"], authors=["me"])
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="run-123",
            pid=321,
            cwd="/tmp/workspace",
            socket_path="/tmp/session.sock",
            started_at=123.5,
            config=asdict(config),
        )

        self.assertEqual(RuntimeMetadata.from_dict(metadata.to_dict()), metadata)

        long_cwd = Path(tempfile.gettempdir()) / ("very-long-directory-" * 20)
        first_path = get_socket_path(long_cwd)
        second_path = get_socket_path(long_cwd)
        self.assertEqual(first_path, second_path)
        self.assertLessEqual(len(os.fsencode(first_path)), 107)
        self.assertEqual(first_path.suffix, ".sock")

    def test_invalid_runtime_metadata_is_ignored(self):
        get_runtime_path(self.store).write_text(
            json.dumps({"protocol_version": "invalid"}), encoding="utf-8"
        )

        self.assertIsNone(load_runtime_metadata(self.store))

    def test_runtime_metadata_written_before_the_agent_fields_still_loads(self):
        # A daemon.json from an earlier version has no agent groups to read.
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "run_id": "run-123",
            "pid": 321,
            "cwd": "/tmp/workspace",
            "socket_path": "/tmp/session.sock",
            "started_at": 123.5,
            "config": {},
        }

        self.assertEqual(RuntimeMetadata.from_dict(payload).agent_groups, ())
        # A daemon that ran one agent at a time recorded a single group.
        one_agent = payload | {"agent_pgid": 4321, "agent_start_ticks": 99}
        self.assertEqual(
            RuntimeMetadata.from_dict(one_agent).agent_groups, ((4321, 99),)
        )

    def test_missing_resume_metadata_does_not_start_daemon(self):
        with (
            patch.object(store_module, "Store", return_value=self.store),
            patch.object(daemon, "start_daemon") as start_daemon,
            redirect_stderr(StringIO()) as stderr,
        ):
            exit_code = main(["--resume"])

        self.assertEqual(exit_code, 1)
        self.assertIn("no background daemon found", stderr.getvalue())
        start_daemon.assert_not_called()

    def test_stale_resume_metadata_does_not_start_daemon(self):
        cwd = str(Path.cwd().resolve())
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="stale-run",
            pid=999_999,
            cwd=cwd,
            socket_path=str(get_socket_path(cwd)),
            started_at=123.5,
            config=asdict(RunConfig("codex", "review", repos=["o/r"], authors=["me"])),
        )
        get_runtime_path(self.store).write_text(
            json.dumps(metadata.to_dict()), encoding="utf-8"
        )

        with patch.object(daemon, "_get_is_socket_live", return_value=False):
            with self.assertRaisesRegex(SessionError, "no longer running"):
                get_resume_metadata(self.store)

            with (
                patch.object(store_module, "Store", return_value=self.store),
                patch.object(daemon, "start_daemon") as start_daemon,
                redirect_stderr(StringIO()) as stderr,
            ):
                exit_code = main(["--resume"])

        self.assertEqual(exit_code, 1)
        self.assertIn("no longer running", stderr.getvalue())
        start_daemon.assert_not_called()

    def test_malformed_runtime_config_is_a_friendly_error_not_a_traceback(self):
        cwd = str(Path.cwd().resolve())
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="run-123",
            pid=os.getpid(),
            cwd=cwd,
            socket_path=str(get_socket_path(cwd)),
            started_at=123.5,
            config={"ai_cli": "codex", "prompt": "review", "legacy_field": 1},
        )
        get_runtime_path(self.store).write_text(
            json.dumps(metadata.to_dict()), encoding="utf-8"
        )

        with (
            patch.object(daemon, "_get_is_socket_live", return_value=True),
            patch.object(store_module, "Store", return_value=self.store),
            patch.object(daemon, "start_daemon") as start_daemon,
            redirect_stderr(StringIO()) as stderr,
        ):
            exit_code = main(["--resume"])

        self.assertEqual(exit_code, 1)
        self.assertIn("daemon.json config is not usable", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_daemon.assert_not_called()

    def test_a_type_error_from_start_daemon_is_not_reported_as_bad_config(self):
        # A normal launch creates prompt.md in the cwd; keep it out of the repo.
        original_cwd = Path.cwd()
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(temporary_directory.name)

        with (
            patch.object(store_module, "Store", return_value=self.store),
            patch("auto_review_pr_dashboard.__main__.discover_prs", return_value=[]),
            patch.object(daemon, "start_daemon", side_effect=TypeError("wrong arg")),
            redirect_stderr(StringIO()) as stderr,
            self.assertRaises(TypeError),
        ):
            main(
                [
                    "codex",
                    "review",
                    "--pr-url",
                    "https://github.com/o/r/pull/7",
                ]
            )

        self.assertEqual(stderr.getvalue(), "")

    def test_slow_client_is_disconnected_before_buffer_grows_without_bound(self):
        session_server = SessionServer(
            RunConfig("codex", "review"),
            self.store,
            str(Path.cwd().resolve()),
            Path("/tmp/not-used.sock"),
            "run-123",
        )
        writer = Mock()
        writer.is_closing.return_value = False
        writer.transport.get_write_buffer_size.return_value = (
            MAX_CLIENT_BUFFER_BYTES + 1
        )
        session_server._client_writer = writer

        session_server._send_to_client({"type": "snapshot"})

        # Aborted, not closed: close() would wait for that full buffer to drain.
        writer.transport.abort.assert_called_once_with()
        writer.write.assert_not_called()
        self.assertIsNone(session_server._client_writer)


class StaleRuntimeFileSweepTestCase(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        # Never the real /tmp runtime directory: the sweep deletes what it finds.
        self.runtime_dir = Path(temporary_directory.name)

    def test_a_stale_lock_and_its_socket_are_swept(self):
        (self.runtime_dir / "a.lock").write_text("", encoding="utf-8")
        # A plain file answers no connect, so the pair counts as stale.
        (self.runtime_dir / "a.sock").write_text("not a socket", encoding="utf-8")

        daemon._remove_stale_runtime_files(self.runtime_dir)

        self.assertFalse((self.runtime_dir / "a.lock").exists())
        self.assertFalse((self.runtime_dir / "a.sock").exists())

    def test_a_held_lock_and_its_socket_survive_the_sweep(self):
        held = (self.runtime_dir / "b.lock").open("a", encoding="utf-8")
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        (self.runtime_dir / "b.sock").write_text("", encoding="utf-8")

        daemon._remove_stale_runtime_files(self.runtime_dir)

        self.assertTrue((self.runtime_dir / "b.lock").exists())
        self.assertTrue((self.runtime_dir / "b.sock").exists())

    def test_a_live_socket_pair_survives_the_sweep(self):
        (self.runtime_dir / "c.lock").write_text("", encoding="utf-8")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.runtime_dir / "c.sock"))
        listener.listen(1)
        self.addCleanup(listener.close)

        daemon._remove_stale_runtime_files(self.runtime_dir)

        self.assertTrue((self.runtime_dir / "c.lock").exists())
        self.assertTrue((self.runtime_dir / "c.sock").exists())

    def test_a_lock_without_a_socket_is_swept(self):
        (self.runtime_dir / "d.lock").write_text("", encoding="utf-8")

        daemon._remove_stale_runtime_files(self.runtime_dir)

        self.assertFalse((self.runtime_dir / "d.lock").exists())

    def test_take_start_lock_retries_when_the_lock_is_swapped_under_it(self):
        socket_path = self.runtime_dir / "x.sock"
        held = (self.runtime_dir / "x.lock").open("a", encoding="utf-8")
        fcntl.flock(held, fcntl.LOCK_EX)
        taken = {}

        def take_lock():
            taken["lock_file"] = daemon._take_start_lock(socket_path)

        taker = threading.Thread(target=take_lock)
        taker.start()
        # The sweep pattern: unlink the lock a waiter is blocked on, then let
        # it wake - the waiter must reject that dead inode and reopen the path.
        (self.runtime_dir / "x.lock").unlink()
        held.close()
        taker.join(5)

        self.assertFalse(taker.is_alive())
        lock_file = taken["lock_file"]
        self.addCleanup(lock_file.close)
        path_stat = os.stat(self.runtime_dir / "x.lock")
        fd_stat = os.stat(lock_file.fileno())
        self.assertEqual(
            (path_stat.st_dev, path_stat.st_ino), (fd_stat.st_dev, fd_stat.st_ino)
        )


class DaemonLifecycleTestCase(unittest.IsolatedAsyncioTestCase):
    def test_bootstrap_payload_is_handed_over_on_stdin_not_argv(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                store = Store(Path(temporary_directory) / "state")

                class HandoffStdin(io.BytesIO):
                    def close(self):
                        """Keep the payload readable after the handoff."""

                stdin = HandoffStdin()
                metadata = RuntimeMetadata(
                    protocol_version=PROTOCOL_VERSION,
                    run_id="run-123",
                    pid=os.getpid(),
                    cwd=str(Path(temporary_directory).resolve()),
                    socket_path=str(get_socket_path(temporary_directory)),
                    started_at=123.5,
                    config=asdict(RunConfig("codex", "review")),
                )

                class FakeProcess:
                    returncode = None

                    def poll(self):
                        return None

                    def wait(self):
                        return 0

                process = FakeProcess()
                process.stdin = stdin
                with (
                    patch.object(
                        daemon.subprocess, "Popen", return_value=process
                    ) as popen,
                    patch.object(
                        daemon, "_get_is_socket_live", side_effect=[False, True]
                    ),
                    patch.object(
                        daemon, "load_runtime_metadata", return_value=metadata
                    ),
                    # The sweep has tests of its own; its socket probes would
                    # overrun the finite side_effect above.
                    patch.object(daemon, "_remove_stale_runtime_files"),
                ):
                    started = start_daemon(RunConfig("codex", "review"), store)

                # Nothing but the module name reaches argv, where every user can
                # read it through `ps`.
                self.assertEqual(
                    popen.call_args.args[0],
                    [sys.executable, "-m", "auto_review_pr_dashboard.daemon"],
                )
                self.assertIs(popen.call_args.kwargs["stdin"], subprocess.PIPE)
                payload = json.loads(stdin.getvalue().decode("utf-8"))
                self.assertEqual(payload["config"]["prompt"], "review")
                self.assertEqual(
                    payload["cwd"], str(Path(temporary_directory).resolve())
                )
                self.assertEqual(
                    payload["socket_path"], str(get_socket_path(payload["cwd"]))
                )
                self.assertEqual(started, metadata)
            finally:
                os.chdir(original_cwd)

    def test_a_child_that_died_still_reports_its_own_log(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                store = Store(Path(temporary_directory) / "state")
                store.prepare()
                log_path = store.base_dir / DAEMON_LOG_FILE_NAME
                log_path.write_text(
                    "ModuleNotFoundError: No module named 'auto_review_pr_dashboard'",
                    encoding="utf-8",
                )

                class DeadChildStdin(io.BytesIO):
                    def close(self):
                        # A buffered payload only meets the dead pipe on flush.
                        super().close()
                        raise BrokenPipeError(32, "Broken pipe")

                class DeadProcess:
                    returncode = 1

                    def __init__(self):
                        self.stdin = DeadChildStdin()
                        self.reaped = threading.Event()

                    def poll(self):
                        return 1

                    def wait(self):
                        self.reaped.set()
                        return 1

                process = DeadProcess()
                with (
                    patch.object(daemon.subprocess, "Popen", return_value=process),
                    patch.object(daemon, "_get_is_socket_live", return_value=False),
                    # Liveness is forced False here, so the real sweep would
                    # clear pairs it must not judge stale.
                    patch.object(daemon, "_remove_stale_runtime_files"),
                    self.assertRaises(SessionError) as raised,
                ):
                    start_daemon(RunConfig("codex", "review"), store)

                # The broken pipe must not replace the daemon's own diagnosis.
                message = str(raised.exception)
                self.assertIn("exited with 1", message)
                self.assertIn("ModuleNotFoundError", message)
                self.assertNotIn("Broken pipe", message)
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.reaped.wait(1))
            finally:
                os.chdir(original_cwd)

    def test_process_start_ticks_identifies_a_live_pid_and_ignores_a_dead_one(self):
        agent = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            start_ticks = daemon.get_process_start_ticks(agent.pid)
            self.assertIsInstance(start_ticks, int)
            self.assertGreater(start_ticks, 0)
        finally:
            kill_agent(agent)

        self.assertIsNone(daemon.get_process_start_ticks(agent.pid))

    def _start_over_crashed_agents(
        self, workspace: Path, starttime_offset: int
    ) -> list[subprocess.Popen]:
        """Run `start_daemon` while a leftover daemon.json names two live agent groups.

        `starttime_offset` shifts the recorded starttimes: 0 means the groups are
        still the ones the crashed daemon knew, anything else is a reused pid.
        """
        store = Store(workspace / "state")
        store.prepare()
        # start_daemon takes this working directory's flock; unlike a real daemon
        # run there is no test teardown here, so the lock file gets unlinked.
        lock_path = Path(get_socket_path(workspace)).with_suffix(".lock")
        self.addCleanup(lock_path.unlink, missing_ok=True)
        agents = [
            subprocess.Popen(["sleep", "60"], start_new_session=True) for _ in range(2)
        ]
        for agent in agents:
            self.addCleanup(kill_agent, agent)
        leftover = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="crashed-run",
            pid=999_999,
            cwd=str(workspace.resolve()),
            socket_path=str(get_socket_path(workspace)),
            started_at=123.5,
            config=asdict(RunConfig("codex", "review")),
            agent_groups=tuple(
                (agent.pid, daemon.get_process_start_ticks(agent.pid) + starttime_offset)
                for agent in agents
            ),
        )
        get_runtime_path(store).write_text(
            json.dumps(leftover.to_dict()), encoding="utf-8"
        )

        class FakeProcess:
            """Publishes runtime metadata the way a real daemon would."""

            returncode = None

            def __init__(self):
                self.stdin = io.BytesIO()

            def poll(self):
                get_runtime_path(store).write_text(
                    json.dumps(
                        replace(
                            leftover,
                            run_id="new-run",
                            pid=os.getpid(),
                            agent_groups=(),
                        ).to_dict()
                    ),
                    encoding="utf-8",
                )
                return None

            def wait(self):
                return 0

        with (
            patch.object(daemon.subprocess, "Popen", return_value=FakeProcess()),
            patch.object(daemon, "_get_is_socket_live", side_effect=[False, True]),
            # The sweep has tests of its own; running it for real here would
            # add socket probes beyond the two the finite side_effect allows.
            patch.object(daemon, "_remove_stale_runtime_files"),
        ):
            started = start_daemon(RunConfig("codex", "review"), store)
        self.assertEqual(started.run_id, "new-run")
        return agents

    def test_a_crashed_daemons_agent_is_killed_before_the_next_daemon_starts(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                agents = self._start_over_crashed_agents(
                    Path(temporary_directory), starttime_offset=0
                )
                deadline = time.monotonic() + 3
                while (
                    any(agent.poll() is None for agent in agents)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)

                self.assertTrue(all(agent.poll() is not None for agent in agents))
            finally:
                os.chdir(original_cwd)

    def test_the_crash_cleanup_does_not_kill_a_process_that_reused_the_pid(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                agents = self._start_over_crashed_agents(
                    Path(temporary_directory), starttime_offset=1
                )
                time.sleep(0.3)

                # The starttimes say these pids went to somebody else, so they stay.
                self.assertTrue(all(agent.poll() is None for agent in agents))
            finally:
                os.chdir(original_cwd)

    def test_start_daemon_sweeps_the_runtime_dir_under_its_lock(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                store = Store(Path(temporary_directory) / "state")
                socket_path = get_socket_path(Path(temporary_directory).resolve())
                self.addCleanup(
                    socket_path.with_suffix(".lock").unlink, missing_ok=True
                )

                with (
                    patch.object(daemon, "_start_daemon_locked"),
                    patch.object(daemon, "_remove_stale_runtime_files") as sweep,
                ):
                    start_daemon(RunConfig("codex", "review"), store)

                # Sweeping inside the held lock is what keeps it race-free.
                sweep.assert_called_once_with(socket_path.parent)
            finally:
                os.chdir(original_cwd)

    async def test_detached_daemon_can_attach_stop_and_clean_up(self):
        original_cwd = Path.cwd()
        metadata = None
        client = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            store = Store()
            try:
                metadata = await asyncio.to_thread(
                    start_daemon, RunConfig("codex", "review"), store
                )
                with self.assertRaisesRegex(SessionError, "already running"):
                    await asyncio.to_thread(
                        start_daemon, RunConfig("codex", "review"), store
                    )
                messages = []
                snapshot_received = asyncio.Event()

                def handle_message(message):
                    messages.append(message)
                    if message.get("type") == "snapshot":
                        snapshot_received.set()

                client = SessionClient(metadata)
                await client.connect(handle_message)
                await asyncio.wait_for(snapshot_received.wait(), timeout=3)
                self.assertTrue(any(m["type"] == "snapshot" for m in messages))

                await client.send_command("stop")
                client.disconnect()
                lock_path = Path(metadata.socket_path).with_suffix(".lock")
                deadline = time.monotonic() + 5
                # The lock goes last: the sweep runs after daemon.json is removed.
                while (
                    get_runtime_path(store).exists() or lock_path.exists()
                ) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)

                self.assertFalse(get_runtime_path(store).exists())
                self.assertFalse(Path(metadata.socket_path).exists())
                self.assertFalse(lock_path.exists())
            finally:
                if client is not None:
                    client.disconnect()
                if metadata is not None:
                    with suppress(ProcessLookupError):
                        os.kill(metadata.pid, signal.SIGTERM)
                    with suppress(ChildProcessError):
                        await asyncio.to_thread(os.waitpid, metadata.pid, 0)
                    Path(metadata.socket_path).with_suffix(".lock").unlink(
                        missing_ok=True
                    )
                os.chdir(original_cwd)

    async def test_a_daemon_recycles_itself_and_the_client_follows_the_new_run_id(self):
        original_cwd = Path.cwd()
        metadata = None
        new_metadata = None
        clients = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            store = Store()
            try:
                metadata = await asyncio.to_thread(
                    start_daemon, RunConfig("codex", "review"), store
                )
                # Another tool's file in the same .tmp/: part of the wipe scope.
                other_tool_file = Path(".tmp") / "other-tool.md"
                other_tool_file.write_text("not ours", encoding="utf-8")

                messages = []
                client = SessionClient(metadata)
                clients.append(client)
                await client.connect(messages.append)

                async def wait_for_message(
                    candidates, predicate, description, timeout=10
                ):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        for message in candidates:
                            if predicate(message):
                                return message
                        await asyncio.sleep(0.05)
                    self.fail(f"the daemon never sent {description}")

                # An empty scope fails discovery, so every loop ends without work
                # and `n` walks the runner up to the recycle point. Discovery runs
                # in a thread: wait for each loop, or `n` lands inside it.
                for index in range(1, RECYCLE_EVERY_LOOPS):
                    await wait_for_message(
                        messages,
                        lambda message: (
                            message.get("type") == "snapshot"
                            and (message.get("record") or {}).get("index") == index
                            and message["status"].endswith("finished")
                        ),
                        f"loop #{index} finishing",
                    )
                    await client.send_command("run_now")

                # Loop 10 hands off instead of idling, and that is what the client
                # sees rather than another finished snapshot.
                await wait_for_message(
                    messages,
                    lambda message: message.get("type") == "recycling",
                    "the recycling notice",
                )

                deadline = time.monotonic() + 10
                while new_metadata is None and time.monotonic() < deadline:
                    candidate = load_runtime_metadata(store)
                    if candidate is not None and candidate.run_id != metadata.run_id:
                        new_metadata = candidate
                    else:
                        await asyncio.sleep(0.05)
                if new_metadata is None:
                    self.fail("the recycled daemon never published its metadata")

                self.assertNotEqual(new_metadata.pid, metadata.pid)
                self.assertFalse(other_tool_file.exists())
                self.assertFalse((store.loops_dir / "loop-0001.json").exists())

                new_messages = []
                new_client = SessionClient(new_metadata)
                clients.append(new_client)
                await new_client.connect(new_messages.append)
                snapshot = await wait_for_message(
                    new_messages,
                    lambda message: message.get("type") == "snapshot",
                    "a snapshot from the new daemon",
                )

                self.assertEqual(snapshot["recycle_count"], 1)
                self.assertEqual(snapshot["record"]["index"], RECYCLE_EVERY_LOOPS)
                self.assertEqual(
                    snapshot["status"], f"Loop #{RECYCLE_EVERY_LOOPS} · finished"
                )
                self.assertIsNotNone(snapshot["next_run_at"])

                await new_client.send_command("stop")
                new_client.disconnect()
                deadline = time.monotonic() + 5
                while get_runtime_path(store).exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                self.assertFalse(get_runtime_path(store).exists())
            finally:
                for client in clients:
                    client.disconnect()
                for entry in (metadata, new_metadata):
                    if entry is None:
                        continue
                    with suppress(ProcessLookupError):
                        os.kill(entry.pid, signal.SIGTERM)
                    with suppress(ChildProcessError):
                        await asyncio.to_thread(os.waitpid, entry.pid, 0)
                if metadata is not None:
                    Path(metadata.socket_path).with_suffix(".lock").unlink(
                        missing_ok=True
                    )
                os.chdir(original_cwd)

    async def test_daemon_survives_the_process_that_launched_it(self):
        original_cwd = Path.cwd()
        metadata = None
        client = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            store = Store()
            package_root = str(Path(__file__).resolve().parents[1])
            child_env = os.environ | {"PYTHONPATH": package_root}
            launch_code = (
                "from auto_review_pr_dashboard.config import RunConfig; "
                "from auto_review_pr_dashboard.daemon import start_daemon; "
                "from auto_review_pr_dashboard.store import Store; "
                "start_daemon(RunConfig('codex', 'review'), Store())"
            )
            try:
                launch = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-c", launch_code],
                    cwd=temporary_directory,
                    env=child_env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(launch.returncode, 0, launch.stderr)
                metadata = load_runtime_metadata(store)
                if metadata is None:
                    self.fail("daemon did not write runtime metadata")

                snapshot_received = asyncio.Event()
                client = SessionClient(metadata)
                await client.connect(
                    lambda message: (
                        snapshot_received.set()
                        if message.get("type") == "snapshot"
                        else None
                    )
                )
                await asyncio.wait_for(snapshot_received.wait(), timeout=3)
                await client.send_command("stop")
                client.disconnect()

                deadline = time.monotonic() + 5
                while get_runtime_path(store).exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                self.assertFalse(get_runtime_path(store).exists())
                self.assertFalse(Path(metadata.socket_path).exists())
            finally:
                if client is not None:
                    client.disconnect()
                if metadata is not None:
                    with suppress(ProcessLookupError):
                        os.kill(metadata.pid, signal.SIGTERM)
                    Path(metadata.socket_path).with_suffix(".lock").unlink(
                        missing_ok=True
                    )
                os.chdir(original_cwd)


class WorkspaceListingTestCase(unittest.IsolatedAsyncioTestCase):
    """`-l` has no index to read: it finds daemons through /proc."""

    async def test_live_daemon_is_listed_and_disappears_after_stop(self):
        original_cwd = Path.cwd()
        metadata = None
        client = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            workspace_cwd = str(Path(temporary_directory).resolve())
            store = Store()
            try:
                metadata = await asyncio.to_thread(
                    start_daemon, RunConfig("codex", "review"), store
                )
                listed = await asyncio.to_thread(get_live_workspaces)
                self.assertIn(
                    (workspace_cwd, metadata.pid),
                    [(entry.cwd, entry.pid) for entry in listed],
                )

                client = SessionClient(metadata)
                stopped = asyncio.Event()
                await client.connect(
                    lambda message: (
                        stopped.set()
                        if message.get("type") == "snapshot"
                        else None
                    )
                )
                await asyncio.wait_for(stopped.wait(), timeout=3)
                await client.send_command("stop")
                client.disconnect()
                deadline = time.monotonic() + 5
                while get_runtime_path(store).exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)

                listed = await asyncio.to_thread(get_live_workspaces)
                self.assertNotIn(workspace_cwd, [entry.cwd for entry in listed])
            finally:
                if client is not None:
                    client.disconnect()
                if metadata is not None:
                    with suppress(ProcessLookupError):
                        os.kill(metadata.pid, signal.SIGTERM)
                    with suppress(ChildProcessError):
                        await asyncio.to_thread(os.waitpid, metadata.pid, 0)
                    Path(metadata.socket_path).with_suffix(".lock").unlink(
                        missing_ok=True
                    )
                os.chdir(original_cwd)

    async def test_daemon_the_client_could_not_attach_to_is_not_listed(self):
        """`-l` must not advertise what --resume would then refuse."""
        original_cwd = Path.cwd()
        metadata = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            workspace_cwd = str(Path(temporary_directory).resolve())
            store = Store()
            try:
                metadata = await asyncio.to_thread(
                    start_daemon, RunConfig("codex", "review"), store
                )
                tampered_cases = (
                    ("incompatible protocol", {"protocol_version": PROTOCOL_VERSION + 1}),
                    ("foreign socket", {"socket_path": "/tmp/not-this-workspace.sock"}),
                    ("foreign cwd", {"cwd": "/tmp/not-this-workspace"}),
                    (
                        "config this version cannot rebuild",
                        {"config": {"ai_cli": "codex", "prompt": "p", "legacy": 1}},
                    ),
                )
                for label, override in tampered_cases:
                    with self.subTest(tampered=label):
                        get_runtime_path(store).write_text(
                            json.dumps(asdict(metadata) | override), encoding="utf-8"
                        )
                        listed = await asyncio.to_thread(get_live_workspaces)
                        self.assertNotIn(
                            workspace_cwd, [entry.cwd for entry in listed]
                        )
            finally:
                if metadata is not None:
                    with suppress(ProcessLookupError):
                        os.kill(metadata.pid, signal.SIGTERM)
                    with suppress(ChildProcessError):
                        await asyncio.to_thread(os.waitpid, metadata.pid, 0)
                    Path(metadata.socket_path).unlink(missing_ok=True)
                    Path(metadata.socket_path).with_suffix(".lock").unlink(
                        missing_ok=True
                    )
                os.chdir(original_cwd)

    def test_stale_daemon_metadata_is_not_listed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace_cwd = Path(temporary_directory).resolve()
            store = Store(workspace_cwd / ".tmp/auto-review-pr-dashboard")
            store.prepare()
            # A pid this process can never own, so no /proc entry maps back to it.
            get_runtime_path(store).write_text(
                json.dumps(
                    asdict(
                        RuntimeMetadata(
                            protocol_version=PROTOCOL_VERSION,
                            run_id="stale-run",
                            pid=-1,
                            cwd=str(workspace_cwd),
                            socket_path=str(get_socket_path(workspace_cwd)),
                            started_at=time.time(),
                            config=asdict(RunConfig("codex", "review")),
                        )
                    )
                ),
                encoding="utf-8",
            )
            listed = get_live_workspaces()
            self.assertNotIn(str(workspace_cwd), [entry.cwd for entry in listed])


class PrintWorkspacesTestCase(unittest.TestCase):
    def _get_metadata(self, cwd: str, pid: int, age_sec: float) -> RuntimeMetadata:
        return RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id=f"run-{pid}",
            pid=pid,
            cwd=cwd,
            socket_path=f"/tmp/{pid}.sock",
            started_at=time.time() - age_sec,
            config=asdict(RunConfig("codex", "review")),
        )

    def test_listing_shows_every_workspace_with_pid_and_uptime(self):
        workspaces = [
            self._get_metadata("/home/dev/alpha", 111, 90),
            self._get_metadata("/home/dev/beta-longer-path", 222, 7300),
        ]
        output = StringIO()
        with (
            patch.object(daemon, "get_live_workspaces", return_value=workspaces),
            redirect_stdout(output),
        ):
            self.assertEqual(print_workspaces(), 0)

        text = output.getvalue()
        self.assertIn("/home/dev/alpha", text)
        self.assertIn("pid 111", text)
        self.assertIn("1m30s", text)
        self.assertIn("/home/dev/beta-longer-path", text)
        self.assertIn("pid 222", text)
        self.assertIn("2h01m", text)
        self.assertIn("2 live daemons", text)

    def test_listing_without_any_daemon_says_there_is_nothing_to_resume(self):
        output = StringIO()
        with (
            patch.object(daemon, "get_live_workspaces", return_value=[]),
            redirect_stdout(output),
        ):
            self.assertEqual(print_workspaces(), 0)
        self.assertIn("no live background daemon found", output.getvalue())


if __name__ == "__main__":
    unittest.main()
