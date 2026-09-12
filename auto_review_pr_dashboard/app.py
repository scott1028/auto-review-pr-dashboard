"""Textual UI: dashboard, PR overview table, and the per-PR detail screen."""

from __future__ import annotations

import asyncio
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, RichLog, Static

from .config import RunConfig
from .models import (
    Classification,
    LoopRecord,
    PostKind,
    PrState,
    SkipReason,
    Verdict,
)
from .runner import LoopRunner
from .session import SessionClient, SessionError
from .stats import format_duration, get_progress_bar, get_repo_progress_text, get_stats
from .store import Store

MAX_BUFFERED_LOG_LINES = 2000
# How far back to read a log file when its lines are no longer buffered - a long
# agent run writes far more than any panel shows.
LOG_TAIL_BYTES = 256 * 1024

STATE_STYLE = {
    PrState.QUEUED: "dim",
    PrState.RUNNING: "bold yellow",
    PrState.DONE: "green",
    PrState.FAILED: "bold red",
    PrState.SKIP: "blue",
    PrState.CANCELLED: "magenta",
}

CLASSIFICATION_STYLE = {
    Classification.BLOCKING: "bold red",
    Classification.NON_BLOCKING: "yellow",
    Classification.BLOCKING_QUESTION: "cyan",
}

VERDICT_STYLE = {
    Verdict.FULL: "bold",
    Verdict.INCREMENTAL: "cyan",
    Verdict.SKIP: "dim",
}

# Column label, key, and fixed width; None width means the column fits its content.
TABLE_COLUMNS = [
    ("#", "index", 3),
    ("PR", "pr", None),
    ("Title", "title", None),
    ("Verdict", "verdict", None),
    ("State", "state", None),
    ("New", "new", None),
    ("Took", "took", None),
]


def get_scope_text(config: RunConfig) -> str:
    """What the agent understood from the prompt, so a mis-parse is obvious."""
    parts = []
    if config.repos:
        parts.append(", ".join(config.repos))
    for label, values in (
        ("author", config.authors),
        ("reviewer", config.reviewers),
    ):
        if values:
            parts.append(f"{label} {', '.join(values)}")
    if config.urls:
        parts.append(f"urls {len(config.urls)}")
    return " · ".join(parts) or "(no scope specified)"


class PrTable(DataTable):
    """PR overview list where a single click opens the row.

    Stock DataTable posts RowSelected only when the clicked row is already the
    cursor row, so the first click on any other row would merely move the cursor.
    """

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        clicked_row = meta.get("row")
        was_cursor_row = clicked_row == self.cursor_row
        await super()._on_click(event)
        if clicked_row is None or clicked_row < 0 or "column" not in meta:
            return
        if was_cursor_row:
            return  # super() already posted RowSelected for this click
        self._post_selected_message()


def add_table_columns(table: PrTable) -> None:
    """Add the dashboard columns; a rebuild adds them again to re-measure widths."""
    for label, key, width in TABLE_COLUMNS:
        table.add_column(label, key=key, width=width)


def get_row_cells(index: int, item) -> list:
    """The cells of one PR row, in TABLE_COLUMNS order."""
    title = item.title if len(item.title) <= 44 else item.title[:43] + "…"
    new_posts = (
        "-"
        if item.state in (PrState.QUEUED, PrState.SKIP)
        else str(len(item.new_posts))
    )
    return [
        str(index),
        item.key,
        title,
        Text(item.verdict_label, style=VERDICT_STYLE[item.verdict]),
        Text(item.state.value, style=STATE_STYLE[item.state]),
        new_posts,
        format_duration(item.duration_sec) if item.started_at else "-",
    ]


def get_cells_signature(cells: list) -> tuple:
    """Comparable form of a row's cells; Text.__eq__ ignores style, so keep it."""
    return tuple(
        (cell.plain, str(cell.style)) if isinstance(cell, Text) else (str(cell), "")
        for cell in cells
    )


