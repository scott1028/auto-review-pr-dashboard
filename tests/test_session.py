"""Detached session metadata, lifecycle, and IPC behavior."""

import asyncio
import fcntl
import io
import json
import os
import shutil
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
    RECYCLE_EVERY_LOOPS,
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

ISOLATED_TEMP_DIR = ""
_previous_tempdir = None
_previous_tmpdir = None


def setUpModule() -> None:
    """Pin every tempdir in this module away from the real /tmp runtime dir.

    start_daemon sweeps stale .lock/.sock pairs out of whatever gettempdir()
    points at: unisolated, a test that forces liveness False would delete the
    files of a daemon the user is really running elsewhere. TMPDIR is set too
    because test_daemon_survives_the_process_that_launched_it runs start_daemon
    in a child python that resolves gettempdir() from the environment.
    """
    global ISOLATED_TEMP_DIR, _previous_tempdir, _previous_tmpdir
    # Short prefix: AF_UNIX socket paths are capped at 108 bytes.
    ISOLATED_TEMP_DIR = tempfile.mkdtemp(prefix="arpd.")
    _previous_tempdir = tempfile.tempdir
    _previous_tmpdir = os.environ.get("TMPDIR")
    tempfile.tempdir = ISOLATED_TEMP_DIR
    os.environ["TMPDIR"] = ISOLATED_TEMP_DIR


def tearDownModule() -> None:
    global ISOLATED_TEMP_DIR
    tempfile.tempdir = _previous_tempdir
    if _previous_tmpdir is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = _previous_tmpdir
    shutil.rmtree(ISOLATED_TEMP_DIR, ignore_errors=True)
    ISOLATED_TEMP_DIR = ""


class RuntimeDirIsolationTestCase(unittest.TestCase):
    def test_socket_paths_resolve_inside_the_isolated_temp_dir(self):
        runtime_dir = get_socket_path(Path.cwd()).parent
        self.assertEqual(
            runtime_dir,
            Path(ISOLATED_TEMP_DIR) / f"auto-review-pr-dashboard-{os.getuid()}",
        )


