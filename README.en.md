[繁體中文](README.md) | **English**

# <img src="docs/icon.png" width="40" alt="" /> auto-review-pr-dashboard

![Mascot: a mint-teal mechanical robot agent with a magnifying glass](docs/mascot.png)

You ask an AI to review PRs, but the AI doesn't remember how far it got: close the
window and the progress is gone. This tool keeps a caretaker (the daemon) in the
background that remembers everything; your window is just its screen — close it, open
another, pick right back up. *How* to review is `prompt.md`'s call; *which* PRs, flags.

## Install

```bash
make install
```

`make install` is just `uv tool install .`: it puts `auto-review-pr-dashboard` on `PATH`
and leaves a symlink at `~/.bashrc.d/auto-review-pr-dashboard` defining a shell function
of the same name, which exports your chosen AI function through `BASH_FUNC_<name>%%` —
open a new shell after installing. Each PR's AI runs under `bash -ic` (asserted by
`tests/test_config_parity.py`), which loads `~/.bashrc` before calling `ai_cli`, so even
when the wrapper is bypassed the function in `~/.bashrc` still works — unless it defines
the same name and shadows the exported one. Whatever your shell prints at startup shows
up in the PR log. No uv? `pip install .` then `python3 -m auto_review_pr_dashboard`.

| Target | Action |
|---|---|
| `make` | `uv sync` — dependencies only, into the local `.venv` |
| `make install` | CLI plus shell wrapper |
| `make uninstall` | removes both, including the daemon socket directory |
| `make test` | run the tests in the uv environment |
| `make clean` | remove `.venv`, `__pycache__`, and the generated `requirements.txt` |
| `make requirements` | optional: generate the pinned `requirements.txt` from `uv.lock` |

`make uninstall` refuses while the daemon is alive (press `q` first, or `FORCE=1`), keeps
`.tmp/auto-review-pr-dashboard/`, and only removes the symlink if it still points here.

