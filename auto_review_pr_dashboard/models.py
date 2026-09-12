"""State held for one loop: candidate PRs, their verdicts, and what got posted."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

REVIEW_MARKER_PREFIX = "<!-- review-pr-branches: head="
PASSED_MARKER_PREFIX = "<!-- review-pr-branches: passed-comment="


class Verdict(str, Enum):
    """Same three verdicts the skill reports; decided from the head SHA marker."""

    FULL = "Full"
    INCREMENTAL = "Incremental"
    SKIP = "Skip"


class PrState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIP = "skip"
    CANCELLED = "cancelled"


class SkipReason(str, Enum):
    DRAFT = "draft"
    WIP_TITLE = "wip-title"
    HEAD_ALREADY_REVIEWED = "head-already-reviewed"


TERMINAL_STATES = (PrState.DONE, PrState.FAILED, PrState.SKIP)
SETTLED_STATES = TERMINAL_STATES + (PrState.CANCELLED,)


class PostKind(str, Enum):
    INLINE = "inline"
    REPLY = "reply"
    SUMMARY = "summary"


class Classification(str, Enum):
    BLOCKING = "blocking"
    NON_BLOCKING = "non-blocking"
    BLOCKING_QUESTION = "blocking-question"


@dataclass
class PostedComment:
    """One comment that appeared on the PR while the AI process was running."""

    kind: PostKind
    comment_id: int
    url: str
    classification: Classification | None = None
    headline: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> PostedComment:
        classification = payload.get("classification")
        return cls(
            kind=PostKind(payload["kind"]),
            comment_id=payload["comment_id"],
            url=payload["url"],
            classification=(
                None if classification is None else Classification(classification)
            ),
            headline=payload.get("headline", ""),
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "comment_id": self.comment_id,
            "url": self.url,
            "classification": None
            if self.classification is None
            else self.classification.value,
            "headline": self.headline,
        }


@dataclass
class PrItem:
    repo: str
    number: int
    url: str
    title: str
    author: str
    head_sha: str
    matched_axes: list[str] = field(default_factory=list)
    last_marker_sha: str | None = None
    verdict: Verdict = Verdict.FULL
    state: PrState = PrState.QUEUED
    skip_reason: SkipReason | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str = ""
    log_path: str = ""
    block_attempts: int = 0
    new_posts: list[PostedComment] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict) -> PrItem:
        skip_reason = payload.get("skip_reason")
        return cls(
            repo=payload["repo"],
            number=payload["number"],
            url=payload["url"],
            title=payload["title"],
            author=payload["author"],
            head_sha=payload["head_sha"],
            matched_axes=list(payload.get("matched_axes", [])),
            last_marker_sha=payload.get("last_marker_sha"),
            verdict=Verdict(payload.get("verdict", Verdict.FULL.value)),
            state=PrState(payload.get("state", PrState.QUEUED.value)),
            skip_reason=None if skip_reason is None else SkipReason(skip_reason),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            exit_code=payload.get("exit_code"),
            error=payload.get("error", ""),
            log_path=payload.get("log_path", ""),
            block_attempts=payload.get("block_attempts", 0),
            new_posts=[
                PostedComment.from_dict(post) for post in payload.get("new_posts", [])
            ],
        )

    @property
    def key(self) -> str:
        """Repo-qualified id; a bare number collides across repos."""
        return f"{self.repo}#{self.number}"

    @property
    def short_head(self) -> str:
        return self.head_sha[:7]

    @property
    def verdict_label(self) -> str:
        if self.skip_reason in (SkipReason.DRAFT, SkipReason.WIP_TITLE):
            return "Skip (WIP)"
        return self.verdict.value

    @property
    def duration_sec(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    def count_posts(self, kind: PostKind) -> int:
        return sum(1 for post in self.new_posts if post.kind is kind)

    def count_findings(self, classification: Classification) -> int:
        return sum(
            1 for post in self.new_posts if post.classification is classification
        )

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "head_sha": self.head_sha,
            "matched_axes": list(self.matched_axes),
            "last_marker_sha": self.last_marker_sha,
            "verdict": self.verdict.value,
            "state": self.state.value,
            "skip_reason": None if self.skip_reason is None else self.skip_reason.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "log_path": self.log_path,
            "block_attempts": self.block_attempts,
            "new_posts": [post.to_dict() for post in self.new_posts],
        }


@dataclass
class LoopRecord:
    """One pass over the candidate pool, persisted so the detail view keeps history."""

    index: int
    started_at: float
    finished_at: float | None = None
    items: list[PrItem] = field(default_factory=list)
    block_events: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict) -> LoopRecord:
        return cls(
            index=payload["index"],
            started_at=payload["started_at"],
            finished_at=payload.get("finished_at"),
            items=[PrItem.from_dict(item) for item in payload.get("items", [])],
            block_events=list(payload.get("block_events", [])),
        )

    def item_by_key(self, key: str) -> PrItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None

    def count_state(self, state: PrState) -> int:
        return sum(1 for item in self.items if item.state is state)

    def count_verdict(self, verdict: Verdict) -> int:
        return sum(1 for item in self.items if item.verdict is verdict)

    @property
    def settled_count(self) -> int:
        return sum(1 for item in self.items if item.state in SETTLED_STATES)

    def repo_progress(self) -> dict[str, tuple[int, int]]:
        """`{repo: (settled, total)}` in first-seen order, for the per-repo counter."""
        progress: dict[str, tuple[int, int]] = {}
        for item in self.items:
            settled, total = progress.get(item.repo, (0, 0))
            is_settled = item.state in SETTLED_STATES
            progress[item.repo] = (settled + (1 if is_settled else 0), total + 1)
        return progress

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "items": [item.to_dict() for item in self.items],
            "block_events": list(self.block_events),
        }
