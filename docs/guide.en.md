[繁體中文](guide.md) | **English**

# auto-review-pr-dashboard guide

[Back to the README](../README.en.md)

## Install details

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

## Daemon and socket details

- Slow clients are handled there too: ticks carry no snapshot, and a client buffering over 1 MiB is dropped.
- (mode `0700`; a `.lock` queues startup, unclaimed `.lock`/`.sock` pairs are cleared on start/recycle/stop); the handshake checks protocol version, run id, cwd;
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
agent's last output in that PR's `error` field.

## Other limits

- A daemon that dies uncleanly leaves its agents running. The next start kills those
  process groups from `agent_groups` in `daemon.json` (skipping pids that were reused);
  descendants that called `setsid` themselves are out of reach.
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
