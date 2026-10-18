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
  keypress is a `command` message, not a runner call.
- **One working directory, one daemon**, over a unix socket at
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock`. Local only,
  no port, no auth; a second start in
  the same directory is rejected.

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

- `-l` reads `/proc`: Linux only, current user's daemons only; the dashboard needs a TTY.
  `--parallel` agents share the working directory, so temp files with fixed names collide.
- The patterns are best-effort (add new wording to `USAGE_LIMIT_PATTERNS` in `config.py`); the consecutive-failure circuit breaker is the real safety net.

## More details

[docs/guide.en.md](docs/guide.en.md): install details, what each loop round does, usage limits, daemon and socket details, other limits, tests and releases.
