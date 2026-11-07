"""The daemon's socket server: attach, snapshot push, commands, shutdown, recycle."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import daemon, server
from auto_review_pr_dashboard.client import SessionClient
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.daemon import (
    PROTOCOL_VERSION,
    RuntimeMetadata,
    get_runtime_path,
    load_runtime_metadata,
)
from auto_review_pr_dashboard.models import LoopRecord, PrItem
from auto_review_pr_dashboard.runner import LoopRunner
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
        # As run() does before serving: daemon.json names this daemon.
        self.store.prepare()
        self.session_server._write_runtime_metadata()
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
            daemon.encode_message(
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
            ((agents[1].pid, daemon.get_process_start_ticks(agents[1].pid)),),
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
            len(daemon.encode_message(self.session_server._get_snapshot())), 2**16
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
            writer.write(daemon.encode_message(message))
        await writer.drain()
        self.assertEqual((await self.read_message(reader))["type"], "snapshot")

        writer.write(
            daemon.encode_message({"type": "command", "action": "toggle_pause"})
        )
        await writer.drain()
        paused_snapshot = await self.read_message(reader)
        self.assertEqual(paused_snapshot["type"], "snapshot")
        self.assertTrue(paused_snapshot["paused"])

        writer.write(daemon.encode_message({"type": "command", "action": "stop"}))
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
            daemon.encode_message({"type": "command", "action": "run_now"})
        )
        await second_writer.drain()
        self.assertEqual((await self.read_message(second_reader))["type"], "snapshot")

    async def wait_until(self, predicate, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition never became true")
            await asyncio.sleep(0.02)

    async def read_until(self, reader, message_type):
        while (message := await self.read_message(reader))["type"] != message_type:
            pass
        return message

    def fill_client_buffer(self):
        """Queue 768KiB, under the drop cap, for a client that reads nothing yet.

        8KiB lines, so a client that later reads stays under its 64KiB line limit.
        """
        line = "x" * (8 * 1024)
        for _ in range(96):
            self.session_server._send_to_client({"type": "log", "key": "k", "line": line})

    async def test_concurrent_attaches_chain_and_every_replaced_client_is_told(self):
        first_reader, _first_writer = await self.attach()
        await self.read_message(first_reader)
        # The first client stops reading, so replacing it has to wait on a drain.
        self.fill_client_buffer()
        # Two attaches land back to back: swapping before any await makes them
        # a chain, each replacing exactly the client current when it landed.
        second_reader, _second_writer = await self.attach()
        third_reader, _third_writer = await self.attach()

        self.assertEqual((await self.read_message(third_reader))["type"], "snapshot")
        for replaced_reader in (first_reader, second_reader):
            replaced = await self.read_until(replaced_reader, "replaced")
            self.assertIn("newer --resume", replaced["message"])
            self.assertEqual(
                await asyncio.wait_for(replaced_reader.readline(), timeout=1), b""
            )
        await self.wait_until(lambda: len(self.session_server._client_writers) == 1)
        self.assertIsNotNone(self.session_server._client_writer)

    async def test_shutdown_does_not_wait_for_a_client_that_stopped_reading(self):
        reader, _writer = await self.attach()
        await self.read_message(reader)
        # Under the drop cap, so the client stays attached with a full buffer.
        self.fill_client_buffer()
        client_writer = self.session_server._client_writer
        self.assertGreater(client_writer.transport.get_write_buffer_size(), 0)

        started = time.monotonic()
        await asyncio.wait_for(self.session_server._shutdown(), timeout=5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.session_server._client_writers, set())
        # Aborted: the unread buffer is dropped, not left waiting to drain.
        self.assertEqual(client_writer.transport.get_write_buffer_size(), 0)

    async def test_a_client_that_stops_reading_is_dropped_and_another_can_attach(self):
        reader, _writer = await self.attach()
        await self.read_message(reader)
        line = "x" * (64 * 1024)
        for _ in range(64):
            self.session_server._send_to_client({"type": "log", "key": "k", "line": line})
            await asyncio.sleep(0)
            if self.session_server._client_writer is None:
                break

        self.assertIsNone(self.session_server._client_writer)
        second_reader, _second_writer = await self.attach()
        self.assertEqual((await self.read_message(second_reader))["type"], "snapshot")
        # The dropped connection was aborted, so its handler ended too.
        await self.wait_until(lambda: len(self.session_server._client_writers) == 1)

    async def test_shutdown_leaves_a_successors_socket_and_daemon_json_alone(self):
        # A successor bound the same path and wrote its own daemon.json.
        self.socket_path.unlink()
        successor_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(successor_socket.close)
        successor_socket.bind(str(self.socket_path))
        successor_socket.listen()
        successor = RuntimeMetadata(
            protocol_version=PROTOCOL_VERSION,
            run_id="run-456",
            pid=os.getpid(),
            cwd=str(Path.cwd().resolve()),
            socket_path=str(self.socket_path),
            started_at=time.time(),
            config=asdict(self.config),
        )
        get_runtime_path(self.store).write_text(
            json.dumps(successor.to_dict()), encoding="utf-8"
        )

        await self.session_server._shutdown()

        self.assertTrue(self.socket_path.exists())
        self.assertEqual(load_runtime_metadata(self.store).run_id, "run-456")

    async def test_shutdown_keeps_a_daemon_json_a_successor_writes_meanwhile(self):
        listener = self.session_server._server
        successor_metadata = replace(load_runtime_metadata(self.store), run_id="run-456")
        socket_path, store = self.socket_path, self.store
        successors = []

        class SuccessorBindsWhileClosing:
            def close(self):
                listener.close()

            async def wait_closed(self):
                await listener.wait_closed()
                if successors:
                    return
                # A successor binds the path and writes its daemon.json right here,
                # after this daemon checked ownership and before it unlinks.
                successors.append(
                    await asyncio.start_unix_server(
                        lambda _reader, writer: writer.close(), path=str(socket_path)
                    )
                )
                get_runtime_path(store).write_text(
                    json.dumps(successor_metadata.to_dict()), encoding="utf-8"
                )

        self.session_server._server = SuccessorBindsWhileClosing()
        try:
            await self.session_server._shutdown()

            self.assertTrue(self.socket_path.exists())
            self.assertEqual(load_runtime_metadata(self.store).run_id, "run-456")
        finally:
            for successor in successors:
                successor.close()
                await successor.wait_closed()

    async def test_shutdown_removes_its_own_socket_and_daemon_json(self):
        await self.session_server._shutdown()

        self.assertFalse(self.socket_path.exists())
        self.assertFalse(get_runtime_path(self.store).exists())

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

        with patch.object(server, "_start_daemon_locked") as start_daemon_locked:
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

        with patch.object(server, "_start_daemon_locked") as start_daemon_locked:
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
                server, "_take_start_lock", side_effect=OSError("lock failed")
            ),
            patch.object(server, "_start_daemon_locked") as start_daemon_locked,
        ):
            await self.session_server._recycle()

        # A recycle that never got the lock must still leave no live socket or
        # daemon.json behind, and the reason has to be readable somewhere.
        self.assertFalse(self.socket_path.exists())
        self.assertFalse(get_runtime_path(self.store).exists())
        start_daemon_locked.assert_not_called()
        log_text = (self.store.base_dir / daemon.DAEMON_LOG_FILE_NAME).read_text(
            encoding="utf-8"
        )
        self.assertIn("recycle failed: lock failed", log_text)

    async def test_recycle_sweeps_the_runtime_dir_after_the_successor_starts(self):
        self.store.prepare()
        self.fake_runner.is_recycle_requested = True

        with (
            patch.object(server, "_start_daemon_locked"),
            patch.object(server, "_remove_stale_runtime_files") as sweep,
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
            # daemon.json still names this daemon, so the socket is its own.
            session_server._write_runtime_metadata()

            await session_server._shutdown()

        self.assertEqual(order, ["unlink", "close", "wait_closed"])


class CrashCleanupTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_a_crash_cleans_up_like_a_stop_and_never_recycles(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = Path(temporary_directory)
            store = Store(base_path / "state")
            store.prepare()
            socket_path = base_path / "daemon.sock"
            session_server = SessionServer(
                RunConfig("codex", "review"),
                store,
                str(Path.cwd().resolve()),
                socket_path,
                "run-123",
            )
            # An agent that ignores TERM, as daemon.json would record it.
            agent = subprocess.Popen(
                ["bash", "-c", "trap '' TERM; sleep 30"], start_new_session=True
            )
            self.addCleanup(kill_agent, agent)
            session_server.agent_groups[agent.pid] = daemon.get_process_start_ticks(
                agent.pid
            )

            async def crash():
                session_server.runner.is_recycle_requested = True
                raise RuntimeError("runner exploded")

            with (
                patch.object(session_server.runner, "run_forever", side_effect=crash),
                patch.object(server, "TERMINATE_GRACE_SEC", 0.2),
                patch.object(server, "_start_daemon_locked") as start_daemon_locked,
            ):
                await asyncio.wait_for(session_server.run(), timeout=10)

            self.assertFalse(socket_path.exists())
            self.assertFalse(get_runtime_path(store).exists())
            log_text = (store.base_dir / daemon.DAEMON_LOG_FILE_NAME).read_text(
                encoding="utf-8"
            )
            self.assertIn("daemon crashed", log_text)
            self.assertIn("runner exploded", log_text)
            start_daemon_locked.assert_not_called()
            # The TERM was ignored, so only the SIGKILL after the grace ends it.
            self.assertEqual(agent.wait(timeout=2), -9)


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


if __name__ == "__main__":
    unittest.main()
