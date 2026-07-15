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
out="$("$BIN" status 2>/dev/null)" || exit 0
case "$out" in
  ""|"no jobs"*) exit 0 ;;
esac

# Cap the context cost: a large fleet must not flood the session.
MAX_LINES=40
total=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
echo "codexspin jobs (running + finished in last 24h) — result/await/send by job id:"
if [ "$total" -gt "$MAX_LINES" ]; then
  printf '%s\n' "$out" | head -n "$MAX_LINES"
  echo "… truncated ($total lines total) — run: codexspin status"
else
  printf '%s\n' "$out"
fi
