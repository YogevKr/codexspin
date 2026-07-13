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
codexspin spawn -n e2e --yolo "Run the playwright suite and fix failures"
codexspin spawn -s read-only "Explain the auth flow in this repo"

codexspin status              # running + last 24h (--all for everything)
codexspin await JOB [JOB...]  # block until done, print results
codexspin result JOB [--json]
codexspin send JOB "follow-up on the same codex thread"
codexspin cancel JOB [--hard]
codexspin logs JOB
codexspin gc --keep-days 7
```

Job ids accept unambiguous prefixes. Every job records the codex thread id, so
`codex resume <thread-id>` drops you into the same session interactively.

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

## Tests

```sh
uv run pytest
```

The suite drives the real runner against a fake app-server
(`tests/fake_codex.py`, selected via `CODEXSPIN_CODEX_BIN`), covering
completion, failure, startup hang, cancel, resume, and dead-runner detection.