class Dashboard(Static):
    """The five summary lines above the PR list."""

    def show(
        self,
        config: RunConfig,
        record,
        status: str,
        idle_remaining: float | None,
        block: tuple[str, float] | None = None,
    ) -> None:
        stats = get_stats(record, time.time())
        bar = get_progress_bar(stats["settled"], stats["total"])
        repo_text = get_repo_progress_text(stats["repo_progress"]) or "-"

        next_loop = (
            f" · next loop in {format_duration(idle_remaining)}"
            if idle_remaining is not None
            else ""
        )
        eta = f" · ETA {format_duration(stats['eta_sec'])}" if stats["eta_sec"] else ""

        lines = []
        if block is not None:
            reason, remaining = block
            lines.append(
                f"[b red]⚠ blocked[/b red]  {reason}  ·  retry in "
                f"[b]{format_duration(remaining)}[/b]  ·  [dim]\\[n] retry now[/dim]"
            )
        lines += [
            f"[b]{config.ai_cli}[/b] · {status}",
            f"Scope    {get_scope_text(config)}",
            f"Progress [green]{bar}[/green]  {stats['settled']}/{stats['total']} PR  ·  {repo_text}",
            "Verdict  "
            + " · ".join(
                f"{verdict.value} {stats['verdicts'][verdict]}" for verdict in Verdict
            )
            + f" · Cancelled {stats['states'][PrState.CANCELLED]}"
            + f" · Failed {stats['states'][PrState.FAILED]}",
            "Posted   "
            + f"inline {stats['posts'][PostKind.INLINE]}"
            + f" · summary {stats['posts'][PostKind.SUMMARY]}"
            + f" · passed-reply {stats['posts'][PostKind.REPLY]}"
            + f" · no-change {stats['no_change']}",
            "Finding  "
            + " · ".join(
                f"{classification.value} {stats['findings'][classification]}"
                for classification in Classification
            ),
            f"Timing   loop elapsed {format_duration(stats['elapsed_sec'])}"
            f" · avg {format_duration(stats['avg_sec'])}/PR{eta}{next_loop}",
        ]
        self.update("\n".join(lines))


