"""Search criteria parsed out of the user's prompt by one agent pass.

The agent only reads the instruction; finding PRs stays deterministic in gh.py.
Everything here validates what the agent produced and caches it per prompt, so a
run that keeps the same prompt never pays for a second parse.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# The agent writes a JSON file, and prints the same JSON between these markers as
# a fallback - a file is easier for an agent to get right than clean stdout.
JSON_FENCE_START = "<<<AUTO_REVIEW_PR_DASHBOARD_JSON"
JSON_FENCE_END = ">>>"
JSON_FENCE_RE = re.compile(
    re.escape(JSON_FENCE_START) + r"\s*(?P<json>.*?)\s*" + re.escape(JSON_FENCE_END),
    re.DOTALL,
)

LIST_FIELDS = ("repos", "authors", "assignees", "reviewers", "urls")
PR_URL_RE = re.compile(
    r"https?://[^/\s]*github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)"
)


class CriteriaError(ValueError):
    """The agent's output could not be turned into usable criteria."""


@dataclass
class Criteria:
    repos: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    # Conditions the agent understood but this layer cannot express (labels,
    # dates, ...). Surfaced as a warning instead of being silently dropped.
    notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in LIST_FIELDS)

    def to_dict(self) -> dict:
        return {name: list(getattr(self, name)) for name in LIST_FIELDS} | {
            "notes": self.notes
        }

    def summary(self) -> str:
        parts = []
        if self.repos:
            parts.append(", ".join(self.repos))
        for label, values in (
            ("author", self.authors),
            ("assignee", self.assignees),
            ("reviewer", self.reviewers),
        ):
            if values:
                parts.append(f"{label} {', '.join(values)}")
        if self.urls:
            parts.append(f"urls {len(self.urls)}")
        return " · ".join(parts) or "(empty)"


def get_prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def get_pr_url_parts(url: str):
    """`(owner/repo, number)` for a GitHub PR url, or None when it is not one."""
    match = PR_URL_RE.search(url or "")
    if not match:
        return None
    return match.group("repo"), int(match.group("number"))


def _clean_list(value, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CriteriaError(
            f"`{field_name}` must be a list, got {type(value).__name__}"
        )

    cleaned = []
    for entry in value:
        if not isinstance(entry, str):
            raise CriteriaError(
                f"`{field_name}` must contain strings, got {type(entry).__name__}"
            )
        entry = entry.strip()
        if entry and entry not in cleaned:
            cleaned.append(entry)
    return cleaned


def parse_criteria(payload: dict) -> Criteria:
    """Validate one decoded JSON object into Criteria. Unknown keys are ignored."""
    if not isinstance(payload, dict):
        raise CriteriaError(f"expected a JSON object, got {type(payload).__name__}")

    values = {name: _clean_list(payload.get(name), name) for name in LIST_FIELDS}

    for url in values["urls"]:
        if get_pr_url_parts(url) is None:
            raise CriteriaError(f"`urls` entry is not a GitHub pull request url: {url}")

    # A repo named only by a url still belongs in scope, so callers can search it.
    for url in values["urls"]:
        repo, _number = get_pr_url_parts(url)
        if repo not in values["repos"]:
            values["repos"].append(repo)

    notes = payload.get("notes") or ""
    if not isinstance(notes, str):
        raise CriteriaError(f"`notes` must be a string, got {type(notes).__name__}")

    return Criteria(**values, notes=notes.strip())


def get_criteria_from_text(text: str) -> Criteria:
    """Fallback path: pull the fenced JSON block out of the agent's stdout."""
    match = JSON_FENCE_RE.search(text or "")
    if not match:
        raise CriteriaError(
            f"no {JSON_FENCE_START} ... {JSON_FENCE_END} block in the agent output"
        )
    try:
        payload = json.loads(match.group("json"))
    except json.JSONDecodeError as error:
        raise CriteriaError(f"fenced block is not valid JSON: {error}") from error
    return parse_criteria(payload)


def get_criteria_from_file(path: Path) -> Criteria:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CriteriaError(f"the agent wrote no criteria file at {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise CriteriaError(f"criteria file at {path} is unusable: {error}") from error
    return parse_criteria(payload)


def get_criteria_from_agent(path: Path, output_text: str) -> Criteria:
    """File first, fenced stdout second; report both failures if neither works."""
    try:
        return get_criteria_from_file(path)
    except CriteriaError as file_error:
        try:
            return get_criteria_from_text(output_text)
        except CriteriaError as text_error:
            raise CriteriaError(f"{file_error}; and {text_error}") from text_error


class CriteriaCache:
    """`criteria.json` next to the other artifacts, keyed by the prompt's hash."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, prompt: str) -> Criteria | None:
        """Cached criteria for this exact prompt, or None when absent or stale."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("prompt_hash") != get_prompt_hash(prompt):
            return None
        try:
            return parse_criteria(payload.get("criteria") or {})
        except CriteriaError:
            return None

    def load_any(self) -> Criteria | None:
        """The last criteria written, whatever prompt produced them.

        Used only as a fallback when a fresh parse fails: stale scope beats no
        scope, and the prompt rarely changes between loops of one run.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return parse_criteria(payload.get("criteria") or {})
        except (OSError, json.JSONDecodeError, CriteriaError):
            return None

    def save(self, prompt: str, criteria: Criteria) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "prompt_hash": get_prompt_hash(prompt),
            "parsed_at": time.time(),
            "prompt": prompt,
            "criteria": criteria.to_dict(),
        }
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
