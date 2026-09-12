"""Model serialization and legacy payload compatibility."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_review_pr_dashboard.models import (
    Classification,
    LoopRecord,
    PostedComment,
    PostKind,
    PrItem,
    PrState,
    SkipReason,
    Verdict,
)


class ModelSerializationTestCase(unittest.TestCase):
    def test_posted_comment_round_trip(self):
        post = PostedComment(
            PostKind.INLINE,
            42,
            "https://github.com/o/r/pull/1#discussion_r42",
            Classification.BLOCKING,
            "Handle the empty response",
        )

        restored = PostedComment.from_dict(post.to_dict())

        self.assertEqual(restored, post)
        self.assertIs(restored.kind, PostKind.INLINE)
        self.assertIs(restored.classification, Classification.BLOCKING)

    def test_pr_item_round_trip(self):
        item = PrItem(
            repo="o/r",
            number=1,
            url="https://github.com/o/r/pull/1",
            title="Fix review state",
            author="octocat",
            head_sha="a1b2c3d4e5f6",
            matched_axes=["author:me", "reviewed-by:me"],
            last_marker_sha="deadbeef",
            verdict=Verdict.INCREMENTAL,
            state=PrState.DONE,
            skip_reason=SkipReason.HEAD_ALREADY_REVIEWED,
            started_at=10.0,
            finished_at=15.5,
            exit_code=0,
            error="",
            log_path=".tmp/logs/pr-1.log",
            block_attempts=2,
            new_posts=[
                PostedComment(
                    PostKind.SUMMARY,
                    43,
                    "https://github.com/o/r/pull/1#issuecomment-43",
                    Classification.NON_BLOCKING,
                    "Review summary",
                )
            ],
        )

        restored = PrItem.from_dict(item.to_dict())

        self.assertEqual(restored, item)
        self.assertIs(restored.verdict, Verdict.INCREMENTAL)
        self.assertIs(restored.state, PrState.DONE)
        self.assertIs(restored.skip_reason, SkipReason.HEAD_ALREADY_REVIEWED)
        self.assertIs(restored.new_posts[0].kind, PostKind.SUMMARY)

    def test_loop_record_round_trip(self):
        item = PrItem(
            repo="o/r",
            number=1,
            url="https://github.com/o/r/pull/1",
            title="Fix review state",
            author="octocat",
            head_sha="a1b2c3d4e5f6",
        )
        record = LoopRecord(
            index=3,
            started_at=10.0,
            finished_at=20.0,
            items=[item],
            block_events=[{"reason": "usage limit", "at": 12.0}],
        )

        restored = LoopRecord.from_dict(record.to_dict())

        self.assertEqual(restored, record)
        self.assertIsInstance(restored.items[0], PrItem)

    def test_legacy_loop_payload_uses_defaults_for_optional_fields(self):
        payload = {
            "index": 1,
            "started_at": 10.0,
            "items": [
                {
                    "repo": "o/r",
                    "number": 1,
                    "url": "https://github.com/o/r/pull/1",
                    "title": "Old persisted PR",
                    "author": "octocat",
                    "head_sha": "a1b2c3d4e5f6",
                }
            ],
        }

        restored = LoopRecord.from_dict(payload)

        self.assertIsNone(restored.finished_at)
        self.assertEqual(restored.block_events, [])
        item = restored.items[0]
        self.assertEqual(item.matched_axes, [])
        self.assertIsNone(item.last_marker_sha)
        self.assertIs(item.verdict, Verdict.FULL)
        self.assertIs(item.state, PrState.QUEUED)
        self.assertIsNone(item.skip_reason)
        self.assertIsNone(item.started_at)
        self.assertIsNone(item.finished_at)
        self.assertIsNone(item.exit_code)
        self.assertEqual(item.error, "")
        self.assertEqual(item.log_path, "")
        self.assertEqual(item.block_attempts, 0)
        self.assertEqual(item.new_posts, [])


if __name__ == "__main__":
    unittest.main()
