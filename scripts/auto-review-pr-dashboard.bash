# Sourced from ~/.bashrc.d/auto-review-pr-dashboard (a symlink to this file).
#
# The shell function exists for one reason: to export a bash-function ai_cli
# before the real CLI starts. Most ai_cli names here (codex-personal sets
# CODEX_HOME, claude-personal, ...) are bash functions from this same
# ~/.bashrc.d, not binaries. Exporting one puts it in the environment as
# BASH_FUNC_<name>%%, so the `bash -c` the python side spawns can still resolve
# it. Real binaries need nothing.
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
