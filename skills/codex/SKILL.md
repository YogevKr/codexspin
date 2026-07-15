---
name: codex
description: Use the local Codex CLI as an independent second agent. Three branches — (1) proactively run `codex review` for a second opinion after completing a substantive change, before presenting it as done or committing; (2) delegate a well-defined implementation task via `codex exec` (foreground one-shots) or `codexspin` (background/parallel jobs), ONLY when the user explicitly asks for Codex to do it; (3) multi-turn follow-ups via `codexspin send` on the same thread. Also covers how to prompt Codex.
---

# Codex

Codex is an independent agent on PATH (`codex`), sharing this working tree and
already authenticated. It is a second opinion, not ground truth: verify what it
reports, own what it changes. It reads the same skills your repo carries.

## If `codex` is not installed

When `codex` is missing from PATH, offer to install it — **ask the user for
approval first**, never install on your own initiative. On yes, follow the
current instructions at https://developers.openai.com/codex/cli. First-run
authentication is interactive — hand that step to the user. Verify with
`codex --version` before proceeding.

## Prompting Codex

Prompt Codex like an operator, not a collaborator: compact, block-structured
with XML tags. State the task, what "done" looks like, and the few constraints
that matter. A tighter prompt beats a bigger run — improve the contract before
raising `--effort`.

- **One task per run.** Split unrelated asks (review, then fix, then docs) into
  separate runs; a mixed prompt gets a mixed result.
- **Name skills instead of restating them.** Codex reads the same skills your
  repo carries — say "follow write-docs for the doc", "obey refactor-clean: no
  compatibility wrappers." Don't re-explain what a skill already carries.
- **Blocks, added only where the task needs them:**
  - `<task>` — the concrete job, the repo/failure context, the expected end
    state. Nearly always present.
  - `<output_contract>` — exact shape, highest-value first, compact.
  - `<default_follow_through>` — take the low-risk interpretation and keep
    going; stop only when a missing detail changes correctness, safety, or an
    irreversible action.
  - `<verification_loop>` — before finalizing, check the result against the
    requirements and the changed files; revise rather than ship the first
    draft. Any risky fix.
  - `<grounding>` — ground every claim in code or tool output; label inferences
    as inferences. Review and research.
  - `<action_safety>` — keep the diff tightly scoped; no drive-by refactors.
    Write tasks.
