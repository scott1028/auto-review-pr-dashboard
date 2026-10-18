[繁體中文](guide.md) | **English**

# auto-review-pr-dashboard guide

This guide covers the details the [README](../README.en.md) leaves out. For a first run,
the README is enough; come back here when something goes wrong or you want to know why the
tool behaves a certain way.

## Glossary

| Term | In plain words |
|---|---|
| daemon | the caretaker running in the background; it owns the queue and the progress |
| dashboard / TUI | the screen in your terminal; it's only the daemon's screen, so closing it stops nothing |
| `ai_cli` | the AI CLI you use. `codex`, `claude`, `pi`, and `opencode` are supported, and so are names with a `-xxx` suffix such as `codex-personal` |
| agent | the AI process that actually reviews one PR |
| `prompt.md` | the review instructions for the agent; the whole file is the prompt |
| round (loop) | one pass: find PRs, then review everything that needs it |
| marker | a hidden comment the agent leaves on the PR when it finishes, like a bookmark recording which commit was reviewed |
| `Full` / `Incremental` / `Skip` | this round's call on a PR: never reviewed / reviewed, but the head has moved since / current head already reviewed |
| cooldown | the time the whole round waits after hitting a usage limit |
| shift change (recycle) | the daemon periodically hands its work to a fresh daemon |
| process group | a process plus the child processes it started; they get killed together |

## What install does

`make install` first runs `uv tool install .`, which puts `auto-review-pr-dashboard` on
`PATH`. Then it leaves a symlink at `~/.bashrc.d/auto-review-pr-dashboard` that defines a
shell function of the same name; a new shell is what loads it.

Why the function? Your `ai_cli` may be a bash function rather than a binary. This shell
function, the wrapper, exports the function you picked through `BASH_FUNC_<name>%%`, so
the processes the daemon starts can call it.

Each PR's AI runs under `bash -ic` (`tests/test_config_parity.py` checks this). `bash -ic`
loads `~/.bashrc` before calling `ai_cli`, so a function defined in `~/.bashrc` still
works even when the wrapper is bypassed. Two things to keep in mind:

- If `~/.bashrc` also defines a function with the same name, it shadows the one the
  wrapper exported.
- Whatever your shell prints at startup also shows up in the PR log.

Every make target:

| Target | What it does |
|---|---|
| `make` | runs `uv sync`: dependencies only, into the local `.venv` |
| `make install` | installs the CLI and the shell wrapper |
| `make uninstall` | removes both, including the daemon socket directory. It refuses while the daemon is running, so press `q` in the dashboard first, or force it with `FORCE=1`. It leaves `.tmp/auto-review-pr-dashboard/` alone, and removes the symlink only if it still points at this repo |
| `make test` | runs the tests in the uv environment |
| `make clean` | removes `.venv`, `__pycache__`, and the generated `requirements.txt` |
| `make requirements` | optional: generates a pinned `requirements.txt` from `uv.lock` |

## What each loop round does

Every round follows the same four steps:

1. **Find PRs.** The tool calls `gh pr list` once per search condition: once per
   `--author`, and for each reviewer once with `review-requested:` and once with
   `reviewed-by:`. The results are merged and deduplicated. The calls are separate because
   GitHub ANDs every condition inside one `--search`. If you give both kinds of scope, a
   PR matching either side is kept.
2. **Decide whether to review.** A Draft, or a PR with `[WIP]` / `WIP:` in its title,
   becomes `Skip (WIP)` right away. For the rest, the tool runs
   `gh pr view --json headRefOid,comments` and finds the newest
   `<!-- review-pr-branches: head=<sha> -->` marker: no marker means `Full`, a marker SHA
   that differs from the current head means `Incremental`, and a matching one means
   `Skip`. `Skip` PRs still show up; they just never get an agent.
3. **Review.** Each remaining PR gets its own agent, at most `--parallel` at a time,
   following `prompt.md`.
4. **Record what was posted.** Before and after each run, the tool snapshots the ids of
   every comment on the PR; comparing the two shows what's new. This trusts GitHub, not
   the agent's output. Only comments carrying the `review-pr-branches` marker count, so
   comments a teammate posts mid-run aren't recorded.

## When an agent hits a usage limit

Once an agent hits a usage limit, every PR after it fails fast. Without a pause, the rest
of the queue would burn through in seconds, so the tool detects this and makes the whole
round stop and wait.

Detection only looks at **failed** runs, so a normal review never trips it. Either of
these counts:

- The tail of the failed output matches a quota pattern, such as `usage limit`, `429`, or
  `quota`.
- Two failures in a row, whatever the error says. This also catches an expired login, a
  broken CLI, or a dead network.

After that, the PR goes back to the **front** of the queue as `queued`, and the round
waits behind a banner for `--cooldown` minutes. Agents already running under `--parallel`
finish first, and if they fail too, those failures count toward the same cooldown. A few
special cases:

- A run that already posted comments, or whose comment snapshot failed so nobody knows
  what it posted, is marked `failed` instead of requeued, since a rerun might post
  duplicates.
- After 3 cooldowns in one round, the round gives up and waits for the next one,
  `--interval` later.
- Every failure records the agent's last output in that PR's `error` field, so you can
  look at it afterwards.

## Shift changes and the socket

The socket's directory is mode `0700`. A `.lock` next to it makes simultaneous startups
wait their turn, and every start, shift change (recycle), and stop clears unclaimed
`.lock`/`.sock` pairs.

When the TUI attaches, it first does a handshake, a quick check that both sides belong
together: the daemon compares the protocol version, run id, and cwd, and accepts only if
all three match. Slow clients are the daemon's job too: the once-a-second tick carries no
snapshot, and the daemon drops any client whose buffer passes 1 MiB.

At a shift change, `run_forever` returns. The old daemon takes `.lock`, clears `.tmp/`,
and hands the loop state to its successor via stdin. The TUI waits for a new `run_id` in
`daemon.json`, then reattaches on its own.

## Limits and edge cases

- If the daemon dies without cleaning up, its agents keep running. On the next start, the
  tool kills those process groups using `agent_groups` in `daemon.json`, skipping any pid
  that another process has since reused. A descendant that called `setsid` itself is out
  of reach.
- The log panel is an in-memory view with limits; the full text is in the `.log` file. A
  line over 2000 characters shows only its first 2000, followed by `(+N chars)`.
- The panel keeps the last 2000 lines, and only running PRs keep buffering. In the `.log`
  file, output that runs past 64KiB without a newline gets hard-wrapped into several
  lines.

## Tests and releases

```bash
make test                               # 145 tests, no network
python3 -m unittest discover -s tests   # same, without uv
```

To ship a release, bump `[project].version` in `pyproject.toml` and push to `master`. The
Release workflow compares it against every published version and publishes only if it's
higher: tests → Linux AppImages (x86_64/aarch64) + macOS bundles (aarch64/x86_64) → GitHub
Release.

To publish by hand, go to Actions → Release → Run workflow; that skips the version check.
The tag looks like `v<version>-<first 8 chars of commit>`. When you bump the version, run
`uv lock` and commit `uv.lock` too.