class DetailScreen(Screen):
    """One PR: verdict, what this loop posted, cross-loop history, and the AI log."""

    BINDINGS = [
        Binding("escape,backspace", "app.pop_screen", "back"),
        Binding("c", "cancel", "cancel this PR"),
        Binding("r", "resume", "resume"),
        Binding("f", "toggle_follow", "follow log"),
    ]

    def __init__(self, pr_key: str):
        super().__init__()
        self.pr_key = pr_key
        self.follow = True
        self.placeholder_shown = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static(id="detail-meta")
            yield Static(id="detail-posts")
            yield Static(id="detail-history")
            yield RichLog(
                id="detail-log",
                max_lines=MAX_BUFFERED_LOG_LINES,
                highlight=False,
                markup=False,
                wrap=False,
            )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_detail()
        self.reload_log()
        self.set_interval(1.0, self.refresh_detail)

    def reload_log(self) -> None:
        """Reload the current loop's log without changing follow mode."""
        log_widget = self.query_one("#detail-log", RichLog)
        log_widget.clear()
        lines = self.app.get_log_lines(self.pr_key)
        self.placeholder_shown = not lines
        for line in lines or ["(no agent log yet — this PR has not started)"]:
            log_widget.write(line)

    def append_log(self, line: str) -> None:
        if not self.follow:
            return
        log_widget = self.query_one("#detail-log", RichLog)
        if self.placeholder_shown:
            log_widget.clear()
            self.placeholder_shown = False
        log_widget.write(line)

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.notify(f"log follow: {'on' if self.follow else 'off'}")

    def action_cancel(self) -> None:
        self.app.cancel_pr(self.pr_key)

    def action_resume(self) -> None:
        self.app.resume_pr(self.pr_key)

    def refresh_detail(self) -> None:
        item = self.app.get_item(self.pr_key)
        if item is None:
            return

        self.query_one("#detail-meta", Static).update(
            f"[b]{item.key}[/b] · {item.title}\n"
            f"Verdict [{VERDICT_STYLE[item.verdict]}]{item.verdict_label}[/] · "
            f"State [{STATE_STYLE[item.state]}]{item.state.value}[/] · "
            f"head {item.short_head} · "
            f"last marker {item.last_marker_sha[:7] if item.last_marker_sha else '(none)'} · "
            f"took {format_duration(item.duration_sec)} · "
            f"exit {item.exit_code if item.exit_code is not None else '-'}"
            + (f"\n[red]{item.error}[/red]" if item.error else "")
            + f"\nmatched: {', '.join(item.matched_axes) or '-'}"
            + f"\nlog: {item.log_path or '-'}"
        )
        self.query_one("#detail-posts", Static).update(self.get_posts_text(item))

        history = self.app.store.get_history(item.key)
        history_text = " · ".join(
            f"Loop #{entry['loop_index']} "
            f"{'Skip (WIP)' if entry['skip_reason'] in (SkipReason.DRAFT.value, SkipReason.WIP_TITLE.value) else entry['verdict']}"
            f"/{entry['state']} "
            f"{entry['posted']} posted"
            for entry in history
        )
        self.query_one("#detail-history", Static).update(
            f"[b]History (across loops)[/b]\n{history_text or '(no earlier loops)'}"
        )

    def get_posts_text(self, item) -> str:
        if not item.new_posts:
            return f"[b]Posted this loop (0)[/b]\n{self.get_no_change_reason(item)}"

        lines = [f"[b]Posted this loop ({len(item.new_posts)})[/b]"]
        for post in item.new_posts:
            # The headline already carries the **blocking** / **non-blocking** prefix
            # verbatim, so only the colour is added here.
            text = post.headline or (
                post.classification.value if post.classification else post.kind.value
            )
            style = CLASSIFICATION_STYLE.get(post.classification, "")
            lines.append(
                f" · {post.kind.value:<7} {f'[{style}]{text}[/]' if style else text}"
            )
            lines.append(f"   {'':<7} [dim]{post.url}[/dim]")
        if item.state is PrState.CANCELLED:
            lines.append(
                "[yellow]Posted before cancelling; if the summary marker never went out, "
                "the next loop reviews this SHA again.[/yellow]"
            )
        return "\n".join(lines)

    def get_no_change_reason(self, item) -> str:
        if item.state is PrState.SKIP:
            if item.skip_reason is SkipReason.DRAFT:
                return "[dim]Skip: Draft PR; no agent started, nothing posted.[/dim]"
            if item.skip_reason is SkipReason.WIP_TITLE:
                return "[dim]Skip: WIP title; no agent started, nothing posted.[/dim]"
            return "[dim]Skip: head SHA matches the last marker; no agent started, nothing posted.[/dim]"
        if item.state is PrState.CANCELLED:
            return "[magenta]Cancelled: nothing was posted before the agent stopped.[/magenta]"
        if item.state is PrState.FAILED:
            return f"[red]Failed: {item.error or 'the agent exited abnormally'}; nothing posted.[/red]"
        if item.state is PrState.DONE:
            return "[dim]The agent posted nothing (unchanged head SHA, or zero findings).[/dim]"
        return "[dim]Not started yet.[/dim]"


