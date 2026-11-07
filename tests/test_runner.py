"""Core queue behaviour with no network or real AI agent."""

import asyncio
import itertools
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_review_pr_dashboard import gate, runner
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.discover import (
    DiscoveredPr,
    DiscoverError,
    parse_discover_output,
)
from auto_review_pr_dashboard.models import (
    LoopRecord,
    PrItem,
    PrState,
    SkipReason,
    Verdict,
)
from auto_review_pr_dashboard.store import Store

HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
OLD_HEAD = "9999999999999999999999999999999999999999"
MOCK_CLI = """#!/usr/bin/env python3
import os, sys, time
print(sys.argv[-1], flush=True)
time.sleep(float(os.environ.get("MOCK_SLEEP", "0")))
print("[mock-ai] done", flush=True)
"""


def make_item(number, repo="o/r", state=PrState.QUEUED):
    return PrItem(
        repo,
        number,
        f"https://github.com/{repo}/pull/{number}",
        f"pr {number}",
        "me",
        HEAD,
        state=state,
    )


def make_found(number, skip_reason=None, matched_axes=("author:me",)):
    return DiscoveredPr(
        "o/r",
        number,
        f"https://github.com/o/r/pull/{number}",
        OLD_HEAD,
        skip_reason,
        list(matched_axes),
    )


def make_snapshot(marker_sha=None, title="pr title"):
    return gate.GateSnapshot(title, "me", HEAD, marker_sha)


