"""Core GitHub discovery, verdict, and comment-classification behavior."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard import gh
from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.models import (
    Classification,
    PostKind,
    PrState,
    SkipReason,
    Verdict,
)

HEAD = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
OLD_HEAD = "9999999999999999999999999999999999999999"
PR_URL = "https://github.com/o/r/pull/73"


def make_config(**overrides):
    values = {
        "ai_cli": "codex",
        "prompt": "review",
        "repos": ["owner/repo"],
        "authors": ["me"],
        "reviewers": ["me"],
        "urls": [],
    }
    values.update(overrides)
    return RunConfig(**values)


def make_hit(number, author="me", url=None, is_draft=False):
    return {
        "number": number,
        "url": url or f"u{number}",
        "title": f"PR {number}",
        "author": None if author is None else {"login": author},
        "isDraft": is_draft,
    }


class GhTest(unittest.TestCase):
    def test_url_and_repo_parsing_tables(self):
        pr_cases = (
            (PR_URL, ("o/r", 73)),
            ("https://git.example.github.com/o/r/pull/9/files", ("o/r", 9)),
            ("https://github.com/o/r/issues/9", None),
            ("https://example.com", None),
            ("", None),
            (None, None),
        )
        for value, expected in pr_cases:
            with self.subTest(parser="PR URL", value=value):
                self.assertEqual(gh.get_pr_url_parts(value), expected)

        repo_cases = (
            ("o/r", "o/r"),
            ("  o/r  ", "o/r"),
            ("https://github.com/o/r", "o/r"),
            ("http://github.com/o/r/", "o/r"),
            ("https://github.com/o/r.git", "o/r"),
            ("git@github.com:o/r.git", "o/r"),
            ("https://github.com/o/r/pull/7", None),
            ("https://github.com/o/r/issues/1", None),
            ("owner", None),
            ("not a repo", None),
            ("", None),
            (None, None),
        )
        for value, expected in repo_cases:
            with self.subTest(parser="repo slug", value=value):
                self.assertEqual(gh.get_repo_slug(value), expected)

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
        self.assertEqual(gh.get_marker_sha(bodies), HEAD)
        self.assertIsNone(gh.get_marker_sha([passed]))

        for head, marker, expected in (
            (HEAD, None, Verdict.FULL),
            (HEAD.upper(), HEAD, Verdict.SKIP),
            (HEAD, OLD_HEAD, Verdict.INCREMENTAL),
        ):
            with self.subTest(marker=marker):
                self.assertIs(gh.decide_verdict(head, marker), expected)

    def test_wip_title_matrix(self):
        for title in ("[WIP] feature", "prefix [wip] suffix", "feature Wip: draft"):
            with self.subTest(title=title):
                self.assertTrue(gh.get_is_wip_title(title))

        for title in ("SWIP: feature", "ordinary wip title", "[ WIP ] feature"):
            with self.subTest(title=title):
                self.assertFalse(gh.get_is_wip_title(title))

    def test_candidate_merge_uses_repo_key_and_tolerates_missing_author(self):
        hit = make_hit(7, author=None)
        del hit["isDraft"]
        merged = gh.merge_candidates(
            [
                ("o/r", "author:me", hit),
                ("o/r", "reviewed-by:me", hit),
                ("other/r", "author:me", hit),
            ]
        )
        self.assertEqual({item["repo"] for item in merged}, {"o/r", "other/r"})
        candidate = next(item for item in merged if item["repo"] == "o/r")
        self.assertEqual(candidate["matched_axes"], ["author:me", "reviewed-by:me"])
        self.assertEqual(candidate["author"], "")
        self.assertFalse(candidate["is_draft"])

    def test_discovery_axes_use_and_between_dimensions(self):
        author_hit, reviewer_hit = make_hit(1, "alice"), make_hit(2, "bob")
        both_axes = make_config(repos=["o/r"], authors=["alice"], reviewers=["bob"])

        def nonmatching_results(args):
            return [author_hit] if "--author" in args else [reviewer_hit]

        with patch.object(gh, "run_gh_json", side_effect=nonmatching_results):
            self.assertEqual(gh.discover_candidates(both_axes), [])

        with patch.object(gh, "run_gh_json", return_value=[author_hit]):
            candidates = gh.discover_candidates(both_axes)
        self.assertEqual([item["number"] for item in candidates], [1])
        self.assertEqual(
            candidates[0]["matched_axes"],
            ["author:alice", "review-requested:bob", "reviewed-by:bob"],
        )

        author_only = make_config(repos=["o/r"], authors=["alice"], reviewers=[])
        with patch.object(gh, "run_gh_json", return_value=[author_hit, reviewer_hit]):
            candidates = gh.discover_candidates(author_only)
        self.assertEqual([item["number"] for item in candidates], [1, 2])

    def test_explicit_url_resolves_and_deduplicates_with_search(self):
        hit = make_hit(73, url=PR_URL, is_draft=True)
        config = make_config(repos=["o/r"], reviewers=[], urls=[PR_URL])
        with patch.object(
            gh,
            "run_gh_json",
            side_effect=lambda args: hit if args[1] == "view" else [hit],
        ):
            candidates = gh.discover_candidates(config)

        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0]["repo"], candidates[0]["number"]), ("o/r", 73))
        self.assertEqual(candidates[0]["matched_axes"], ["url", "author:me"])
        self.assertTrue(candidates[0]["is_draft"])
        self.assertIn("isDraft", gh.PR_LIST_FIELDS.split(","))

    def test_search_discovery_preserves_draft_state(self):
        config = make_config(repos=["o/r"], authors=["alice"], reviewers=[])
        with patch.object(
            gh,
            "run_gh_json",
            return_value=[make_hit(1, author="alice", is_draft=True)],
        ):
            candidates = gh.discover_candidates(config)

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["is_draft"])

    def test_discover_pr_items_sorts_and_assigns_states(self):
        config = make_config(repos=["o/r1", "o/r2"], reviewers=[])
        candidates = [
            {
                "repo": repo,
                "number": number,
                "url": f"u{number}",
                "title": "",
                "author": "me",
                "matched_axes": ["author:me"],
            }
            for repo, number in (("o/r2", 7), ("o/r1", 15), ("o/r1", 12))
        ]
        heads = {"u12": (HEAD, None), "u15": (HEAD, OLD_HEAD), "u7": (HEAD, HEAD)}
        with (
            patch.object(gh, "discover_candidates", return_value=candidates),
            patch.object(gh, "get_head_and_marker", side_effect=lambda url: heads[url]),
        ):
            items = gh.discover_pr_items(config)

        self.assertEqual([item.key for item in items], ["o/r1#12", "o/r1#15", "o/r2#7"])
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

    def test_wip_title_takes_priority_over_marker_verdict(self):
        candidate = {
            "repo": "o/r",
            "number": 1,
            "url": "u1",
            "title": "Feature WIP: still changing",
            "author": "me",
            "matched_axes": ["author:me"],
        }
        with (
            patch.object(gh, "discover_candidates", return_value=[candidate]),
            patch.object(gh, "get_head_and_marker", return_value=(HEAD, OLD_HEAD)),
        ):
            item = gh.discover_pr_items(make_config())[0]

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
                candidate = {
                    "repo": "o/r",
                    "number": 1,
                    "url": "u1",
                    "title": title,
                    "author": "me",
                    "is_draft": True,
                    "matched_axes": ["author:me"],
                }
                with (
                    patch.object(gh, "discover_candidates", return_value=[candidate]),
                    patch.object(
                        gh,
                        "get_head_and_marker",
                        return_value=(HEAD, marker_sha),
                    ),
                ):
                    item = gh.discover_pr_items(make_config())[0]

                self.assertIs(item.verdict, Verdict.SKIP)
                self.assertIs(item.state, PrState.SKIP)
                self.assertIs(item.skip_reason, SkipReason.DRAFT)
                self.assertEqual(item.verdict_label, "Skip (WIP)")
                self.assertEqual(item.to_dict()["skip_reason"], "draft")

    def test_classify_new_posts_matrix(self):
        old = {"id": 1, "body": "**blocking** old", "html_url": "old"}
        before = {"review": {1: old}, "issue": {}}
        self.assertEqual(gh.classify_new_posts(before, before), [])

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
        posts = gh.classify_new_posts(before, after)

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
            with patch.object(gh, "run_gh_json", return_value=returned):
                self.assertEqual(gh._paginated(["api", "x"]), expected)


if __name__ == "__main__":
    unittest.main()
