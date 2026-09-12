"""Loop orchestration: discover, queue, run one AI process per PR, cancel, resume.

UI-agnostic on purpose - it only pushes events through `on_event`, so the whole
queue behaviour is testable with a mock AI CLI and no terminal.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections import deque

from dataclasses import dataclass, field

from . import gh
from .config import RunConfig, get_ai_command, get_is_usage_limit
from .models import LoopRecord, PrItem, PrState, Verdict
from .store import Store

TERMINATE_GRACE_SEC = 5
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


class LoopRunner:
    def __init__(self, config: RunConfig, store: Store, on_event=None):
        self.config = config
        self.store = store
        self.on_event = on_event or (lambda *args: None)

        self.loop_index = 0
        self.record: LoopRecord | None = None
        self.queue: deque[str] = deque()
        self.paused = False
        self.next_run_at: float | None = None
        self.blocked_until: float | None = None
        self.block_reason = ""
        self.consecutive_failures = 0

        self._stopped = False
        self._run_now = False
        self._current_key: str | None = None
        self._current_process: asyncio.subprocess.Process | None = None
        self._cancelled_keys: set[str] = set()

    # ---- events -------------------------------------------------------------

    def emit(self, name: str, *payload) -> None:
        self.on_event(name, *payload)

    # ---- control ------------------------------------------------------------

    def stop(self) -> None:
        self._stopped = True
        self._run_now = True
        if self._current_process is not None:
            self._terminate(self._current_process, signal.SIGTERM)

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

        self._cancelled_keys.add(key)
        if item.state is PrState.QUEUED:
            self._remove_from_queue(key)
            item.state = PrState.CANCELLED
            self.emit("item_update", item)
            return True

        if self._current_process is not None and self._current_key == key:
            item.error = "cancel requested"
            self.emit("item_update", item)
            # SIGTERM now, SIGKILL after the grace period if it ignores us.
            asyncio.get_running_loop().create_task(
                self._stop_process(self._current_process)
            )
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

    # ---- main loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        while not self._stopped:
            await self.run_loop()
            if self._stopped:
                break
            if self.blocked_until is not None:
                await self._wait_while_blocked()
            else:
                await self._idle_until_next_loop()

    async def _idle_until_next_loop(self) -> None:
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

        if not self.validate_scope():
            items = []
        else:
            try:
                items = await asyncio.to_thread(
                    gh.discover_pr_items,
                    self.config,
                    lambda done, total, item: self.emit(
                        "discovery_progress", done, total, item
                    ),
                )
            except gh.GhError as error:
                self.emit("error", f"discovery failed: {error}")
                items = []

        self.record.items = items
        self.queue = deque(item.key for item in items if item.state is PrState.QUEUED)
        self.consecutive_failures = 0
        self.emit("items_ready", self.record)

        while self.queue and not self._stopped:
            await self._wait_until_runnable()
            if self._stopped:
                break
            key = self.queue.popleft()
            item = self.record.item_by_key(key)
            if item is None or item.state is not PrState.QUEUED:
                continue
            await self._run_item(item)

        self.record.finished_at = time.time()
        self.store.save_loop(self.record)
        self.emit("loop_end", self.record)
        return self.record

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

    # ---- scope validation ---------------------------------------------------

    def validate_scope(self) -> bool:
        """Validate that CLI-provided scope is usable. False = skip this loop."""
        if not self.config.repos and not self.config.urls:
            self.emit("error", "no --repo-url or --pr-url specified")
            return False
        return True

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
        if not self.start_cooldown(f"{item.key}: {reason}", item.key, item.error):
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

    async def _spawn_agent(
        self, command, log_path, timeout_sec, log_key=None
    ) -> AgentRun:
        """Run one agent process, streaming its output to a log file."""
        output_tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
        error = ""
        exit_code = None
        timed_out = False

        # Opened before the spawn, so a log that cannot be written is reported as
        # itself instead of being blamed on the agent binary.
        try:
            log_file = open(log_path, "a", encoding="utf-8")
        except OSError as os_error:
            return AgentRun(
                exit_code=1, error=f"cannot open log {log_path}: {os_error}"
            )

        try:
            with log_file:
                log_file.write(f"$ {' '.join(command[:-1])} <prompt>\n")
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                self._current_process = process
                try:
                    await asyncio.wait_for(
                        self._pump_output(process, log_key, log_file, output_tail),
                        timeout=timeout_sec,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    error = f"timed out after {round(timeout_sec / 60)}min"
                    await self._stop_process(process)
                except Exception as pump_error:
                    # This coroutine runs as a Textual worker: an escaping error
                    # takes the whole app down and leaves the agent alive in its
                    # own process group. Fail just this PR instead.
                    error = f"reading agent output failed: {pump_error}"
                    await self._stop_process(process)
                exit_code = await process.wait()
        except FileNotFoundError:
            # The command always execs `bash`, so this is bash missing - a real
            # "<ai_cli>: command not found" comes back as bash's own exit 127.
            error = f"cannot start {self.config.ai_cli}: bash not found"
            exit_code = 127
        except OSError as os_error:
            error = str(os_error)
            exit_code = 1
        finally:
            self._current_process = None

        return AgentRun(
            exit_code=exit_code,
            output_tail=output_tail,
            error=error,
            timed_out=timed_out,
        )

    async def _run_item(self, item: PrItem) -> None:
        item.state = PrState.RUNNING
        item.error = ""
        item.started_at = time.time()
        item.log_path = str(self.store.get_log_path(self.loop_index, item))
        self._current_key = item.key
        self.emit("item_update", item)

        before = await self._snapshot(item)
        command = get_ai_command(
            self.config.ai_cli,
            self.config.prompt,
            item.repo,
            item.url,
        )
        try:
            run = await self._spawn_agent(
                command,
                item.log_path,
                self.config.pr_timeout_min * 60,
                log_key=item.key,
            )
        finally:
            self._current_key = None
        item.exit_code = run.exit_code

        after = await self._snapshot(item)
        item.new_posts = gh.classify_new_posts(before, after)
        item.finished_at = time.time()

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

        self._handle_failure(item, run.output_tail, run.timed_out, run.error)

    def _handle_failure(
        self, item: PrItem, output_tail, timed_out, preset_error
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

        self.consecutive_failures += 1
        is_usage_limit = get_is_usage_limit(failure_text)
        is_systemic = (
            is_usage_limit or self.consecutive_failures >= SYSTEMIC_FAILURE_THRESHOLD
        )

        if not is_systemic or item.new_posts:
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

    async def _snapshot(self, item: PrItem) -> dict:
        try:
            return await asyncio.to_thread(gh.snapshot_comments, item.repo, item.number)
        except gh.GhError as error:
            self.emit("error", f"{item.key}: comment snapshot failed: {error}")
            return {"review": {}, "issue": {}}

    async def _pump_output(self, process, log_key, log_file, output_tail) -> None:
        """Stream the agent's output, splitting lines ourselves.

        `read()` has no separator limit, so a line longer than asyncio's own
        readline() limit is data rather than a crash.
        """
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
        self._terminate(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SEC)
        except asyncio.TimeoutError:
            self._terminate(process, signal.SIGKILL)

    def _terminate(self, process, sig) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass


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
