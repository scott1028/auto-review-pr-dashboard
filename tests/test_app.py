import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.text import Text
from textual.widgets import RichLog, Static

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import runner as runner_module
from auto_review_pr_dashboard.app import (
    MAX_BUFFERED_LOG_LINES,
    STATE_STYLE,
    AutoReviewPrDashboardApp,
    ConfirmQuitScreen,
    Dashboard,
    DetailScreen,
    PrTable,
    get_cells_signature,
)
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.models import (
    LoopRecord,
    PrItem,
    PrState,
    SkipReason,
    Verdict,
)
from auto_review_pr_dashboard.store import Store


def make_item(number, repo="o/r", state=PrState.QUEUED, verdict=Verdict.FULL, title=None):
    return PrItem(
        repo=repo,
        number=number,
        url=f"https://github.com/{repo}/pull/{number}",
        title=f"pr {number}" if title is None else title,
        author="me",
        head_sha="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        verdict=verdict,
        state=state,
    )


async def noop_forever(_runner):
    return None


class FakeRunner:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeSession:
    def __init__(self, config, record=None):
        self.commands = []
        self.is_disconnected = False
        self.snapshot = {
            "type": "snapshot",
            "config": config.__dict__,
            "record": None if record is None else record.to_dict(),
            "status": "Loop #1 · running",
            "paused": False,
            "next_run_at": None,
            "blocked_until": None,
            "block_reason": "",
            "current_key": None,
            "phase_started_at": time.time(),
        }

    async def connect(self, on_message):
        on_message(self.snapshot)

    async def send_command(self, action, key=None):
        self.commands.append((action, key))

    def disconnect(self):
        self.is_disconnected = True


class AppCoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig("codex-personal", "review", ["o/r"], ["me"], ["me"])
        self.app = AutoReviewPrDashboardApp(self.config, store=Store(Path(self.tmp.name)))
        self.runner_patch = patch.object(
            runner_module.LoopRunner, "run_forever", new=noop_forever
        )
        self.runner_patch.start()

    def tearDown(self):
        self.runner_patch.stop()
        self.tmp.cleanup()

    def load_items(self, app, items):
        record = LoopRecord(index=1, started_at=0.0, items=items)
        app.runner.record = record
        app.handle_runner_event("items_ready", (record,))

    async def test_dashboard_and_table_show_core_review_state(self):
        self.config.repos = ["o/r1", "o/r2"]
        self.config.authors = ["<author-username>"]
        self.config.urls = ["https://github.com/o/r1/pull/12"]
        app = self.app
        async with app.run_test() as pilot:
            self.load_items(
                app,
                [
                    make_item(12, "o/r1"),
                    make_item(15, "o/r2", verdict=Verdict.INCREMENTAL),
                    make_item(18, "o/r2", PrState.SKIP, Verdict.SKIP),
                ],
            )
            await pilot.pause()
            self.assertEqual(app.query_one(PrTable).row_count, 3)
            rendered = str(app.query_one(Dashboard).content)
            expected = (
                "Full 1 · Incremental 1 · Skip 1|1/3 PR|codex-personal|o/r1|o/r2|"
                "author <author-username>|urls 1|r1 0/1 · r2 1/2"
            )
            for text in expected.split("|"):
                self.assertIn(text, rendered)

    async def test_enter_opens_selected_pr_detail(self):
        app = self.app
        async with app.run_test() as pilot:
            self.load_items(app, [make_item(12), make_item(15)])
            await pilot.pause()
            table = app.query_one(PrTable)
            table.move_cursor(row=1)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertEqual(app.screen.pr_key, "o/r#15")

    def test_wip_skip_reason_text(self):
        item = make_item(12, state=PrState.SKIP, verdict=Verdict.SKIP)
        item.skip_reason = SkipReason.WIP_TITLE
        screen = DetailScreen(item.key)

        self.assertIn("WIP title", screen.get_no_change_reason(item))

    def test_draft_skip_reason_text(self):
        item = make_item(12, state=PrState.SKIP, verdict=Verdict.SKIP)
        item.skip_reason = SkipReason.DRAFT
        screen = DetailScreen(item.key)

        self.assertIn("Draft PR", screen.get_no_change_reason(item))

    async def test_history_shows_wip_label_and_supports_legacy_loop_json(self):
        item = make_item(12, state=PrState.SKIP, verdict=Verdict.SKIP)
        item.skip_reason = SkipReason.WIP_TITLE
        self.app.store.save_loop(LoopRecord(index=1, started_at=0.0, items=[item]))
        legacy_path = self.app.store.loops_dir / "loop-0002.json"
        legacy_payload = LoopRecord(
            index=2,
            started_at=0.0,
            items=[make_item(12, state=PrState.SKIP, verdict=Verdict.SKIP)],
        ).to_dict()
        del legacy_payload["items"][0]["skip_reason"]
        legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
        draft_item = make_item(12, state=PrState.SKIP, verdict=Verdict.SKIP)
        draft_item.skip_reason = SkipReason.DRAFT
        self.app.store.save_loop(
            LoopRecord(index=3, started_at=0.0, items=[draft_item])
        )

        async with self.app.run_test() as pilot:
            self.load_items(self.app, [item])
            self.app.push_screen(DetailScreen(item.key))
            await pilot.pause()
            rendered = str(self.app.screen.query_one("#detail-history").content)

        self.assertIn("Loop #1 Skip (WIP)/skip", rendered)
        self.assertIn("Loop #2 Skip/skip", rendered)
        self.assertIn("Loop #3 Skip (WIP)/skip", rendered)

    async def test_list_cancel_and_resume_update_queue_tail(self):
        app = self.app
        async with app.run_test() as pilot:
            self.load_items(app, [make_item(12), make_item(15)])
            app.runner.queue.extend(["o/r#12", "o/r#15"])
            await pilot.pause()
            app.query_one(PrTable).focus()
            cases = (
                ("c", PrState.CANCELLED, ["o/r#15"]),
                ("r", PrState.QUEUED, ["o/r#15", "o/r#12"]),
            )
            for key, state, queue in cases:
                await pilot.press(key)
                await pilot.pause()
                self.assertIs(app.get_item("o/r#12").state, state)
                self.assertEqual(list(app.runner.queue), queue)

    async def test_log_buffer_keeps_only_the_newest_pr(self):
        app = self.app
        async with app.run_test() as pilot:
            self.load_items(app, [make_item(12), make_item(15)])
            app.handle_runner_event("log", ("o/r#12", "first"))
            app.handle_runner_event("log", ("o/r#15", "second"))
            await pilot.pause()
            self.assertEqual(app._log_buffer, {"o/r#15": ["second"]})

    async def test_get_log_lines_reads_only_the_tail_of_a_big_log(self):
        lines = [f"line {index:07d}" for index in range(60000)]
        log_path = Path(self.tmp.name) / "big.log"
        log_path.write_text("\n".join(lines) + "\n")
        item = make_item(12)
        item.log_path = str(log_path)

        app = self.app
        async with app.run_test() as pilot:
            self.load_items(app, [item])
            await pilot.pause()
            tail = app.get_log_lines("o/r#12")

        self.assertEqual(len(tail), MAX_BUFFERED_LOG_LINES)
        self.assertEqual(tail[-1], lines[-1])
        self.assertNotIn(lines[0], tail)

    async def test_blocked_banner_shows_and_unblocked_clears(self):
        app = self.app
        async with app.run_test() as pilot:
            self.load_items(app, [make_item(12), make_item(15)])
            app.handle_runner_event(
                "blocked", ("o/r#12: usage limit / rate limited", time.time() + 1800)
            )
            await pilot.pause()
            rendered = str(app.query_one(Dashboard).content)
            for text in ("⚠ blocked", "usage limit / rate limited", "retry in", "29m"):
                self.assertIn(text, rendered)
            app.handle_runner_event("unblocked", ())
            await pilot.pause()
            self.assertNotIn("⚠ blocked", str(app.query_one(Dashboard).content))

    async def test_background_exits_only_the_remote_dashboard(self):
        session = FakeSession(self.config)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")

        self.assertEqual(app.return_value, "background")
        self.assertEqual(session.commands, [])
        self.assertTrue(session.is_disconnected)

    async def test_quit_sends_stop_before_exiting_remote_dashboard(self):
        session = FakeSession(self.config)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()

        self.assertEqual(app.return_value, "stopped")
        self.assertEqual(session.commands, [("stop", None)])
        self.assertTrue(session.is_disconnected)

    async def test_quit_with_a_running_pr_asks_first_and_defaults_to_no(self):
        app = self.app
        async with app.run_test() as pilot:
            app.runner = FakeRunner()
            self.load_items(app, [make_item(12, state=PrState.RUNNING)])
            await pilot.press("q")
            await pilot.pause()

            self.assertIsInstance(app.screen, ConfirmQuitScreen)
            self.assertEqual(app.focused.id, "confirm-quit-no")
            message = app.screen.query_one("#confirm-quit-message", Static)
            self.assertIn("o/r#12", str(message.content))
            self.assertFalse(app.runner.stopped)

            await pilot.press("enter")  # the default answer keeps the run alive
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmQuitScreen)
            self.assertFalse(app.runner.stopped)
            self.assertTrue(app.is_running)

            await pilot.press("q")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmQuitScreen)
            self.assertFalse(app.runner.stopped)

    async def test_quit_stops_the_run_once_yes_is_chosen(self):
        app = self.app
        async with app.run_test() as pilot:
            app.runner = FakeRunner()
            self.load_items(app, [make_item(12, state=PrState.RUNNING)])
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.focused.id, "confirm-quit-yes")

            await pilot.press("enter")
            await pilot.pause()

        self.assertTrue(app.runner.stopped)

    async def test_quit_skips_the_dialog_when_nothing_is_running(self):
        app = self.app
        async with app.run_test() as pilot:
            app.runner = FakeRunner()
            self.load_items(app, [make_item(12, state=PrState.DONE)])
            await pilot.press("q")
            await pilot.pause()

        self.assertTrue(app.runner.stopped)

    async def test_remote_snapshot_clears_log_buffer_for_a_new_loop(self):
        first_record = LoopRecord(1, time.time(), items=[make_item(12)])
        session = FakeSession(self.config, first_record)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            app._log_buffer = {"o/r#12": ["old loop"]}
            next_snapshot = session.snapshot | {
                "record": LoopRecord(
                    2, time.time(), items=[make_item(12)]
                ).to_dict()
            }
            app.handle_session_message(next_snapshot)

            self.assertEqual(app._log_buffer, {})

    async def test_remote_tick_keeps_a_paused_countdown_from_hitting_zero(self):
        session = FakeSession(self.config)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            deadline = time.time() + 600
            app.handle_session_message(
                session.snapshot | {"paused": True, "next_run_at": deadline}
            )
            await pilot.pause()
            paused_remaining = app.idle_remaining

            # The daemon holds the countdown by pushing the deadline back.
            app.handle_session_message(
                {
                    "type": "tick",
                    "next_run_at": deadline + 1,
                    "blocked_until": None,
                }
            )
            await pilot.pause()

        self.assertIsNotNone(paused_remaining)
        self.assertGreater(app.idle_remaining, paused_remaining)

    async def test_remote_tick_keeps_a_paused_cooldown_from_hitting_zero(self):
        session = FakeSession(self.config)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            blocked_until = time.time() + 900
            app.handle_session_message(
                session.snapshot | {"paused": True, "blocked_until": blocked_until}
            )
            await pilot.pause()
            paused_remaining = app.block_remaining

            # The daemon holds the cooldown by pushing its deadline out too.
            app.handle_session_message(
                {
                    "type": "tick",
                    "next_run_at": None,
                    "blocked_until": blocked_until + 1,
                }
            )
            await pilot.pause()

        self.assertIsNotNone(paused_remaining)
        self.assertGreater(app.block_remaining, paused_remaining)

    async def test_remote_snapshot_switches_a_pinned_log_panel(self):
        session = FakeSession(self.config)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#activity-log", RichLog)
            app._log_buffer = {"o/r#12": ["agent A"], "o/r#15": ["agent B"]}
            app.show_activity_log("o/r#12", True)
            app.activity_pinned = True
            await pilot.pause()

            app.handle_session_message(session.snapshot | {"current_key": "o/r#15"})
            await pilot.pause()
            rendered = "".join(line.text for line in panel.lines)

            self.assertEqual(app.activity_key, "o/r#15")
            self.assertIn("o/r#15", panel.border_title)
            self.assertIn("agent B", rendered)
            self.assertNotIn("agent A", rendered)
            self.assertTrue(panel.display)

    async def test_remote_snapshot_clears_the_panel_for_a_repeated_pr_key(self):
        loop_one = LoopRecord(index=1, started_at=0.0, items=[make_item(12)])
        session = FakeSession(self.config, record=loop_one)
        app = AutoReviewPrDashboardApp(
            self.config, store=Store(Path(self.tmp.name)), session=session
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#activity-log", RichLog)
            app._log_buffer = {"o/r#12": ["loop-1 output"]}
            app.show_activity_log("o/r#12", True)
            app.activity_pinned = True
            await pilot.pause()

            # Loop 2, same PR key: the panel must not carry loop 1's lines.
            loop_two = LoopRecord(index=2, started_at=0.0, items=[make_item(12)])
            app.handle_session_message(
                session.snapshot
                | {"record": loop_two.to_dict(), "current_key": "o/r#12"}
            )
            app.handle_session_message(
                {"type": "log", "key": "o/r#12", "line": "loop-2 output"}
            )
            await pilot.pause()

            self.assertNotIn("loop-1 output", "".join(l.text for l in panel.lines))
            self.assertIn("loop-2 output", "".join(l.text for l in panel.lines))
            self.assertEqual(app.activity_key, "o/r#12")
            self.assertTrue(panel.display)

    async def test_local_loop_start_clears_the_panel_for_a_repeated_pr_key(self):
        async with self.app.run_test() as pilot:
            self.load_items(self.app, [make_item(12)])
            panel = self.app.query_one("#activity-log", RichLog)
            self.app.show_activity_log("o/r#12", True)
            self.app.activity_pinned = True
            self.app.handle_runner_event("log", ("o/r#12", "loop-1 output"))
            await pilot.pause()

            self.app.handle_runner_event("loop_start", (2,))
            loop_two = LoopRecord(index=2, started_at=0.0, items=[make_item(12)])
            self.app.runner.record = loop_two
            self.app.handle_runner_event("items_ready", (loop_two,))
            running = loop_two.items[0]
            running.state = PrState.RUNNING
            self.app.handle_runner_event("item_update", (running,))
            self.app.handle_runner_event("log", ("o/r#12", "loop-2 output"))
            await pilot.pause()

            rendered = "".join(line.text for line in panel.lines)
            self.assertNotIn("loop-1 output", rendered)
            self.assertIn("loop-2 output", rendered)
            self.assertEqual(self.app.activity_key, "o/r#12")
            self.assertTrue(panel.display)

    async def test_open_detail_screen_reloads_its_log_on_the_next_loop(self):
        item = make_item(12)
        async with self.app.run_test() as pilot:
            self.load_items(self.app, [item])
            self.app.push_screen(DetailScreen(item.key))
            await pilot.pause()
            detail_log = self.app.screen.query_one("#detail-log", RichLog)
            self.app.handle_runner_event("log", (item.key, "loop-1 output"))
            await pilot.pause()

            self.app.handle_runner_event("loop_start", (2,))
            await pilot.pause()
            cleared = "".join(line.text for line in detail_log.lines)

            loop_two = LoopRecord(index=2, started_at=0.0, items=[make_item(12)])
            self.app.runner.record = loop_two
            self.app.handle_runner_event("log", ("o/r#12", "loop-2 output"))
            await pilot.pause()
            rendered = "".join(line.text for line in detail_log.lines)
            stayed_open = isinstance(self.app.screen, DetailScreen)
            still_following = self.app.screen.follow

        self.assertTrue(stayed_open)
        self.assertTrue(still_following)
        self.assertNotIn("loop-1 output", cleared)
        self.assertIn("no agent log yet", cleared)
        self.assertNotIn("loop-1 output", rendered)
        self.assertIn("loop-2 output", rendered)


# Long enough that the title column pushes the table past the narrow viewport used
# below, so those tests really do have a horizontal scrollbar to keep.
WIDE_TITLE = "long title that keeps going for a while" * 2
NARROW_TITLE = "medium width title"


class PrTableRefreshTest(unittest.IsolatedAsyncioTestCase):
    """The 1s refresh reconciles rows by key instead of clearing the whole table."""

    VIEWPORT = (40, 24)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig("codex-personal", "review", ["o/r"], ["me"], ["me"])
        self.app = AutoReviewPrDashboardApp(self.config, store=Store(Path(self.tmp.name)))
        self.runner_patch = patch.object(
            runner_module.LoopRunner, "run_forever", new=noop_forever
        )
        self.runner_patch.start()

    def tearDown(self):
        self.runner_patch.stop()
        self.tmp.cleanup()

    def load_items(self, items):
        record = LoopRecord(index=1, started_at=0.0, items=items)
        self.app.runner.record = record
        self.app.handle_runner_event("items_ready", (record,))

    @staticmethod
    def get_row_keys(table):
        return [row.key.value for row in table.ordered_rows]

    @staticmethod
    def scroll_table_right(table, cells):
        """Scroll like a wheel gesture would; those read scroll_target_x, not scroll_x."""
        for _ in range(cells):
            table.scroll_right(animate=False)

    async def test_refresh_shrinks_the_table_when_titles_get_shorter(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12, title=WIDE_TITLE), make_item(15, title=WIDE_TITLE)])
            await pilot.pause()
            table = app.query_one(PrTable)
            wide_width = table.virtual_size.width
            self.assertGreater(wide_width, table.size.width)

            for item in app.record.items:
                item.title = NARROW_TITLE
            app.refresh_view()
            await pilot.pause()

            self.assertEqual(self.get_row_keys(table), ["o/r#12", "o/r#15"])
            self.assertLess(table.virtual_size.width, wide_width)

    async def test_refresh_shrinks_the_table_when_the_widest_row_is_removed(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12, title=WIDE_TITLE), make_item(15, title=NARROW_TITLE)])
            await pilot.pause()
            table = app.query_one(PrTable)
            wide_width = table.virtual_size.width

            self.load_items([make_item(15, title=NARROW_TITLE)])
            await pilot.pause()

            self.assertEqual(self.get_row_keys(table), ["o/r#15"])
            self.assertLess(table.virtual_size.width, wide_width)

    async def test_refresh_keeps_the_horizontal_scroll_position(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12, title=WIDE_TITLE), make_item(15, title=WIDE_TITLE)])
            await pilot.pause()
            table = app.query_one(PrTable)
            self.scroll_table_right(table, 10)
            await pilot.pause()
            self.assertAlmostEqual(table.scroll_x, 10)
            self.assertAlmostEqual(table.scroll_target_x, 10)

            for item in app.record.items:
                item.title = NARROW_TITLE
            app.refresh_view()
            await pilot.pause()

            # The table is narrower now but still overflows, so the scroll must stay.
            self.assertGreater(table.virtual_size.width, table.size.width)
            self.assertAlmostEqual(table.scroll_x, 10)
            self.assertAlmostEqual(table.scroll_target_x, 10)

            # A wheel gesture continues from scroll_target_x, so it must not snap back.
            table.scroll_right(animate=False)
            await pilot.pause()
            self.assertAlmostEqual(table.scroll_x, 11)

    async def test_rebuilding_for_a_removed_row_keeps_the_horizontal_scroll(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12, title=WIDE_TITLE), make_item(15, title=NARROW_TITLE)])
            await pilot.pause()
            table = app.query_one(PrTable)
            self.scroll_table_right(table, 8)
            await pilot.pause()
            self.assertAlmostEqual(table.scroll_x, 8)
            self.assertAlmostEqual(table.scroll_target_x, 8)

            self.load_items([make_item(15, title=NARROW_TITLE)])
            await pilot.pause()

            self.assertEqual(self.get_row_keys(table), ["o/r#15"])
            self.assertGreater(table.virtual_size.width, table.size.width)
            self.assertAlmostEqual(table.scroll_x, 8)
            self.assertAlmostEqual(table.scroll_target_x, 8)

            # A wheel gesture continues from scroll_target_x, so it must not snap back.
            table.scroll_right(animate=False)
            await pilot.pause()
            self.assertAlmostEqual(table.scroll_x, 9)

    async def test_refresh_only_updates_the_row_that_changed(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12, title=NARROW_TITLE), make_item(15, title=NARROW_TITLE)])
            await pilot.pause()
            table = app.query_one(PrTable)
            updated_keys = []
            real_update_cell = table.update_cell

            def spy_update_cell(row_key, *args, **kwargs):
                updated_keys.append(getattr(row_key, "value", row_key))
                return real_update_cell(row_key, *args, **kwargs)

            table.update_cell = spy_update_cell
            app.refresh_view()
            await pilot.pause()
            unchanged_updates = list(updated_keys)

            app.record.items[1].state = PrState.DONE
            app.record.items[1].started_at = 0.0
            updated_keys.clear()
            app.refresh_view()
            await pilot.pause()
            table.update_cell = real_update_cell
            state_cell = table.get_cell("o/r#15", "state")

        self.assertEqual(unchanged_updates, [])
        self.assertEqual(set(updated_keys), {"o/r#15"})
        self.assertEqual(state_cell.plain, PrState.DONE.value)
        self.assertEqual(str(state_cell.style), STATE_STYLE[PrState.DONE])

    async def test_refresh_keeps_the_cursor_on_the_same_pr_when_a_row_is_inserted(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12), make_item(15)])
            await pilot.pause()
            table = app.query_one(PrTable)
            table.move_cursor(row=1)
            await pilot.pause()

            self.load_items([make_item(9), make_item(12), make_item(15)])
            await pilot.pause()

            self.assertEqual(self.get_row_keys(table), ["o/r#9", "o/r#12", "o/r#15"])
            self.assertEqual(table.cursor_row, 2)

    async def test_refresh_drops_removed_rows_and_renumbers_the_rest(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12), make_item(15), make_item(18)])
            await pilot.pause()
            table = app.query_one(PrTable)

            self.load_items([make_item(15), make_item(18)])
            await pilot.pause()

            self.assertEqual(self.get_row_keys(table), ["o/r#15", "o/r#18"])
            self.assertEqual(
                [table.get_cell("o/r#15", "index"), table.get_cell("o/r#18", "index")],
                ["1", "2"],
            )
            # The cursor's PR was dropped too, so it falls back to the first row.
            self.assertEqual(table.cursor_row, 0)

    async def test_refresh_moves_the_cursor_back_when_its_row_is_removed(self):
        app = self.app
        async with app.run_test(size=self.VIEWPORT) as pilot:
            self.load_items([make_item(12), make_item(15), make_item(18)])
            await pilot.pause()
            table = app.query_one(PrTable)
            table.move_cursor(row=2)
            await pilot.pause()

            self.load_items([make_item(12), make_item(15)])
            await pilot.pause()

            # The cursor's PR is gone, so it lands on the last remaining row.
            self.assertEqual(table.cursor_row, 1)
            self.assertEqual(self.get_row_keys(table)[table.cursor_row], "o/r#15")


class CellsSignatureTest(unittest.TestCase):
    """Skipping unchanged rows must not miss a restyled cell: Text.__eq__ does."""

    def test_style_only_change_changes_the_signature(self):
        self.assertNotEqual(
            get_cells_signature([Text("done", style="green")]),
            get_cells_signature([Text("done", style="bold green")]),
        )

    def test_identical_cells_keep_the_same_signature(self):
        self.assertEqual(
            get_cells_signature(["3", Text("x", style="dim")]),
            get_cells_signature(["3", Text("x", style="dim")]),
        )
