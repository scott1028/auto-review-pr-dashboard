"""Dashboard numbers. Pure functions, no textual import, so they stay testable."""

from __future__ import annotations

from .models import (
    SETTLED_STATES,
    Classification,
    LoopRecord,
    PostKind,
    PrState,
    Verdict,
)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def get_stats(record: LoopRecord | None, now: float) -> dict:
    """Everything the dashboard shows, derived from one loop record."""
    if record is None:
        return {
            "settled": 0,
            "total": 0,
            "repo_progress": {},
            "verdicts": {verdict: 0 for verdict in Verdict},
            "states": {state: 0 for state in PrState},
            "posts": {kind: 0 for kind in PostKind},
            "findings": {classification: 0 for classification in Classification},
            "no_change": 0,
            "elapsed_sec": 0.0,
            "avg_sec": None,
            "eta_sec": None,
        }

    durations = [
        item.duration_sec
        for item in record.items
        if item.state in (PrState.DONE, PrState.FAILED)
        and item.duration_sec is not None
    ]
    avg_sec = sum(durations) / len(durations) if durations else None
    remaining = sum(
        1 for item in record.items if item.state in (PrState.QUEUED, PrState.RUNNING)
    )

    return {
        "settled": record.settled_count,
        "total": len(record.items),
        "repo_progress": record.repo_progress(),
        "verdicts": {verdict: record.count_verdict(verdict) for verdict in Verdict},
        "states": {state: record.count_state(state) for state in PrState},
        "posts": {
            kind: sum(item.count_posts(kind) for item in record.items)
            for kind in PostKind
        },
        "findings": {
            classification: sum(
                item.count_findings(classification) for item in record.items
            )
            for classification in Classification
        },
        "no_change": sum(
            1
            for item in record.items
            if item.state in SETTLED_STATES and not item.new_posts
        ),
        "elapsed_sec": (record.finished_at or now) - record.started_at,
        "avg_sec": avg_sec,
        "eta_sec": None if avg_sec is None or not remaining else avg_sec * remaining,
    }


def get_progress_bar(settled: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * settled / total)
    return "█" * filled + "░" * (width - filled)


def get_repo_progress_text(repo_progress: dict) -> str:
    return " · ".join(
        f"{repo.split('/')[-1]} {settled}/{total}"
        for repo, (settled, total) in repo_progress.items()
    )
