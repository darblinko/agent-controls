# shellcheck shell=bash
# claude-profiles.sh: run Claude Code under separate configuration profiles.
# Source it from ~/.bashrc or ~/.zshrc:
#
#   . /path/to/agent-controls/shell/claude-profiles.sh
#
# claude_profile <name> [claude arguments...]
#   Runs `claude` with CLAUDE_CONFIG_DIR=$HOME/.claude-<name>, so each profile
#   has its own settings, permissions, hooks, plugins, memory and transcripts.
#   When the file $CLAUDE_TOKEN_DIR/<name> exists and is non-empty, its
#   contents are passed as CLAUDE_CODE_OAUTH_TOKEN, so each profile can sign in
#   to a different account. CLAUDE_TOKEN_DIR defaults to
#   ${XDG_CONFIG_HOME:-$HOME/.config}/claude-tokens. Keep that directory mode
#   700 and each token file mode 600.
#
# Profile names are limited to letters, digits, dot, dash and underscore and
# must start with a letter or digit, so a name cannot point outside $HOME or
# the token directory.

claude_profile() {
  local profile=${1:-}
  if [[ ! $profile =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "usage: claude_profile <name> [claude arguments...]" >&2
    echo "name: letters, digits, dot, dash or underscore, starting with a letter or digit" >&2
    return 2
  fi
  shift
  local token_dir=${CLAUDE_TOKEN_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/claude-tokens}
  local token_file="$token_dir/$profile"
  if [[ -s $token_file ]]; then
    CLAUDE_CONFIG_DIR="$HOME/.claude-$profile" CLAUDE_CODE_OAUTH_TOKEN="$(<"$token_file")" claude "$@"
  else
    CLAUDE_CONFIG_DIR="$HOME/.claude-$profile" claude "$@"
  fi
}