def kill_agent(process: subprocess.Popen) -> None:
    """Take a test's stand-in agent down whether or not the test killed it first."""
    with suppress(ProcessLookupError):
        process.kill()
    process.wait()


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
        self.is_recycle_requested = False
        self.current_key = item.key
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
        self.assertEqual(snapshot["recycle_count"], 0)

    async def test_daemon_json_tracks_every_running_agent_group(self):
        agents = [
            subprocess.Popen(["sleep", "60"], start_new_session=True) for _ in range(2)
        ]
        for agent in agents:
            self.addCleanup(kill_agent, agent)
        self.store.prepare()

        for agent in agents:
            self.session_server._handle_runner_event("agent_started", (agent.pid,))
        self.session_server._handle_runner_event("agent_finished", (agents[0].pid,))

        # The finished agent is gone; the one still running stays recorded.
        self.assertEqual(
            load_runtime_metadata(self.store).agent_groups,
            ((agents[1].pid, session.get_process_start_ticks(agents[1].pid)),),
        )

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

    async def test_recycle_wipes_tmp_and_hands_the_loop_to_a_new_daemon(self):
        base_dir = self.socket_path.parent / ".tmp" / "auto-review-pr-dashboard"
        store = Store(base_dir)
        store.prepare()
        other_tool_file = base_dir.parent / "other-tool.md"
        other_tool_file.write_text("not ours, still cleared", encoding="utf-8")
        (store.loops_dir / "loop-0004.json").write_text("{}", encoding="utf-8")
        self.session_server.store = store
        self.fake_runner.is_recycle_requested = True
        reader, _writer = await self.attach()
        await self.read_message(reader)

        with patch.object(session, "_start_daemon_locked") as start_daemon_locked:
            await self.session_server._recycle()

        self.assertEqual((await self.read_message(reader))["type"], "recycling")
        # The whole .tmp/ is in scope, other tools' files and our own records alike.
        self.assertFalse(other_tool_file.exists())
        self.assertFalse(base_dir.exists())
        self.assertEqual(start_daemon_locked.call_args.args, (self.config, store))
        handoff = start_daemon_locked.call_args.kwargs["handoff"]
        self.assertEqual(handoff["loop_index"], 4)
        self.assertEqual(handoff["recycle_count"], 1)
        self.assertFalse(handoff["paused"])
        self.assertGreater(handoff["next_run_at"], time.time())
        self.assertEqual(handoff["record"], self.fake_runner.record.to_dict())

    async def test_recycle_leaves_a_base_dir_that_is_not_under_tmp_alone(self):
        self.store.prepare()
        self.fake_runner.is_recycle_requested = True
        state_path = self.store.base_dir / "state.json"
        state_path.write_text("{}", encoding="utf-8")
        loop_path = self.store.loops_dir / "loop-0004.json"
        loop_path.write_text("{}", encoding="utf-8")

        with patch.object(session, "_start_daemon_locked") as start_daemon_locked:
            await self.session_server._recycle()

        # This store is not under a directory named .tmp, so the guard skips it.
        self.assertTrue(state_path.exists())
        self.assertTrue(loop_path.exists())
        self.assertEqual(start_daemon_locked.call_count, 1)

    async def test_a_failed_recycle_still_shuts_the_daemon_down_and_logs_it(self):
        self.store.prepare()
        self.fake_runner.is_recycle_requested = True
        self.assertTrue(self.socket_path.exists())

        with (
            patch.object(
                session, "_take_start_lock", side_effect=OSError("lock failed")
            ),
            patch.object(session, "_start_daemon_locked") as start_daemon_locked,
        ):
            await self.session_server._recycle()

        # A recycle that never got the lock must still leave no live socket or
        # daemon.json behind, and the reason has to be readable somewhere.
        self.assertFalse(self.socket_path.exists())
        self.assertFalse(get_runtime_path(self.store).exists())
        start_daemon_locked.assert_not_called()
        log_text = (self.store.base_dir / session.DAEMON_LOG_FILE_NAME).read_text(
            encoding="utf-8"
        )
        self.assertIn("recycle failed: lock failed", log_text)

    async def test_recycle_sweeps_the_runtime_dir_after_the_successor_starts(self):
        self.store.prepare()
        self.fake_runner.is_recycle_requested = True

        with (
            patch.object(session, "_start_daemon_locked"),
            patch.object(session, "_remove_stale_runtime_files") as sweep,
        ):
            await self.session_server._recycle()

        # The successor's socket is live and this daemon's own lock is held, so
        # the sweep can only ever clear older stale pairs.
        sweep.assert_called_once_with(self.socket_path.parent)


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

        session._remove_stale_runtime_files(self.runtime_dir)

        self.assertFalse((self.runtime_dir / "a.lock").exists())
        self.assertFalse((self.runtime_dir / "a.sock").exists())

    def test_a_held_lock_and_its_socket_survive_the_sweep(self):
        held = (self.runtime_dir / "b.lock").open("a", encoding="utf-8")
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        (self.runtime_dir / "b.sock").write_text("", encoding="utf-8")

        session._remove_stale_runtime_files(self.runtime_dir)

        self.assertTrue((self.runtime_dir / "b.lock").exists())
        self.assertTrue((self.runtime_dir / "b.sock").exists())

    def test_a_live_socket_pair_survives_the_sweep(self):
        (self.runtime_dir / "c.lock").write_text("", encoding="utf-8")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.runtime_dir / "c.sock"))
        listener.listen(1)
        self.addCleanup(listener.close)

        session._remove_stale_runtime_files(self.runtime_dir)

        self.assertTrue((self.runtime_dir / "c.lock").exists())
        self.assertTrue((self.runtime_dir / "c.sock").exists())

    def test_a_lock_without_a_socket_is_swept(self):
        (self.runtime_dir / "d.lock").write_text("", encoding="utf-8")

        session._remove_stale_runtime_files(self.runtime_dir)

        self.assertFalse((self.runtime_dir / "d.lock").exists())

    def test_take_start_lock_retries_when_the_lock_is_swapped_under_it(self):
        socket_path = self.runtime_dir / "x.sock"
        held = (self.runtime_dir / "x.lock").open("a", encoding="utf-8")
        fcntl.flock(held, fcntl.LOCK_EX)
        taken = {}

        def take_lock():
            taken["lock_file"] = session._take_start_lock(socket_path)

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


