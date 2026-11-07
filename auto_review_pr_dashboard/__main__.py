"""CLI entry point: `python3 -m auto_review_pr_dashboard <ai_cli> [<prompt>] [options]`."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import (
    SUPPORTED_AI_CLIS,
    RunConfig,
    UnsupportedAiCliError,
    get_ai_cli_family,
)
from .discover import DiscoverError, discover_prs
from .gate import GhError
from .models import PrState, Verdict

USAGE_EXAMPLES = """prompt file:
  prompt.md in the current working directory is read once at startup.
  When prompt.md is absent, it is created with: code review
  prompt.md is always the prompt source; the positional prompt is ignored.
  Empty or whitespace-only files are rejected; --resume does not reread it.
  Add any custom skill invocation to prompt.md itself, for example:
    /baseline-fe-code-review
    1. Focus on finding bugs.
    2. Treat missing tests as non-blocking; do not hold the feature release.

ai_cli arguments:
  Everything after `--` is passed to <ai_cli> unchanged, after its built-in flags
  and before the prompt.

examples:
  auto-review-pr-dashboard codex --repo-url https://github.com/owner/repo --author <author-username> --dry-run
  auto-review-pr-dashboard codex --repo-url https://github.com/<owner>/<repo1> --repo-url https://github.com/<owner>/<repo2> --author <author-username> --reviewer <reviewer-username>
  auto-review-pr-dashboard codex --pr-url https://github.com/<owner>/<repo>/pull/<pr-number>
  auto-review-pr-dashboard claude --pr-url https://github.com/<owner>/<repo>/pull/<pr-number>
  auto-review-pr-dashboard codex --pr-url https://github.com/<owner>/<repo>/pull/<pr-number> -- --yolo
  auto-review-pr-dashboard --resume
  auto-review-pr-dashboard -l
"""

# Both spellings of the listing flag, shared by parse_args and main so the
# "must be used alone" rule and the dispatch cannot drift apart.
LIST_ARGV = (["-l"], ["--list"])


class PreflightError(RuntimeError):
    """pr-discover is not usable yet, or refused the scope."""


class PromptError(RuntimeError):
    """./prompt.md could not be read, or has nothing in it."""


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-review-pr-dashboard",
        description=(
            "Loop over open PRs and review each one with an AI CLI, one process per PR, "
            "with a dashboard showing progress, per-PR logs, and cancel / resume. Search scope "
            "is specified via --repo-url, --pr-url, --author, and --reviewer flags, which "
            "go to pr-discover unchanged; it must be on PATH."
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
        default=30,
        metavar="MIN",
        help="kill one PR's AI process after this long (default: 30)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=30,
        metavar="MIN",
        help="wait this long after a suspected usage limit before retrying (default: 30)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        metavar="N",
        help="review up to N PRs at once (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="discover PRs and print the verdict table, without starting any AI process",
    )
    return parser


def get_prompt() -> str:
    """The whole of ./prompt.md, created with `code review` when absent.

    Called after preflight, so a refused preflight never creates the file.
    """
    prompt_path = Path.cwd() / "prompt.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            prompt_path.write_text("code review\n", encoding="utf-8")
            prompt = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PromptError(
                f"prompt.md could not be created or read: {error}"
            ) from error
    except (OSError, UnicodeError) as error:
        raise PromptError(f"prompt.md could not be read: {error}") from error

    if not prompt.strip():
        raise PromptError("prompt.md must not be empty or whitespace-only")
    return prompt


def parse_args(argv: list[str] | None = None) -> RunConfig | None:
    parser = get_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first `--` goes to the AI CLI unchanged.
    ai_cli_args = []
    parser_args = raw_args
    if "--" in raw_args:
        split_index = raw_args.index("--")
        parser_args = raw_args[:split_index]
        ai_cli_args = raw_args[split_index + 1 :]
    args = parser.parse_args(parser_args)

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

    # Scope values are not validated here: pr-discover checks them and says why.
    if args.interval < 1:
        parser.error("--interval must be a positive integer")
    if args.pr_timeout < 1:
        parser.error("--pr-timeout must be a positive integer")
    if args.cooldown < 1:
        parser.error("--cooldown must be a positive integer")
    if args.parallel < 1:
        parser.error("--parallel must be a positive integer")

    return RunConfig(
        ai_cli=args.ai_cli,
        # main reads prompt.md after preflight: a refused preflight leaves none.
        prompt="",
        repos=args.repo_url,
        urls=args.pr_url,
        authors=args.author,
        reviewers=args.reviewer,
        interval_min=args.interval,
        pr_timeout_min=args.pr_timeout,
        cooldown_min=args.cooldown,
        dry_run=args.dry_run,
        ai_cli_args=ai_cli_args,
        max_parallel=args.parallel,
    )


def run_preflight(config: RunConfig) -> None:
    """Run pr-discover once, so a bad scope or a missing CLI stops the start."""
    try:
        discover_prs(config)
    except DiscoverError as error:
        raise PreflightError(f"pr-discover is not usable: {error}") from error


def print_dry_run(config: RunConfig) -> int:
    """Discover PRs and print the verdict table, without starting any AI process."""
    from .runner import LoopRunner
    from .store import Store

    print()
    print(f"repos     : {', '.join(config.repos) or '(none)'}")
    print(f"authors   : {', '.join(config.authors) or '(none)'}")
    print(f"reviewers : {', '.join(config.reviewers) or '(none)'}")
    print(f"urls      : {', '.join(config.urls) or '(none)'}")
    print()

    items = LoopRunner(config, Store()).build_items()
    if not items:
        print("no candidate PR found")
        return 0

    key_width = max(len(item.key) for item in items)
    for item in items:
        title = item.title if len(item.title) <= 48 else item.title[:47] + "\u2026"
        # A failed PR's marker is unknown, so it has no verdict to show.
        is_failed = item.state is PrState.FAILED
        label = PrState.FAILED.value if is_failed else item.verdict_label
        print(f"{item.key:<{key_width}}  {label:<11}  head {item.short_head}  {title}")
        print(f"{'':<{key_width}}  matched: {', '.join(item.matched_axes)}")
        if is_failed:
            print(f"{'':<{key_width}}  {item.error}")

    decided = [item for item in items if item.state is not PrState.FAILED]
    failed = len(items) - len(decided)
    queued = sum(1 for item in decided if item.verdict is not Verdict.SKIP)
    print()
    print(
        f"scanned {len(items)} \u2014 "
        f"Full {sum(1 for i in decided if i.verdict is Verdict.FULL)} / "
        f"Incremental {sum(1 for i in decided if i.verdict is Verdict.INCREMENTAL)} / "
        f"Skip {sum(1 for i in decided if i.verdict is Verdict.SKIP)}; "
        f"{queued} would be reviewed, {failed} failed"
    )
    return 1 if failed else 0


def print_workspaces() -> int:
    """List the working directories whose daemon is still attachable."""
    from .daemon import get_live_workspaces
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

    from .client import SessionClient
    from .daemon import (
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
            run_preflight(config)
            config.prompt = get_prompt()
            if config.dry_run:
                return print_dry_run(config)
            metadata = start_daemon(config, store)
    except (PreflightError, PromptError, GhError, DiscoverError, SessionError) as error:
        print(f"auto-review-pr-dashboard: {error}", file=sys.stderr)
        return 1

    from .app import AutoReviewPrDashboardApp

    app = AutoReviewPrDashboardApp(config, store=store, session=SessionClient(metadata))
    result = app.run()
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
        # The app records why the connection died; only here can it be read.
        print(f"auto-review-pr-dashboard: {app.exit_message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
