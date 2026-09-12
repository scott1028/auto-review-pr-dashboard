"""All GitHub access, through the `gh` CLI.

Everything here is deterministic - PR discovery, head SHA, marker lookup, and the
before/after comment diff. None of it needs an AI, so the dashboard can show a full
verdict table before spending a single token.

Search axes and the marker format follow the review-pr-branches skill.
"""

from __future__ import annotations

import json
import re
import subprocess

from .config import RunConfig
from .models import (
    Classification,
    PostedComment,
    PostKind,
    PrItem,
    PrState,
    SkipReason,
    Verdict,
)

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

PR_LIST_FIELDS = "number,url,title,author,isDraft"
WIP_TITLE_RE = re.compile(r"\[wip\]|\bwip:", re.IGNORECASE)
PR_URL_RE = re.compile(
    r"https?://[^/\s]*github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)"
)
# `owner/repo`, `https://github.com/owner/repo(.git)(/)`, or `git@host:owner/repo.git`.
# Anchored, so a pull request url can never slip through as a repo.
REPO_SLUG_RE = re.compile(
    r"^(?:https?://[^/\s]*github\.com/|git@[^:\s]+:)?(?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)


class GhError(RuntimeError):
    """A gh call failed, or the scope could not be resolved."""


def run_gh(args: list[str]) -> str:
    process = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SEC,
    )
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


def get_pr_url_parts(url: str):
    """`(owner/repo, number)` for a GitHub PR url, or None when it is not one."""
    match = PR_URL_RE.search(url or "")
    if not match:
        return None
    return match.group("repo"), int(match.group("number"))


def get_repo_slug(value: str):
    """`owner/repo` from a slug, a GitHub repo url, or an ssh remote; None otherwise.

    Everything downstream builds paths out of this - `repos/<repo>/pulls/...` for
    `gh api` and the per-PR log file name - so a raw url here corrupts both.
    """
    match = REPO_SLUG_RE.match((value or "").strip())
    return match.group("repo") if match else None


def resolve_scope(config: RunConfig) -> RunConfig:
    """Validate scope from CLI flags. No auto-fill — caller must specify everything."""
    if config.urls:
        # Explicit URLs identify their own repos; add them to scope.
        for url in config.urls:
            parts = get_pr_url_parts(url)
            if parts is not None:
                repo, _number = parts
                if repo not in config.repos:
                    config.repos.append(repo)
    return config


def get_search_axes(config: RunConfig) -> list[tuple[str, list[str]]]:
    """One (axis, gh-args) pair per search. GitHub ANDs qualifiers inside one
    --search string, so these MUST stay separate calls and be unioned."""
    axes = []
    for author in config.authors:
        axes.append((f"author:{author}", ["--author", author]))
    for reviewer in config.reviewers:
        axes.append(
            (
                f"review-requested:{reviewer}",
                ["--search", f"review-requested:{reviewer}"],
            )
        )
        axes.append(
            (f"reviewed-by:{reviewer}", ["--search", f"reviewed-by:{reviewer}"])
        )
    return axes


def merge_candidates(hits: list[tuple[str, str, dict]]) -> list[dict]:
    """Union (repo, axis, pr-json) hits, de-duplicating on `<owner>/<repo>#<number>`.

    A bare number would collapse repoA#123 and repoB#123 into one and silently
    drop the second repo's PR.
    """
    merged: dict[str, dict] = {}
    for repo, axis, hit in hits:
        key = f"{repo}#{hit['number']}"
        candidate = merged.get(key)
        if candidate is None:
            author = hit.get("author") or {}
            merged[key] = {
                "repo": repo,
                "number": hit["number"],
                "url": hit["url"],
                "title": hit.get("title", ""),
                "author": author.get("login", ""),
                "is_draft": hit.get("isDraft", False),
                "matched_axes": [axis],
            }
            continue
        if axis not in candidate["matched_axes"]:
            candidate["matched_axes"].append(axis)
    return list(merged.values())