def kill_group(pgid):
    """Cleanup for a test's own process group, whether or not it is still there."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class FakeLogFile:
    """Log stand-in that fails like a full disk."""

    def __init__(self, fail_writes_after=None, fail_close=False):
        self.text = []
        self.fail_writes_after = fail_writes_after
        self.fail_close = fail_close
        self.writes = 0

    def _raise_if_full(self):
        if self.fail_writes_after is not None and self.writes > self.fail_writes_after:
            raise OSError(28, "No space left on device")

    def write(self, text):
        self.writes += 1
        self._raise_if_full()
        self.text.append(text)

    def flush(self):
        self._raise_if_full()

    def close(self):
        if self.fail_close:
            raise OSError(28, "No space left on device")


class FakeTime:
    """Stands in for the module-level `time` so patched sleeps move the clock."""

    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.environ.pop, "MOCK_SLEEP", None)
        self.mock_cli = Path(tmp.name) / "mock-ai"
        self.mock_cli.write_text(MOCK_CLI)
        self.mock_cli.chmod(0o755)
        self.store = Store(Path(tmp.name) / "state")
        self.events = []
        # When set, the agent is this bash script instead of the mock CLI.
        self.agent_script = None

    def make_runner(self, items, recycle_every_loops=None, **overrides):
        config = RunConfig("codex-test-stub", "review", repos=["o/r"], **overrides)
        self.items = items
        return runner.LoopRunner(
            config,
            self.store,
            lambda name, *args: self.events.append((name, args)),
            recycle_every_loops=recycle_every_loops,
        )

    async def run_loop(self, loop_runner, agent_runs=None, snapshots=None):
        self.command_calls = []

        def command(*args, **kwargs):
            self.command_calls.append((args, kwargs))
            if self.agent_script is not None:
                # bash -c, not -ic: an interactive bash would load ~/.bashrc
                return ["bash", "-c", self.agent_script]
            return [str(self.mock_cli), "--flag", args[-1]]

        spawn_patch = (
            nullcontext()
            if agent_runs is None
            else patch.object(loop_runner, "_spawn_agent", side_effect=agent_runs)
        )
        found = [
            DiscoveredPr(item.repo, item.number, item.url, item.head_sha)
            for item in self.items
        ]
        # A marker on the head is how a PR comes out as a Skip.
        gate_snapshots = {
            item.url: gate.GateSnapshot(
                item.title,
                item.author,
                item.head_sha,
                item.head_sha if item.state is PrState.SKIP else None,
            )
            for item in self.items
        }
        with (
            patch.object(runner, "discover_prs", return_value=found),
            patch.object(
                gate, "get_gate_snapshot", side_effect=gate_snapshots.__getitem__
            ),
            patch.object(
                gate, "snapshot_comments", side_effect=snapshots or (lambda *a: {})
            ),
            patch.object(
                runner.asyncio, "to_thread", side_effect=lambda fn, *a: fn(*a)
            ),
            patch.object(runner, "get_ai_command", side_effect=command),
            spawn_patch,
        ):
            return await loop_runner.run_loop()

    def agent_pgids(self):
        return [args[0] for name, args in self.events if name == "agent_started"]

    async def wait_for_agents(self, count=1):
        while len(self.agent_pgids()) < count:
            await asyncio.sleep(0.01)

    async def assert_group_dead(self, pgid, within=3.0):
        deadline = time.monotonic() + within
        while runner.get_is_group_alive(pgid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(runner.get_is_group_alive(pgid), f"group {pgid} still alive")

    def patch_log_open(self, fake_log):
        # runner calls the builtin `open`; a module global shadows it
        return patch.object(runner, "open", create=True, return_value=fake_log)

    async def test_timeout_kills_group_of_term_ignoring_agent(self):
        # bash shrugs off TERM, but the child sleep must die with the group
        self.agent_script = 'trap "" TERM; sleep 30 & wait'
        loop_runner = self.make_runner([make_item(1)], pr_timeout_min=1 / 60)
        started = time.monotonic()
        record = await self.run_loop(loop_runner)
        elapsed = time.monotonic() - started

        self.assertIs(record.items[0].state, PrState.FAILED)
        self.assertIn("timed out", record.items[0].error)
        # the grace period ends in SIGKILL: without it this waits out the sleep
        self.assertLess(elapsed, 12)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_agent_that_closes_stdout_still_times_out(self):
        # the timeout belongs to the process, not to its output
        self.agent_script = "exec 1>&-; sleep 5"
        loop_runner = self.make_runner([make_item(1)], pr_timeout_min=1 / 60)
        record = await self.run_loop(loop_runner)

        self.assertIs(record.items[0].state, PrState.FAILED)
        self.assertIn("timed out", record.items[0].error)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_detached_child_does_not_delay_completion(self):
        # the child holds the pipe open; the leader's exit still settles the PR
        self.agent_script = "sleep 9 & exit 0"
        started = time.monotonic()
        record = await self.run_loop(self.make_runner([make_item(1)]))

        self.assertIs(record.items[0].state, PrState.DONE)
        self.assertLess(time.monotonic() - started, 4)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_detached_child_on_closed_pipe_leaves_no_group(self):
        # the child holds no pipe, so nothing blocks the pump: only an
        # unconditional SIGKILL at the end of the drain clears it
        self.agent_script = "sleep 9 >/dev/null 2>&1 & exit 0"
        started = time.monotonic()
        record = await self.run_loop(self.make_runner([make_item(1)]))

        self.assertIs(record.items[0].state, PrState.DONE)
        self.assertLess(time.monotonic() - started, 4)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_cancel_running_agent_kills_group_quickly(self):
        self.agent_script = "sleep 30"
        loop_runner = self.make_runner([make_item(1)])
        task = asyncio.create_task(self.run_loop(loop_runner))
        await self.wait_for_agents()
        started = time.monotonic()
        self.assertTrue(loop_runner.cancel("o/r#1"))
        record = await task

        self.assertIs(record.items[0].state, PrState.CANCELLED)
        self.assertLess(time.monotonic() - started, 3)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_cancel_in_the_post_run_window_is_refused(self):
        loop_runner = self.make_runner([make_item(1)])
        snapshot_calls = []
        results = []

        def snapshots(repo, number):
            snapshot_calls.append(number)
            if len(snapshot_calls) == 2:
                # The post-run snapshot: the agent is gone already.
                results.append(loop_runner.cancel(f"{repo}#{number}"))
            return {}

        record = await self.run_loop(loop_runner, snapshots=snapshots)

        self.assertEqual(results, [False])
        # the normal decision stands, unchanged by the refused cancel
        self.assertIs(record.items[0].state, PrState.DONE)
        self.assertEqual(record.items[0].error, "")

    async def test_cancel_while_the_output_drains_is_refused(self):
        # the leader exits at once; the child keeps the pipe open for a while
        self.agent_script = "(sleep 1; echo late) & exit 0"
        loop_runner = self.make_runner([make_item(1)])
        task = asyncio.create_task(self.run_loop(loop_runner))
        await self.wait_for_agents()
        while loop_runner._processes["o/r#1"].returncode is None:
            await asyncio.sleep(0.01)

        self.assertFalse(loop_runner.cancel("o/r#1"))
        record = await task
        self.assertIs(record.items[0].state, PrState.DONE)

    async def test_cancelled_agent_task_kills_the_group_at_once(self):
        # TERM is ignored, so only the guard's synchronous SIGKILL ends it
        loop_runner = self.make_runner([])
        task = asyncio.create_task(
            loop_runner._spawn_agent(
                ["bash", "-c", "trap '' TERM; sleep 30"],
                self.mock_cli.parent / "agent.log",
                60,
                "o/r#1",
            )
        )
        await self.wait_for_agents()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_group_dead(self.agent_pgids()[0], within=1.0)

    async def test_a_requeued_pr_starts_without_its_last_finish_time(self):
        loop_runner = self.make_runner([make_item(1)], cooldown_min=1 / 600)
        runs = iter((runner.AgentRun(1, ["usage limit"]), runner.AgentRun(0)))
        seen = []

        async def spawn(*args, **kwargs):
            item = loop_runner.record.item_by_key("o/r#1")
            seen.append((item.finished_at, item.exit_code))
            return next(runs)

        record = await self.run_loop(loop_runner, spawn)

        # the cooldown put it back after a finished run; this run starts clean
        self.assertEqual(seen, [(None, None), (None, None)])
        self.assertIs(record.items[0].state, PrState.DONE)

    async def test_stop_process_waits_for_the_whole_group(self):
        # The leader dies on TERM; its child ignores TERM and holds no pipe, so
        # the leader's exit says nothing about the group.
        loop_runner = self.make_runner([])
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            "(trap '' TERM; echo ready; exec sleep 30 >/dev/null 2>&1) & wait",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(kill_group, process.pid)
        await process.stdout.readline()  # the child ignores TERM from here on

        with patch.object(runner, "TERMINATE_GRACE_SEC", 0.5):
            await loop_runner._stop_process(process)

        # SIGKILL lands and the child is reaped asynchronously, so allow a
        # moment; a child left alive would sleep on for 30s.
        await self.assert_group_dead(process.pid, within=1.0)
        await process.wait()

    async def test_agent_started_event_error_leaves_no_group(self):
        self.agent_script = "sleep 30"
        loop_runner = self.make_runner([make_item(1)])

        def broken_listener(name, *args):
            self.events.append((name, args))
            if name == "agent_started":
                raise RuntimeError("ui is broken")

        loop_runner.on_event = broken_listener
        with self.assertRaises(RuntimeError):
            await self.run_loop(loop_runner)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_cancel_run_loop_task_leaves_no_group(self):
        self.agent_script = "sleep 30"
        loop_runner = self.make_runner([make_item(1)])
        task = asyncio.create_task(self.run_loop(loop_runner))
        await self.wait_for_agents()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_cancel_race_skips_spawn(self):
        loop_runner = self.make_runner([])
        item = make_item(1)
        loop_runner.record = LoopRecord(index=1, started_at=time.time(), items=[item])
        loop_runner._cancelled_keys.add("o/r#1")
        with (
            patch.object(gate, "snapshot_comments", return_value={}),
            patch.object(loop_runner, "_spawn_agent", AsyncMock()) as spawn_agent,
        ):
            await loop_runner._run_item(item)

        self.assertIs(item.state, PrState.CANCELLED)
        self.assertIsNone(item.started_at)
        spawn_agent.assert_not_called()
        self.assertIn("item_update", [name for name, _ in self.events])

    async def test_cancel_during_the_pre_run_snapshot_spawns_nothing(self):
        loop_runner = self.make_runner([make_item(1), make_item(2)])
        snapshot_started = asyncio.Event()
        release_snapshot = asyncio.Event()
        snapshot_keys = []

        async def held_snapshot(item):
            snapshot_keys.append(item.key)
            if len(snapshot_keys) == 1:
                # PR 1's pre-run snapshot: hold it until the cancel is in.
                snapshot_started.set()
                await release_snapshot.wait()
            return {}

        with patch.object(loop_runner, "_snapshot", new=held_snapshot):
            task = asyncio.create_task(self.run_loop(loop_runner))
            await snapshot_started.wait()
            self.assertTrue(loop_runner.cancel("o/r#1"))
            release_snapshot.set()
            record = await task

        first, second = record.items
        self.assertIs(first.state, PrState.CANCELLED)
        self.assertEqual(first.error, "cancelled by user")
        self.assertFalse(Path(first.log_path).exists())
        # Only PR 2 ever got an agent.
        self.assertEqual([args[3] for args, _ in self.command_calls], [second.url])
        self.assertEqual(len(self.agent_pgids()), 1)
        self.assertEqual(loop_runner._running_keys, [])
        self.assertIsNone(loop_runner.current_key)
        self.assertIs(second.state, PrState.DONE)

    async def test_three_way_cancel_race_parallel_two(self):
        self.agent_script = "sleep 30"
        loop_runner = self.make_runner(
            [make_item(number) for number in (1, 2, 3)], max_parallel=2
        )
        task = asyncio.create_task(self.run_loop(loop_runner))
        await self.wait_for_agents(2)  # #1 #2 running, #3 queued
        started = time.monotonic()
        for key in ("o/r#2", "o/r#3", "o/r#1"):
            self.assertTrue(loop_runner.cancel(key))
        record = await task

        self.assertEqual(
            [item.state for item in record.items], [PrState.CANCELLED] * 3
        )
        self.assertLess(time.monotonic() - started, 4)
        for pgid in self.agent_pgids():
            await self.assert_group_dead(pgid)

    async def test_loop_index_continues_after_restart(self):
        self.store.save_state(RunConfig("codex-test-stub", "review"), 41)
        loop_runner = self.make_runner([])
        self.assertEqual(loop_runner.loop_index, 41)

        record = await self.run_loop(loop_runner)

        self.assertEqual(record.index, 42)
        self.assertTrue((self.store.loops_dir / "loop-0042.json").is_file())

    async def test_full_log_fails_only_that_pr(self):
        loop_runner = self.make_runner([make_item(1), make_item(2)])
        real_get_log_path = self.store.get_log_path

        def log_path_for(loop_index, item):
            if item.number == 1:
                return Path("/dev/full")
            return real_get_log_path(loop_index, item)

        with patch.object(self.store, "get_log_path", side_effect=log_path_for):
            record = await self.run_loop(loop_runner)

        first, second = record.items
        self.assertIs(first.state, PrState.FAILED)
        self.assertIn("log", first.error)
        self.assertIs(second.state, PrState.DONE)
        self.assertIn("loop_end", [name for name, _ in self.events])
        for pgid in self.agent_pgids():
            await self.assert_group_dead(pgid)

    async def test_log_write_failure_after_clean_exit_is_not_done(self):
        self.agent_script = "echo hi; exit 0"
        with self.patch_log_open(FakeLogFile(fail_writes_after=1)):
            item = (await self.run_loop(self.make_runner([make_item(1)]))).items[0]

        self.assertIs(item.state, PrState.FAILED)
        self.assertIn("writing log failed", item.error)
        self.assertNotEqual(item.exit_code, 0)

    async def test_log_write_failure_during_the_drain_is_not_done(self):
        # the leader exits 0 first; the child writes later, so the log only
        # breaks while the output is being drained
        self.agent_script = "(sleep 0.3; echo late) & exit 0"
        with self.patch_log_open(FakeLogFile(fail_writes_after=1)):
            item = (await self.run_loop(self.make_runner([make_item(1)]))).items[0]

        self.assertIs(item.state, PrState.FAILED)
        self.assertIn("writing log failed", item.error)
        self.assertNotEqual(item.exit_code, 0)
        await self.assert_group_dead(self.agent_pgids()[0])

    async def test_log_close_failure_after_clean_exit_is_not_done(self):
        self.agent_script = "exit 0"
        with self.patch_log_open(FakeLogFile(fail_close=True)):
            item = (await self.run_loop(self.make_runner([make_item(1)]))).items[0]

        self.assertIs(item.state, PrState.FAILED)
        self.assertIn("closing the log failed", item.error)
        self.assertNotEqual(item.exit_code, 0)

    async def test_discovery_failure_is_a_loop_level_error(self):
        for failure in (
            DiscoverError("pr-discover exited 1: boom"),
            gate.GhError("gh not found on PATH"),
        ):
            with self.subTest(failure=failure):
                self.events.clear()
                loop_runner = self.make_runner([])
                with patch.object(runner, "discover_prs", side_effect=failure):
                    record = await loop_runner.run_loop()

                self.assertEqual(record.items, [])
                self.assertIn(("error", (f"discovery failed: {failure}",)), self.events)
                self.assertIn("loop_end", [name for name, _ in self.events])

    async def test_prompt_is_forwarded_to_each_agent(self):
        loop_runner = self.make_runner([make_item(1)], ai_cli_args=["--yolo"])

        await self.run_loop(loop_runner)

        self.assertEqual(
            self.command_calls,
            [
                (
                    (
                        "codex-test-stub",
                        "review",
                        "o/r",
                        "https://github.com/o/r/pull/1",
                    ),
                    {"ai_cli_args": ["--yolo"]},
                )
            ],
        )

    async def run_on_fake_clock(self, loop_runner, agent_runs, actions=None):
        """Run `run_loop` on a clock that only moves when the runner sleeps.

        `actions` maps a sleep count to a callable fired right after that sleep.
        Every sleep is recorded as (paused, blocked_until, blocked ticks so far),
        which is what a paused client would have seen at that moment.
        """
        clock = FakeTime()
        actions = actions or {}
        observations = []
        sleeps = 0

        async def fake_sleep(seconds):
            nonlocal sleeps
            clock.now += seconds
            sleeps += 1
            if sleeps > 200:
                raise AssertionError("the wait never finished")
            action = actions.get(sleeps)
            if action is not None:
                action()
            observations.append(
                (
                    loop_runner.paused,
                    loop_runner.blocked_until,
                    sum(1 for name, _ in self.events if name == "blocked_tick"),
                )
            )

        with (
            patch.object(runner, "time", clock),
            patch.object(runner.asyncio, "sleep", fake_sleep),
        ):
            record = await self.run_loop(loop_runner, agent_runs)
        return record, observations

    async def test_happy_path_runs_queue_and_persists_progress_and_logs(self):
        record = await self.run_loop(
            self.make_runner(
                [make_item(1), make_item(2, state=PrState.SKIP), make_item(3)]
            )
        )
        self.assertEqual(
            [item.state for item in record.items],
            [PrState.DONE, PrState.SKIP, PrState.DONE],
        )
        self.assertIsNone(record.items[1].started_at)
        self.assertEqual(runner.get_progress(record), (3, 3))
        self.assertEqual(record.repo_progress(), {"o/r": (3, 3)})
        self.assertTrue((self.store.loops_dir / "loop-0001.json").is_file())
        for item in (record.items[0], record.items[2]):
            log = Path(item.log_path).read_text()
            self.assertIn("[mock-ai] done", log)
            self.assertIn(item.url, log)

    async def test_long_output_line_is_logged_in_full_and_shown_truncated(self):
        long_cli = self.mock_cli.parent / "long-line-ai"
        long_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            'sys.stdout.write("x" * 200_000 + "\\n")\n'
            'sys.stdout.write("[mock-ai] done\\n")\n'
        )
        long_cli.chmod(0o755)
        self.mock_cli = long_cli

        item = (await self.run_loop(self.make_runner([make_item(1)]))).items[0]

        # Past asyncio's readline limit, so this used to raise and kill the run.
        self.assertIs(item.state, PrState.DONE)
        # Every character survives; only the line is wrapped, past a chunk's worth.
        # Count the agent output only: the "$ <command>" header line holds a random
        # temp path, which can carry an "x" of its own.
        body = Path(item.log_path).read_text().split("\n", 1)[1]
        self.assertEqual(body.count("x"), 200_000)
        shown = [args[1] for name, args in self.events if name == "log"]
        self.assertIn("[mock-ai] done", shown)
        self.assertTrue(any(line.endswith("chars)") for line in shown))
        self.assertLess(max(len(line) for line in shown), 2 * runner.MAX_UI_LINE_CHARS)

    def test_prune_logs_keeps_only_the_newest_loop_dirs(self):
        self.store.prepare()
        for index in range(1, 16):
            (self.store.logs_dir / f"loop-{index:04d}").mkdir()
        (self.store.logs_dir / "loop-notes").mkdir()

        self.store.prune_logs()

        kept = sorted(path.name for path in self.store.logs_dir.iterdir())
        expected = [f"loop-{index:04d}" for index in range(6, 16)]
        self.assertEqual(kept, expected + ["loop-notes"])

    def test_log_path_is_safe_for_normal_and_separator_heavy_repos(self):
        loop_dir = self.store.logs_dir / "loop-0001"
        for repo in ("o/r", "https://github.com/o/r"):
            path = self.store.get_log_path(1, make_item(7, repo))
            self.assertEqual(path.parent, loop_dir)
            self.assertNotIn("/", path.name)
        self.assertEqual(self.store.get_log_path(1, make_item(7)).name, "o__r__7.log")

    async def test_failed_agent_handles_non_zero_exit_and_missing_command(self):
        cases = [
            (runner.AgentRun(3), 3, ""),
            (runner.AgentRun(127, error="not found"), 127, "not found"),
        ]
        for agent_run, code, error in cases:
            record = await self.run_loop(self.make_runner([make_item(1)]), [agent_run])
            self.assertIs(record.items[0].state, PrState.FAILED)
            self.assertEqual(record.items[0].exit_code, code)
            self.assertIn(error, record.items[0].error)

    async def test_timeout_stops_process_and_marks_failed(self):
        os.environ["MOCK_SLEEP"] = "30"
        loop_runner = self.make_runner([make_item(1)], pr_timeout_min=1 / 120)
        started_at = time.monotonic()
        item = (await self.run_loop(loop_runner)).items[0]
        self.assertLess(time.monotonic() - started_at, 2)
        self.assertIs(item.state, PrState.FAILED)
        self.assertIn("timed out", item.error)
        self.assertEqual(loop_runner._processes, {})

    async def test_queued_cancel_resume_moves_item_to_tail_and_preserves_order(self):
        loop_runner = self.make_runner([make_item(1), make_item(2), make_item(3)])
        order = []

        async def spawn(*args, **kwargs):
            order.append(kwargs["log_key"])
            if kwargs["log_key"] == "o/r#1":
                loop_runner.cancel("o/r#2")
                item = loop_runner.record.item_by_key("o/r#2")
                self.assertIs(item.state, PrState.CANCELLED)
                self.assertEqual(list(loop_runner.queue), ["o/r#3"])
                self.assertIsNone(item.started_at)
                loop_runner.resume("o/r#2")
                self.assertIs(item.state, PrState.QUEUED)
                self.assertEqual(list(loop_runner.queue), ["o/r#3", "o/r#2"])
            return runner.AgentRun(0)

        record = await self.run_loop(loop_runner, spawn)
        self.assertEqual(order, ["o/r#1", "o/r#3", "o/r#2"])
        self.assertIs(record.item_by_key("o/r#2").state, PrState.DONE)

    async def test_running_cancel_kills_process_and_continues_next_pr(self):
        os.environ["MOCK_SLEEP"] = "30"
        loop_runner = self.make_runner([make_item(1), make_item(2)])

        async def cancel():
            while not loop_runner._processes:
                await asyncio.sleep(0.01)
            os.environ["MOCK_SLEEP"] = "0"
            loop_runner.cancel("o/r#1")

        task = asyncio.create_task(cancel())
        record = await self.run_loop(loop_runner)
        await task
        self.assertIs(record.items[0].state, PrState.CANCELLED)
        self.assertIsNotNone(record.items[0].started_at)
        self.assertIs(record.items[1].state, PrState.DONE)

    async def test_quota_retries_same_pr_first_and_emits_block_events(self):
        loop_runner = self.make_runner(
            [make_item(1), make_item(2)], cooldown_min=1 / 600
        )
        runs = iter(
            (
                runner.AgentRun(1, ["usage limit"]),
                runner.AgentRun(0),
                runner.AgentRun(0),
            )
        )
        order = []

        async def spawn(*args, **kwargs):
            order.append(kwargs["log_key"])
            return next(runs)

        record = await self.run_loop(loop_runner, spawn)
        first = record.items[0]
        self.assertEqual((first.state, first.block_attempts), (PrState.DONE, 1))
        self.assertEqual(order, ["o/r#1", "o/r#1", "o/r#2"])
        self.assertIn("usage limit", record.block_events[0]["reason"])
        names = [name for name, _ in self.events]
        self.assertTrue({"blocked", "blocked_tick", "unblocked"} <= set(names))
        self.assertIsNone(loop_runner.blocked_until)

    async def test_pause_during_cooldown_freezes_it_and_holds_the_queue(self):
        loop_runner = self.make_runner([make_item(1)], cooldown_min=1 / 60)
        clock = FakeTime()
        # Pauses after the first cooldown tick, resumes after five of them.
        pause_after, resume_after = 1, 6
        sleeps = 0
        started_paused = []
        held_deadlines = []

        async def spawn(*args, **kwargs):
            started_paused.append(loop_runner.paused)
            if len(started_paused) == 1:
                return runner.AgentRun(1, ["usage limit"])
            return runner.AgentRun(0)

        async def fake_sleep(seconds):
            nonlocal sleeps
            clock.now += seconds
            sleeps += 1
            if sleeps > 200:
                raise AssertionError("the cooldown wait never finished")
            if sleeps in (pause_after, resume_after):
                loop_runner.toggle_pause()
            if loop_runner.paused and loop_runner.blocked_until is not None:
                held_deadlines.append(loop_runner.blocked_until)

        with (
            patch.object(runner, "time", clock),
            patch.object(runner.asyncio, "sleep", fake_sleep),
        ):
            record = await self.run_loop(loop_runner, spawn)

        # Nothing may start while paused; the retry waits for the resume.
        self.assertEqual(started_paused, [False, False])
        # The cooldown held still instead of counting down to zero.
        self.assertGreater(len(held_deadlines), 1)
        self.assertEqual(
            [round(value - held_deadlines[0], 6) for value in held_deadlines],
            list(range(len(held_deadlines))),
        )
        remaining = [args[0] for name, args in self.events if name == "blocked_tick"]
        # Frozen at the configured cooldown: a pause must not invent a second.
        self.assertEqual(
            {round(value, 6) for value in remaining},
            {round(loop_runner.config.cooldown_min * 60, 6)},
        )
        self.assertIs(record.items[0].state, PrState.DONE)

    async def test_cooldown_starting_while_paused_freezes_it_and_holds_the_queue(self):
        loop_runner = self.make_runner([make_item(1)], cooldown_min=5 / 60)
        started_paused = []

        async def spawn(*args, **kwargs):
            started_paused.append(loop_runner.paused)
            if len(started_paused) == 1:
                # `p` pressed while the agent ran, and that agent then fails.
                loop_runner.toggle_pause()
                return runner.AgentRun(1, ["usage limit"])
            return runner.AgentRun(0)

        record, seen = await self.run_on_fake_clock(
            loop_runner, spawn, {4: loop_runner.toggle_pause}
        )

        # The cooldown froze even though the pause came first: the deadline moved
        # out one second per paused second instead of counting down.
        held = [blocked for paused, blocked, _ in seen if paused and blocked]
        self.assertGreater(len(held), 2)
        self.assertEqual(
            [round(value - held[0], 6) for value in held], list(range(len(held)))
        )
        # A paused client got ticks, and they showed one frozen countdown value.
        ticks_before_resume = seen[3][2]
        self.assertGreaterEqual(ticks_before_resume, 3)
        remaining = [args[0] for name, args in self.events if name == "blocked_tick"]
        # The frozen countdown is the configured cooldown, not one second more.
        self.assertEqual(
            {round(value, 6) for value in remaining[:ticks_before_resume]},
            {round(loop_runner.config.cooldown_min * 60, 6)},
        )
        # Nothing started while paused; the retry ran after the resume.
        self.assertEqual(started_paused, [False, False])
        self.assertIs(record.items[0].state, PrState.DONE)

    async def test_run_now_during_a_paused_cooldown_still_waits_for_the_resume(self):
        loop_runner = self.make_runner([make_item(1)], cooldown_min=30 / 60)
        started_paused = []

        async def spawn(*args, **kwargs):
            started_paused.append(loop_runner.paused)
            if len(started_paused) == 1:
                loop_runner.toggle_pause()
                return runner.AgentRun(1, ["usage limit"])
            return runner.AgentRun(0)

        # `n` skips the cooldown, but the pause keeps holding the queue.
        record, _seen = await self.run_on_fake_clock(
            loop_runner, spawn, {2: loop_runner.run_now, 5: loop_runner.toggle_pause}
        )

        self.assertEqual(started_paused, [False, False])
        self.assertIsNone(loop_runner.blocked_until)
        self.assertIs(record.items[0].state, PrState.DONE)

    async def test_stop_ends_a_paused_cooldown_wait_without_starting_work(self):
        loop_runner = self.make_runner([make_item(1)], cooldown_min=30 / 60)
        started_paused = []

        async def spawn(*args, **kwargs):
            started_paused.append(loop_runner.paused)
            loop_runner.toggle_pause()
            return runner.AgentRun(1, ["usage limit"])

        _record, _seen = await self.run_on_fake_clock(
            loop_runner, spawn, {2: loop_runner.stop}
        )

        # `q` during a paused cooldown: the wait is over and nothing was retried.
        self.assertEqual(started_paused, [False])
        self.assertTrue(loop_runner._stopped)
        self.assertIsNone(loop_runner.blocked_until)

    async def test_recycle_every_loops_returns_after_the_nth_loop(self):
        loop_runner = self.make_runner([make_item(1)], recycle_every_loops=2)
        idles = []

        async def fake_idle():
            idles.append(loop_runner.loop_index)

        async def fake_run_loop():
            loop_runner.loop_index += 1

        with (
            patch.object(loop_runner, "_idle_until_next_loop", fake_idle),
            patch.object(loop_runner, "run_loop", fake_run_loop),
        ):
            await loop_runner.run_forever()

        # Loop 2 hands over instead of idling, so the daemon can restart itself.
        self.assertTrue(loop_runner.is_recycle_requested)
        self.assertEqual(loop_runner.loop_index, 2)
        self.assertEqual(idles, [1])

    async def test_a_handed_over_countdown_idles_before_the_first_loop(self):
        loop_runner = self.make_runner([make_item(1)])
        loop_runner.next_run_at = 1_500.0
        order = []

        async def fake_idle():
            order.append("idle")
            loop_runner.next_run_at = None

        async def fake_run_loop():
            order.append("loop")
            loop_runner.loop_index += 1
            loop_runner._stopped = True

        with (
            patch.object(loop_runner, "_idle_until_next_loop", fake_idle),
            patch.object(loop_runner, "run_loop", fake_run_loop),
        ):
            await loop_runner.run_forever()

        # The successor picks the countdown up rather than running a loop at once.
        self.assertEqual(order, ["idle", "loop"])

    async def test_idle_keeps_a_deadline_it_was_handed(self):
        loop_runner = self.make_runner([], interval_min=60)
        loop_runner.next_run_at = 1_030.0
        clock = FakeTime()

        async def fake_sleep(seconds):
            clock.now += seconds

        with (
            patch.object(runner, "time", clock),
            patch.object(runner.asyncio, "sleep", fake_sleep),
        ):
            await loop_runner._idle_until_next_loop()

        # A fresh idle would have replaced this with a full interval from now.
        self.assertEqual(
            [args[0] for name, args in self.events if name == "idle_start"],
            [1_030.0],
        )
        self.assertIsNone(loop_runner.next_run_at)

    async def test_stop_kills_an_agent_that_ignores_sigterm(self):
        stubborn_cli = self.mock_cli.parent / "stubborn-ai"
        stubborn_cli.write_text("#!/usr/bin/env bash\ntrap '' TERM\nsleep 30\n")
        stubborn_cli.chmod(0o755)
        self.mock_cli = stubborn_cli
        loop_runner = self.make_runner([make_item(1)])
        agent_pids = []

        async def stop_once_started():
            while not loop_runner._processes:
                await asyncio.sleep(0.01)
            agent_pids.append(loop_runner._processes["o/r#1"].pid)
            loop_runner.stop()

        started_at = time.monotonic()
        with patch.object(runner, "TERMINATE_GRACE_SEC", 0.2):
            stop_task = asyncio.create_task(stop_once_started())
            record = await self.run_loop(loop_runner)
            await stop_task

        # SIGTERM is trapped, so the grace period has to end in SIGKILL.
        self.assertLess(time.monotonic() - started_at, 5)
        self.assertIs(record.items[0].state, PrState.FAILED)
        with self.assertRaises(ProcessLookupError):
            os.kill(agent_pids[0], 0)

    async def test_repeated_stop_escalates_the_running_agent_only_once(self):
        loop_runner = self.make_runner([make_item(1)])
        os.environ["MOCK_SLEEP"] = "0.5"

        async def stop_three_times_once_started():
            while not loop_runner._processes:
                await asyncio.sleep(0.01)
            # The paths that double up in practice: SIGTERM handler plus "stop"
            # command, or cancel() followed by stop().
            loop_runner.cancel(loop_runner.current_key)
            loop_runner.stop()
            loop_runner.stop()

        with patch.object(loop_runner, "_stop_process", AsyncMock()) as stop_process:
            stop_task = asyncio.create_task(stop_three_times_once_started())
            await self.run_loop(loop_runner)
            await stop_task

        # One process, one escalation task: the guard, not a pile of SIGTERMs.
        self.assertEqual(stop_process.await_count, 1)

    async def test_agent_started_and_finished_events_bracket_the_agent(self):
        loop_runner = self.make_runner([make_item(1)])

        await self.run_loop(loop_runner)

        names = [name for name, _ in self.events]
        started = [args for name, args in self.events if name == "agent_started"]
        self.assertEqual(len(started), 1)
        self.assertGreater(started[0][0], 0)
        self.assertLess(names.index("agent_started"), names.index("agent_finished"))
        finished = [args for name, args in self.events if name == "agent_finished"]
        # The daemon tracks several groups, so the finish names its pid.
        self.assertEqual(finished, started)

        # A command that never execs has no process for the daemon to record.
        self.mock_cli = self.mock_cli.parent / "missing-ai"
        self.events.clear()

        await self.run_loop(self.make_runner([make_item(1)]))

        self.assertNotIn("agent_started", [name for name, _ in self.events])
        self.assertNotIn("agent_finished", [name for name, _ in self.events])

    async def test_two_silent_failures_trip_breaker(self):
        items = [make_item(1), make_item(2)]
        loop_runner = self.make_runner(items, cooldown_min=0)
        runs = iter((runner.AgentRun(3), runner.AgentRun(3), runner.AgentRun(0)))
        record = await self.run_loop(loop_runner, runs)
        first, second = record.items
        self.assertIs(first.state, PrState.FAILED)
        self.assertEqual((second.state, second.block_attempts), (PrState.DONE, 1))
        self.assertIn("2 failures", record.block_events[0]["reason"])

    def test_failure_after_posting_is_not_requeued(self):
        item = make_item(1, state=PrState.RUNNING)
        item.exit_code = 1
        item.new_posts = [object()]
        loop_runner = self.make_runner([item])
        loop_runner.record = LoopRecord(1, 0, items=[item])
        loop_runner._handle_failure(item, ["usage limit"], False, "")
        self.assertIs(item.state, PrState.FAILED)
        self.assertEqual(loop_runner.record.block_events, [])
        self.assertEqual(list(loop_runner.queue), [])

    async def test_three_blocks_abandon_loop(self):
        loop_runner = self.make_runner(
            [make_item(1), make_item(2), make_item(3)], cooldown_min=0
        )
        failed = runner.AgentRun(1, ["usage limit"])
        record = await self.run_loop(loop_runner, lambda *args, **kwargs: failed)
        self.assertEqual(len(record.block_events), 4)
        self.assertTrue(all(item.state is PrState.FAILED for item in record.items))
        self.assertIn("giving up", record.items[1].error)

    async def test_failure_with_unknown_posts_is_not_requeued(self):
        timeout = subprocess.TimeoutExpired("gh", 30)
        limited = runner.AgentRun(1, ["usage limit"])
        # Either snapshot failing leaves what the agent posted unknown.
        for failed_snapshots in (({}, timeout), (timeout, {})):
            with self.subTest(failed_snapshots=failed_snapshots):
                self.events.clear()
                loop_runner = self.make_runner([make_item(1)], cooldown_min=0)
                snapshots = itertools.chain(failed_snapshots, itertools.repeat({}))

                record = await self.run_loop(
                    loop_runner, lambda *args, **kwargs: limited, snapshots
                )

                # A rerun could post the same comments twice.
                self.assertEqual(len(self.command_calls), 1)
                self.assertIs(record.items[0].state, PrState.FAILED)
                self.assertIn("posts unknown", record.items[0].error)
                self.assertEqual(record.block_events, [])

    def test_failure_after_the_loop_gave_up_logs_no_more_blocks(self):
        item = make_item(1, state=PrState.RUNNING)
        item.exit_code = 1
        loop_runner = self.make_runner([item])
        loop_runner.record = LoopRecord(1, 0, items=[item])
        loop_runner.record.block_events = [{}] * (runner.MAX_BLOCKS_PER_LOOP + 1)

        # An agent still running when the 4th block abandoned the loop.
        loop_runner._handle_failure(item, ["usage limit"], False, "")

        self.assertIs(item.state, PrState.FAILED)
        self.assertEqual(
            len(loop_runner.record.block_events), runner.MAX_BLOCKS_PER_LOOP + 1
        )
        self.assertNotIn("error", [name for name, _ in self.events])

    async def test_parallel_runs_at_most_max_parallel_agents_at_once(self):
        loop_runner = self.make_runner(
            [make_item(number) for number in range(1, 6)], max_parallel=3
        )
        running_counts = []

        async def spawn(*args, **kwargs):
            running_counts.append(len(loop_runner._running_keys))
            await asyncio.sleep(0.05)
            return runner.AgentRun(0)

        record = await self.run_loop(loop_runner, spawn)

        self.assertEqual(max(running_counts), 3)
        self.assertTrue(all(item.state is PrState.DONE for item in record.items))

    async def test_cooldown_starts_no_new_pr_and_lets_running_ones_finish(self):
        loop_runner = self.make_runner(
            [make_item(1), make_item(2), make_item(3)],
            max_parallel=2,
            cooldown_min=1 / 600,
        )
        starts = []

        async def spawn(*args, **kwargs):
            key = kwargs["log_key"]
            starts.append(key)
            if starts == ["o/r#1"]:
                return runner.AgentRun(1, ["usage limit"])
            if key == "o/r#2":
                # Still running when #1 hits the limit.
                await asyncio.sleep(0.1)
            return runner.AgentRun(0)

        record = await self.run_loop(loop_runner, spawn)

        self.assertEqual(starts, ["o/r#1", "o/r#2", "o/r#1", "o/r#3"])
        unblocked_at = [name for name, _ in self.events].index("unblocked")
        updates = [
            (index, args[0].key)
            for index, (name, args) in enumerate(self.events)
            if name == "item_update"
        ]
        # #2 finished inside the cooldown; #3 waited for the cooldown to end.
        self.assertLess(max(i for i, key in updates if key == "o/r#2"), unblocked_at)
        self.assertGreater(min(i for i, key in updates if key == "o/r#3"), unblocked_at)
        self.assertEqual(len(record.block_events), 1)
        self.assertTrue(all(item.state is PrState.DONE for item in record.items))

    async def test_agents_hitting_the_limit_together_share_one_block(self):
        items = [make_item(1), make_item(2), make_item(3)]
        loop_runner = self.make_runner(items, max_parallel=3, cooldown_min=1 / 600)
        attempts = []

        async def spawn(*args, **kwargs):
            attempts.append(kwargs["log_key"])
            attempt = len(attempts)
            # Let all three start before any of them fails.
            await asyncio.sleep(0)
            if attempt <= 3:
                return runner.AgentRun(1, ["usage limit"])
            return runner.AgentRun(0)

        record = await self.run_loop(loop_runner, spawn)

        # Three blocks at once would have spent the loop's whole block budget.
        self.assertEqual(len(record.block_events), 1)
        self.assertEqual(sorted(attempts), ["o/r#1"] * 2 + ["o/r#2"] * 2 + ["o/r#3"] * 2)
        self.assertEqual(
            [(item.state, item.block_attempts) for item in record.items],
            [(PrState.DONE, 1)] * 3,
        )

    async def test_failure_absorbed_by_a_cooldown_does_not_arm_the_breaker(self):
        items = [make_item(1), make_item(2), make_item(3)]
        loop_runner = self.make_runner(items, max_parallel=2, cooldown_min=1 / 600)
        attempts = []

        async def spawn(*args, **kwargs):
            key = kwargs["log_key"]
            attempts.append(key)
            if key == "o/r#1":
                if attempts.count(key) == 1:
                    return runner.AgentRun(1, ["usage limit"])
                await asyncio.sleep(0.05)
                return runner.AgentRun(0)
            if key == "o/r#2":
                if attempts.count(key) == 1:
                    # Fails inside #1's cooldown, so it is absorbed.
                    await asyncio.sleep(0.05)
                # Fails again, alone: one ordinary failure, not two in a row.
                return runner.AgentRun(3)
            return runner.AgentRun(0)

        record = await self.run_loop(loop_runner, spawn)

        self.assertEqual(len(record.block_events), 1)
        self.assertEqual(
            [(item.state, item.block_attempts) for item in record.items],
            [(PrState.DONE, 1), (PrState.FAILED, 1), (PrState.DONE, 0)],
        )

    async def test_stop_kills_every_running_agent_without_a_block(self):
        os.environ["MOCK_SLEEP"] = "30"
        loop_runner = self.make_runner(
            [make_item(1), make_item(2), make_item(3)], max_parallel=2
        )
        agent_pids = []

        async def stop_once_both_started():
            while len(loop_runner._processes) < 2:
                await asyncio.sleep(0.01)
            agent_pids.extend(process.pid for process in loop_runner._processes.values())
            loop_runner.stop()

        started_at = time.monotonic()
        stop_task = asyncio.create_task(stop_once_both_started())
        record = await self.run_loop(loop_runner)
        await stop_task

        self.assertLess(time.monotonic() - started_at, 5)
        self.assertEqual(
            [item.state for item in record.items],
            [PrState.FAILED, PrState.FAILED, PrState.QUEUED],
        )
        # Two kills in a row are `q`, not an outage.
        self.assertEqual(record.block_events, [])
        for pid in agent_pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_stop_before_the_agent_spawns_still_stops_it(self):
        os.environ["MOCK_SLEEP"] = "30"
        loop_runner = self.make_runner([make_item(1)])

        async def stop_during_snapshot(item):
            # `q` lands while the before-snapshot runs, so no process exists yet.
            loop_runner.stop()
            return {}

        started_at = time.monotonic()
        with patch.object(loop_runner, "_snapshot", side_effect=stop_during_snapshot):
            record = await self.run_loop(loop_runner)

        self.assertLess(time.monotonic() - started_at, 5)
        self.assertIs(record.items[0].state, PrState.FAILED)

    async def test_cancelling_the_whole_queue_during_a_cooldown_ends_the_loop(self):
        loop_runner = self.make_runner(
            [make_item(1), make_item(2)], cooldown_min=5 / 60
        )

        def cancel_all():
            loop_runner.cancel("o/r#1")
            loop_runner.cancel("o/r#2")

        # The queue empties mid-wait; popping it afterwards used to raise.
        record, _ = await self.run_on_fake_clock(
            loop_runner, [runner.AgentRun(1, ["usage limit"])], {1: cancel_all}
        )

        self.assertEqual(
            [item.state for item in record.items],
            [PrState.CANCELLED, PrState.CANCELLED],
        )


class BuildItemsTest(unittest.TestCase):
    """One loop's items: pr-discover's skips first, then the head marker."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "state")
        self.events = []

    def build_items(self, found, snapshots):
        """`snapshots` holds one get_gate_snapshot result or error per PR."""
        loop_runner = runner.LoopRunner(
            RunConfig("codex-test-stub", "review", repos=["o/r"]),
            self.store,
            lambda name, *args: self.events.append((name, args)),
        )
        with (
            patch.object(runner, "discover_prs", return_value=found),
            patch.object(
                gate, "get_gate_snapshot", side_effect=snapshots
            ) as get_gate_snapshot,
        ):
            items = loop_runner.build_items()
        self.snapshot_urls = [
            call.args[0] for call in get_gate_snapshot.call_args_list
        ]
        return items

    def test_marker_decides_the_verdict_in_discovery_order(self):
        items = self.build_items(
            [make_found(12), make_found(15), make_found(7)],
            [make_snapshot(), make_snapshot(OLD_HEAD), make_snapshot(HEAD)],
        )

        self.assertEqual([item.key for item in items], ["o/r#12", "o/r#15", "o/r#7"])
        self.assertEqual(
            [item.verdict for item in items],
            [Verdict.FULL, Verdict.INCREMENTAL, Verdict.SKIP],
        )
        self.assertEqual(
            [item.state for item in items],
            [PrState.QUEUED, PrState.QUEUED, PrState.SKIP],
        )
        self.assertEqual(
            [item.skip_reason for item in items],
            [None, None, SkipReason.HEAD_ALREADY_REVIEWED],
        )
        self.assertEqual(
            [item.last_marker_sha for item in items], [None, OLD_HEAD, HEAD]
        )

    def test_unchanged_from_pr_discover_still_gets_a_marker_verdict(self):
        found = parse_discover_output(
            '{"prs": [{"key": "o/r#1", "url": "u1", "head_sha": "abc", '
            '"action": "skipped", "reason": "unchanged"}]}'
        )
        item = self.build_items(found, [make_snapshot()])[0]

        self.assertIs(item.verdict, Verdict.FULL)
        self.assertIs(item.state, PrState.QUEUED)
        self.assertIsNone(item.skip_reason)

    def test_wip_title_takes_priority_over_marker_verdict(self):
        item = self.build_items(
            [make_found(1, SkipReason.WIP_TITLE)],
            [make_snapshot(OLD_HEAD, title="Feature WIP: still changing")],
        )[0]

        self.assertIs(item.verdict, Verdict.SKIP)
        self.assertIs(item.state, PrState.SKIP)
        self.assertIs(item.skip_reason, SkipReason.WIP_TITLE)
        self.assertEqual(item.verdict_label, "Skip (WIP)")
        self.assertEqual(item.to_dict()["skip_reason"], "wip-title")

    def test_draft_takes_priority_over_wip_title_and_marker_verdict(self):
        for title, marker_sha in (
            ("Feature still changing", None),
            ("Feature WIP: still changing", HEAD),
        ):
            with self.subTest(title=title, marker_sha=marker_sha):
                item = self.build_items(
                    [make_found(1, SkipReason.DRAFT)],
                    [make_snapshot(marker_sha, title=title)],
                )[0]

                self.assertIs(item.verdict, Verdict.SKIP)
                self.assertIs(item.state, PrState.SKIP)
                self.assertIs(item.skip_reason, SkipReason.DRAFT)
                self.assertEqual(item.verdict_label, "Skip (WIP)")
                self.assertEqual(item.to_dict()["skip_reason"], "draft")

    def test_skipped_prs_still_show_their_title_and_marker(self):
        found = [make_found(1, SkipReason.DRAFT), make_found(2, SkipReason.WIP_TITLE)]
        items = self.build_items(
            found, [make_snapshot(OLD_HEAD), make_snapshot(HEAD, title="[WIP] two")]
        )

        self.assertEqual(self.snapshot_urls, [pr.url for pr in found])
        self.assertEqual([item.title for item in items], ["pr title", "[WIP] two"])
        self.assertEqual([item.last_marker_sha for item in items], [OLD_HEAD, HEAD])

    def test_snapshot_fills_the_item_and_discovery_keeps_the_axes(self):
        item = self.build_items(
            [make_found(1, matched_axes=["url", "author:me"])], [make_snapshot()]
        )[0]

        self.assertEqual(
            (item.title, item.author, item.head_sha), ("pr title", "me", HEAD)
        )
        self.assertEqual(item.matched_axes, ["url", "author:me"])

    def test_a_skipped_pr_whose_snapshot_fails_stays_skipped(self):
        for skip_reason in (SkipReason.DRAFT, SkipReason.WIP_TITLE):
            with self.subTest(skip_reason=skip_reason):
                item = self.build_items(
                    [make_found(1, skip_reason)], [gate.GhError("gh pr view failed")]
                )[0]

                self.assertIs(item.verdict, Verdict.SKIP)
                self.assertIs(item.state, PrState.SKIP)
                self.assertIs(item.skip_reason, skip_reason)
                self.assertEqual(
                    (item.head_sha, item.title, item.author, item.error),
                    (OLD_HEAD, "", "", ""),
                )

    def test_a_failed_gate_check_has_no_verdict(self):
        items = self.build_items(
            [make_found(1), make_found(2, SkipReason.DRAFT)],
            [gate.GhError("gh pr view failed: 502"), gate.GhError("gh pr view failed: 502")],
        )

        self.assertEqual(
            [(item.state, item.verdict) for item in items],
            [(PrState.FAILED, Verdict.UNKNOWN), (PrState.SKIP, Verdict.SKIP)],
        )

    def test_one_gh_error_fails_only_that_pr(self):
        items = self.build_items(
            [make_found(1), make_found(2), make_found(3)],
            [make_snapshot(), gate.GhError("gh pr view failed: 502"), make_snapshot()],
        )

        self.assertEqual(
            [item.state for item in items],
            [PrState.QUEUED, PrState.FAILED, PrState.QUEUED],
        )
        self.assertEqual(items[1].error, "gate check failed: gh pr view failed: 502")
        self.assertEqual(items[1].head_sha, OLD_HEAD)
        self.assertEqual(items[1].matched_axes, ["author:me"])

    def test_progress_is_emitted_per_pr(self):
        items = self.build_items(
            [make_found(1), make_found(2, SkipReason.DRAFT), make_found(3)],
            [make_snapshot(), make_snapshot(), gate.GhError("boom")],
        )

        self.assertEqual(
            [args for name, args in self.events if name == "discovery_progress"],
            [(1, 3, items[0]), (2, 3, items[1]), (3, 3, items[2])],
        )
