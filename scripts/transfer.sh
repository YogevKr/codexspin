#!/bin/bash
# Fixed-argument wrapper for /codexspin:transfer. User-supplied slash-command
# text must never be interpolated into shell source; manual source selection is
# available through `codexspin transfer --source ...` in a terminal.

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=find-codexspin.sh
. "$PLUGIN_ROOT/scripts/find-codexspin.sh"

# The same minimal GUI PATH that hides codexspin commonly hides Codex and its
# runtime too. Restore all supported install prefixes before launching either.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
BIN="$(find_codexspin)" || {
  echo "codexspin: CLI not found; install 0.2.0+ with: uv tool install git+https://github.com/YogevKr/codexspin" >&2
  exit 127
}
if ! "$BIN" transfer --help >/dev/null 2>&1; then
  echo "codexspin: /codexspin:transfer requires codexspin CLI 0.2.0+; upgrade codexspin and retry" >&2
  exit 2
fi
exec "$BIN" transfer
