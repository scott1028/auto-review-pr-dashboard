# auto-review-pr-dashboard

A review loop whose queue belongs to the dashboard, not the agent: it discovers PRs
with `gh`, then runs **one AI process per PR**, so progress, per-PR logs, and cancel
/ resume are possible. The instruction always comes from the caller's `prompt.md`.

## Install

```bash
make install
```

`uv tool install .` puts an `auto-review-pr-dashboard` executable on `PATH`, and a
symlink at `~/.bashrc.d/auto-review-pr-dashboard` defines a shell function of the same
name. Both are needed; open a new shell afterwards.

The function is not a convenience: most `ai_cli` names (`codex-personal`, even plain
`codex`) are **bash functions** from `~/.bashrc.d`, not binaries, and only the calling
interactive shell can `export -f` them. It exports the named function as
`BASH_FUNC_<name>%%` before handing off, so the agent — spawned as
`bash -c '"$0" "$@"' <ai_cli> ...` — still resolves it, and stays exported afterwards.

| Target | Action |
|---|---|
| `make` | `uv sync` — dependencies only, into a local `.venv` |
| `make install` | the CLI plus the shell wrapper |
| `make uninstall` | removes both, plus the daemon socket directory |
| `make test` | the suite, inside the uv environment |
| `make clean` | removes `.venv`, `__pycache__`, and a generated `requirements.txt` |
| `make requirements` | optional: a pinned `requirements.txt` from `uv.lock` |

`make uninstall` refuses while a daemon runs (`q` it first, or `FORCE=1`), keeps
`.tmp/auto-review-pr-dashboard/`, and only removes the symlink when it still points here.
Without uv, `pip install .` plus `python3 -m auto_review_pr_dashboard` works.

## Usage

```bash
auto-review-pr-dashboard <ai_cli> [<prompt>] [options]
auto-review-pr-dashboard --resume
auto-review-pr-dashboard -l
  scope — at least one of --repo-url / --pr-url is required:
  --repo-url OWNER/REPO|URL  repo to search (repeatable; requires --author)
  --pr-url URL               review this PR only (repeatable)
  --author LOGIN             filter by author login (repeatable)
  --reviewer LOGIN           filter by reviewer login (repeatable)

  --interval MIN      minutes between loops (default: 60)
  --pr-timeout MIN    kill one PR's AI process after this long (default: 30)
  --cooldown MIN      wait this long after a suspected usage limit (default: 30)
  --dry-run           discover, print the verdict table, review nothing
  -l, --list          list working directories with a live daemon to resume
```

```bash
auto-review-pr-dashboard codex --repo-url owner/repo --author <login> --dry-run
auto-review-pr-dashboard claude --pr-url https://github.com/owner/repo/pull/<n>
```

