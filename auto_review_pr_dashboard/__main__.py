"""CLI entry point: `python3 -m auto_review_pr_dashboard <ai_cli> [<prompt>] [options]`."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .config import (
    SUPPORTED_AI_CLIS,
    RunConfig,
    UnsupportedAiCliError,
    get_ai_cli_family,
)
from .models import Verdict

USAGE_EXAMPLES = """prompt file:
  prompt.md in the current working directory is read once at startup.
  When prompt.md is absent, it is created with: code review
  prompt.md is always the prompt source; the positional prompt is ignored.
  Empty or whitespace-only files are rejected; --resume does not reread it.
  Add any custom skill invocation to prompt.md itself, for example:
    /baseline-fe-code-review
    1. Focus on finding bugs.
    2. Treat missing tests as non-blocking; do not hold the feature release.

examples:
  auto-review-pr-dashboard codex --repo-url https://github.com/owner/repo --author <author-username> --dry-run
  auto-review-pr-dashboard codex --repo-url https://github.com/<owner>/<repo1> --repo-url https://github.com/<owner>/<repo2> --author <author-username> --reviewer <reviewer-username>
  auto-review-pr-dashboard codex --pr-url https://github.com/<owner>/<repo>/pull/<pr-number>
  auto-review-pr-dashboard claude --pr-url https://github.com/<owner>/<repo>/pull/<pr-number>
  auto-review-pr-dashboard --resume
  auto-review-pr-dashboard -l
