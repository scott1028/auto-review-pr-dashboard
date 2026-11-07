"""main(): preflight, prompt.md, the daemon start, and the dry run table."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import __main__ as cli
from auto_review_pr_dashboard import app, client, daemon, discover, gate, runner
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.discover import DiscoveredPr, DiscoverError
from auto_review_pr_dashboard.models import SkipReason

HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
SCOPE = ["--repo-url", "o/r", "--author", "me"]
START = ["codex", *SCOPE]
DRY_RUN = [*START, "--dry-run"]


def make_found(number, matched_axes=("author:me",), skip_reason=None):
    return DiscoveredPr(
        "o/r",
        number,
        f"https://github.com/o/r/pull/{number}",
        HEAD,
        skip_reason,
        list(matched_axes),
    )


def make_snapshot(marker_sha=None, title="Fix thing"):
    return gate.GateSnapshot(title, "me", HEAD, marker_sha)


class MainTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous_cwd = Path.cwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous_cwd)
        self.prompt_path = Path("prompt.md")
        self.start_daemon = self.patch(daemon, "start_daemon")
        self.preflight_discover = self.patch(cli, "discover_prs", return_value=[])
        self.dashboard = self.patch(app, "AutoReviewPrDashboardApp")
        self.dashboard.return_value.run.return_value = None
        self.patch(client, "SessionClient")

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class PreflightTest(MainTestCase):
    def test_failure_returns_one_with_the_reason_and_starts_nothing(self):
        self.preflight_discover.side_effect = DiscoverError(
            "pr-discover exited 2: --pr-url 'x' is not a pull request url"
        )
        for argv in (START, DRY_RUN):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.run_main(argv)

                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("auto-review-pr-dashboard: ", stderr)
                self.assertIn("is not a pull request url", stderr)
                self.assertFalse(self.prompt_path.exists())
                self.start_daemon.assert_not_called()
                self.dashboard.assert_not_called()

    def test_a_pr_discover_that_cannot_run_returns_one(self):
        self.preflight_discover.side_effect = discover.discover_prs
        denied = PermissionError(13, "Permission denied", "pr-discover")
        self.patch(discover.subprocess, "run", side_effect=denied)

        code, stdout, stderr = self.run_main(START)

        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(
            stderr,
            "auto-review-pr-dashboard: pr-discover is not usable: cannot run "
            "pr-discover: [Errno 13] Permission denied: 'pr-discover'\n",
        )
        self.start_daemon.assert_not_called()

    def test_raises_preflight_error_on_a_discover_error(self):
        self.preflight_discover.side_effect = DiscoverError("pr-discover not found")
        config = RunConfig("codex", "", ["o/r"], ["me"])
        with self.assertRaises(cli.PreflightError) as caught:
            cli.run_preflight(config)
        self.assertIn("pr-discover not found", str(caught.exception))
        self.preflight_discover.assert_called_once_with(config)

    def test_scope_values_reach_pr_discover_unchanged(self):
        self.run_main(
            [
                "codex",
                "--repo-url", "https://github.com/o/r.git",
                "--repo-url", "o/r",
                "--pr-url", "nonsense",
                "--author", "me",
            ]
        )
        config = self.preflight_discover.call_args.args[0]
        self.assertEqual(config.repos, ["https://github.com/o/r.git", "o/r"])
        self.assertEqual(config.urls, ["nonsense"])


class StartTest(MainTestCase):
    def test_prompt_is_read_after_preflight_and_before_the_daemon(self):
        order = []
        self.preflight_discover.side_effect = lambda config: (
            order.append(("preflight", self.prompt_path.exists())) or []
        )
        self.start_daemon.side_effect = lambda config, store: (
            order.append(("daemon", config.prompt)) or MagicMock()
        )

        code, _, stderr = self.run_main(START)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            order, [("preflight", False), ("daemon", "code review\n")]
        )
        self.dashboard.return_value.run.assert_called_once()

    def test_a_bad_prompt_file_returns_one_and_starts_nothing(self):
        self.prompt_path.write_text(" \n", encoding="utf-8")

        code, _, stderr = self.run_main(START)

        self.assertEqual(code, 1)
        self.assertIn(
            "auto-review-pr-dashboard: prompt.md must not be empty", stderr
        )
        self.start_daemon.assert_not_called()

    def test_a_lost_connection_prints_why_and_returns_one(self):
        for result, message in (
            ("connection-error", "socket [/x] vanished"),
            ("disconnected", "connection to background daemon closed"),
        ):
            with self.subTest(result=result):
                self.dashboard.return_value.run.return_value = result
                self.dashboard.return_value.exit_message = message

                code, stdout, stderr = self.run_main(START)

                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(stderr, f"auto-review-pr-dashboard: {message}\n")

    def test_quit_prints_nothing(self):
        self.dashboard.return_value.run.return_value = "stopped"
        self.dashboard.return_value.exit_message = ""

        self.assertEqual(self.run_main(START), (0, "", ""))

    def test_a_refused_daemon_returns_one(self):
        self.start_daemon.side_effect = daemon.SessionError("already running")

        code, _, stderr = self.run_main(START)

        self.assertEqual(code, 1)
        self.assertIn("auto-review-pr-dashboard: already running", stderr)


class NoPreflightTest(MainTestCase):
    def assert_untouched(self):
        self.preflight_discover.assert_not_called()
        self.assertFalse(self.prompt_path.exists())

    def test_resume_skips_preflight_and_prompt(self):
        metadata = SimpleNamespace(config={"ai_cli": "codex", "prompt": "review"})
        self.patch(daemon, "get_resume_metadata", return_value=metadata)

        code, _, _ = self.run_main(["--resume"])

        self.assertEqual(code, 0)
        self.assert_untouched()
        self.dashboard.return_value.run.assert_called_once()

    def test_list_skips_preflight_and_prompt(self):
        self.patch(daemon, "get_live_workspaces", return_value=[])
        for argv in (["-l"], ["--list"]):
            with self.subTest(argv=argv):
                code, stdout, _ = self.run_main(argv)

                self.assertEqual(code, 0)
                self.assertIn("nothing to --resume", stdout)
                self.assert_untouched()


class DryRunTest(MainTestCase):
    def run_dry(self, found, snapshots):
        self.patch(runner, "discover_prs", return_value=found)
        self.patch(gate, "get_gate_snapshot", side_effect=snapshots)
        return self.run_main(DRY_RUN)

    def test_prints_verdicts_and_the_matched_line(self):
        code, stdout, _ = self.run_dry(
            [
                make_found(1, ["author:me", "reviewed-by:me"]),
                make_found(2, skip_reason=SkipReason.DRAFT),
                make_found(3),
            ],
            [make_snapshot(), make_snapshot(), make_snapshot(HEAD)],
        )

        self.assertEqual(code, 0)
        self.assertIn("repos     : o/r", stdout)
        self.assertRegex(stdout, r"o/r#1  Full +head a1b2c3d  Fix thing\n")
        self.assertIn("matched: author:me, reviewed-by:me", stdout)
        self.assertRegex(stdout, r"o/r#2  Skip \(WIP\) +head a1b2c3d  Fix thing\n")
        self.assertIn(
            "scanned 3 — Full 1 / Incremental 0 / Skip 2; "
            "1 would be reviewed, 0 failed",
            stdout,
        )
        self.start_daemon.assert_not_called()

    def test_a_failed_pr_is_printed_with_its_error_and_returns_one(self):
        code, stdout, _ = self.run_dry(
            [make_found(1), make_found(2)],
            [gate.GhError("gh pr view failed: 502"), make_snapshot()],
        )

        self.assertEqual(code, 1)
        self.assertRegex(
            stdout,
            r"o/r#1  failed +head a1b2c3d  \n"
            r" +matched: author:me\n"
            r" +gate check failed: gh pr view failed: 502\n",
        )
        # A PR whose marker is unknown has no verdict to count.
        self.assertIn(
            "scanned 2 — Full 1 / Incremental 0 / Skip 0; "
            "1 would be reviewed, 1 failed",
            stdout,
        )

    def test_a_skipped_pr_that_cannot_be_read_is_not_a_failure(self):
        code, stdout, _ = self.run_dry(
            [make_found(1, skip_reason=SkipReason.DRAFT)], [gate.GhError("boom")]
        )

        self.assertEqual(code, 0)
        self.assertRegex(stdout, r"o/r#1  Skip \(WIP\) +head a1b2c3d  \n")
        self.assertNotIn("boom", stdout)
        self.assertIn(
            "scanned 1 \u2014 Full 0 / Incremental 0 / Skip 1; "
            "0 would be reviewed, 0 failed",
            stdout,
        )

    def test_no_candidates_is_not_an_error(self):
        code, stdout, _ = self.run_dry([], [])

        self.assertEqual(code, 0)
        self.assertIn("no candidate PR found", stdout)

    def test_discovery_failing_after_preflight_returns_one(self):
        self.patch(runner, "discover_prs", side_effect=DiscoverError("gone"))

        code, _, stderr = self.run_main(DRY_RUN)

        self.assertEqual(code, 1)
        self.assertIn("auto-review-pr-dashboard: gone", stderr)


if __name__ == "__main__":
    unittest.main()
