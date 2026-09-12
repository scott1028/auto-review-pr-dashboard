"""AI CLI invocation table and per-PR prompt scoping."""

import re
from dataclasses import dataclass, field

# family -> flags placed between the binary name and the prompt argument
AI_CLI_FLAGS = {
    # -s workspace-write + network_access=true so the agent can run gh, etc.
    # --skip-git-repo-check so multi-repo runs can start from a non-repo parent dir
    "codex": [
        "exec",
        "-s",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--skip-git-repo-check",
    ],
    # headless mode denies tool calls by default; bypassPermissions allows gh / network tools
    "claude": ["-p", "--permission-mode", "bypassPermissions"],
    # no sandbox, tools have network access by default
    "pi": ["-p"],
    # no sandbox flag, permissions come from opencode.json
    "opencode": ["run"],
}

SUPPORTED_AI_CLIS = tuple(AI_CLI_FLAGS)

# Appended to the user prompt so one AI process reviews exactly one PR.
# The autonomous-run signal is stated explicitly, otherwise the skill falls back
# to interactive and stalls.
# MUST NOT contain re-review keywords - they would override the skill's own
# `skipped` detection and cause duplicate comments on an unchanged head SHA.
SCOPE_PROMPT_TEMPLATE = """
---
[auto-review-pr-dashboard scope]
This is an automated scheduled run with no user present. Do not ask any question;
post the review directly (autonomous mode).
Review exactly this one pull request: {url}
Repository: {repo}. Do not discover, list, or review any other pull request in this run.
The current working directory is a plain parent directory, not a git checkout, so read
all PR content through `gh` instead of the working tree.
"""


class UnsupportedAiCliError(ValueError):
    """Raised when the ai_cli argument matches no known family."""


def get_ai_cli_family(ai_cli):
    """Map `codex`, `codex-personal`, ... to the `codex` flag family."""
    for family in AI_CLI_FLAGS:
        if ai_cli == family or ai_cli.startswith(f"{family}-"):
            return family
    raise UnsupportedAiCliError(
        f"Unsupported ai_cli: {ai_cli} "
        f"(supported: {', '.join(SUPPORTED_AI_CLIS)} or their -* variants)"
    )


def get_scoped_prompt(prompt, repo, url):
    return prompt + SCOPE_PROMPT_TEMPLATE.format(url=url, repo=repo)


def get_ai_command(ai_cli, prompt, repo, url):
    """Full argv for reviewing one PR, run through bash.

    Most of these CLIs are bash functions from `~/.bashrc.d/*`, not binaries -
    `codex-personal` for instance only exists as a function that sets CODEX_HOME.
    execvp cannot see those, so the command goes through `bash -c '"$0" "$@"'`:
    `"$0"` is an ordinary command word, so bash resolves functions exported by
    the launcher (`export -f`) before falling back to PATH.
    """
    scoped_prompt = get_scoped_prompt(prompt, repo, url)
    return get_agent_command(ai_cli, scoped_prompt)


def get_agent_command(ai_cli, prompt):
    family = get_ai_cli_family(ai_cli)
    return ["bash", "-c", '"$0" "$@"', ai_cli, *AI_CLI_FLAGS[family], prompt]


# Matched against the tail of a failed agent run only (see get_is_usage_limit).
# Wording differs per CLI and changes between versions, so this is a best-effort
# fast path; runner.py also has a vendor-agnostic consecutive-failure breaker.
# Claude 2.1.218 really does ship "usage limit reached", "credit balance too
# low", and rate_limit / 429 handling.
USAGE_LIMIT_PATTERNS = (
    r"usage limit",
    r"usage_cap_reached",
    r"rate[ _-]?limit",
    r"\b429\b",
    r"too many requests",
    r"quota",
    r"credit balance",
    r"overloaded",
    r"\b529\b",
)

USAGE_LIMIT_RE = re.compile("|".join(USAGE_LIMIT_PATTERNS), re.IGNORECASE)


def get_is_usage_limit(output_tail):
    """True when failed agent output looks like a quota / rate-limit block.

    Callers MUST only pass output from a run that already failed. A successful
    review can legitimately quote `429` or "quota" from the diff it reviewed.
    """
    return bool(USAGE_LIMIT_RE.search(output_tail or ""))


@dataclass
class RunConfig:
    ai_cli: str
    prompt: str
    repos: list = field(default_factory=list)
    authors: list = field(default_factory=list)
    reviewers: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    interval_min: int = 60
    pr_timeout_min: int = 20
    cooldown_min: int = 30
    dry_run: bool = False
