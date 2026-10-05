# Sourced from ~/.bashrc.d/auto-review-pr-dashboard (a symlink to this file).
#
# Export the selected ai_cli function as BASH_FUNC_<name>%% for child processes.
# Agent shells use `bash -ic`, so ~/.bashrc may define helpers or replace it.
# PATH binaries need no export.
#
# The CLI itself is installed separately by `make install` (uv tool install).

auto-review-pr-dashboard() {
  # `type -P` skips this function and finds the installed executable on PATH.
  local cli
  cli="$(type -P auto-review-pr-dashboard)"
  if [ -z "$cli" ]; then
    echo "auto-review-pr-dashboard: CLI not on PATH; run 'make install' in the repo" >&2
    return 1
  fi

  if [ $# -eq 0 ]; then
    set -- --help
  fi

  # `--` so a leading flag such as -l is treated as a name, not a declare option.
  if declare -F -- "$1" >/dev/null 2>&1; then
    export -f -- "$1"
  fi

  # No cd: artifacts land in the caller's directory, usually the multi-repo parent.
  "$cli" "$@"
}