class ConfirmQuitScreen(ModalScreen[bool]):
    """Double check on q while an agent runs. No holds focus, so quitting takes a move."""

    BINDINGS = [
        Binding("escape,q", "cancel", "keep running"),
        Binding("left", "app.focus_previous", "no", show=False),
        Binding("right", "app.focus_next", "yes", show=False),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-quit"):
            yield Static(f"[b]Quit?[/b]\n{self.message}", id="confirm-quit-message")
            with Horizontal(id="confirm-quit-buttons"):
                yield Button("No, keep running", id="confirm-quit-no", variant="primary")
                yield Button("Yes, quit", id="confirm-quit-yes", variant="error")
            yield Static("[dim]←/→ to choose · enter to confirm · esc to keep running[/dim]")

    def on_mount(self) -> None:
        self.query_one("#confirm-quit-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-quit-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class AutoReviewPrDashboardApp(App):
    CSS = """
    Dashboard {
        height: auto;
        padding: 0 1;
        border: round $accent;
    }
    PrTable {
        height: 1fr;
    }
    #activity-log {
        height: 10;
        border: round $accent;
        display: none;
    }
    #detail-meta, #detail-posts, #detail-history {
        height: auto;
        padding: 0 1;
        border: round $accent;
    }
    #detail-log {
        height: 1fr;
        min-height: 6;
        border: round $accent;
    }
    ConfirmQuitScreen {
        align: center middle;
        background: $background 60%;
    }
    #confirm-quit {
        width: 66;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #confirm-quit-buttons {
        height: auto;
        align: center middle;
        padding: 1 0 0 0;
    }
    #confirm-quit-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("enter", "open_detail", "detail"),
        Binding("c", "cancel", "cancel"),
        Binding("r", "resume", "resume"),
        Binding("n", "run_now", "run now"),
        Binding("p", "toggle_pause", "pause"),
        Binding("d", "background", "background"),
        Binding("l", "toggle_activity_log", "agent log"),
        Binding("q", "quit_app", "quit"),
    ]

    TITLE = "auto-review-pr-dashboard"

    def __init__(
        self,
        config: RunConfig,
        store: Store | None = None,
        session: SessionClient | None = None,
    ):
        super().__init__()
        self.config = config
        self.store = store or Store()
        self.session = session
        self.runner: LoopRunner | None = None
        self.record = None
        self.status = "starting"
        self.paused = False
        self.idle_remaining: float | None = None
        self.next_run_at: float | None = None
        self.block_reason = ""
        self.block_remaining: float | None = None
        self.blocked_until: float | None = None
        self._log_buffer: dict[str, list[str]] = {}
        # Which agent's output the activity panel is following, and whether the
        # user pinned it open (otherwise it auto-hides once the queue is ready).
        self.activity_key: str | None = None
        self.activity_pinned = False
        self.phase_started_at = time.time()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Dashboard(id="dashboard")
        yield RichLog(
            id="activity-log",
            max_lines=MAX_BUFFERED_LOG_LINES,
            highlight=False,
            markup=False,
            wrap=False,
        )
        yield PrTable(id="pr-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        add_table_columns(self.query_one(PrTable))
        self._event_loop = asyncio.get_running_loop()
        if self.session is not None:
            self.run_worker(self.connect_session(), name="session")
        else:
            self.runner = LoopRunner(
                self.config, self.store, on_event=self.emit_runner_event
            )
            self.run_worker(self.runner.run_forever(), name="runner")
        self.set_interval(1.0, self.refresh_view)

    def on_unmount(self) -> None:
        if self.session is not None:
            self.session.disconnect()
        elif self.runner is not None:
            self.runner.stop()

    async def connect_session(self) -> None:
        try:
            await self.session.connect(self.handle_session_message)
        except (ConnectionError, OSError, SessionError) as error:
            self.notify(str(error), severity="error", timeout=10)
            self.exit(result="connection-error")

    def handle_session_message(self, message: dict) -> None:
        message_type = message.get("type")
        if message_type == "snapshot":
            self.config = RunConfig(**message["config"])
            record = message.get("record")
            next_record = None if record is None else LoopRecord.from_dict(record)
            current_loop = None if self.record is None else self.record.index
            next_loop = None if next_record is None else next_record.index
            new_loop = next_loop is not None and next_loop != current_loop
            self.record = next_record
            if new_loop:
                # A repeated PR key must not retain the previous loop's lines.
                self._log_buffer.clear()
                self.reset_log_views()
            self.status = message["status"]
            self.paused = bool(message["paused"])
            self.next_run_at = message.get("next_run_at")
            self.blocked_until = message.get("blocked_until")
            self.block_reason = message.get("block_reason", "")
            self.phase_started_at = message["phase_started_at"]
            current_key = message.get("current_key")
            if current_key is not None and current_key != self.activity_key:
                # Switching through show_activity_log() clears the panel, replays
                # that PR's buffer, and retitles it; a bare assignment would mix
                # two PRs' output under the old title.
                self.show_activity_log(
                    current_key, self.query_one("#activity-log", RichLog).display
                )
            self.refresh_view()
        elif message_type == "tick":
            # Only sent while paused: the runner keeps pushing the deadline out.
            self.next_run_at = message.get("next_run_at")
            self.blocked_until = message.get("blocked_until")
            self.refresh_view()
        elif message_type == "log":
            self.handle_runner_event("log", (message["key"], message["line"]))
        elif message_type == "notice":
            self.notify(
                message["message"],
                severity=message.get("severity", "information"),
            )
        elif message_type == "error":
            self.notify(message["message"], severity="error", timeout=10)
            if message.get("fatal"):
                self.exit(result="connection-error")
        elif message_type == "replaced":
            self.notify(message["message"], severity="warning")
            self.exit(result="replaced")
        elif message_type == "disconnected":
            self.notify(message["message"], severity="error")
            self.exit(result="disconnected")

    # ---- runner events ------------------------------------------------------

    def emit_runner_event(self, name: str, *payload) -> None:
        """Called from the event loop AND from gh worker threads, so always hop
        back onto the loop before touching widgets."""
        if self._event_loop is None:
            return
        self._event_loop.call_soon_threadsafe(self.handle_runner_event, name, payload)

    def handle_runner_event(self, name: str, payload: tuple) -> None:
        if name == "log":
            key, line = payload
            if key not in self._log_buffer:
                # One agent runs at a time, so only the newest PR needs a buffer;
                # the others fall back to their log file in get_log_lines().
                self._log_buffer.clear()
            buffer = self._log_buffer.setdefault(key, [])
            buffer.append(line)
            del buffer[:-MAX_BUFFERED_LOG_LINES]
            self.append_activity_log(key, line)
            if isinstance(self.screen, DetailScreen) and self.screen.pr_key == key:
                self.screen.append_log(line)
            return

        if name == "loop_start":
            self.status = f"Loop #{payload[0]} · discovering"
            self.record = None
            self._log_buffer.clear()
            self.reset_log_views()
            self.phase_started_at = time.time()
        elif name == "discovery_progress":
            done, total, _item = payload
            self.status = f"Loop #{self.runner.loop_index} · discovering {done}/{total}"
        elif name == "items_ready":
            self.record = payload[0]
            self.status = f"Loop #{self.record.index} · running"
            self.idle_remaining = None
            self.phase_started_at = time.time()
            if not self.activity_pinned:
                self.show_activity_log(None, False)
        elif name == "loop_end":
            self.record = payload[0]
            self.status = f"Loop #{self.record.index} · finished"
        elif name == "idle_start":
            self.idle_remaining = max(0.0, payload[0] - time.time())
        elif name == "idle_tick":
            self.idle_remaining = payload[0]
        elif name == "blocked":
            self.block_reason, blocked_until = payload
            self.block_remaining = max(0.0, blocked_until - time.time())
            self.notify(f"blocked: {self.block_reason}", severity="warning", timeout=15)
        elif name == "blocked_tick":
            self.block_remaining = payload[0]
        elif name == "unblocked":
            self.block_reason = ""
            self.block_remaining = None
        elif name == "paused":
            self.paused = payload[0]
        elif name == "error":
            self.notify(str(payload[0]), severity="error", timeout=10)

        if name == "item_update" and payload[0].state is PrState.RUNNING:
            self.show_activity_log(payload[0].key, self.activity_pinned)

        self.refresh_view()

    # ---- view ---------------------------------------------------------------

    def get_phase_elapsed_text(self) -> str:
        """Something that visibly moves even while the agent is silent."""
        elapsed = format_duration(time.time() - self.phase_started_at)
        # criteria parsing removed
        return elapsed

    def refresh_view(self) -> None:
        if self.next_run_at is not None:
            self.idle_remaining = max(0.0, self.next_run_at - time.time())
        elif self.session is not None:
            self.idle_remaining = None
        if self.blocked_until is not None:
            self.block_remaining = max(0.0, self.blocked_until - time.time())
        elif self.session is not None:
            self.block_remaining = None
        status = f"{self.status} · {self.get_phase_elapsed_text()}"
        if self.paused:
            status = f"{status} · [yellow]paused[/yellow]"
        block = (
            (self.block_reason, self.block_remaining)
            if self.block_remaining is not None
            else None
        )
        self.query_one(Dashboard).show(
            self.config, self.record, status, self.idle_remaining, block
        )
        self.refresh_table()

    def refresh_table(self) -> None:
        """Reconcile the table with the record by row key.

        Clearing the table every second would leave auto-width columns at their
        historical maximum, so the scrollbar keeps room for text that is gone, and
        clear() resets scroll_x, snapping a scrolled reader back left. An ordinary
        refresh therefore pushes only changed cells; the table is rebuilt once the
        row set itself changes, since re-adding the columns is what lets a column
        shrink after its widest row is gone.
        """
        table = self.query_one(PrTable)
        items = [] if self.record is None else list(self.record.items)
        keys = [item.key for item in items]
        ordered_keys = [row.key for row in table.ordered_rows]

        if ordered_keys == keys:
            for index, item in enumerate(items, start=1):
                cells = get_row_cells(index, item)
                if get_cells_signature(cells) == get_cells_signature(
                    table.get_row(item.key)
                ):
                    continue
                for (_label, column_key, _width), value in zip(TABLE_COLUMNS, cells):
                    table.update_cell(item.key, column_key, value, update_width=True)
            return

        cursor_row = table.cursor_row
        cursor_key = ordered_keys[cursor_row] if ordered_keys else None
        scroll_x = table.scroll_x
        table.clear(columns=True)
        add_table_columns(table)
        for index, item in enumerate(items, start=1):
            table.add_row(*get_row_cells(index, item), key=item.key)
        if items:
            # The cursor follows its own PR rather than the row number it sat on.
            row = (
                keys.index(cursor_key)
                if cursor_key in keys
                else min(cursor_row, len(items) - 1)
            )
            table.move_cursor(row=row)
        # clear() zeroes scroll_x and scroll_target_x, and the next wheel gesture is
        # derived from the target, so restoring only scroll_x snaps back left on it.
        table.scroll_target_x = table.scroll_x = scroll_x

    # ---- activity log panel -------------------------------------------------

    def reset_log_views(self) -> None:
        """Clear rendered log content and identity so a new loop starts fresh."""
        self.activity_key = None
        panel = self.query_one("#activity-log", RichLog)
        panel.clear()
        panel.border_title = "agent log · -"
        if isinstance(self.screen, DetailScreen):
            self.screen.reload_log()

    def show_activity_log(self, key: str | None, visible: bool) -> None:
        """Point the panel at one agent's output and show or hide it."""
        panel = self.query_one("#activity-log", RichLog)
        if key is not None and key != self.activity_key:
            self.activity_key = key
            panel.clear()
            for line in self.get_log_lines(key):
                panel.write(line)
        panel.border_title = f"agent log · {self.activity_key or '-'}"
        panel.display = visible

    def append_activity_log(self, key: str, line: str) -> None:
        panel = self.query_one("#activity-log", RichLog)
        if panel.display and key == self.activity_key:
            panel.write(line)

    def action_toggle_activity_log(self) -> None:
        panel = self.query_one("#activity-log", RichLog)
        if panel.display:
            self.activity_pinned = False
            panel.display = False
            return
        self.activity_pinned = True
        key = self.activity_key or "loop"
        self.activity_key = None  # force a replay of the buffer
        self.show_activity_log(key, True)

    def get_item(self, key: str):
        return self.record.item_by_key(key) if self.record else None

    def get_log_lines(self, key: str) -> list[str]:
        buffered = self._log_buffer.get(key)
        if buffered:
            return list(buffered)

        item = self.get_item(key)
        if item is None or not item.log_path:
            return []
        # Tail only: reading a whole log of a long run would pull all of it into
        # memory just to show the last screenful.
        try:
            with open(item.log_path, "rb") as log_file:
                log_file.seek(0, 2)
                start = max(0, log_file.tell() - LOG_TAIL_BYTES)
                log_file.seek(start)
                tail = log_file.read()
        except OSError:
            return []
        if start:
            tail = tail.partition(b"\n")[2]  # drop the half line the seek landed in
        lines = tail.decode("utf-8", errors="replace").splitlines()
        return lines[-MAX_BUFFERED_LOG_LINES:]

    def get_cursor_key(self) -> str | None:
        table = self.query_one(PrTable)
        if self.record is None or not self.record.items:
            return None
        if not 0 <= table.cursor_row < len(self.record.items):
            return None
        return self.record.items[table.cursor_row].key

    # ---- actions ------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Fires for both a mouse click and enter on the highlighted row."""
        self.push_screen(DetailScreen(str(event.row_key.value)))

    def action_open_detail(self) -> None:
        key = self.get_cursor_key()
        if key:
            self.push_screen(DetailScreen(key))

    def cancel_pr(self, key: str) -> None:
        if self.session is not None:
            self.send_session_command("cancel", key)
            self.notify(f"cancel requested for {key}")
        elif self.runner and self.runner.cancel(key):
            self.notify(f"cancelled {key}")
        else:
            self.notify(f"{key} is not queued or running", severity="warning")
        self.refresh_view()

    def resume_pr(self, key: str) -> None:
        if self.session is not None:
            self.send_session_command("resume", key)
            self.notify(f"resume requested for {key}")
        elif self.runner and self.runner.resume(key):
            self.notify(f"{key} requeued at the tail")
        else:
            self.notify(f"{key} is not cancelled", severity="warning")
        self.refresh_view()

    def action_cancel(self) -> None:
        key = self.get_cursor_key()
        if key:
            self.cancel_pr(key)

    def action_resume(self) -> None:
        key = self.get_cursor_key()
        if key:
            self.resume_pr(key)

    def action_run_now(self) -> None:
        if self.session is not None:
            self.send_session_command("run_now")
        elif self.runner is not None:
            self.runner.run_now()
        else:
            return
        self.notify(
            "cooldown ended, retrying now"
            if self.block_remaining is not None
            else "skipping the countdown, starting the next loop"
        )

    def action_toggle_pause(self) -> None:
        if self.session is not None:
            self.send_session_command("toggle_pause")
        elif self.runner:
            paused = self.runner.toggle_pause()
            self.notify("paused after the current PR" if paused else "resumed")

    def action_background(self) -> None:
        if self.session is None:
            self.notify("background mode needs a daemon session", severity="warning")
            return
        self.exit(result="background")

    def action_quit_app(self) -> None:
        running_keys = self.get_running_keys()
        if not running_keys:
            self.quit_app()
            return
        self.push_screen(
            ConfirmQuitScreen(self.get_quit_message(running_keys)),
            self.handle_quit_confirmed,
        )

    def get_running_keys(self) -> list[str]:
        if self.record is None:
            return []
        return [
            item.key for item in self.record.items if item.state is PrState.RUNNING
        ]

    def get_quit_message(self, running_keys: list[str]) -> str:
        running = ", ".join(running_keys)
        tail = (
            "Quitting stops the daemon, so the queue stops with it. "
            "Press [b]d[/b] instead to background the run."
            if self.session is not None
            else "Quitting stops the review loop as well."
        )
        return f"Still reviewing {running}; its agent gets killed.\n{tail}"

    def handle_quit_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.quit_app()

    def quit_app(self) -> None:
        if self.session is not None:
            self.run_worker(self.stop_session(), name="stop-session")
            return
        if self.runner:
            self.runner.stop()
        self.exit()

    def send_session_command(self, action: str, key: str | None = None) -> None:
        self.run_worker(
            self.send_session_command_async(action, key),
            name=f"session-command-{action}",
        )

    async def send_session_command_async(
        self, action: str, key: str | None = None
    ) -> None:
        try:
            await self.session.send_command(action, key)
        except (ConnectionError, OSError, SessionError) as error:
            self.notify(str(error), severity="error", timeout=10)

    async def stop_session(self) -> None:
        try:
            await self.session.send_command("stop")
        except (ConnectionError, OSError, SessionError) as error:
            self.notify(str(error), severity="error", timeout=10)
        finally:
            self.exit(result="stopped")
