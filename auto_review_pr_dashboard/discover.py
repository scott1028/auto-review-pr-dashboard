"""PR discovery through `pr-discover`, dry-run JSON mode only.

We never pass `--`, so pr-discover only prints its summary and dispatches
nothing; the head-marker verdict in gate.py decides what actually gets an agent.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from .config import RunConfig
from .models import SkipReason

DISCOVER_TIMEOUT_SEC = 120

# pr-discover dry runs only ever report these two as our skips; `unchanged` and
# a missing reason (pending) still need the marker check, anything else is a bug.
DISCOVER_SKIP_REASONS = {
    "draft": SkipReason.DRAFT,
    "wip-title": SkipReason.WIP_TITLE,
}


class DiscoverError(RuntimeError):
    """pr-discover failed, timed out, or returned output we cannot use."""


@dataclass
class DiscoveredPr:
    repo: str
    number: int
    url: str
    head_sha: str
    skip_reason: SkipReason | None = None
    matched_axes: list[str] = field(default_factory=list)


def get_discover_args(config: RunConfig) -> list[str]:
    """Scope flags only. A trailing `--` would make pr-discover dispatch."""
    args = ["pr-discover"]
    for repo in config.repos:
        args += ["--repo-url", repo]
    for url in config.urls:
        args += ["--pr-url", url]
    for author in config.authors:
        args += ["--author", author]
    for reviewer in config.reviewers:
        args += ["--reviewer", reviewer]
    return args


def get_stderr_tail(stderr: str | bytes | None) -> str:
    """Last non-empty line, for error messages; bytes-safe for TimeoutExpired."""
    text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else "no output"


def parse_discover_output(stdout: str) -> list[DiscoveredPr]:
    """Parse the JSON summary pr-discover prints on stdout."""
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise DiscoverError(
            f"pr-discover returned non-JSON output: {error}"
        ) from error
    if not isinstance(summary, dict):
        raise DiscoverError(
            f"pr-discover summary is not a JSON object: {json.dumps(summary)[:80]}"
        )
    prs = summary.get("prs")
    if not isinstance(prs, list):
        raise DiscoverError(
            f"pr-discover summary has no `prs` list: {json.dumps(summary)[:80]}"
        )

    discovered = []
    for entry in prs:
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or "#" not in key:
            raise DiscoverError(
                f"pr-discover entry has no `owner/repo#n` key: {key!r}"
            )
        repo, number_text = key.rsplit("#", 1)
        try:
            number = int(number_text)
        except ValueError as error:
            raise DiscoverError(
                f"pr-discover key has no PR number: {key!r}"
            ) from error
        reason = entry.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise DiscoverError(
                f"pr-discover entry has a non-string reason: {reason!r}"
            )
        if reason is None or reason == "unchanged":
            # `pending` has no reason; `unchanged` still needs the marker check.
            skip_reason = None
        elif reason in DISCOVER_SKIP_REASONS:
            skip_reason = DISCOVER_SKIP_REASONS[reason]
        else:
            raise DiscoverError(f"pr-discover reported unknown reason: {reason}")
        discovered.append(
            DiscoveredPr(
                repo=repo,
                number=number,
                url=entry.get("url", ""),
                head_sha=entry.get("head_sha", ""),
                skip_reason=skip_reason,
                # Missing on a pr-discover older than this field.
                matched_axes=list(entry.get("matched_axes") or []),
            )
        )
    return discovered


def discover_prs(config: RunConfig) -> list[DiscoveredPr]:
    args = get_discover_args(config)
    try:
        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=DISCOVER_TIMEOUT_SEC,
        )
    except FileNotFoundError as error:
        raise DiscoverError("pr-discover not found on PATH") from error
    except OSError as error:
        # Not executable, or replaced while we exec it.
        raise DiscoverError(f"cannot run pr-discover: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise DiscoverError(
            f"pr-discover timed out after {DISCOVER_TIMEOUT_SEC}s:"
            f" {get_stderr_tail(error.stderr)}"
        ) from error

    if process.returncode != 0:
        raise DiscoverError(
            f"pr-discover exited {process.returncode}: {get_stderr_tail(process.stderr)}"
        )
    try:
        return parse_discover_output(process.stdout)
    except DiscoverError as error:
        raise DiscoverError(
            f"{error} (pr-discover stderr: {get_stderr_tail(process.stderr)})"
        ) from error
