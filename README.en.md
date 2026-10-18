[繁體中文](README.md) | **English**

# <img src="docs/icon.png" width="40" alt="" /> auto-review-pr-dashboard

![Mascot: a mint-teal mechanical robot agent with a magnifying glass](docs/mascot.png)

This tool has an AI review your GitHub PRs one at a time, one AI process per PR. An AI
forgets how far it got when you close its window, so a caretaker in the background (the
daemon, a long-running process) remembers which PRs are waiting, done, or half-reviewed.
The dashboard is just its screen: close it, open another, and pick right back up.

## Install

```bash
make install
```

`make install` runs `uv tool install .` to put `auto-review-pr-dashboard` on `PATH`, plus
a symlink at `~/.bashrc.d/auto-review-pr-dashboard` defining a same-name shell function
that exports your AI function through `BASH_FUNC_<name>%%`: your `ai_cli` (the AI CLI you
use, e.g. `codex`) may be a bash function, not a binary. Open a new shell after
installing.

Each PR's AI runs under `bash -ic` (asserted by `tests/test_config_parity.py`), which
loads `~/.bashrc` before calling `ai_cli`, so its functions work even when the wrapper is
bypassed. Keep in mind: a same-name definition there shadows the exported one, and
whatever your shell prints at startup lands in the PR log.

| Target | What it does |
|---|---|
| `make` | `uv sync`: dependencies only, into the local `.venv` |
| `make install` | install the CLI and the shell wrapper |
| `make uninstall` | remove both, including the daemon socket directory. Refuses while the daemon is alive (press `q` in the dashboard first, or force it with `FORCE=1`); keeps `.tmp/auto-review-pr-dashboard/`; removes the symlink only if it still points at this repo |
| `make test` | run the tests in the uv environment |
| `make clean` | remove `.venv`, `__pycache__`, and the generated `requirements.txt` |
| `make requirements` | optional: generate a pinned `requirements.txt` from `uv.lock` |