The prompt says *how* to review, the flags say *which* PRs — scope is never inferred
from prompt text, and anything unsearchable ("PRs labelled X") belongs in the prompt.
`--repo-url` accepts `owner/repo` or any git url, a PR url is rejected with a pointer
to `--pr-url`, and inline env vars reach `gh` and the agent. A normal launch starts a
detached daemon for the caller's working directory and attaches to it (see
[Architecture](#architecture-tui--ipc--service)); `--resume` reattaches from that same
directory, must be used alone, and replaces any previous dashboard, while a crashed
daemon is reported, not restarted. `--dry-run` stays foreground-only.

### Prompt selection

`prompt.md` is read at startup; if absent it is created containing exactly
`code review`. The whole file becomes the prompt (a positional prompt is ignored, an
empty one rejected), and no skill invocation is ever added — add your own:

```text
code review
/baseline-fe-code-review
1. Focus on finding bugs.
```

A generated single-PR scope block is appended; a daemon run reads the file once and
persists it, so later edits — and `--resume` — never change that run.

## Keys

| Key | Where | Action |
|---|---|---|
| `↑` / `↓`, mouse click | list | select a PR (a single click opens it) |
| `enter` | list | open the PR detail screen |
| `c` | list / detail | cancel this PR — running agent gets its process group killed |
| `r` | list / detail | resume a cancelled PR at the tail of this loop's queue |
| `n` | anywhere | skip whatever countdown is running — idle wait or cooldown |
| `p` | anywhere | pause (finishes the current PR, then holds; countdown freezes) |
| `l` | anywhere | show / hide the live agent log panel |
| `d` | anywhere | background the run — detach and close the dashboard, daemon keeps going |
| `f` / `esc` | detail | toggle log follow / back to the list |
| `q` | anywhere | stop the daemon (including the running agent) and quit — asks first while a PR runs, `No` preselected |

## Architecture: TUI → IPC → Service

```text
  terminal (disposable)            detached daemon (owns the work)
┌──────────────────────┐  JSON   ┌──────────────────────────────────┐
│ app.py — Textual TUI │  lines  │ session.py — SessionServer       │
│  render snapshot     │◀────────│   snapshot / log / tick / notice │
│  keypress → command  │────────▶│   cancel resume run_now pause    │
└──────────────────────┘  unix   │ runner.py — LoopRunner           │
    close it, lose nothing       │   queue, gh, one agent per PR    │
    --resume reattaches          │ store.py — .tmp/ artifacts       │
                                 └──────────────────────────────────┘
                                       └─▶ its process group: <ai_cli> on one PR
```

Why the extra hop, instead of running the loop inside the TUI:

- **A review outlives its terminal.** One PR can take 30 minutes and a loop runs for
  hours; as a child of the terminal, the agent dies with a closed tab or a lost SSH
  session. The daemon is spawned with `start_new_session`, so `d`, a closed window and
  a dropped connection all leave the work running.
- **A half-finished review is not free.** An agent killed mid-run may already have
  posted inline comments but not its head marker, so the next loop re-reviews that SHA.
  Process lifetime belongs to whatever owns the queue, not to whatever draws it.
- **The service must not know about the UI.** `LoopRunner` only pushes events into an
  `on_event` callback — which is what lets the same class run under the daemon and
  in-process for tests, driven by a mock AI CLI with no terminal attached.
- **The socket is the contract, so the TUI stays stateless.** Every attach starts with
  a full snapshot, the daemon pushes a new one on each state change, and a keypress is
  a `command` message, not a call into the runner — a reattached dashboard renders the
  last snapshot instead of holding the truth. Backpressure is daemon-side too: ticks
  send no snapshot, and a client buffering past 1 MiB is dropped.
- **One daemon per working directory, on a unix socket** at
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock` (mode `0700`, a sibling `.lock`
  serialising startup) — local only, no port, no auth to get wrong, though the handshake
  still checks protocol version, run id, and cwd. Artifacts and the daemon share one
  scope, so a second launch in that directory is refused, not interleaved.

## What it does per loop

1. **Discovery** — one `gh pr list` per scope axis (`--author` per author,
   `review-requested:` and `reviewed-by:` per reviewer), unioned and de-duplicated;
   GitHub ANDs qualifiers inside one `--search`, so these stay separate calls. Given
   both, the union keeps PRs matching one of each; `--pr-url` needs no `--author`.
2. **Verdict** — a Draft PR or a `[WIP]` / `WIP:` title is `Skip (WIP)`; otherwise
   `gh pr view --json headRefOid,comments` and the latest
   `<!-- review-pr-branches: head=<sha> -->` marker decide `Full` / `Incremental` /
   `Skip`. A `Skip` stays visible but starts no agent; the verdict is display + queue.
3. **Review** — one agent process per remaining PR, sequentially, from `prompt.md`.
4. **Diff** — comment ids are snapshotted before and after each run, so "what got
   posted" comes from GitHub, never from parsing agent output; only comments carrying
   this skill's markers count, so a teammate's mid-run comment is not credited.

## When the agent hits its usage limit

Detection needs a **failed** run, so a normal review cannot trip it: the failed
output's tail matches a quota pattern (`usage limit`, `429`, `quota`, ...), or two
failures land in a row whatever the wording — which also catches lost auth, a broken
CLI, or a dead network. The PR then returns to the **front** of the queue as `queued`
and the loop suspends for `--cooldown` minutes behind a banner `n` can skip, so the
rest of the queue is not burned seconds after the first failure. A run that had already
posted comments is marked `failed` instead, since a rerun could duplicate them; after 3
cooldowns the loop gives up and waits for `--interval`. Every failure records the
agent's last output in the PR's `error` field.

## Artifacts

Written under the **caller's** working directory (usually the multi-repo parent dir):

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # active daemon metadata and socket location
├── daemon.log                  # daemon diagnostics
├── state.json                  # run config + current loop index
├── loops/loop-0003.json        # per-loop record: verdicts, states, posted diff, timings
└── logs/loop-0003/owner__repo__7.log
```

`loops/*.json` is kept forever; `logs/` keeps the **10 newest loop dirs** — 10 loops,
not 10 hours. `MAX_KEPT_LOOP_DIRS` in `store.py` keeps more.

## Caveats

- Cancelling mid-review can leave inline comments posted with no summary marker, so the
  next loop reviews that SHA again; the detail screen says so. A daemon crash is not
  recovered automatically for the same reason — a restart could repeat posted comments.
- `-l` reads `/proc`: Linux-only and current user only. Concurrency is fixed at one PR
  at a time, and a TTY is required.
- The log panels are memory-bounded, not a faithful copy of the `.log`: lines past 2000
  characters show `(+N chars)`, only the last 2000 are kept, only the running PR stays
  buffered, and output with no newline for 64KiB is wrapped in the file itself.
- The usage-limit pattern list is best effort; add new wording to `USAGE_LIMIT_PATTERNS`
  in `config.py` — the consecutive-failure breaker is the safety net behind it.

## Tests

```bash
make test                               # 92 tests, no network
python3 -m unittest discover -s tests   # same, without uv
```

`tests/test_config_parity.py` asserts a bash-function `ai_cli` really runs via `bash -c`.
