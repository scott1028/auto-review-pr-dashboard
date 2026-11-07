"""What this package asks `gh` about a PR it already knows, once pr-discover found it.

Everything here is deterministic - head SHA, marker lookup, and the before/after
comment diff. None of it needs an AI, so the dashboard can show a full verdict table
before spending a single token.

The marker format follows the review-pr-branches skill.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from .models import Classification, PostedComment, PostKind, Verdict

GH_TIMEOUT_SEC = 120

# `<!-- review-pr-branches: head=<sha> -->`; the passed-reply marker carries
# `passed-comment=` before `head=`, so this pattern must not match it.
REVIEW_MARKER_RE = re.compile(
    r"<!--[\s]*review-pr-branches:[\s]*head=([0-9a-fA-F]{7,40})[\s]*-->"
)
PASSED_MARKER_RE = re.compile(
    r"<!--[\s]*review-pr-branches:[\s]*passed-comment=\d+[\s]+head="
)
CLASSIFICATION_RE = re.compile(r"^\s*\*\*(blocking|non-blocking|blocking-question)\*\*")


class GhError(RuntimeError):
    """A gh call failed, timed out, or returned output we cannot use."""


def run_gh(args: list[str]) -> str:
    try:
        process = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except FileNotFoundError as error:
        raise GhError("gh not found on PATH") from error
    except OSError as error:
        raise GhError(f"cannot run gh: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise GhError(
            f"gh {' '.join(args)} timed out after {GH_TIMEOUT_SEC}s"
        ) from error
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip().splitlines()
        raise GhError(
            f"gh {' '.join(args)} failed: {detail[-1] if detail else 'no output'}"
        )
    return process.stdout


def run_gh_json(args: list[str]):
    output = run_gh(args).strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise GhError(
            f"gh {' '.join(args)} returned non-JSON output: {error}"
        ) from error


def get_marker_sha(comment_bodies: list[str]) -> str | None:
    """Latest `<!-- review-pr-branches: head=<sha> -->` in chronological comments."""
    for body in reversed(comment_bodies):
        match = REVIEW_MARKER_RE.search(body or "")
        if match:
            return match.group(1)
    return None


def decide_verdict(head_sha: str, marker_sha: str | None) -> Verdict:
    if marker_sha is None:
        return Verdict.FULL
    if marker_sha.lower() == head_sha.lower():
        return Verdict.SKIP
    return Verdict.INCREMENTAL


@dataclass
class GateSnapshot:
    title: str
    author: str
    head_sha: str
    marker_sha: str | None


def get_gate_snapshot(url: str) -> GateSnapshot:
    """Title, author, head sha, and the latest review marker, in one gh call."""
    payload = (
        run_gh_json(["pr", "view", url, "--json", "title,author,headRefOid,comments"])
        or {}
    )
    bodies = [comment.get("body", "") for comment in payload.get("comments") or []]
    author = payload.get("author") or {}
    return GateSnapshot(
        title=payload.get("title", ""),
        author=author.get("login", ""),
        head_sha=payload.get("headRefOid", ""),
        marker_sha=get_marker_sha(bodies),
    )


def _paginated(args: list[str]) -> list[dict]:
    """`gh api --paginate --slurp` returns an array of pages; flatten it."""
    pages = run_gh_json(args) or []
    if pages and isinstance(pages[0], list):
        return [item for page in pages for item in page]
    return pages


def snapshot_comments(repo: str, number: int) -> dict:
    """Every comment currently on the PR, keyed by id, for before/after diffing."""
    review_comments = _paginated(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/pulls/{number}/comments?per_page=100",
        ]
    )
    issue_comments = _paginated(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/issues/{number}/comments?per_page=100",
        ]
    )
    return {
        "review": {comment["id"]: comment for comment in review_comments},
        "issue": {comment["id"]: comment for comment in issue_comments},
    }


def get_headline(body: str) -> str:
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def classify_new_posts(before: dict, after: dict) -> list[PostedComment]:
    """Comments that appeared while the AI ran, restricted to ones this skill writes.

    Anything else on the PR (a teammate replying mid-run) is ignored, so the
    counters never credit someone else's comment to this loop.
    """
    posts: list[PostedComment] = []

    for comment_id, comment in sorted(after.get("review", {}).items()):
        if comment_id in before.get("review", {}):
            continue
        body = comment.get("body", "")
        if comment.get("in_reply_to_id") and PASSED_MARKER_RE.search(body):
            posts.append(
                PostedComment(
                    kind=PostKind.REPLY,
                    comment_id=comment_id,
                    url=comment.get("html_url", ""),
                    headline=get_headline(body),
                )
            )
            continue
        match = CLASSIFICATION_RE.match(body)
        if not match:
            continue
        posts.append(
            PostedComment(
                kind=PostKind.INLINE,
                comment_id=comment_id,
                url=comment.get("html_url", ""),
                classification=Classification(match.group(1)),
                headline=get_headline(body),
            )
        )

    for comment_id, comment in sorted(after.get("issue", {}).items()):
        if comment_id in before.get("issue", {}):
            continue
        body = comment.get("body", "")
        if not REVIEW_MARKER_RE.search(body):
            continue
        posts.append(
            PostedComment(
                kind=PostKind.SUMMARY,
                comment_id=comment_id,
                url=comment.get("html_url", ""),
                headline=get_headline(body),
            )
        )

    return posts