No uv? `pip install .`, then run `python3 -m auto_review_pr_dashboard`. No Python either?
[Releases](https://github.com/scott1028/auto-review-pr-dashboard/releases) ship prebuilt
binaries: a Linux AppImage (`x86_64` / `aarch64`; without FUSE filesystem support, prefix
`APPIMAGE_EXTRACT_AND_RUN=1`) and a macOS tar.gz (`aarch64` / `x86_64`; unpack it, run
`bin/auto-review-pr-dashboard`).

## Start reviewing PRs

In one line: the prompt decides *how* to review, the flags decide *which* PRs; the tool
follows the flags round after round, finding PRs and sending an AI to review each one.

```bash
auto-review-pr-dashboard <ai_cli> [<prompt>] [options] [-- <ai_cli args>...]
auto-review-pr-dashboard --resume
auto-review-pr-dashboard -l
  scope — at least one of --repo-url / --pr-url:
  --repo-url OWNER/REPO|URL  repos to search (repeatable; needs --author)
  --pr-url URL               review only this PR (repeatable; no --author needed)
  --author LOGIN             filter by author (repeatable)
  --reviewer LOGIN           filter by reviewer (repeatable)

  --interval MIN      minutes between loop rounds (default: 60)
  --pr-timeout MIN    kill one PR's AI process after this many minutes (default: 30)
  --cooldown MIN      minutes to wait after a suspected usage limit (default: 30)
  --parallel N        review at most N PRs at a time (default: 1)
  --dry-run           explore only, print the verdict table, review nothing
  -l, --list          list working dirs with a live daemon to resume
  -- ARGS...          pass ARGS to <ai_cli>: after built-in flags, before the prompt

auto-review-pr-dashboard codex --repo-url owner/repo --author <login> --dry-run
auto-review-pr-dashboard claude --pr-url https://github.com/owner/repo/pull/<n>
auto-review-pr-dashboard codex --pr-url https://github.com/owner/repo/pull/<n> -- --yolo
```

Scope is never guessed from prompt text, so conditions the search can't express ("PRs with
label X") are useless in the prompt. `--repo-url` takes `owner/repo` or any git url; a PR
url is rejected with a hint to use `--pr-url`. Inline env vars reach `gh` and the agent
(the AI process that reviews a PR).

A normal start spawns a detached daemon (cut loose from the terminal) for *your current
working directory* and attaches. To come back, run `--resume` alone from that directory;
it replaces the open dashboard. A dead daemon is only reported, never auto-restarted;
`--dry-run` stays in the foreground.

### Writing prompt.md

`prompt.md` decides how the agent reviews. It is read at startup; if missing you get one
containing exactly `code review`. The whole file is the prompt (the positional prompt
argument is ignored, empty is rejected), plus an auto-generated single-PR scope. The
daemon reads it once, so editing it or `--resume` never makes it reread. Skill invocations
are never added automatically; add them yourself, e.g.:

```text
code review
/baseline-fe-code-review
```

## Dashboard keys

| Key | Where | Action |
|---|---|---|
| `↑` / `↓`, mouse click | list | select a PR (a single click opens it) |
| `enter` | list | open the PR detail page |
| `c` | list / detail | cancel this PR: the running agent's whole process group (it and its child processes) is killed |
| `r` | list / detail | re-queue a cancelled PR at the end of this round's queue |
| `n` | anywhere | skip any countdown, either the idle wait between rounds or a cooldown |
| `p` | anywhere | pause: running PRs finish, then nothing new starts; countdowns freeze |
| `l` | anywhere | show / hide the live agent log panel |
| `d` | anywhere | detach: close the dashboard, the daemon keeps running |
| `f` / `esc` | detail | toggle log follow (on: new lines keep arriving; off: the log holds still) / back to the list |
| `q` | anywhere | stop the daemon and quit (each agent gets SIGTERM, then SIGKILL after `TERMINATE_GRACE_SEC` — 5s, in `runner.py`); asks first if PRs are running, defaults to No |

## Architecture: TUI → IPC → Service

TUI is the terminal screen, IPC is the channel between two processes (a unix socket here),
and Service is the daemon that runs the reviews.

```text
  terminal (disposable)          detached daemon (owns the work)
┌──────────────────────┐  JSON   ┌──────────────────────────────────┐
│ app.py — Textual TUI │  lines  │ session.py — SessionServer       │
│  render snapshot     │◀────────│   snapshot / log / tick / notice │
│  keypress → command  │────────▶│   cancel resume run_now pause    │
└──────────────────────┘  unix   │ runner.py — LoopRunner           │
   close it, lose nothing        │ store.py — .tmp/ artifacts       │
   --resume picks it up          └──────────────────────────────────┘
                                       └─▶ one process group per running PR: <ai_cli>
```

- **Reviews outlive your terminal.** A PR can take 30 minutes and a loop round hours, and
  an agent parented to the terminal dies with its window or SSH session;
  `start_new_session` prevents that, so `d` or closing the window keeps the work going.
- **Killing mid-review isn't free.** An agent may have posted inline comments but not the
  closing marker (a hidden comment recording the reviewed commit), so the next round
  re-reviews that SHA; the detail page says so. The queue holder owns the process's life.
- **The service shouldn't know the UI.** `LoopRunner` only pushes events to an `on_event`
  callback, so the same class runs under the daemon or in-process against a mock CLI.
- **The socket is the contract, so the TUI stays stateless.** Every attach gets a full
  snapshot (the current state), the daemon pushes one on every state change, and a
  keypress is a `command` message, not a runner call. Slow clients are handled there too:
  ticks carry no snapshot, and a client buffering over 1 MiB is dropped.
- **One working directory, one daemon**, over a unix socket at
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock` (mode `0700`; a `.lock` queues
  startup, unclaimed `.lock`/`.sock` pairs are cleared on start/recycle/stop). Local only,
  no port, no auth; the handshake checks protocol version, run id, cwd; a second start in
  the same directory is rejected.
- **Long runs change shifts (recycle).** `run_forever` returns at recycle time; the old
  daemon takes `.lock`, clears `.tmp/`, and hands loop state to its successor via stdin;
  the TUI waits for a new `run_id` in `daemon.json`, then reattaches.

## What each loop round does

1. **Discover** — one `gh pr list` call per scope axis (one filter condition): one per
   `--author`, and per reviewer one each for `review-requested:` and `reviewed-by:`,
   unioned then deduplicated. GitHub ANDs everything in one `--search`, hence the separate
   calls; with both scopes, PRs matching either side are kept.
2. **Decide** — Draft or `[WIP]` / `WIP:` in the title means `Skip (WIP)`. Otherwise
   `gh pr view --json headRefOid,comments` finds the newest
   `<!-- review-pr-branches: head=<sha> -->` marker: none means `Full`, a different SHA
   `Incremental`, the same SHA `Skip`; `Skip` PRs stay visible but never get an agent.
3. **Review** — one agent per remaining PR, at most `--parallel` at once, per `prompt.md`.
4. **Diff** — comment ids are snapshotted before and after each run; what was posted comes
   from GitHub, not from parsing the agent's output. Only comments with this skill's
   marker count, so teammates posting mid-run aren't recorded.

## When an agent hits a usage limit

Once a usage limit hits, every PR fails fast and the rest of the queue would burn in
seconds. Detection only looks at **failed** runs, so normal reviews never trip it: a
failed output's tail matches a quota pattern (`usage limit`, `429`, `quota`, …), or two
failures in a row regardless of wording, which also catches expired logins, a broken CLI,
or a dead network.

That PR returns to the **front** of the queue as `queued`, and the loop waits behind a
banner for `--cooldown` minutes; running `--parallel` agents finish and their failures
join that cooldown. A run that already posted comments, or whose comment snapshot failed
unnoticed, is marked `failed` instead, since rerunning may duplicate posts.

After 3 cooldowns the round gives up and waits for `--interval`. Each failure records the
agent's last output in that PR's `error` field. The patterns are best-effort (add new
wording to `USAGE_LIMIT_PATTERNS` in `config.py`); the consecutive-failure circuit breaker
is the real safety net.

## Where files go

Everything is written under **the working directory you ran the command in** (usually that
multi-repo parent):

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # current daemon metadata and socket location
├── daemon.log                  # daemon diagnostics
├── state.json                  # run config + current loop number
├── loops/loop-0003.json        # per-round record: verdicts, states, post diffs, times
└── logs/loop-0003/owner__repo__7.log
```

Every `RECYCLE_EVERY_LOOPS` rounds (10, in `session.py`) the recycle deletes the whole
`.tmp/`, other tools' files included, so `loops/` and `daemon.log` don't grow forever; the
status bar shows these cleanups as `cleaned N×`. In between, `logs/` keeps **the 10 most
recent loop directories** (10 rounds, not 10 hours); raise `MAX_KEPT_LOOP_DIRS` in
`store.py` to keep more.

## Pitfalls to watch for

- A daemon that dies uncleanly leaves its agents running. The next start kills those
  process groups from `agent_groups` in `daemon.json` (skipping pids that were reused);
  descendants that called `setsid` themselves are out of reach.
- `-l` reads `/proc`: Linux only, current user's daemons only; the dashboard needs a TTY.
  `--parallel` agents share the working directory, so temp files with fixed names collide.
- The log panel is a memory-bounded view; the full text is in the `.log`. Lines over 2000
  chars show the first 2000 plus `(+N chars)`; the panel keeps the last 2000 lines, only
  running PRs buffer, and >64KiB without a newline is hard-wrapped in the file.

## Run tests, ship a release

```bash
make test                               # 145 tests, no network
python3 -m unittest discover -s tests   # same, without uv
```

To ship a release, bump `[project].version` in `pyproject.toml` and push to `master`; the
Release workflow publishes only when that beats every published version: tests → Linux
AppImages (x86_64/aarch64) + macOS bundles (aarch64/x86_64) → GitHub Release. By hand:
Actions → Release → Run workflow, no version check; tag
`v<version>-<first 8 chars of commit>`. When bumping, run `uv lock` and commit `uv.lock`.
