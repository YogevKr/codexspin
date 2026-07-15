# codexspin

Spin and manage parallel Codex sessions from the command line (or from Claude
Code) via the `codex app-server` JSON-RPC API. Replaces the tmux screen-scrape
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

Requires `codex` on PATH, already authenticated (`codex login`).

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

codexspin status              # running + last 24h (--all for everything)
codexspin await JOB [JOB...]  # block until done, print results
codexspin result JOB [--json]
codexspin send JOB "follow-up on the same codex thread"
codexspin handoff JOB HOST ["continue on the remote"]
codexspin cancel JOB [--hard]
codexspin logs JOB
codexspin doctor              # codex binary / app-server / auth / defaults
codexspin gc --keep-days 7
```

## Remote hosts

Add `--host NAME` to `spawn`, `status`, `result`, `await`, `send`, `cancel`,
`logs`, `doctor`, or `gc` to run that same command on another machine:

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
  (`~/.codexspin/worktrees/<job-id>`, branch `codexspin/<job-id>`) so parallel
  jobs never fight over the tree. The worktree's git metadata dir is added to
  the job's sandbox writable roots (per-job app-server `-c` override), so the
  job can `git commit` its own work — tell it to. `gc` removes only clean
  worktrees — committed work survives on the branch, uncommitted work keeps
  the job. `--writable-root DIR` (repeatable) adds further writable dirs.
- `--max-minutes N` interrupts a runaway job (phase `timeout`).
- `status` shows each job's resolved model/effort and the latest ChatGPT
  quota reading (`account/rateLimits/updated` pushed by the app-server).

## How it works

- `spawn` double-forks a detached runner (`codexspin.runner`) per job — no
  shared broker, no tmux; a dead runner is always detected (phase `died`).
- The runner owns one `codex app-server` process, starts a persistent thread
  (`approvalPolicy: never`, your chosen sandbox), streams every notification
  to `events.jsonl`, and keeps `state.json` fresh (phase, activity, thread id).
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
during startup, default 180).

## Claude Code skill

This repo is also a Claude Code plugin: the `skills/codex` skill (how to
drive codex as a second agent — review discipline, delegation prompting,
sandbox choices, codexspin routing, failure-mode recoveries) plus a
SessionStart hook that lists running/recently-finished codexspin jobs at the
top of every session, so detached fleets are never forgotten.

```sh
claude plugin marketplace add YogevKr/codexspin
claude plugin install codexspin@codexspin
```

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
