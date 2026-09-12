"""Core queue behaviour with no network or real AI agent."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

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

    def make_runner(self, items, **overrides):
        config = RunConfig("codex-test-stub", "review", repos=["o/r"], **overrides)
        self.items = items
        return runner.LoopRunner(
            config, self.store, lambda name, *args: self.events.append((name, args))
        )

    async def run_loop(self, loop_runner, agent_runs=None):
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
            patch.object(gh, "snapshot_comments", return_value={}),
            patch.object(
                runner.asyncio, "to_thread", side_effect=lambda fn, *a: fn(*a)
            ),
            patch.object(runner, "get_ai_command", side_effect=command),
            spawn_patch,
        ):
            return await loop_runner.run_loop()

    async def test_prompt_is_forwarded_to_each_agent(self):
        loop_runner = self.make_runner([make_item(1)])

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
                    {},
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
        self.assertIsNone(loop_runner._current_process)

    async def test_queued_cancel_resume_moves_item_to_tail_and_preserves_order(self):
        loop_runner = self.make_runner([make_item(1), make_item(2), make_item(3)])
        order = []

        async def spawn(*args, **kwargs):
            order.append(loop_runner._current_key)
            if loop_runner._current_key == "o/r#1":
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
            while loop_runner._current_process is None:
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
            order.append(loop_runner._current_key)
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
