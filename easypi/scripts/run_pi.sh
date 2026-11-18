#!/usr/bin/env bash
set -euo pipefail

# Non-interactive wrapper for invoking `pi` from Antigravity.
# Usage:
#   run_pi.sh <session-id> <prompt-file> [extra pi args...]
# The prompt file keeps design text out of the shell command line, so the
# host hook never re-parses backticks or $(...) that belong to the design.

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <session-id> <prompt-file> [extra pi args...]" >&2
  exit 2
fi

SESSION_ID="$1"
PROMPT_FILE="$2"
shift 2

if [ ! -f "$PROMPT_FILE" ]; then
  echo "Error: prompt 文件不存在或不是普通文件: $PROMPT_FILE" >&2
  exit 2
fi

exec pi -p --session-id "$SESSION_ID" "$@" -- "$(cat "$PROMPT_FILE")" </dev/null
