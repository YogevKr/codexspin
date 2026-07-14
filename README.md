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
uv tool install --editable ~/projects/codexspin
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
codexspin cancel JOB [--hard]
codexspin logs JOB
codexspin doctor              # codex binary / app-server / auth / defaults
codexspin gc --keep-days 7
```

Job ids accept unambiguous prefixes. Every job records the codex thread id, so
`codex resume <thread-id>` drops you into the same session interactively.

- `-w/--worktree` runs the job in a fresh git worktree
  (`~/.codexspin/worktrees/<job-id>`, branch `codexspin/<job-id>`) so parallel
  jobs never fight over the tree. `gc` removes only clean worktrees —
  committed work survives on the branch, uncommitted work keeps the job.
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

Model/effort default to your `~/.codex/config.toml`; override with
`-m/--model` and `-e/--effort`.

Environment variables: `CODEXSPIN_HOME` (state root, default `~/.codexspin`),
`CODEXSPIN_CODEX_BIN` (codex binary override — the test suite points it at a
fake), `CODEXSPIN_STARTUP_TIMEOUT` (seconds to wait for app-server responses
during startup, default 180).

## Tests

```sh
uv run pytest
```

The suite drives the real runner against a fake app-server
(`tests/fake_codex.py`, selected via `CODEXSPIN_CODEX_BIN`), covering
completion, failure, startup hang, cancel, resume, and dead-runner detection.