def get_url_hits(urls: list[str]) -> list[tuple[str, str, dict]]:
    """Resolve explicitly named PR urls into candidate hits.

    A url identifies its own repo, so it is reviewed whether or not that repo is
    in the searched scope.
    """
    hits = []
    for url in urls:
        parts = get_pr_url_parts(url)
        if parts is None:
            continue
        repo, _number = parts
        found = run_gh_json(["pr", "view", url, "--json", PR_LIST_FIELDS])
        if found:
            hits.append((repo, "url", found))
    return hits


def discover_candidates(config: RunConfig) -> list[dict]:
    """Discover open PRs matching the scope.

    When both --author and --reviewer are specified, results are AND'd across axes:
    a PR must match at least one author axis AND at least one reviewer axis to be included.
    When only one dimension is specified, all matching PRs are returned.
    """
    hits: list[tuple[str, str, dict]] = get_url_hits(config.urls)

    axes = get_search_axes(config)
    if not axes:
        # No author/reviewer filters — return URL hits only
        return merge_candidates(hits)

    has_author_axis = bool(config.authors)
    has_reviewer_axis = bool(config.reviewers)

    for repo in config.repos:
        for axis, axis_args in axes:
            found = run_gh_json(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--json",
                    PR_LIST_FIELDS,
                    *axis_args,
                ]
            )
            for hit in found or []:
                hits.append((repo, axis, hit))

    merged = merge_candidates(hits)

    # AND across axes: if both author and reviewer are specified,
    # only keep PRs that match at least one of each.
    if has_author_axis and has_reviewer_axis:

        def is_author_axis(axis_name):
            return axis_name.startswith("author:")

        def is_reviewer_axis(axis_name):
            return axis_name.startswith("review-requested:") or axis_name.startswith(
                "reviewed-by:"
            )

        merged = [
            candidate
            for candidate in merged
            if any(is_author_axis(a) for a in candidate["matched_axes"])
            and any(is_reviewer_axis(a) for a in candidate["matched_axes"])
        ]

    return merged


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


def get_is_wip_title(title: str) -> bool:
    return WIP_TITLE_RE.search(title) is not None


def get_head_and_marker(url: str) -> tuple[str, str | None]:
    payload = run_gh_json(["pr", "view", url, "--json", "headRefOid,comments"]) or {}
    bodies = [comment.get("body", "") for comment in payload.get("comments") or []]
    return payload.get("headRefOid", ""), get_marker_sha(bodies)


def discover_pr_items(config: RunConfig, on_progress=None) -> list[PrItem]:
    """Full discovery pass: search, then resolve each candidate's verdict.

    `on_progress(done, total, item)` lets the UI fill the table while this runs -
    the per-PR head/marker calls are one round trip each.
    """
    candidates = discover_candidates(config)
    repo_order = {repo: index for index, repo in enumerate(config.repos)}
    candidates.sort(
        key=lambda hit: (repo_order.get(hit["repo"], len(repo_order)), hit["number"])
    )

    items = []
    for done, candidate in enumerate(candidates, start=1):
        head_sha, marker_sha = get_head_and_marker(candidate["url"])
        is_draft = candidate.get("is_draft", False)
        is_wip_title = get_is_wip_title(candidate["title"])
        if is_draft:
            verdict = Verdict.SKIP
            skip_reason = SkipReason.DRAFT
        elif is_wip_title:
            verdict = Verdict.SKIP
            skip_reason = SkipReason.WIP_TITLE
        else:
            verdict = decide_verdict(head_sha, marker_sha)
            skip_reason = (
                SkipReason.HEAD_ALREADY_REVIEWED
                if verdict is Verdict.SKIP
                else None
            )
        item = PrItem(
            repo=candidate["repo"],
            number=candidate["number"],
            url=candidate["url"],
            title=candidate["title"],
            author=candidate["author"],
            head_sha=head_sha,
            matched_axes=candidate["matched_axes"],
            last_marker_sha=marker_sha,
            verdict=verdict,
            state=PrState.SKIP if verdict is Verdict.SKIP else PrState.QUEUED,
            skip_reason=skip_reason,
        )
        items.append(item)
        if on_progress:
            on_progress(done, len(candidates), item)
    return items


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
