"""Dashboard numbers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_review_pr_dashboard.models import LoopRecord, PrItem, PrState
from auto_review_pr_dashboard.stats import get_stats


def make_item(number, state, duration_sec=None):
    item = PrItem("o/r", number, f"https://github.com/o/r/pull/{number}", "", "me", "a")
    item.state = state
    if duration_sec is not None:
        item.started_at, item.finished_at = 0.0, duration_sec
    return item


class StatsTestCase(unittest.TestCase):
    def test_eta_counts_parallel_slots_in_waves(self):
        items = [make_item(1, PrState.DONE, 60.0)] + [
            make_item(number, PrState.QUEUED) for number in range(2, 9)
        ]
        record = LoopRecord(1, 0.0, items=items)

        self.assertEqual(get_stats(record, 100.0)["eta_sec"], 7 * 60.0)
        # Seven PRs left on three slots finish in three waves.
        self.assertEqual(get_stats(record, 100.0, max_parallel=3)["eta_sec"], 3 * 60.0)


if __name__ == "__main__":
    unittest.main()