- **Anti-patterns:** vague framing ("take a look"), no output contract ("report
  back"), "think harder" in place of a contract, mixing jobs in one run, and
  demanding certainty the evidence can't support.
- **Recon-instead-of-code failure mode (codex 0.144 collab):** implementation
  tasks can come back as "investigation complete, recommendations sent to the
  parent agent" with zero files changed — codex spawned read-only sub-agents
  and stopped at a plan. Prevent it by stating in the prompt: "Do NOT spawn
  sub-agents or delegate; implement it yourself; done only when tests pass and
  the work is committed." Recovery on a warm thread: `codexspin send <job>
  "Good recon — now IMPLEMENT it. ..."` (restate the contract).

## Review — proactive

Run a Codex review whenever you have a substantive diff you'd want a second set
of eyes on — a refactor, a tricky algorithm, renderer work, a security-sensitive
change — before declaring it done or committing. Skip it for trivial edits
(typos, comments, doc-only).

1. Pick the diff scope: `codex review --uncommitted` for working-tree changes,
   `--base <branch>` for a branch diff, `--commit <sha>` for a landed commit.
   Scope flags and custom instructions are mutually exclusive (despite what
   `--help` implies): `codex review "<instructions>"` reviews the default
   scope with your framing, a scope flag takes no prompt. When you do write
   instructions, scope the risk area — never state the answer you expect
   (keep the review unprimed).
2. Triage every finding: confirm it against the code before acting. Preserve
   Codex's evidence boundaries — an inference it labelled is not a fact.
   Overlap with your own doubts is high-priority evidence; a finding you
   dismiss needs a stated reason, not silence.
3. Report the outcome to the user — what Codex flagged, what you fixed, what
   you dismissed and why. Done when every finding is either fixed or
   explicitly dismissed.

## Implementation — explicit ask only

Delegate implementation to Codex only when the user names Codex for the task.
Never hand it work on your own initiative, and never re-delegate follow-up
work without a fresh ask.

**Routing rule:** a single quick task you will wait on → `codex exec`
(below). Anything backgrounded, parallel ("spin codex sessions for all"),
or expecting follow-up turns → `codexspin` (next section). Never background
a raw `codex exec` with nohup/watchdogs anymore — that pattern is retired.

1. Slice the task sharp before delegating — goal, constraints, and how to
   verify — using the prompt discipline above. An underspecified task stays
   with you until a fresh agent couldn't misread it.
2. Start from a clean tree (or record the baseline commit) so Codex's diff is
   separable from yours.
3. Pick the sandbox by what the task must RUN:
   - Pure code + typecheck/unit: `codex exec --sandbox workspace-write "<task>"`.
     Network is off; add `-c sandbox_workspace_write.network_access=true` only
     when the task must fetch (e.g. new deps).
   - **Browser verification, dev servers, or full test runs: use
     `codex exec --dangerously-bypass-approvals-and-sandbox "<task>"`.** The
     sandbox blocks localhost binds (`listen EPERM` on vite/playwright), so a
     sandboxed codex ships code it never saw run. Bypass trades that blindness
     for zero OS control: only in a dedicated git worktree, only with a prompt
     you authored end-to-end (never relaying third-party text), and the diff
     review you owe afterwards is the control.
   Use `-o <file>` to capture the final message and background long calls.
   Non-interactive runs never ask for approval either way.
4. Follow up with `codex exec resume <session-id> "<follow-up>"`, taking the
   id from the run header. `resume --last` means the most recent session
   globally — a review or any other codex run in between will hijack it.
   **`resume` rejects `--sandbox`, `-o`, and `--skip-git-repo-check` in any
   position** — set the sandbox via config override instead:
   `codex exec resume <id> -c sandbox_mode="workspace-write" "<follow-up>"`,
   and capture the final message from stdout (there is no `-o`). If you expect
   more than one follow-up turn, prefer codexspin below (spawn + `send`) over
   chaining resumes.
5. You own the result: read the full diff, run the tests, and only then report
   it. "Codex says it's done" is not done.

## Exec liveness (foreground `codex exec` only)

- **Redirect stdin every launch.** With a piped stdin, exec prints `Reading
  additional input from stdin...` and blocks forever — always
  `codex exec ... < /dev/null`.
- **Launch from the repo/worktree ROOT.** The writable sandbox root is the
  CWD at launch: exec'd from a subdirectory (e.g. `web/`), every edit outside
  it is rejected as "writing outside of the project" and a `never` approval
  policy can't recover — the run burns with zero files changed.
- **Long prompts via file:** `codex exec ... "$(cat prompt.txt)"` — check the
  file exists first; a missing file silently sends the fallback string as the
  task.
- Backgrounded raw exec (nohup + session-file watchdogs) is retired — use
  codexspin below instead.

## codexspin — background, parallel, and multi-turn jobs

`codexspin` (on PATH; `brew install yogevkr/tap/codexspin` or
`uv tool install git+https://github.com/YogevKr/codexspin`) runs detached Codex jobs on
the `codex app-server` API — real job state instead of tmux screen-scraping
or nohup watchdogs. Same prompt discipline and explicit-ask rule apply.
Verified against codex-cli 0.144 (July 2026).

1. `codexspin spawn -n <name> [-C <repo-root>] "<task>"` — returns a job id
   immediately (sandbox defaults to workspace-write). Spawn several in one go
   for parallel work — **always add `-w/--worktree` when spawning more than
   one job on the same repo** (fresh worktree per job, branch
   `codexspin/<job-id>`; the git metadata dir is auto-added to the sandbox's
   writable roots, so the job CAN and SHOULD commit its own work — say so in
   the prompt). Add
   `--max-minutes <n>` on unattended fleets so runaways self-interrupt.
   `--yolo` for jobs that must bind localhost / run dev servers / fetch
   network — same controls as the exec bypass below: worktree (`-w` covers
   it), self-authored prompt, diff review after. `-m/--model -e/--effort`
   override config defaults; `status` shows the resolved model and current
   ChatGPT quota burn — check it before spawning a large fleet.
2. `codexspin await <job> [<job>...]` as a background Bash task — blocks until
   done and prints each result (final message, touched files). No polling.
3. `codexspin status` — live phase + current activity per job;
   `codexspin logs <job>` for the event tail; `codexspin cancel <job>` to
   interrupt (`--hard` kills the process group).
4. `codexspin send <job> "<follow-up>"` — next turn on the same warm thread.
   Refuses while the job is running; await it first. The printed
   `codex resume <thread-id>` line drops the user into the session
   interactively.
5. Runners are per-job and double-forked: a dead runner shows as phase
   `died`, never as silent "working". Startup failures land in
   `result.json` with the app-server stderr tail.
6. Remote machines: `--host <name>` runs any codexspin command over ssh;
   `codexspin handoff <job> <host>` migrates a job mid-task and resumes its
   warm thread there (remote needs codexspin + codex + auth).

## Rules

- Don't touch the working tree while a Codex exec is running on it.
- `--sandbox read-only` (the default) for consultation and questions;
  `workspace-write` only for delegated implementation.
- `--dangerously-bypass-approvals-and-sandbox` is reserved for tasks that must
  run browsers/servers/full suites (above) — dedicated worktree, self-authored
  prompt, mandatory diff review after. `--full-auto` is deprecated (just an
  alias for workspace-write) — don't reach for it.
- Leave `--effort` unset (accepted: none, minimal, low, medium, high, xhigh)
  and omit model overrides unless the user asks — tighten the prompt first.
