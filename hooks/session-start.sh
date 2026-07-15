#!/bin/bash
# SessionStart hook: surface codexspin background jobs so detached fleets are
# never forgotten across sessions. Silent when there is nothing to report.
command -v codexspin >/dev/null 2>&1 || exit 0
out="$(codexspin status 2>/dev/null)" || exit 0
case "$out" in
  ""|"no jobs"*) exit 0 ;;
esac
echo "codexspin jobs (running + finished in last 24h) — result/await/send by job id:"
echo "$out"
