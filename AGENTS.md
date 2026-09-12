# AGENTS.md

Repo-specific rules. Global agent rules still apply; this file adds the constraints
that are specific to this repository.

## README.md writing constraints

### Dos

- `README.md` MUST stay at 200 lines or fewer. Verify with `wc -l README.md`.
- `README.md` MUST read as a TL;DR: what it is, install, usage, keys, why the
  architecture looks like that, then behaviour and caveats.
- Each fact MUST appear exactly once. When two sections explain the same mechanism,
  keep the one closest to where a reader needs it and delete the other.
- These MUST stay complete while trimming, because nothing else documents them for a
  user: every CLI flag, every dashboard key, the artifact paths, and the file that
  holds a tunable constant (for example `MAX_KEPT_LOOP_DIRS` in `store.py`).
- Design rationale MUST live in `## Architecture: TUI → IPC → Service`, and MUST name
  the real mechanism behind each claim (`start_new_session`, the `on_event` callback,
  snapshot push, the socket path), not generic architecture talk.
- Prose lines MUST wrap at 90 columns or fewer.
- Diagrams MUST be ASCII inside a fenced block.
- An in-page link MUST match the anchor GitHub generates for its heading.

### Don'ts

- `README.md` MUST NOT restate `--help` beyond the one usage block.
- `README.md` MUST NOT carry release notes, a changelog, or migration history.
- `README.md` MUST NOT describe a generated file as if it were committed, and MUST NOT
  tell a reader to use a file that a fresh clone does not contain.

### Optional

- Table rows, and any line inside a fenced block (shell examples, diagrams, file
  trees), MAY exceed 90 columns when wrapping would break them.

## Dependency single source of truth

- `pyproject.toml` is the source of truth for dependency ranges; `uv.lock` is the
  source of truth for pins.
- `requirements.txt` is a generated artifact (`make requirements`, from `uv.lock`) and
  MUST NOT be committed or hand-edited.