"""

# Both spellings of the listing flag, shared by parse_args and main so the
# "must be used alone" rule and the dispatch cannot drift apart.
LIST_ARGV = (["-l"], ["--list"])


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-review-pr-dashboard",
        description=(
            "Loop over open PRs and review each one with an AI CLI, one process per PR, "
            "with a dashboard showing progress, per-PR logs, and cancel / resume. Search scope "
            "is specified via --repo-url, --pr-url, --author, and --reviewer flags."
        ),
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ai_cli",
        nargs="?",
        help=f"{' | '.join(SUPPORTED_AI_CLIS)} (or <name>-* variants, e.g. codex-personal)",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="positional prompt; ignored in favour of ./prompt.md",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="attach to this working directory's background daemon",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list working directories with a live background daemon to --resume",
    )
    parser.add_argument(
        "--repo-url",
        action="append",
        default=[],
        metavar="OWNER/REPO|URL",
        help="repository to search, as owner/repo or its GitHub url "
        "(may be repeated; requires --author)",
    )
    parser.add_argument(
        "--pr-url",
        action="append",
        default=[],
        metavar="URL",
        help="explicit PR URL to review (may be repeated; at least one of --repo-url or --pr-url is required)",
    )
    parser.add_argument(
        "--author",
        action="append",
        default=[],
        metavar="LOGIN",
        help="filter by author login (required with --repo-url; may be repeated)",
    )
    parser.add_argument(
        "--reviewer",
        action="append",
        default=[],
        metavar="LOGIN",
        help="filter by reviewer login (optional; may be repeated)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="MIN",
        help="minutes between loops (default: 60)",
    )
    parser.add_argument(
        "--pr-timeout",
        type=int,
        default=20,
        metavar="MIN",
        help="kill one PR's AI process after this long (default: 20)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=30,
        metavar="MIN",
        help="wait this long after a suspected usage limit before retrying (default: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="discover PRs and print the verdict table, without starting any AI process",
    )
    return parser


def get_scope_flags(parser, args) -> tuple[list[str], list[str]]:
    """Normalise --repo-url to `owner/repo` and reject anything unusable.

    Everything downstream builds paths from these - `repos/<repo>/pulls/...` for
    `gh api`, and the per-PR log file name - so a url reaching them silently
    corrupts both. Fail here, before the dashboard takes over the terminal.
    """
    from .gh import get_pr_url_parts, get_repo_slug

    repos: list[str] = []
    for value in args.repo_url:
        slug = get_repo_slug(value)
        if slug is None:
            parser.error(
                f"--repo-url {value!r} is not a repository; use owner/repo or "
                "https://github.com/owner/repo (a pull request url goes to --pr-url)"
            )
        if slug not in repos:
            repos.append(slug)

    urls: list[str] = []
    for value in args.pr_url:
        if get_pr_url_parts(value) is None:
            parser.error(
                f"--pr-url {value!r} is not a pull request url; expected "
                "https://github.com/owner/repo/pull/<number>"
            )
        if value not in urls:
            urls.append(value)

    return repos, urls


def parse_args(argv: list[str] | None = None) -> RunConfig | None:
    parser = get_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_args)

    if args.resume:
        if raw_args != ["--resume"]:
            parser.error("--resume must be used alone")
        return None

    if args.list:
        if raw_args not in LIST_ARGV:
            parser.error("--list must be used alone")
        return None

    if args.ai_cli is None:
        parser.error("ai_cli is required unless using --resume")

    try:
        get_ai_cli_family(args.ai_cli)
    except UnsupportedAiCliError as error:
        parser.error(str(error))

    prompt_path = Path.cwd() / "prompt.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            prompt_path.write_text("code review\n", encoding="utf-8")
            prompt = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            parser.error(f"prompt.md could not be created or read: {error}")
    except (OSError, UnicodeError) as error:
        parser.error(f"prompt.md could not be read: {error}")

    if not prompt.strip():
        parser.error("prompt.md must not be empty or whitespace-only")
    if not args.repo_url and not args.pr_url:
        parser.error("at least one of --repo-url or --pr-url is required")
    if args.repo_url and not args.author:
        parser.error(
            "--author is required when using --repo-url (at least one author must be specified to search for PRs)"
        )
    if args.interval < 1:
        parser.error("--interval must be a positive integer")
    if args.pr_timeout < 1:
        parser.error("--pr-timeout must be a positive integer")
    if args.cooldown < 1:
        parser.error("--cooldown must be a positive integer")

    repos, urls = get_scope_flags(parser, args)

    return RunConfig(
        ai_cli=args.ai_cli,
        prompt=prompt,
        repos=repos,
        urls=urls,
        authors=args.author,
        reviewers=args.reviewer,
        interval_min=args.interval,
        pr_timeout_min=args.pr_timeout,
        cooldown_min=args.cooldown,
        dry_run=args.dry_run,
    )


async def print_dry_run(config: RunConfig) -> int:
    """Discover PRs and print the verdict table, without starting any AI process."""
    from .gh import discover_pr_items

    if not config.repos and not config.urls:
        print("no scope specified", file=sys.stderr)
        return 1

    print()
    print(f"repos     : {', '.join(config.repos) or '(none)'}")
    print(f"authors   : {', '.join(config.authors) or '(none)'}")
    print(f"reviewers : {', '.join(config.reviewers) or '(none)'}")
    print(f"urls      : {', '.join(config.urls) or '(none)'}")
    print()

    items = discover_pr_items(config)
    if not items:
        print("no candidate PR found")
        return 0

    key_width = max(len(item.key) for item in items)
    for item in items:
        title = item.title if len(item.title) <= 48 else item.title[:47] + "\u2026"
        print(
            f"{item.key:<{key_width}}  {item.verdict_label:<11}  "
            f"head {item.short_head}  {title}"
        )
        print(f"{'':<{key_width}}  matched: {', '.join(item.matched_axes)}")

    queued = sum(1 for item in items if item.verdict is not Verdict.SKIP)
    print()
    print(
        f"scanned {len(items)} \u2014 "
        f"Full {sum(1 for i in items if i.verdict is Verdict.FULL)} / "
        f"Incremental {sum(1 for i in items if i.verdict is Verdict.INCREMENTAL)} / "
        f"Skip {sum(1 for i in items if i.verdict is Verdict.SKIP)}; "
        f"{queued} would be reviewed"
    )
    return 0


def print_workspaces() -> int:
    """List the working directories whose daemon is still attachable."""
    from .session import get_live_workspaces
    from .stats import format_duration

    workspaces = get_live_workspaces()
    if not workspaces:
        print("no live background daemon found; nothing to --resume")
        return 0

    now = time.time()
    cwd_width = max(len(metadata.cwd) for metadata in workspaces)
    print()
    for metadata in workspaces:
        print(
            f"{metadata.cwd:<{cwd_width}}  pid {metadata.pid:<7}  "
            f"up {format_duration(now - metadata.started_at)}"
        )
    print()
    print(
        f"{len(workspaces)} live daemon{'s' if len(workspaces) > 1 else ''} "
        "\u2014 cd into one, then: auto-review-pr-dashboard --resume"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    config = parse_args(raw_args)
    if raw_args in LIST_ARGV:
        return print_workspaces()

    from .session import (
        SessionClient,
        SessionError,
        get_resume_metadata,
        start_daemon,
    )
    from .store import Store

    store = Store()
    try:
        if config is None:
            # get_resume_metadata has already rejected a config this version
            # cannot rebuild, so this cannot raise.
            metadata = get_resume_metadata(store)
            config = RunConfig(**metadata.config)
        else:
            metadata = start_daemon(config, store) if not config.dry_run else None
    except SessionError as error:
        print(f"auto-review-pr-dashboard: {error}", file=sys.stderr)
        return 1

    from .gh import GhError

    try:
        if config.dry_run:
            return asyncio.run(print_dry_run(config))
    except GhError as error:
        print(f"auto-review-pr-dashboard: {error}", file=sys.stderr)
        return 1

    from .app import AutoReviewPrDashboardApp

    result = AutoReviewPrDashboardApp(
        config,
        store=store,
        session=SessionClient(metadata),
    ).run()
    if result == "background":
        print(
            "auto-review-pr-dashboard: running in background; "
            "resume with auto-review-pr-dashboard --resume"
        )
    elif result == "replaced":
        print(
            "auto-review-pr-dashboard: this dashboard was replaced by a newer --resume"
        )
    elif result in ("connection-error", "disconnected"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
