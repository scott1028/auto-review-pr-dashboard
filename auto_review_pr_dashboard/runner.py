"""Loop orchestration: discover, queue, run one AI process per PR, cancel, resume.

UI-agnostic on purpose - it only pushes events through `on_event`, so the whole
queue behaviour is testable with a mock AI CLI and no terminal.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field

from . import gate
from .config import RunConfig, get_ai_command, get_is_usage_limit
from .discover import DiscoveredPr, DiscoverError, discover_prs
from .models import LoopRecord, PrItem, PrState, SkipReason, Verdict
from .store import Store

TERMINATE_GRACE_SEC = 5
# After the leader exits, output gets this long to drain before stragglers
# holding the pipe open are SIGKILLed.
OUTPUT_DRAIN_SEC = 2
OUTPUT_TAIL_LINES = 20
ERROR_TAIL_LINES = 3
# Agent output is read in chunks and split here: readline() raises past asyncio's
# own 64KiB limit, and one long line would then kill the whole run.
READ_CHUNK_BYTES = 64 * 1024
# Caps only what the UI holds in memory - the log file keeps every line in full.
MAX_UI_LINE_CHARS = 2000
# Two failures in a row are treated as a systemic block even when the agent's
# wording matches no known pattern - covers auth loss, a broken CLI, network down.
SYSTEMIC_FAILURE_THRESHOLD = 2
# After this many cooldowns in one loop, give up on the loop instead of paying
# another cooldown per remaining PR.
MAX_BLOCKS_PER_LOOP = 3


@dataclass
class AgentRun:
    """One finished agent process."""

    exit_code: int | None
    output_tail: deque = field(default_factory=deque)
    error: str = ""
    timed_out: bool = False

    @property
    def failed(self) -> bool:
        return self.timed_out or self.exit_code != 0


def get_is_group_alive(pgid: int) -> bool:
    """Any process left in the group? A not-yet-reaped leader keeps it alive."""
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class LoopRunner:
    def __init__(
        self,
        config: RunConfig,
        store: Store,
        on_event=None,
        recycle_every_loops: int | None = None,
    ):
        self.config = config
        self.store = store
        self.on_event = on_event or (lambda *args: None)
        # None = never recycle, which is what the in-process dashboard does.
        self.recycle_every_loops = recycle_every_loops

        # Continue where the last daemon left off, so loop numbers are never
        # reused after a restart. A recycle's handoff overrides this.
        self.loop_index = store.load_loop_index()
        self.record: LoopRecord | None = None
        self.queue: deque[str] = deque()
        self.paused = False
        self.next_run_at: float | None = None
        self.blocked_until: float | None = None
        self.block_reason = ""
        self.consecutive_failures = 0
        self.is_recycle_requested = False

        self._stopped = False
        self._run_now = False
        # Running PRs in start order, so the last one is the newest.
        self._running_keys: list[str] = []
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_keys: set[str] = set()
        # Agent gone, result being decided: cancel() is too late for these.
        self._finishing_keys: set[str] = set()
        # The one escalation task per process, kept referenced so it is not
        # garbage-collected before it runs.
        self._stop_tasks: dict[asyncio.subprocess.Process, asyncio.Task] = {}

    @property
    def current_key(self) -> str | None:
        """The newest running PR, which the activity panel follows."""
        return self._running_keys[-1] if self._running_keys else None

    # ---- events -------------------------------------------------------------

    def emit(self, name: str, *payload) -> None:
        self.on_event(name, *payload)

    # ---- control ------------------------------------------------------------

    def stop(self) -> None:
        self._stopped = True
        self._run_now = True
        for process in list(self._processes.values()):
            # Escalates like cancel() does: SIGTERM, then SIGKILL after the grace.
            self._schedule_stop(process)

    def run_now(self) -> None:
        """Skip whatever countdown is running - idle between loops, or a cooldown."""
        self._run_now = True

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        self.emit("paused", self.paused)
        return self.paused

    def cancel(self, key: str) -> bool:
        """Drop a PR from this loop. A running one gets its process group killed."""
        item = self.record.item_by_key(key) if self.record else None
        if item is None or item.state not in (PrState.QUEUED, PrState.RUNNING):
            return False
        process = self._processes.get(key)
        if key in self._finishing_keys or (
            process is not None and process.returncode is not None
        ):
            # The agent is gone already: its result is decided now, not undone.
            return False

        self._cancelled_keys.add(key)
        if item.state is PrState.QUEUED:
            self._remove_from_queue(key)
            item.state = PrState.CANCELLED
            self.emit("item_update", item)
            return True

        if process is not None:
            item.error = "cancel requested"
            self.emit("item_update", item)
            # SIGTERM now, SIGKILL after the grace period if it ignores us.
            self._schedule_stop(process)
        return True

    def resume(self, key: str) -> bool:
        """Put a cancelled PR back, at the tail of this loop's queue."""
        item = self.record.item_by_key(key) if self.record else None
        if item is None or item.state is not PrState.CANCELLED:
            return False

        self._cancelled_keys.discard(key)
        item.state = PrState.QUEUED
        item.started_at = None
        item.finished_at = None
        item.exit_code = None
        item.error = ""
        self._remove_from_queue(key)
        self.queue.append(key)
        self.emit("item_update", item)
        self.emit("queue_changed", list(self.queue))
        return True

    def _remove_from_queue(self, key: str) -> None:
        self.queue = deque(queued for queued in self.queue if queued != key)

    def _schedule_stop(self, process) -> None:
        """Escalate one process at most once, however many paths ask for it."""
        if process in self._stop_tasks:
            return
        # Keep the task on self: a bare create_task can be garbage-collected
        # before it ever runs.
        self._stop_tasks[process] = asyncio.get_running_loop().create_task(
            self._stop_process(process)
        )

    # ---- main loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        while not self._stopped:
            # A recycled daemon is handed a countdown already running: honour it
            # instead of starting a fresh interval.
            if self.next_run_at is not None:
                await self._idle_until_next_loop()
                if self._stopped:
                    break
            await self.run_loop()
            if self._stopped:
                break
            if (
                self.recycle_every_loops is not None
                and self.loop_index % self.recycle_every_loops == 0
            ):
                # The daemon restarts itself here: a fresh process, and a clean
                # .tmp/ for the loops ahead. Idle would be the next step instead.
                self.is_recycle_requested = True
                return
            if self.blocked_until is not None:
                await self._wait_while_blocked()
            else:
                await self._idle_until_next_loop()

    async def _idle_until_next_loop(self) -> None:
        if self.next_run_at is None:
            self.next_run_at = time.time() + self.config.interval_min * 60
        self.emit("idle_start", self.next_run_at)
        while not self._run_now and not self._stopped:
            await asyncio.sleep(1)
            if self.paused:
                self.next_run_at += 1
            remaining = max(0.0, self.next_run_at - time.time())
            self.emit("idle_tick", remaining)
            if remaining <= 0:
                break
        self._run_now = False
        self.next_run_at = None

    async def run_loop(self) -> LoopRecord:
        self.loop_index += 1
        self.record = LoopRecord(index=self.loop_index, started_at=time.time())
        self._cancelled_keys.clear()
        self.store.save_state(self.config, self.loop_index)
        self.store.prune_logs()
        self.emit("loop_start", self.loop_index)

        try:
            items = await asyncio.to_thread(self.build_items)
        except (gate.GhError, DiscoverError) as error:
            # A dead gh or a broken pr-discover is a loop-level failure: the
            # next loop may succeed, so use no items instead of killing the run.
            self.emit("error", f"discovery failed: {error}")
            items = []

        self.record.items = items
        self.queue = deque(item.key for item in items if item.state is PrState.QUEUED)
        self.consecutive_failures = 0
        self.emit("items_ready", self.record)

        await self._run_queue()

        self.record.finished_at = time.time()
        self.store.save_loop(self.record)
        self.emit("loop_end", self.record)
        return self.record

    # ---- building items -----------------------------------------------------

    def build_items(self) -> list[PrItem]:
        """Discover, then read every PR's title and marker.

        Runs in a worker thread; every call inside is synchronous gh work. PRs
        pr-discover skipped are read too, so their title and marker still show.
        """
        discovered = discover_prs(self.config)
        items = []
        for done, found in enumerate(discovered, start=1):
            item = _build_gated_item(found)
            items.append(item)
            self.emit("discovery_progress", done, len(discovered), item)
        return items

    # ---- queue --------------------------------------------------------------

    async def _run_queue(self) -> None:
        """Start queued PRs while a slot is free, until nothing is queued or running."""
        running: set[asyncio.Task] = set()
        try:
            while True:
                if running and (
                    len(running) >= self.config.max_parallel
                    or not self.queue
                    or self._stopped
                ):
                    done, running = await asyncio.wait(
                        running, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                    continue
                if not self.queue or self._stopped:
                    return
                # The only caller, so one countdown ticks however many PRs run.
                await self._wait_until_runnable()
                # A cancel during the wait can empty the queue.
                if self._stopped or not self.queue:
                    continue
                item = self.record.item_by_key(self.queue.popleft())
                if item is None or item.state is not PrState.QUEUED:
                    continue
                running.add(asyncio.create_task(self._run_item(item)))
        except BaseException:
            # asyncio.CancelledError of the loop task included: agents live in
            # their own process groups and must never be left behind.
            self.stop()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*running, return_exceptions=True)
            raise

    async def _wait_until_runnable(self) -> None:
        """Hold the queue until neither a pause nor a cooldown stops the next PR."""
        while not self._stopped:
            # The cooldown wait runs first: it is what freezes and ticks the
            # deadline when the pause came before the cooldown.
            if self.blocked_until is not None:
                await self._wait_while_blocked()
            await self._wait_while_paused()
            if self._stopped or self.blocked_until is None:
                return

    async def _wait_while_paused(self) -> None:
        while self.paused and not self._stopped:
            await asyncio.sleep(0.2)

    # ---- usage limit / systemic block ---------------------------------------

    def start_cooldown(self, reason: str, pr_key: str = "", detail: str = "") -> bool:
        """Suspend work for the cooldown. False = too many blocks, give up on the loop."""
        self.record.block_events.append(
            {"at": time.time(), "pr": pr_key, "reason": reason, "agent_output": detail}
        )
        if len(self.record.block_events) > MAX_BLOCKS_PER_LOOP:
            return False

        self.blocked_until = time.time() + self.config.cooldown_min * 60
        self.block_reason = reason
        self.consecutive_failures = 0
        self.emit("blocked", reason, self.blocked_until)
        return True

    def enter_cooldown(self, item: PrItem, reason: str) -> bool:
        """Block on a PR whose agent hit a quota. False = give up on the loop.

        The PR goes back to the FRONT of the queue: it was never reviewed, so it
        keeps its place instead of being punished for the outage.
        """
        item.block_attempts += 1
        if self.blocked_until is not None:
            # Another agent's block is already counting down: same outage, so no
            # new event, no fresh deadline, and the breaker starts over.
            self.consecutive_failures = 0
        elif not self.start_cooldown(f"{item.key}: {reason}", item.key, item.error):
            return False

        item.state = PrState.QUEUED
        item.error = f"blocked ({reason}) \u00b7 {item.error}"
        self.queue.appendleft(item.key)
        self.emit("item_update", item)
        return True

    def abandon_loop(self, reason: str) -> None:
        """Too many cooldowns: fail what is left so the loop can end and retry later."""
        while self.queue:
            item = self.record.item_by_key(self.queue.popleft())
            if item is not None and item.state is PrState.QUEUED:
                item.state = PrState.FAILED
                item.error = reason
                self.emit("item_update", item)
        self.emit("error", reason)

    async def _wait_while_blocked(self) -> None:
        while self.blocked_until is not None and not self._stopped:
            remaining = self.blocked_until - time.time()
            if remaining <= 0 or self._run_now:
                break
            self.emit("blocked_tick", remaining)
            await asyncio.sleep(1)
            # Compensate after the sleep, matching the idle loop's order, so a
            # pause that predates the cooldown does not extend the first tick.
            if self.paused:
                self.blocked_until += 1

        if self.blocked_until is not None:
            self.blocked_until = None
            self.block_reason = ""
            self._run_now = False
            self.emit("unblocked")

    # ---- one PR -------------------------------------------------------------

    async def _spawn_agent(self, command, log_path, timeout_sec, log_key) -> AgentRun:
        """Run one agent process, streaming its output to a log file."""
        # Opened before the spawn, so a log that cannot be written is reported as
        # itself instead of being blamed on the agent binary.
        try:
            log_file = open(log_path, "a", encoding="utf-8")
        except OSError as os_error:
            return AgentRun(
                exit_code=1, error=f"cannot open log {log_path}: {os_error}"
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            self._close_log(log_file)
            # The command always execs `bash`, so this is bash missing - a real
            # "<ai_cli>: command not found" comes back as bash's own exit 127.
            return AgentRun(
                exit_code=127,
                error=f"cannot start {self.config.ai_cli}: bash not found",
            )
        except OSError as os_error:
            self._close_log(log_file)
            return AgentRun(exit_code=1, error=str(os_error))

        self._processes[log_key] = process
        try:
            return await self._await_agent(
                process, command, log_file, log_key, timeout_sec
            )
        finally:
            del self._processes[log_key]
            self._stop_tasks.pop(process, None)
            self.emit("agent_finished", process.pid)

    async def _await_agent(
        self, process, command, log_file, log_key, timeout_sec
    ) -> AgentRun:
        output_tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
        pump_task = asyncio.create_task(
            self._pump_output(process, log_key, log_file, output_tail, command)
        )
        wait_task = asyncio.create_task(self._await_exit(process))
        run: AgentRun | None = None
        try:
            # Inside the guard: a listener that raises must not leave the group
            # alive and unregistered. The daemon records the group from this.
            self.emit("agent_started", process.pid)
            if self._stopped or log_key in self._cancelled_keys:
                # q or c landed before there was a process to signal.
                self._schedule_stop(process)
            run = await self._collect_agent(
                process, pump_task, wait_task, output_tail, timeout_sec
            )
            return run
        except BaseException:
            # Left by an exception, CancelledError included. The SIGKILL is
            # synchronous, so no agent can outlive this coroutine.
            self._terminate(process, signal.SIGKILL)
            pump_task.cancel()
            await asyncio.gather(pump_task, wait_task, return_exceptions=True)
            raise
        finally:
            close_error = self._close_log(log_file)
            if run is not None and close_error:
                run.error = f"{run.error} {close_error}".strip()
                run.exit_code = run.exit_code or 1

    def _close_log(self, log_file) -> str:
        """Close the log; a failure is reported, never raised out of a run."""
        try:
            log_file.close()
        except OSError as os_error:
            return f"closing the log failed: {os_error}"
        return ""

    async def _collect_agent(
        self, process, pump_task, wait_task, output_tail, timeout_sec
    ) -> AgentRun:
        """The timeout covers the process; the log gets a short drain after it."""
        error = ""
        timed_out = False
        exit_code = None
        deadline = time.monotonic() + timeout_sec
        pending = {wait_task, pump_task}
        while pending and exit_code is None:
            done, pending = await asyncio.wait(
                pending,
                timeout=max(0.0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
                error = f"timed out after {round(timeout_sec / 60)}min"
                await self._stop_process(process)
                break
            if pump_task in done and pump_task.exception() is not None:
                # The log broke: stop the agent now, not whenever it exits.
                self._terminate(process, signal.SIGKILL)
                break
            if wait_task in done:
                exit_code = wait_task.result()
        if exit_code is None:
            exit_code = await wait_task
        log_error = await self._drain_output(process, pump_task)
        if log_error:
            error = f"{error} {log_error}".strip()
            # A log that cannot be written is a failure even when the agent
            # itself exited 0, so it is never judged DONE.
            exit_code = exit_code or 1
        return AgentRun(
            exit_code=exit_code,
            output_tail=output_tail,
            error=error,
            timed_out=timed_out,
        )

    async def _await_exit(self, process) -> int:
        """The leader's exit code, without asyncio's wait().

        wait() also waits for the stdout pipe to close, which a surviving
        child can hold open for hours - the process timeout is about the
        leader, not its stragglers. asyncio's child watcher reaps the leader
        and fills in returncode regardless of the pipe.
        """
        while process.returncode is None:
            await asyncio.sleep(0.05)
        return process.returncode

    async def _drain_output(self, process, pump_task) -> str:
        """Leader gone: give output OUTPUT_DRAIN_SEC, then always clear stragglers.

        Returns the log error when the pump broke, at any point.
        """
        if not pump_task.done():
            # A surviving child holds the pipe open; the leader's exit code
            # already stands, so this wait is bounded. `wait` never re-raises
            # what the pump hit: the `await` below turns that into log_error.
            await asyncio.wait({pump_task}, timeout=OUTPUT_DRAIN_SEC)
        if not pump_task.done():
            pump_task.cancel()
        log_error = ""
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        except Exception as pump_error:
            log_error = f"writing log failed: {pump_error}"
        # Kill unconditionally: a detached child that outlived the leader is
        # still ours, and nothing may run past the loop.
        self._terminate(process, signal.SIGKILL)
        return log_error

    async def _run_item(self, item: PrItem) -> None:
        if item.key in self._cancelled_keys:
            # Cancelled between the queue check and this task's start: never
            # spawn for it.
            item.state = PrState.CANCELLED
            if not item.error:
                item.error = "cancelled by user"
            self.emit("item_update", item)
            return
        item.state = PrState.RUNNING
        item.error = ""
        # A resumed PR inherits the last run's finish time; leaving it set makes
        # the running duration read as negative until this run finishes.
        item.finished_at = None
        item.exit_code = None
        item.started_at = time.time()
        item.log_path = str(self.store.get_log_path(self.loop_index, item))
        self._running_keys.append(item.key)
        self.emit("item_update", item)

        before = await self._snapshot(item)
        if item.key in self._cancelled_keys:
            # Cancelled while the pre-run snapshot ran: there is still no
            # process, so end here instead of spawning one only to stop it.
            self._running_keys.remove(item.key)
            item.state = PrState.CANCELLED
            item.finished_at = time.time()
            if not item.error:
                item.error = "cancelled by user"
            self.emit("item_update", item)
            return
        command = get_ai_command(
            self.config.ai_cli,
            self.config.prompt,
            item.repo,
            item.url,
            ai_cli_args=self.config.ai_cli_args,
        )
        try:
            run = await self._spawn_agent(
                command,
                item.log_path,
                self.config.pr_timeout_min * 60,
                log_key=item.key,
            )
        finally:
            self._running_keys.remove(item.key)
        item.exit_code = run.exit_code
        self._finishing_keys.add(item.key)

        after = await self._snapshot(item)
        # Without both snapshots, what the agent posted is unknown.
        is_posting_unknown = before is None or after is None
        item.new_posts = (
            [] if is_posting_unknown else gate.classify_new_posts(before, after)
        )
        item.finished_at = time.time()
        # cancel() refused once the agent was gone, so a key still in
        # _cancelled_keys means the cancel took effect.
        self._finishing_keys.discard(item.key)

        if item.key in self._cancelled_keys:
            item.state = PrState.CANCELLED
            if not item.error:
                item.error = run.error or "cancelled by user"
            self.emit("item_update", item)
            return

        if not run.timed_out and item.exit_code == 0:
            item.state = PrState.DONE
            self.consecutive_failures = 0
            self.emit("item_update", item)
            return

        self._handle_failure(
            item, run.output_tail, run.timed_out, run.error, is_posting_unknown
        )

    def _handle_failure(
        self,
        item: PrItem,
        output_tail,
        timed_out,
        preset_error,
        is_posting_unknown=False,
    ) -> None:
        """A failed run is either this PR's problem, or the whole run is blocked."""
        failure_text = "\n".join(output_tail)
        tail_lines = [line.strip() for line in output_tail if line.strip()]
        item.error = (
            " \u00b7 ".join(
                part
                for part in (preset_error, " / ".join(tail_lines[-ERROR_TAIL_LINES:]))
                if part
            )
            or f"agent exited with {item.exit_code}"
        )
        if is_posting_unknown:
            item.error = f"{item.error} \u00b7 comment snapshot failed: posts unknown"

        self.consecutive_failures += 1
        is_usage_limit = get_is_usage_limit(failure_text)
        # A failure landing during a cooldown is presumed the same outage; a
        # timeout is still the PR's own problem.
        is_during_cooldown = self.blocked_until is not None and not timed_out
        is_systemic = (
            is_usage_limit
            or is_during_cooldown
            or self.consecutive_failures >= SYSTEMIC_FAILURE_THRESHOLD
        )
        # Agents still running when the loop gave up must not log more blocks.
        is_loop_abandoned = len(self.record.block_events) > MAX_BLOCKS_PER_LOOP

        # A rerun could duplicate posted, or possibly posted, comments; and a run
        # killed by `q` is not an outage.
        if (
            not is_systemic
            or item.new_posts
            or is_posting_unknown
            or self._stopped
            or is_loop_abandoned
        ):
            item.state = PrState.FAILED
            self.emit("item_update", item)
            return

        reason = (
            "usage limit / rate limited"
            if is_usage_limit
            else f"{self.consecutive_failures} failures in a row (quota or environment?)"
        )
        if self.enter_cooldown(item, reason):
            return

        item.state = PrState.FAILED
        self.emit("item_update", item)
        self.abandon_loop(
            f"blocked {MAX_BLOCKS_PER_LOOP} times this loop; giving up until the next loop"
        )

    async def _snapshot(self, item: PrItem) -> dict | None:
        """The PR's comments by id, or None when gh could not fetch them."""
        try:
            return await asyncio.to_thread(gate.snapshot_comments, item.repo, item.number)
        except (gate.GhError, subprocess.SubprocessError, OSError) as error:
            # A gh timeout or a missing gh fails this snapshot, not the whole loop.
            self.emit("error", f"{item.key}: comment snapshot failed: {error}")
            return None

    async def _pump_output(self, process, log_key, log_file, output_tail, command) -> None:
        """Stream the agent's output, splitting lines ourselves.

        Starts with the command header, so a log write failure is seen by the
        caller, which stops the agent instead of blaming it. `read()` has no
        separator limit, so a line longer than asyncio's own readline() limit
        is data rather than a crash.
        """
        log_file.write(f"$ {' '.join(command[:-1])} <prompt>\n")
        log_file.flush()
        pending = b""
        while True:
            chunk = await process.stdout.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            *lines, pending = (pending + chunk).split(b"\n")
            for raw_line in lines:
                self._handle_output_line(raw_line, log_key, log_file, output_tail)
            if len(pending) > READ_CHUNK_BYTES:
                # No newline in sight. Flushing wraps the line in the log file,
                # which beats letting `pending` grow without a bound.
                self._handle_output_line(pending, log_key, log_file, output_tail)
                pending = b""
            log_file.flush()

        if pending:
            self._handle_output_line(pending, log_key, log_file, output_tail)
            log_file.flush()

    def _handle_output_line(self, raw_line, log_key, log_file, output_tail) -> None:
        line = raw_line.decode("utf-8", errors="replace")
        log_file.write(line + "\n")
        # The file keeps the text; only the in-memory copies drop characters.
        short_line = get_truncated_line(line)
        output_tail.append(short_line)
        if log_key is not None:
            self.emit("log", log_key, short_line)

    async def _stop_process(self, process) -> None:
        """SIGTERM the group; SIGKILL it if anything outlives the grace."""
        self._terminate(process, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_GRACE_SEC
        while time.monotonic() < deadline:
            if not get_is_group_alive(process.pid):
                return
            await asyncio.sleep(0.1)
        self._terminate(process, signal.SIGKILL)

    def _terminate(self, process, sig) -> None:
        # start_new_session=True makes the agent its own group leader, so the
        # pgid is its pid even once the leader has exited - the signal must
        # still reach surviving children.
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _build_gated_item(found: DiscoveredPr) -> PrItem:
    item = PrItem(
        repo=found.repo,
        number=found.number,
        url=found.url,
        title="",
        author="",
        head_sha=found.head_sha,
        matched_axes=found.matched_axes,
    )
    try:
        snapshot = gate.get_gate_snapshot(item.url)
    except gate.GhError as error:
        if found.skip_reason is not None:
            # pr-discover already skipped it; the snapshot only fills in its title.
            item.verdict = Verdict.SKIP
            item.state = PrState.SKIP
            item.skip_reason = found.skip_reason
            return item
        # One unreachable PR must not stop the others.
        item.state = PrState.FAILED
        item.verdict = Verdict.UNKNOWN
        item.error = f"gate check failed: {error}"
        return item

    item.title = snapshot.title
    item.author = snapshot.author
    item.head_sha = snapshot.head_sha
    item.last_marker_sha = snapshot.marker_sha
    if found.skip_reason is not None:
        item.verdict = Verdict.SKIP
        item.state = PrState.SKIP
        item.skip_reason = found.skip_reason
        return item

    item.verdict = gate.decide_verdict(snapshot.head_sha, snapshot.marker_sha)
    if item.verdict is Verdict.SKIP:
        item.state = PrState.SKIP
        item.skip_reason = SkipReason.HEAD_ALREADY_REVIEWED
    return item


def get_truncated_line(line: str) -> str:
    """Cap one line for the UI; the log file still holds it in full."""
    if len(line) <= MAX_UI_LINE_CHARS:
        return line
    return f"{line[:MAX_UI_LINE_CHARS]}… (+{len(line) - MAX_UI_LINE_CHARS} chars)"


def get_progress(record: LoopRecord | None) -> tuple[int, int]:
    if record is None:
        return (0, 0)
    return (record.settled_count, len(record.items))


def get_verdict_counts(record: LoopRecord | None) -> dict[Verdict, int]:
    if record is None:
        return {verdict: 0 for verdict in Verdict}
    return {verdict: record.count_verdict(verdict) for verdict in Verdict}
