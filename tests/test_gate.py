"""gh calls, the marker verdict, and comment classification."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import gate
from auto_review_pr_dashboard.models import Classification, PostKind, Verdict

HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
OLD_HEAD = "9999999999999999999999999999999999999999"
PR_URL = "https://github.com/o/r/pull/73"


class GateTest(unittest.TestCase):
    def test_marker_and_verdict_matrix(self):
        passed = (
            f"checked! & passed!\n"
            f"<!-- review-pr-branches: passed-comment=42 head={HEAD} -->"
        )
        bodies = [
            f"first\n<!-- review-pr-branches: head={OLD_HEAD} -->",
            passed,
            f"latest\n<!-- review-pr-branches: head={HEAD} -->",
        ]
        self.assertEqual(gate.get_marker_sha(bodies), HEAD)
        self.assertIsNone(gate.get_marker_sha([passed]))

        for head, marker, expected in (
            (HEAD, None, Verdict.FULL),
            (HEAD.upper(), HEAD, Verdict.SKIP),
            (HEAD, OLD_HEAD, Verdict.INCREMENTAL),
        ):
            with self.subTest(marker=marker):
                self.assertIs(gate.decide_verdict(head, marker), expected)

    def test_classify_new_posts_matrix(self):
        old = {"id": 1, "body": "**blocking** old", "html_url": "old"}
        before = {"review": {1: old}, "issue": {}}
        self.assertEqual(gate.classify_new_posts(before, before), [])

        bodies = {
            2: "**blocking** (R1) — null check",
            3: "**non-blocking** (R2) — typing",
            4: "**blocking-question** (R3) — design?",
            5: (
                "checked! & passed!\n"
                f"<!-- review-pr-branches: passed-comment=1 head={HEAD} -->"
            ),
            6: "nit: unrelated",
        }
        reviews = {
            comment_id: {
                "id": comment_id,
                "body": body,
                "html_url": f"r{comment_id}",
            }
            for comment_id, body in bodies.items()
        }
        reviews[5]["in_reply_to_id"] = 1
        after = {
            "review": {1: old, **reviews},
            "issue": {
                11: {
                    "id": 11,
                    "body": f"summary\n<!-- review-pr-branches: head={HEAD} -->",
                    "html_url": "issue11",
                },
                12: {"id": 12, "body": "LGTM", "html_url": "issue12"},
            },
        }
        posts = gate.classify_new_posts(before, after)

        self.assertEqual([post.comment_id for post in posts], [2, 3, 4, 5, 11])
        self.assertEqual(
            [post.kind for post in posts],
            [PostKind.INLINE] * 3 + [PostKind.REPLY, PostKind.SUMMARY],
        )
        self.assertEqual(
            [post.classification for post in posts],
            [
                Classification.BLOCKING,
                Classification.NON_BLOCKING,
                Classification.BLOCKING_QUESTION,
                None,
                None,
            ],
        )

    def test_pagination_normalization(self):
        for returned, expected in (
            ([[{"id": 1}], [{"id": 2}]], [{"id": 1}, {"id": 2}]),
            ([{"id": 1}], [{"id": 1}]),
            (None, []),
        ):
            with patch.object(gate, "run_gh_json", return_value=returned):
                self.assertEqual(gate._paginated(["api", "x"]), expected)


class RunGhTest(unittest.TestCase):
    def test_missing_binary_is_a_gh_error(self):
        with (
            patch.object(gate.subprocess, "run", side_effect=FileNotFoundError()),
            self.assertRaises(gate.GhError) as caught,
        ):
            gate.run_gh(["pr", "view", PR_URL])
        self.assertEqual(str(caught.exception), "gh not found on PATH")

    def test_a_gh_that_cannot_run_is_a_gh_error(self):
        denied = PermissionError(13, "Permission denied", "gh")
        with (
            patch.object(gate.subprocess, "run", side_effect=denied),
            self.assertRaises(gate.GhError) as caught,
        ):
            gate.run_gh(["pr", "view", PR_URL])
        self.assertEqual(
            str(caught.exception), "cannot run gh: [Errno 13] Permission denied: 'gh'"
        )

    def test_timeout_is_a_gh_error_that_says_so(self):
        timed_out = subprocess.TimeoutExpired("gh", gate.GH_TIMEOUT_SEC)
        with (
            patch.object(gate.subprocess, "run", side_effect=timed_out),
            self.assertRaises(gate.GhError) as caught,
        ):
            gate.run_gh(["pr", "view", PR_URL])
        self.assertIn("gh pr view", str(caught.exception))
        self.assertIn(f"timed out after {gate.GH_TIMEOUT_SEC}s", str(caught.exception))


class GateSnapshotTest(unittest.TestCase):
    def get_snapshot(self, payload):
        process = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(payload), stderr=""
        )
        with patch.object(gate.subprocess, "run", return_value=process) as run:
            snapshot = gate.get_gate_snapshot(PR_URL)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["gh", "pr", "view", PR_URL, "--json", "title,author,headRefOid,comments"],
        )
        return snapshot

    def test_everything_comes_from_one_gh_call(self):
        snapshot = self.get_snapshot(
            {
                "title": "Fix it",
                "author": {"login": "me"},
                "headRefOid": HEAD,
                "comments": [
                    {"body": f"<!-- review-pr-branches: head={HEAD} -->"},
                    {"body": f"<!-- review-pr-branches: head={OLD_HEAD} -->"},
                    {"body": "LGTM"},
                ],
            }
        )
        self.assertEqual(snapshot, gate.GateSnapshot("Fix it", "me", HEAD, OLD_HEAD))

    def test_deleted_author_and_no_comments_are_tolerated(self):
        snapshot = self.get_snapshot(
            {"title": "Fix it", "author": None, "headRefOid": HEAD, "comments": None}
        )
        self.assertEqual(snapshot, gate.GateSnapshot("Fix it", "", HEAD, None))


if __name__ == "__main__":
    unittest.main()
