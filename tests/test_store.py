"""Store: what a later run reads back from .tmp/auto-review-pr-dashboard/."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard.config import RunConfig
from auto_review_pr_dashboard.store import Store


class LoadLoopIndexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "state")

    def test_missing_state_is_zero(self):
        self.assertEqual(self.store.load_loop_index(), 0)

    def test_round_trip_with_save_state(self):
        self.store.save_state(RunConfig("codex", "review"), 7)
        self.assertEqual(self.store.load_loop_index(), 7)

    def test_corrupt_state_is_zero(self):
        self.store.prepare()
        state_path = self.store.base_dir / "state.json"
        for content in ("{oops", '{"loop_index": "x"}', "[]", "{}"):
            with self.subTest(content=content):
                state_path.write_text(content, encoding="utf-8")
                self.assertEqual(self.store.load_loop_index(), 0)


if __name__ == "__main__":
    unittest.main()
