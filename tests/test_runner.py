"""Core queue behaviour with no network or real AI agent."""

import asyncio
import itertools
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_review_pr_dashboard import gh, runner
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.models import LoopRecord, PrItem, PrState
from auto_review_pr_dashboard.store import Store

HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
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
            return [str(self.mock_cli), "--flag", args[-1]]

        spawn_patch = (
            nullcontext()
            if agent_runs is None
            else patch.object(loop_runner, "_spawn_agent", side_effect=agent_runs)
        )
        with (
            patch.object(gh, "discover_pr_items", return_value=self.items),
            patch.object(
                gh, "snapshot_comments", side_effect=snapshots or (lambda *a: {})
            ),
            patch.object(
                runner.asyncio, "to_thread", side_effect=lambda fn, *a: fn(*a)
            ),
            patch.object(runner, "get_ai_command", side_effect=command),
            spawn_patch,
        ):
            return await loop_runner.run_loop()

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
        skip = make_item(2, state=PrState.SKIP)
        record = await self.run_loop(
            self.make_runner([make_item(1), skip, make_item(3)])
        )
        self.assertEqual(
            [item.state for item in record.items],
            [PrState.DONE, PrState.SKIP, PrState.DONE],
        )
        self.assertIsNone(skip.started_at)
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
        self.assertIs(items[0].state, PrState.FAILED)
        self.assertEqual((items[1].state, items[1].block_attempts), (PrState.DONE, 1))
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
            [(item.state, item.block_attempts) for item in items],
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
            [(item.state, item.block_attempts) for item in items],
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
