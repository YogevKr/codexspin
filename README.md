# codexspin

Spin and manage parallel Codex sessions from the command line (or from Claude
Code), and transfer Claude Code sessions into Codex with their visible history,
via the `codex app-server` JSON-RPC API. Replaces the tmux screen-scrape
(`codex-agent`) and the `nohup` + session-file-watchdog pattern for background
`codex exec` runs.

Why it exists: OpenAI's [codex-plugin-cc](https://github.com/openai/codex-plugin-cc)
has the right job model (real state, status, results, resumable threads) but
caps the sandbox at `workspace-write`. codexspin is the same idea with all
three sandbox modes, including `danger-full-access` (`--yolo`) for jobs that
must bind localhost, run dev servers, or fetch the network.

## Install

```sh
uv tool install git+https://github.com/YogevKr/codexspin
# or, from a clone, for development:
uv tool install --editable .
```

Requires `codex` on PATH, already authenticated (`codex login`). The Claude
Code transfer command requires the codexspin 0.2.0+ CLI.

## Usage

```sh
# spawn detached jobs (default sandbox: workspace-write)
codexspin spawn -n pagerduty "Implement the PagerDuty integration... "
codexspin spawn -n e2e --yolo -w "Run the playwright suite and fix failures"
codexspin spawn -s read-only "Explain the auth flow in this repo"

# parallel fleet: one worktree per job, no tree conflicts
codexspin spawn -w -n fix-a "Fix the retry logic..."
codexspin spawn -w -n fix-b "Fix the pagination..."
codexspin spawn -w -n fix-c --max-minutes 30 "Refactor the dispatcher..."

codexspin status              # running + last 24h (--all for everything;
                              # --all-sessions to include other Claude sessions' jobs)
codexspin status --attention  # urgent/quiet/unseen results + compact working count
codexspin status --watch      # refresh the fleet view every second
codexspin run "..."          # foreground: spawn + wait + print (one job you'll watch)
codexspin await JOB [JOB...]  # block until done, print results
codexspin result JOB [--json]
codexspin send JOB "..." [--wait]   # follow-up on the warm thread (--wait blocks + prints)
codexspin transfer [--source CLAUDE_JSONL]  # Claude Code session -> resumable Codex thread
codexspin handoff JOB HOST ["continue on the remote"]
codexspin cancel JOB [--hard]
codexspin logs JOB
codexspin archive JOB [JOB…] # hide jobs; compact events; preserve result + resume
codexspin doctor              # codex binary / app-server / auth / defaults
codexspin gc [JOB…]           # this project by default; JOB ids or --everywhere to widen; --dry-run
```

## Remote hosts

Add `--host NAME` to `spawn`, `status`, `result`, `await`, `send`, `cancel`,
`logs`, `archive`, `doctor`, or `gc` to run that same command on another machine:

```sh
codexspin spawn --host buildbox -C /srv/project "Fix the failing integration test"
codexspin status --host buildbox
codexspin await --host buildbox JOB
```

This executes the equivalent of `ssh NAME codexspin <command> <arguments>` and
passes stdout, stderr, and the remote exit code through unchanged. `codexspin`
must be installed on the remote machine. Spawn and send prompts are rewritten
to `-` and piped over SSH stdin, so quotes and newlines never become part of the
SSH command line.

Job ids belong to the selected host. Likewise, `-C/--cwd` is interpreted on
the remote machine exactly as supplied; codexspin does not translate local
paths or job ids.

Job ids accept unambiguous prefixes. Every job records the codex thread id, so
`codex resume <thread-id>` drops you into the same session interactively.

- `-w/--worktree` runs the job in a fresh git worktree
  (`<repo>/.worktrees/<job-id>`, branch `codexspin/<job-id>`) so parallel jobs
  never fight over the tree. CodexSpin adds `/.worktrees/` to the repository's
  local `.git/info/exclude`; it does not edit the tracked `.gitignore`. The
  primary checkout owns this directory even when spawning from a linked
  worktree. Repositories with a bare common Git directory have no primary
  checkout, so CodexSpin uses a sibling `.worktrees/` directory instead. The
  worktree's git metadata dir is added to
  the job's sandbox writable roots (per-job app-server `-c` override), so the
  job can `git commit` its own work — tell it to. `gc` removes only clean
  worktrees — committed work survives on the branch, uncommitted work keeps
  the job. `--writable-root DIR` (repeatable) adds further writable dirs.
- `--max-minutes N` interrupts a runaway job (phase `timeout`).
- `status` shows each job's resolved model/effort and the latest ChatGPT
  quota reading (`account/rateLimits/updated` pushed by the app-server).
- Attention is presentation state, separate from the runner-owned execution
  phase: failed/died/timeout jobs are `urgent`, stale live jobs are `quiet`,
  and completed-unseen jobs need `review`. Printing a result via `result`,
  `run`, `await`, or `send --wait` acknowledges that turn in a separate
  `attention.json` sidecar. `status --attention` never drops unseen results at
  the normal 24-hour cutoff. Jobs created before attention tracking are
  treated as already seen on upgrade. `archive` hides a terminal generation
  from normal and attention views and compacts raw events to the 1 MB terminal
  tail while preserving its structured results and native Codex resume ID.
  `status --all`, `result`, and `send` still work, and a resumed generation
  becomes visible.