[Releases](https://github.com/scott1028/auto-review-pr-dashboard/releases) ship prebuilt
binaries — no Python, no uv: Linux gets an AppImage (`x86_64`/`aarch64`; without FUSE
prefix `APPIMAGE_EXTRACT_AND_RUN=1`), macOS a tar.gz (`aarch64`/`x86_64`, unpack and run
`bin/auto-review-pr-dashboard`).

## Usage

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
  --pr-timeout MIN    kill an AI process running longer than this many hours (default: 30)
  --cooldown MIN      minutes to wait after a suspected usage limit (default: 30)
  --parallel N        review at most N PRs at a time (default: 1)
  --dry-run           explore only, print the verdict table, review nothing
  -l, --list          list working dirs with a live daemon to resume
  -- ARGS...          pass ARGS to <ai_cli>: after built-in flags, before the prompt

auto-review-pr-dashboard codex --repo-url owner/repo --author <login> --dry-run
auto-review-pr-dashboard claude --pr-url https://github.com/owner/repo/pull/<n>
auto-review-pr-dashboard codex --pr-url https://github.com/owner/repo/pull/<n> -- --yolo
```

In one line: the prompt decides *how* to review, the flags decide *which* PRs. Scope is
never guessed from prompt text, and conditions the search can't express ("PRs with label
X") are useless in the prompt. `--repo-url` takes `owner/repo` or any git url; a PR url
is rejected with a hint to use `--pr-url`; inline env vars reach `gh` and the agent. A
normal start spawns a detached daemon for *your current working directory* and attaches;
`--resume` runs alone from the same directory, replacing the previous dashboard; a dead
daemon is only reported, never auto-restarted; `--dry-run` stays in the foreground.

`prompt.md` is read at startup; if missing you get one containing exactly `code review`.
The whole file is the prompt (the positional prompt argument is ignored, empty is
rejected). Skill invocations are never added automatically — add them yourself, e.g.:

```text
code review
/baseline-fe-code-review
```

An auto-generated single-PR scope is appended; the daemon reads the file once, so
editing it or `--resume` doesn't affect this round.

## Keys

| Key | Where | Action |
|---|---|---|
| `↑` / `↓`, mouse click | list | select a PR (a single click opens it) |
| `enter` | list | open the PR detail page |
| `c` | list / detail | cancel this PR — the running agent's whole process group is killed |
| `r` | list / detail | re-queue a cancelled PR at the end of this round's queue |
| `n` | anywhere | skip any countdown — idle wait or cooldown |
| `p` | anywhere | pause (let running PRs finish, then hold; countdowns freeze) |
| `l` | anywhere | show / hide the live agent log panel |
| `d` | anywhere | detach — detach and close the dashboard, the daemon keeps running |
| `f` / `esc` | detail | toggle log follow / back to the list |
| `q` | anywhere | stop the daemon and quit (each agent gets SIGTERM, then SIGKILL after `TERMINATE_GRACE_SEC` — 5s, in `runner.py`); asks first if PRs are running, defaults to No |

## Architecture: TUI → IPC → Service

```text
  terminal (disposable)          detached daemon (owns the work)
┌──────────────────────┐  JSON   ┌──────────────────────────────────┐
│ app.py — Textual TUI │  lines  │ session.py — SessionServer       │
│  render snapshot     │◀────────│   snapshot / log / tick / notice │
│  keypress → command  │────────▶│   cancel resume run_now pause    │
└──────────────────────┘  unix   │ runner.py — LoopRunner           │
   close it, lose nothing        │ store.py — .tmp/ artifacts       │
                                  └────────────────────────────────┴─▶ one process group per running PR: <ai_cli>
```

- **Reviews outlive your terminal.** A PR can take 30 minutes and a loop round hours; an
  agent parented to the terminal dies with it; `start_new_session` prevents that.
- **Killing mid-review isn't free.** An agent killed partway may have posted inline
  comments but not the closing marker, so the next round re-reviews that SHA — the
  detail page tells you. The queue holder owns the process's life.
- **The service shouldn't know the UI.** `LoopRunner` only pushes events to an `on_event`
  callback, so the same class runs under the daemon or in-process against a mock CLI.
- **The socket is the contract, so the TUI stays stateless.** Every attach receives a
  full snapshot, the daemon pushes one on every state change, and a keypress is a
  `command` message; a client whose buffer exceeds 1 MiB is dropped.
- **One working directory, one daemon, over a unix socket**:
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock` (mode `0700`; a `.lock` queues
  startup, unclaimed pairs are cleared on start/recycle/stop) — local only, no port, no
  auth; the handshake checks protocol version, run id, cwd; same scope twice is rejected.
- **Long runs recycle themselves.** `run_forever` returns at recycle time; the successor
  takes `.lock`, clears `.tmp/`, gets loop state via stdin; the TUI waits for `run_id`.

## What each loop round does

1. **Discover** — one `gh pr list` call per scope axis (one per `--author`; per reviewer
   one each for `review-requested:` and `reviewed-by:`), unioned then deduplicated —
   GitHub ANDs one `--search`, hence the separate calls. With both scopes the union
   keeps PRs matching either.
2. **Decide** — Draft or `[WIP]` / `WIP:` in the title means `Skip (WIP)`; otherwise
   `gh pr view --json headRefOid,comments` and the newest
   `<!-- review-pr-branches: head=<sha> -->` marker decide `Full`/`Incremental`/`Skip`;
   `Skip` PRs stay visible but never get an agent.
3. **Review** — one agent per remaining PR, at most `--parallel` at a time.
4. **Diff** — comment ids are snapshotted before and after each run; only comments with
   this skill's marker count, so teammates posting mid-run aren't recorded.

## When an agent hits a usage limit

Detection needs one **failed** run, so normal reviews never trip it: the tail of a failed
output matches a quota pattern (`usage limit`, `429`, `quota`, …), or two consecutive
failures regardless of wording — which also catches expired logins, a broken CLI, or a
dead network. That PR returns to the **front** of the queue as `queued` and the loop
stops behind a banner for `--cooldown` minutes; press `n` to skip. Running `--parallel`
agents finish and their failures join the block. A run that already posted comments, or
whose snapshot failed unnoticed, is marked `failed` instead, since rerunning may
duplicate; after 3 cooldowns the round waits for `--interval`. Each failure records the
agent's last output in that PR's `error` field.

## Artifacts

Everything is written under **the working directory you ran the command in** (usually
that multi-repo parent):

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # current daemon metadata and socket location
├── daemon.log                  # daemon diagnostics
├── state.json                  # run config + current loop number
├── loops/loop-0003.json        # per-round record: verdicts, states, post diffs, times
└── logs/loop-0003/owner__repo__7.log
```

Every `RECYCLE_EVERY_LOOPS` rounds (10, in `session.py`) the whole `.tmp/` is deleted —
files other tools left there too — so `loops/` and `daemon.log` don't grow forever; the
status bar shows these cleanups as `cleaned N×`. In between, `logs/` keeps **the 10 most
recent loop directories** (10 rounds, not 10 hours); `MAX_KEPT_LOOP_DIRS` in `store.py`.

## Caveats

- A daemon that dies uncleanly leaves agents running; the next start kills the process
  groups in `agent_groups` (`daemon.json`, skipping recycled pids); `setsid` ones escape.
- `-l` reads `/proc`: Linux only, current user only; needs a TTY. `--parallel` agents
  share the working directory, so temp files with fixed names collide.
- The log panel is memory-bounded, not a copy of the `.log`: lines over 2000 chars show
  `(+N chars)` keeping the last 2000; only running PRs buffer; >64KiB runs get wrapped.
- The usage-limit patterns are best-effort; add new wording to `USAGE_LIMIT_PATTERNS` in
  `config.py` — the consecutive-failure circuit breaker is the real safety net.

## Development and releases

```bash
make test                               # 145 tests, no network
python3 -m unittest discover -s tests   # same, without uv
```

To ship a release: bump `[project].version` in `pyproject.toml` and push to `master`;
the workflow publishes only when higher: tests → Linux AppImages (x86_64/aarch64) +
macOS bundles (aarch64/x86_64) → GitHub Release. Manual: Actions → Release → Run
workflow; tag `v<version>-<first 8 chars of commit>`; when bumping, `uv lock` + commit it.
