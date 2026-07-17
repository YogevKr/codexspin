#!/bin/bash
# SessionStart hook: surface codexspin background jobs so detached fleets are
# never forgotten across sessions. Silent when there is nothing to report.

# GUI-launched sessions (desktop app) get a minimal PATH without the usual
# CLI install locations — fall back to them explicitly.
find_codexspin() {
  command -v codexspin 2>/dev/null && return 0
  for candidate in "$HOME/.local/bin/codexspin" /opt/homebrew/bin/codexspin /usr/local/bin/codexspin; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

BIN="$(find_codexspin)" || exit 0

# Scope status to the session being started: jobs spawned by other live Claude
# sessions collapse into a one-line summary instead of flooding every session.
# The hook's stdin JSON is authoritative — the inherited env may carry a stale
# or absent id (e.g. GUI-launched sessions).
input="$(cat 2>/dev/null || true)"
session_id="$(printf '%s' "$input" \
  | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
if [ -n "$session_id" ]; then
  export CLAUDE_CODE_SESSION_ID="$session_id"
fi

out="$("$BIN" status 2>/dev/null)" || exit 0
case "$out" in
  ""|"no jobs"*) exit 0 ;;
esac

# Cap the context cost: a large fleet must not flood the session.
MAX_LINES=40
total=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
echo "codexspin jobs (this session + unowned, last 24h) — result/await/send by job id:"
if [ "$total" -gt "$MAX_LINES" ]; then
  printf '%s\n' "$out" | head -n "$MAX_LINES"
  echo "… truncated ($total lines total) — run: codexspin status"
  # The other-sessions summary is the last line; don't let truncation eat it.
  last="$(printf '%s\n' "$out" | tail -n 1)"
  case "$last" in
    "+ "*) printf '%s\n' "$last" ;;
  esac
else
  printf '%s\n' "$out"
fi
