[繁體中文](README.md) | **English**

# <img src="docs/icon.png" width="40" alt="" /> auto-review-pr-dashboard

![Mascot: a mint-teal mechanical robot agent with a magnifying glass](docs/mascot.png)

This tool has an AI review your GitHub PRs one by one, with a separate AI process for each
PR.

An AI can't keep track of its own progress: close the window and it forgets how far it
got. So this tool keeps a caretaker running in the background (the daemon, a process that
stays up on its own), and the caretaker remembers which PRs are waiting, which are done,
and which are half-reviewed. The dashboard you look at is just the caretaker's screen.
Close it whenever you like, then open another one to pick up where you left off.

## Install

Before you start, you need:

- `gh`, logged in. The tool uses it to find PRs and read comments.
- Python 3.10 or newer, plus uv (no uv works too, see below)
- An AI CLI, such as `codex` or `claude`

```bash
make install
```

Open a new shell afterwards and you're ready. What `make install` actually does, and the
other make targets, are in the guide under
[What install does](docs/guide.en.md#what-install-does).

No uv? Run `pip install .`, then start the tool with
`python3 -m auto_review_pr_dashboard`. Don't want Python at all?
[Releases](https://github.com/scott1028/auto-review-pr-dashboard/releases) has prebuilt
binaries for both `x86_64` and `aarch64`:

- Linux: an AppImage. If your system has no FUSE (the AppImage uses it to mount itself),
  put `APPIMAGE_EXTRACT_AND_RUN=1` in front of the command.
- macOS: a tar.gz. Unpack it and run `bin/auto-review-pr-dashboard`.

## Start reviewing PRs

There's one rule to remember: the prompt decides *how* to review, the flags decide *which*
PRs.

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

Scope comes only from the flags; the tool never guesses it from the prompt text. So a
condition the search can't express, like "PRs with label X", does nothing in the prompt.
If you're not sure the scope is right, run once with `--dry-run` first.

`--repo-url` takes `owner/repo` or any git url. Give it a PR url and it refuses, telling
you to use `--pr-url` instead. Inline env vars in front of the command reach both `gh` and
the agent (the AI process that actually reviews a PR).

When you start it, the tool spawns a daemon for your current working directory and
attaches to it. To come back later, run `--resume` on its own from the same directory; the
dashboard that was open quits and the new one takes over. If the daemon dies, the tool
tells you but never restarts it. `--dry-run` always runs in the foreground.

### Writing prompt.md

`prompt.md` lives in the directory you run the command from, and it holds the review
instructions for the agent. If it doesn't exist on the first run, the tool creates one
containing just `code review`.

The whole file is the prompt: the `<prompt>` argument on the command line is ignored, and
an empty file is rejected. The tool never adds skill invocations for you, so if you want a
skill, write it in yourself, for example:

```text
code review
/baseline-fe-code-review
```

Before sending each PR, the tool appends an auto-generated scope note that limits the
agent to that one PR. The daemon reads the prompt once at startup and keeps it, so editing
the file or running `--resume` never rereads it. To switch prompts, stop with `q` and
start again.

## Dashboard keys

The dashboard has two screens: the list shows every PR, and the detail page shows one PR's
status and log.

| Key | Where | Action |
|---|---|---|
| `↑` / `↓`, mouse click | list | select a PR; with the mouse, a single click opens it |
| `enter` | list | open the PR's detail page |
| `c` | list / detail | cancel this PR, killing the running agent along with its child processes (its whole process group) |
| `r` | list / detail | put a cancelled PR back into this round, at the end of the queue |
| `n` | anywhere | stop waiting and skip the countdown, whether it's the wait between rounds or a cooldown (the pause after a usage limit) |
| `p` | anywhere | pause: PRs already running finish, but no new ones start; countdowns freeze |
| `l` | anywhere | show or hide the live agent log panel |
| `d` | anywhere | detach: close the dashboard and leave the daemon running |
| `f` / `esc` | detail | toggle log follow (on: new log lines keep arriving; off: the log holds still) / back to the list |
| `q` | anywhere | stop the daemon and quit. Each agent gets SIGTERM, then SIGKILL after `TERMINATE_GRACE_SEC` (5s, in `runner.py`). If PRs are still running it asks first, defaulting to No |

## Architecture: TUI → IPC → Service

The program is split into three parts, and this section explains why. The TUI is the
screen you see in the terminal, IPC is the channel two processes use to talk (a unix
socket here), and the Service is the daemon that actually runs the reviews.

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

- **Reviews have to outlive your terminal.** A PR can take 30 minutes to review, and a
  loop round can run for hours. If the agent were a child of the terminal, it would die
  the moment the window closed or SSH dropped. So the daemon starts with
  `start_new_session`, and the work keeps going when you press `d` or just close the
  window.
- **Killing a review halfway has a cost.** A killed agent may already have posted some
  inline comments but not the closing marker. The marker is a hidden comment that works
  like a bookmark, recording which commit was reviewed; without it, the next round reviews
  the same SHA again, and the detail page warns you about it. That's why the side holding
  the queue decides when a process gets killed.
- **The service shouldn't know what the UI looks like.** `LoopRunner` only hands events to
  an `on_event` callback, so the same class runs inside the daemon or in-process against a
  fake CLI.
- **The two sides only talk over the socket, so the TUI keeps no state.** On every attach
  the daemon first sends a full snapshot (the whole current state), then pushes a new one
  whenever something changes. A keypress becomes a `command` message sent over; it never
  calls the runner directly.
- **One working directory, one daemon.** Starting a second one in the same directory is
  refused. The two sides talk over the unix socket at
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock`: local only, no port, no auth.

## Where files go

Everything is written under **the working directory you ran the command in**, usually the
parent directory that holds all your repos:

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # current daemon metadata and socket location
├── daemon.log                  # daemon diagnostics
├── state.json                  # run config + current loop number
├── loops/loop-0003.json        # per-round record: verdicts, states, post diffs, times
└── logs/loop-0003/owner__repo__7.log
```

Every `RECYCLE_EVERY_LOOPS` rounds (10, set in `session.py`) the daemon changes shifts,
handing its work to a fresh daemon. A shift change wipes the whole `.tmp/`, including
files other tools left there, which keeps `loops/` and `daemon.log` from growing forever.
The status bar counts these wipes as `cleaned N×`.

Between shift changes, `logs/` keeps only **the 10 most recent loop directories** (that's
10 rounds, not 10 hours). To keep more, change `MAX_KEPT_LOOP_DIRS` in `store.py`.

## Things to know first

- With `--parallel`, the agents share one working directory, so temp files with fixed
  names collide.
- `-l` finds daemons by reading `/proc`, so it only works on Linux and only sees your own
  user's daemons. The dashboard itself needs a TTY.
- Usage limits are detected by matching the agent's error output, and the pattern list
  can't cover everything. When you see new wording, add it to `USAGE_LIMIT_PATTERNS` in
  `config.py`; the real safety net is the "two failures in a row" rule.

## Learn more

The details are in the [guide](docs/guide.en.md):

- [Glossary](docs/guide.en.md#glossary): what daemon, agent, and marker mean here
- [What install does](docs/guide.en.md#what-install-does): why there's a shell function,
  plus every make target
- [What each loop round does](docs/guide.en.md#what-each-loop-round-does): how PRs are
  found and how the tool decides whether to review one
- [When an agent hits a usage limit](docs/guide.en.md#when-an-agent-hits-a-usage-limit):
  detection, how long it waits, when it gives up
- [Shift changes and the socket](docs/guide.en.md#shift-changes-and-the-socket): startup
  queueing, the handshake, the handoff
- [Limits and edge cases](docs/guide.en.md#limits-and-edge-cases): a daemon that dies, log
  panel limits
- [Tests and releases](docs/guide.en.md#tests-and-releases): test commands and the release
  flow
