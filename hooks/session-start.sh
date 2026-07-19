#!/bin/bash
# SessionStart hook: surface codexspin background jobs so detached fleets are
# never forgotten across sessions. Silent when there is nothing to report.

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=../scripts/find-codexspin.sh
. "$PLUGIN_ROOT/scripts/find-codexspin.sh"

input="$(cat)"
# Preserve the raw hook metadata without depending on an installed/upgraded
# codexspin CLI. The Python CLI validates and reads transcript_path later.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  metadata_dir="${CLAUDE_PLUGIN_DATA:-$(dirname "$CLAUDE_ENV_FILE")}"
  if mkdir -p "$metadata_dir" 2>/dev/null; then
    # CLAUDE_PLUGIN_DATA persists across sessions. Bound our metadata footprint
    # while leaving fresh files available to still-running Claude sessions.
    find "$metadata_dir" -type f -name 'session-start.*' -mtime +0 \
      -exec rm -f {} + 2>/dev/null || :
    metadata_file="$(mktemp "$metadata_dir/session-start.XXXXXX")" || metadata_file=""
    if [ -n "$metadata_file" ]; then
      printf '%s' "$input" > "$metadata_file"
      printf 'export CODEXSPIN_SESSION_METADATA=%q\n' "$metadata_file" >> "$CLAUDE_ENV_FILE"
    fi
  fi
fi

BIN="$(find_codexspin)" || exit 0

# Scope status to the session being started: jobs spawned by other live Claude
# sessions collapse into a one-line summary instead of flooding every session.
# The hook's stdin JSON (read once above) is authoritative — the inherited env
# may carry a stale or absent id (e.g. GUI-launched sessions).
session_id="$(printf '%s' "$input" \
  | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
if [ -n "$session_id" ]; then
  export CLAUDE_CODE_SESSION_ID="$session_id"
fi

out="$("$BIN" status --attention 2>/dev/null)" || exit 0
case "$out" in
  ""|"no jobs"*) exit 0 ;;
esac

# Cap the context cost: a large fleet must not flood the session.
MAX_LINES=40
total=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
echo "codexspin attention (this session + unowned) — result/await/send by job id:"
if [ "$total" -gt "$MAX_LINES" ]; then
  printf '%s\n' "$out" | head -n "$MAX_LINES"
  echo "… truncated ($total lines total) — run: codexspin status --attention"
  # The other-sessions summary is the last line; don't let truncation eat it.
  last="$(printf '%s\n' "$out" | tail -n 1)"
  case "$last" in
    "+ "*) printf '%s\n' "$last" ;;
  esac
else
  printf '%s\n' "$out"
fi
