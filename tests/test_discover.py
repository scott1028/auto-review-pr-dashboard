"""pr-discover's JSON summary, and the ways running it can fail."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import discover
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.discover import (
    DiscoverError,
    discover_prs,
    get_discover_args,
    parse_discover_output,
)
from auto_review_pr_dashboard.models import SkipReason

URL_7 = "https://github.com/o/r/pull/7"


def make_summary(*entries: dict) -> str:
    return json.dumps({"dry_run": True, "run_count": None, "prs": list(entries)})


def make_entry(key: str, action: str, reason: str | None = None) -> dict:
    entry = {"key": key, "url": URL_7, "head_sha": "abc123", "action": action}
    if reason is not None:
        entry["reason"] = reason
    return entry


class TestDiscoverArgs(unittest.TestCase):
    def test_scope_flags_repeated_and_no_double_dash(self):
        config = RunConfig(
            ai_cli="opencode",
            prompt="p",
            repos=["o/r1", "o/r2"],
            authors=["me", "other"],
            reviewers=["rev"],
            urls=[URL_7],
        )
        args = get_discover_args(config)
        self.assertEqual(
            args,
            [
                "pr-discover",
                "--repo-url",
                "o/r1",
                "--repo-url",
                "o/r2",
                "--pr-url",
                URL_7,
                "--author",
                "me",
                "--author",
                "other",
                "--reviewer",
                "rev",
            ],
        )
        self.assertNotIn("--", args)

    def test_empty_config_has_nothing_but_the_command(self):
        self.assertEqual(get_discover_args(RunConfig(ai_cli="x", prompt="p")), ["pr-discover"])


class TestParseDiscoverOutput(unittest.TestCase):
    def test_pending_and_unchanged_have_no_skip_reason(self):
        parsed = parse_discover_output(
            make_summary(
                make_entry("o/r#1", "pending"),
                make_entry("o/r#2", "skipped", "unchanged"),
            )
        )
        self.assertEqual([pr.skip_reason for pr in parsed], [None, None])
        self.assertEqual([(pr.repo, pr.number) for pr in parsed], [("o/r", 1), ("o/r", 2)])
        self.assertEqual(parsed[0].url, URL_7)
        self.assertEqual(parsed[0].head_sha, "abc123")

    def test_draft_and_wip_map_to_skip_reasons(self):
        parsed = parse_discover_output(
            make_summary(
                make_entry("some-owner/some.repo#12", "skipped", "draft"),
                make_entry("o/r#3", "skipped", "wip-title"),
            )
        )
        self.assertEqual(parsed[0].skip_reason, SkipReason.DRAFT)
        self.assertEqual((parsed[0].repo, parsed[0].number), ("some-owner/some.repo", 12))
        self.assertEqual(parsed[1].skip_reason, SkipReason.WIP_TITLE)

    def test_unknown_reason_raises(self):
        with self.assertRaises(DiscoverError):
            parse_discover_output(make_summary(make_entry("o/r#1", "skipped", "stale")))

    def test_own_skip_reason_is_not_accepted(self):
        # head-already-reviewed is our marker verdict, never pr-discover's
        with self.assertRaises(DiscoverError):
            parse_discover_output(
                make_summary(make_entry("o/r#1", "skipped", "head-already-reviewed"))
            )

    def test_matched_axes_are_read_in_order(self):
        entry = make_entry("o/r#1", "pending")
        entry["matched_axes"] = ["url", "author:me", "reviewed-by:me"]
        parsed = parse_discover_output(make_summary(entry))
        self.assertEqual(parsed[0].matched_axes, ["url", "author:me", "reviewed-by:me"])

    def test_missing_or_null_matched_axes_is_an_empty_list(self):
        # An installed pr-discover can be older than this field.
        with_null = make_entry("o/r#2", "pending")
        with_null["matched_axes"] = None
        parsed = parse_discover_output(
            make_summary(make_entry("o/r#1", "pending"), with_null)
        )
        self.assertEqual([pr.matched_axes for pr in parsed], [[], []])

    def test_unchanged_entry_from_a_pr_discover_snapshot_needs_the_marker_check(self):
        # What pr-discover prints when ./.tmp/pr-discover/prs.json holds this head.
        entry = make_entry("o/r#1", "skipped", "unchanged")
        entry["matched_axes"] = ["author:me"]
        found = parse_discover_output(make_summary(entry))[0]
        self.assertIsNone(found.skip_reason)
        self.assertEqual(found.head_sha, "abc123")
        self.assertEqual(found.matched_axes, ["author:me"])

    def test_wrong_shape_raises(self):
        cases = {
            "top-level list": "[]",
            "top-level null": "null",
            "entry without key": json.dumps({"prs": [{"url": URL_7}]}),
            "key without number": json.dumps(
                {"prs": [{"key": "o/r", "url": URL_7, "action": "pending"}]}
            ),
            "key with non-numeric number": json.dumps(
                {"prs": [{"key": "o/r#x", "url": URL_7, "action": "pending"}]}
            ),
        }
        for name, stdout in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(DiscoverError):
                    parse_discover_output(stdout)

    def test_bad_json_raises(self):
        with self.assertRaises(DiscoverError):
            parse_discover_output("not json at all")

    def test_summary_without_prs_list_raises(self):
        for name, stdout in {
            "error object": json.dumps({"error": "x"}),
            "prs not a list": json.dumps({"prs": 5}),
        }.items():
            with self.subTest(case=name):
                with self.assertRaises(DiscoverError):
                    parse_discover_output(stdout)

    def test_non_string_reason_raises(self):
        stdout = json.dumps(
            {"prs": [{"key": "o/r#1", "action": "skipped", "reason": 7}]}
        )
        with self.assertRaises(DiscoverError):
            parse_discover_output(stdout)


class TestDiscoverPrs(unittest.TestCase):
    def run_with(self, process=None, side_effect=None):
        with patch(
            "auto_review_pr_dashboard.discover.subprocess.run",
            return_value=process,
            side_effect=side_effect,
        ) as mock_run:
            return discover_prs(RunConfig(ai_cli="x", prompt="p", repos=["o/r"])), mock_run

    def test_success_returns_prs_and_no_double_dash(self):
        process = subprocess.CompletedProcess(
            [], 0, stdout=make_summary(make_entry("o/r#1", "pending")), stderr=""
        )
        prs, mock_run = self.run_with(process)
        self.assertEqual(len(prs), 1)
        passed_args = mock_run.call_args.args[0]
        self.assertNotIn("--", passed_args)
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 120)

    def test_nonzero_exit_raises_with_stderr_tail(self):
        process = subprocess.CompletedProcess([], 1, stdout="", stderr="line one\ngh: boom")
        with self.assertRaises(DiscoverError) as caught:
            self.run_with(process)
        self.assertIn("gh: boom", str(caught.exception))

    def test_missing_binary_raises(self):
        with self.assertRaises(DiscoverError):
            self.run_with(side_effect=FileNotFoundError())

    def test_a_binary_that_cannot_run_raises(self):
        cases = {
            FileNotFoundError(2, "No such file", "pr-discover"): (
                "pr-discover not found on PATH"
            ),
            PermissionError(13, "Permission denied", "pr-discover"): (
                "cannot run pr-discover: [Errno 13] Permission denied: 'pr-discover'"
            ),
        }
        for failure, message in cases.items():
            with self.subTest(failure=failure):
                with self.assertRaises(DiscoverError) as caught:
                    self.run_with(side_effect=failure)
                self.assertEqual(str(caught.exception), message)

    def test_timeout_raises_with_stderr_tail(self):
        timed_out = subprocess.TimeoutExpired(
            cmd="pr-discover", timeout=120, stderr=b"line one\nwarn: slow gh\n"
        )
        with self.assertRaises(DiscoverError) as caught:
            self.run_with(side_effect=timed_out)
        self.assertIn("warn: slow gh", str(caught.exception))
        # would show as b'warn: slow gh' if the bytes were not decoded
        self.assertNotIn("b'", str(caught.exception))

    def test_timeout_without_stderr_still_raises(self):
        timed_out = subprocess.TimeoutExpired(cmd="pr-discover", timeout=120)
        with self.assertRaises(DiscoverError) as caught:
            self.run_with(side_effect=timed_out)
        self.assertIn("no output", str(caught.exception))

    def test_bad_json_raises_with_stderr_tail(self):
        process = subprocess.CompletedProcess([], 0, stdout="garbage", stderr="warn: slow gh")
        with self.assertRaises(DiscoverError) as caught:
            self.run_with(process)
        self.assertIn("warn: slow gh", str(caught.exception))

    def test_module_timeout_constant(self):
        self.assertEqual(discover.DISCOVER_TIMEOUT_SEC, 120)


if __name__ == "__main__":
    unittest.main()
