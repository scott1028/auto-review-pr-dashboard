"""Persistence under `.tmp/auto-review-pr-dashboard/` in the current working directory.

Working files stay inside the cwd, per the review-pr-branches skill's working file
location rule - the cwd is usually a plain multi-repo parent directory.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from .config import RunConfig
from .models import LoopRecord, PrItem

DEFAULT_BASE_DIR = Path(".tmp/auto-review-pr-dashboard")
# Counted in loops, not hours: a loop lasts as long as its PRs take to review, and
# --interval only spaces loops apart, so no setting maps to a real time span. At
# the default 60min interval with short loops this is roughly 10 hours.
MAX_KEPT_LOOP_DIRS = 10


class Store:
    def __init__(self, base_dir: Path | str = DEFAULT_BASE_DIR):
        self.base_dir = Path(base_dir)
        self.loops_dir = self.base_dir / "loops"
        self.logs_dir = self.base_dir / "logs"

    def prepare(self) -> None:
        self.loops_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def save_state(self, config: RunConfig, loop_index: int) -> None:
        self.prepare()
        payload = {"config": asdict(config), "loop_index": loop_index}
        (self.base_dir / "state.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def prune_logs(self) -> None:
        """Drop the oldest loop log dirs - nothing else ever deletes them.

        Sorted on the index as a number: past loop 9999 the zero padding stops
        holding, and a lexical sort would delete the newest dirs instead.
        """
        loop_dirs = sorted(
            (
                path
                for path in self.logs_dir.glob("loop-*")
                if path.is_dir() and path.name.removeprefix("loop-").isdigit()
            ),
            key=lambda path: int(path.name.removeprefix("loop-")),
        )
        for stale_dir in loop_dirs[:-MAX_KEPT_LOOP_DIRS]:
            shutil.rmtree(stale_dir, ignore_errors=True)

    def get_log_path(self, loop_index: int, item: PrItem) -> Path:
        loop_dir = self.logs_dir / f"loop-{loop_index:04d}"
        loop_dir.mkdir(parents=True, exist_ok=True)
        # Flatten every separator: a repo carrying extra slashes would otherwise
        # name a nested directory that does not exist, and the open() would fail.
        return loop_dir / f"{item.repo.replace('/', '__')}__{item.number}.log"

    def save_loop(self, record: LoopRecord) -> Path:
        self.prepare()
        path = self.loops_dir / f"loop-{record.index:04d}.json"
        path.write_text(
            json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def get_history(self, key: str) -> list[dict]:
        """Every past loop that saw this PR, oldest first, for the detail view."""
        history = []
        for path in sorted(self.loops_dir.glob("loop-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in payload.get("items", []):
                if f"{item['repo']}#{item['number']}" != key:
                    continue
                history.append(
                    {
                        "loop_index": payload.get("index"),
                        "verdict": item.get("verdict"),
                        "state": item.get("state"),
                        "skip_reason": item.get("skip_reason"),
                        "posted": len(item.get("new_posts", [])),
                    }
                )
        return history
