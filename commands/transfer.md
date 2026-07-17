---
description: Transfer the current Claude Code session into a resumable Codex thread
disable-model-invocation: true
allowed-tools: Bash(bash:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/transfer.sh"`

Present the command output to the user exactly as returned. Preserve the Codex session ID and the `codex resume <session-id>` command.