class RecycleHandoffTestCase(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = Store(Path(temporary_directory.name) / "state")
        self.config = RunConfig("codex", "review", repos=["o/r"], authors=["me"])

    def test_a_recycled_daemon_carries_the_loop_and_the_clean_count(self):
        record = LoopRecord(index=10, started_at=100.0, items=[])
        session_server = SessionServer(
            self.config,
            self.store,
            str(Path.cwd().resolve()),
            Path("/tmp/not-used.sock"),
            "run-2",
            handoff={
                "loop_index": 10,
                "next_run_at": 500.0,
                "paused": True,
                "record": record.to_dict(),
                "recycle_count": 2,
            },
        )

        snapshot = session_server._get_snapshot()

        # The dashboard must not lose the countdown, the pause, or the table.
        self.assertEqual(snapshot["recycle_count"], 2)
        self.assertEqual(snapshot["status"], "Loop #10 · finished")
        self.assertTrue(snapshot["paused"])
        self.assertEqual(snapshot["next_run_at"], 500.0)
        self.assertEqual(snapshot["record"], record.to_dict())
        self.assertEqual(session_server.runner.loop_index, 10)
        self.assertEqual(
            session_server.runner.recycle_every_loops, RECYCLE_EVERY_LOOPS
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
                        session.subprocess, "Popen", return_value=process
                    ) as popen,
                    patch.object(
                        session, "_get_is_socket_live", side_effect=[False, True]
                    ),
                    patch.object(
                        session, "load_runtime_metadata", return_value=metadata
                    ),
                    # The sweep has tests of its own; its socket probes would
                    # overrun the finite side_effect above.
                    patch.object(session, "_remove_stale_runtime_files"),
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
                    # Liveness is forced False here, so the real sweep would
                    # clear pairs it must not judge stale.
                    patch.object(session, "_remove_stale_runtime_files"),
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
            start_ticks = session.get_process_start_ticks(agent.pid)
            self.assertIsInstance(start_ticks, int)
            self.assertGreater(start_ticks, 0)
        finally:
            kill_agent(agent)

        self.assertIsNone(session.get_process_start_ticks(agent.pid))

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
                (agent.pid, session.get_process_start_ticks(agent.pid) + starttime_offset)
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
            patch.object(session.subprocess, "Popen", return_value=FakeProcess()),
            patch.object(session, "_get_is_socket_live", side_effect=[False, True]),
            # The sweep has tests of its own; running it for real here would
            # add socket probes beyond the two the finite side_effect allows.
            patch.object(session, "_remove_stale_runtime_files"),
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
                    patch.object(session, "_start_daemon_locked"),
                    patch.object(session, "_remove_stale_runtime_files") as sweep,
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

                # An empty scope ends every loop as soon as it starts, so `n`
                # walks the runner up to the recycle point.
                for index in range(1, RECYCLE_EVERY_LOOPS):
                    await client.send_command("run_now")
                    await wait_for_message(
                        messages,
                        lambda message: (
                            message.get("type") == "snapshot"
                            and (message.get("record") or {}).get("index") == index
                            and message["status"].endswith("finished")
                        ),
                        f"loop #{index} finishing",
                    )

                # Loop 10 hands off instead of idling, and that is what the client
                # sees rather than another finished snapshot.
                await client.send_command("run_now")
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