- Jobs spawned inside a Claude Code session are tagged with that session's id
  (from `CLAUDE_CODE_SESSION_ID`). Inside a session, `status` shows that
  session's jobs plus untagged ones and collapses other sessions' jobs into a
  one-line count; `--all-sessions` lists everything, labeling each foreign job
  with `session <id-prefix>`. Outside a session everything is listed. Scoping
  is visibility only — jobs stay detached, nothing is killed when a session
  ends, and every command that takes an explicit job id works across sessions.

## How it works

- `spawn` double-forks a detached runner (`codexspin.runner`) per job — no
  shared broker, no tmux; a dead runner is always detected (phase `died`).
- The runner owns one `codex app-server` process, starts a persistent thread
  (`approvalPolicy: never`, your chosen sandbox), streams notifications to a
  two-segment `events.jsonl` capped at 10 MB while active, compacts it to a
  1 MB diagnostic tail at completion, and keeps `state.json` fresh (phase,
  activity, thread id). Oversized individual notifications are represented by
  a small truncation marker; result extraction still uses the live event.
- Terminal result (final message, touched files, command count, duration)
  lands in `result.json`; turn history accrues in `results.jsonl`.
- State lives under `~/.codexspin/jobs/<job-id>/` (`CODEXSPIN_HOME` overrides).
- `send` resumes the recorded thread (`thread/resume`) with a fresh runner.

## Remote handoff

`codexspin handoff <job> <host> [prompt]` migrates a job and resumes its Codex
thread on another machine. A running job is cleanly cancelled first. The
command then uses `rsync --relative` to copy the job's cwd tree, its rollout
file under `~/.codex/sessions/`, and `~/.codexspin/jobs/<job-id>/` to the same
absolute paths on the remote before running `codexspin send` there.

The local files are never removed. The local `state.json` remains terminal and
records `handed_off_to: <host>`. The remote machine is assumed to use the same
username and home-directory layout, with `codexspin`, `codex`, and your Codex
authentication already installed. After handoff, follow it with:

```sh
ssh <host> codexspin status <job-id>
```

When `prompt` is omitted, codexspin asks the remote agent to re-read its prior
context and continue to completion. Set `CODEXSPIN_SSH_BIN` or
`CODEXSPIN_RSYNC_BIN` to override the transport executables (defaults: `ssh`
and `rsync`).

Model/effort default to your `~/.codex/config.toml`; override with
`-m/--model` and `-e/--effort`.

Environment variables: `CODEXSPIN_HOME` (state root, default `~/.codexspin`),
`CODEXSPIN_CODEX_BIN` (codex binary override — the test suite points it at a
fake), `CODEXSPIN_SSH_BIN` (ssh transport override, default `ssh`),
`CODEXSPIN_RSYNC_BIN` (rsync transport override, default `rsync`), and
`CODEXSPIN_STARTUP_TIMEOUT` (seconds to wait for app-server responses
during startup, default 180). `CODEXSPIN_EVENTS_MAX_BYTES` controls the active
event-log cap (default 10000000); `CODEXSPIN_EVENTS_TERMINAL_BYTES` controls
the completed-job tail (default 1000000). Session transfer also supports
`CODEXSPIN_TRANSFER_TIMEOUT` (seconds to wait for the native import, default
120).

## Claude Code skill

This repo is also a Claude Code plugin: the `skills/codex` skill (how to
drive codex as a second agent — review discipline, delegation prompting,
sandbox choices, codexspin routing, failure-mode recoveries), a command for
transferring the current Claude session into Codex, and a SessionStart hook
that lists that session's running/recently-finished codexspin jobs (plus a
one-line count of other sessions' fleets) at the top of every session, so
detached fleets are never forgotten.

```sh
claude plugin marketplace add YogevKr/codexspin
claude plugin install codexspin@codexspin
```

### Continue a Claude Code session in Codex

From the Claude Code session you want to continue, run:

```text
/codexspin:transfer
```

The plugin automatically captures the current session's transcript path, then
uses Codex's native importer to create a persistent thread. The imported user
and assistant turns appear as visible history in Codex. On success, the command
prints the new session ID and the exact command for opening it interactively:

```sh
codex resume <thread-id>
```

To transfer a different Claude session, use the CLI's explicit source override
from a terminal:

```sh
codexspin transfer --source ~/.claude/projects/<project>/<session-id>.jsonl
```

Codex accepts only `.jsonl` Claude transcripts stored beneath
`~/.claude/projects/`; codexspin validates this restriction before importing.
If the installed Codex app-server does not support session import, codexspin
detects it and prints the upgrade command
(`npm install -g @openai/codex@latest`) before asking you to retry.

Prefer just the skill without hooks? Symlink it instead (don't do both, or
the skill loads twice):

```sh
ln -s "$(pwd)/skills/codex" ~/.claude/skills/codex
```

## Tests

```sh
uv run pytest
```

The suite drives the real runner against a fake app-server
(`tests/fake_codex.py`, selected via `CODEXSPIN_CODEX_BIN`), covering
completion, failure, startup hang, cancel, resume, and dead-runner detection.
Remote tests use `tests/fake_ssh.py` with a separate `CODEXSPIN_HOME` to model
another machine.
