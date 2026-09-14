"""Detached session metadata, lifecycle, and IPC behavior."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import session
from auto_review_pr_dashboard import store as store_module
from auto_review_pr_dashboard.__main__ import main, print_workspaces
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.models import LoopRecord, PrItem
from auto_review_pr_dashboard.runner import LoopRunner
from auto_review_pr_dashboard.session import (
    DAEMON_LOG_FILE_NAME,
    MAX_CLIENT_BUFFER_BYTES,
    PROTOCOL_VERSION,
    RuntimeMetadata,
    SessionClient,
    SessionError,
    SessionServer,
    get_live_workspaces,
    get_resume_metadata,
    get_runtime_path,
    get_socket_path,
    load_runtime_metadata,
    start_daemon,
)
from auto_review_pr_dashboard.store import Store


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

    def test_missing_resume_metadata_does_not_start_daemon(self):
        with (
            patch.object(store_module, "Store", return_value=self.store),
            patch.object(session, "start_daemon") as start_daemon,
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

        with patch.object(session, "_get_is_socket_live", return_value=False):
            with self.assertRaisesRegex(SessionError, "no longer running"):
                get_resume_metadata(self.store)

            with (
                patch.object(store_module, "Store", return_value=self.store),
                patch.object(session, "start_daemon") as start_daemon,
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
            patch.object(session, "_get_is_socket_live", return_value=True),
            patch.object(store_module, "Store", return_value=self.store),
            patch.object(session, "start_daemon") as start_daemon,
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
            patch.object(session, "start_daemon", side_effect=TypeError("wrong arg")),
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

        writer.close.assert_called_once_with()
        self.assertIsNone(session_server._client_writer)


class FakeRunner:
    def __init__(self, on_event):
        item = PrItem(
            repo="o/r",
            number=7,
            url="https://github.com/o/r/pull/7",
            title="PR 7",
            author="me",
            head_sha="abcdef123456",
        )
        self.record = LoopRecord(4, 100.0, items=[item])
        self.loop_index = 4
        self.paused = False
        self.next_run_at = 200.0
        self.blocked_until = None
        self.block_reason = ""
        self._current_key = item.key
        self.commands = []
        self.on_event = on_event

    def cancel(self, key):
        self.commands.append(("cancel", key))
        return key == "o/r#7"

    def resume(self, key):
        self.commands.append(("resume", key))
        return key == "o/r#7"

    def run_now(self):
        self.commands.append(("run_now", None))

    def toggle_pause(self):
        self.commands.append(("toggle_pause", None))
        self.paused = not self.paused
        self.on_event("paused", self.paused)
        return self.paused

    def stop(self):
        self.commands.append(("stop", None))


class SessionServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        base_path = Path(temporary_directory.name)
        self.store = Store(base_path / "state")
        self.config = RunConfig("codex", "review", repos=["o/r"], authors=["me"])
        self.socket_path = base_path / "session.sock"
        self.session_server = SessionServer(
            self.config,
            self.store,
            str(Path.cwd().resolve()),
            self.socket_path,
            "run-123",
        )
        self.fake_runner = FakeRunner(self.session_server.emit_runner_event)
        self.session_server.runner = cast(LoopRunner, self.fake_runner)
        self.session_server.status = "Loop #4 · running"
        self.session_server._event_loop = asyncio.get_running_loop()
        self.session_server._server = await asyncio.start_unix_server(
            self.session_server._handle_client, path=str(self.socket_path)
        )
        self.client_writers = []
        self.addAsyncCleanup(self.cleanup_session)

    async def cleanup_session(self):
        for writer in self.client_writers:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
        await self.session_server._shutdown()
        await asyncio.sleep(0)

    async def attach(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        self.client_writers.append(writer)
        writer.write(
            session._encode_message(
                {
                    "type": "attach",
                    "protocol_version": PROTOCOL_VERSION,
                    "run_id": "run-123",
                    "cwd": str(Path.cwd().resolve()),
                }
            )
        )
        await writer.drain()
        return reader, writer

    async def read_message(self, reader):
        line = await asyncio.wait_for(reader.readline(), timeout=1)
        self.assertTrue(line, "session connection closed before a message arrived")
        return json.loads(line)

    async def test_attach_receives_current_snapshot(self):
        reader, _writer = await self.attach()

        snapshot = await self.read_message(reader)

        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["config"], asdict(self.config))
        self.assertEqual(snapshot["record"], self.fake_runner.record.to_dict())
        self.assertEqual(snapshot["status"], "Loop #4 · running")
        self.assertEqual(snapshot["current_key"], "o/r#7")
        self.assertFalse(snapshot["paused"])

    async def test_paused_tick_carries_the_moved_deadline(self):
        reader, _writer = await self.attach()
        await self.read_message(reader)

        # An unpaused countdown needs nothing: the client counts down by itself.
        self.fake_runner.paused = False
        self.session_server._handle_runner_event("idle_tick", (180.0,))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), timeout=0.2)

        # Pausing pushed next_run_at back, so the client needs the new deadline.
        self.fake_runner.paused = True
        self.fake_runner.next_run_at = 400.0
        self.session_server._handle_runner_event("idle_tick", (400.0,))
        tick = await self.read_message(reader)

        self.assertEqual(tick["type"], "tick")
        self.assertEqual(tick["next_run_at"], 400.0)
        self.assertIsNone(tick["blocked_until"])
        self.assertNotIn("record", tick)

    async def test_paused_blocked_tick_carries_the_moved_cooldown_deadline(self):
        reader, _writer = await self.attach()
        await self.read_message(reader)

        # A paused runner pushes blocked_until out, so each tick carries a new one.
        self.fake_runner.paused = True
        self.fake_runner.next_run_at = None
        deadlines = []
        for second in (0.0, 1.0):
            self.fake_runner.blocked_until = 500.0 + second
            self.session_server._handle_runner_event(
                "blocked_tick", (500.0 + second,)
            )
            tick = await self.read_message(reader)
            self.assertEqual(tick["type"], "tick")
            self.assertIsNone(tick["next_run_at"])
            deadlines.append(tick["blocked_until"])

        self.assertEqual(deadlines, [500.0, 501.0])

    async def test_session_client_receives_large_snapshot(self):
        self.fake_runner.record.items[0].error = "x" * 70_000
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="run-123",
            pid=os.getpid(),
            cwd=str(Path.cwd().resolve()),
            socket_path=str(self.socket_path),
            started_at=123.5,
            config=asdict(self.config),
        )
        messages = []
        client = SessionClient(metadata)

        await client.connect(messages.append)
        deadline = asyncio.get_running_loop().time() + 1
        while not messages:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("large snapshot did not reach the session client")
            await asyncio.sleep(0.01)

        client.disconnect()
        self.assertGreater(
            len(session._encode_message(self.session_server._get_snapshot())), 2**16
        )
        self.assertEqual(messages[0]["type"], "snapshot")

    async def test_commands_are_dispatched_to_runner(self):
        reader, writer = await self.attach()
        await self.read_message(reader)

        for action, key in (
            ("cancel", "o/r#7"),
            ("resume", "o/r#7"),
            ("run_now", None),
        ):
            message = {"type": "command", "action": action}
            if key is not None:
                message["key"] = key
            writer.write(session._encode_message(message))
        await writer.drain()
        self.assertEqual((await self.read_message(reader))["type"], "snapshot")

        writer.write(
            session._encode_message({"type": "command", "action": "toggle_pause"})
        )
        await writer.drain()
        paused_snapshot = await self.read_message(reader)
        self.assertEqual(paused_snapshot["type"], "snapshot")
        self.assertTrue(paused_snapshot["paused"])

        writer.write(session._encode_message({"type": "command", "action": "stop"}))
        await writer.drain()
        notice = await self.read_message(reader)
        self.assertEqual(notice["type"], "notice")
        self.assertIn("stopping", notice["message"])
        self.assertEqual(
            self.fake_runner.commands,
            [
                ("cancel", "o/r#7"),
                ("resume", "o/r#7"),
                ("run_now", None),
                ("toggle_pause", None),
                ("stop", None),
            ],
        )

    async def test_command_waits_for_the_client_connection(self):
        metadata = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="run-123",
            pid=os.getpid(),
            cwd=str(Path.cwd().resolve()),
            socket_path=str(self.socket_path),
            started_at=123.5,
            config=asdict(self.config),
        )
        client = SessionClient(metadata)
        command = asyncio.create_task(client.send_command("run_now"))
        await asyncio.sleep(0)
        self.assertFalse(command.done())

        await client.connect(lambda _message: None)
        await asyncio.wait_for(command, timeout=1)
        deadline = asyncio.get_running_loop().time() + 1
        while ("run_now", None) not in self.fake_runner.commands:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("run_now command did not reach the daemon")
            await asyncio.sleep(0.01)
        client.disconnect()

    async def test_second_client_takes_over_first_client(self):
        first_reader, _first_writer = await self.attach()
        await self.read_message(first_reader)

        second_reader, second_writer = await self.attach()
        second_snapshot = await self.read_message(second_reader)
        replaced = await self.read_message(first_reader)

        self.assertEqual(second_snapshot["type"], "snapshot")
        self.assertEqual(replaced["type"], "replaced")
        self.assertIn("newer --resume", replaced["message"])
        self.assertEqual(
            await asyncio.wait_for(first_reader.readline(), timeout=1), b""
        )

        second_writer.write(
            session._encode_message({"type": "command", "action": "run_now"})
        )
        await second_writer.drain()
        self.assertEqual((await self.read_message(second_reader))["type"], "snapshot")


class ShutdownOrderTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_socket_is_unlinked_while_the_daemon_still_owns_it(self):
        order = []

        class RecordingSocketPath:
            def unlink(self, missing_ok=False):
                order.append("unlink")

        class RecordingListener:
            def close(self):
                order.append("close")

            async def wait_closed(self):
                order.append("wait_closed")

        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Store(Path(temporary_directory) / "state")
            store.prepare()
            # Closing the listener first lets a successor bind the same path, so
            # an unlink after it can delete that successor's socket.
            session_server = SessionServer(
                RunConfig("codex", "review"),
                store,
                str(Path.cwd().resolve()),
                cast(Path, RecordingSocketPath()),
                "run-123",
            )
            session_server._server = cast(asyncio.Server, RecordingListener())

            await session_server._shutdown()

        self.assertEqual(order, ["unlink", "close", "wait_closed"])


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
                        session.subprocess, "Popen", return_value=process
                    ) as popen,
                    patch.object(
                        session, "_get_is_socket_live", side_effect=[False, True]
                    ),
                    patch.object(
                        session, "load_runtime_metadata", return_value=metadata
                    ),
                ):
                    started = start_daemon(RunConfig("codex", "review"), store)

                # Nothing but the module name reaches argv, where every user can
                # read it through `ps`.
                self.assertEqual(
                    popen.call_args.args[0],
                    [sys.executable, "-m", "auto_review_pr_dashboard.session"],
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
                    patch.object(session.subprocess, "Popen", return_value=process),
                    patch.object(session, "_get_is_socket_live", return_value=False),
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
                    with suppress(ChildProcessError):
                        await asyncio.to_thread(os.waitpid, metadata.pid, 0)
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
                "from auto_review_pr_dashboard.session import start_daemon; "
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
            patch.object(session, "get_live_workspaces", return_value=workspaces),
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
            patch.object(session, "get_live_workspaces", return_value=[]),
            redirect_stdout(output),
        ):
            self.assertEqual(print_workspaces(), 0)
        self.assertIn("no live background daemon found", output.getvalue())


if __name__ == "__main__":
    unittest.main()
