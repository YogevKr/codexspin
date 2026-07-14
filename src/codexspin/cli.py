"""codexspin — spin and manage parallel Codex sessions.

  codexspin spawn [-s SANDBOX | --yolo] [-w|--worktree] [--max-minutes N] [-m MODEL] [-e EFFORT] [-C DIR] [-n NAME] "prompt"
  codexspin status [JOB]
  codexspin result JOB [--json]
  codexspin await JOB [JOB...] [--timeout SECS]
  codexspin send JOB "follow-up"
  codexspin handoff JOB HOST ["follow-up"]
  codexspin cancel JOB [--hard]
  codexspin logs JOB [-n LINES]
  codexspin doctor
  codexspin gc [--keep-days N]

Add --host NAME to run any command above on NAME over ssh.
"""

from __future__ import annotations

import argparse
import fcntl
import shlex
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .jobs import (
    SANDBOX_MODES,
    pid_is_runner,
    TERMINAL_PHASES,
    fmt_elapsed,
    job_dir,
    jobs_root,
    list_jobs,
    load_state,
    new_job_id,
    read_json,
    resolve_job_id,
    write_json,
)


_DETACH = """
import subprocess, sys
out = open(sys.argv[1], "a")
p = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=out,
                     stderr=out, start_new_session=True)
print(p.pid)
"""

_REMOTE_COMMANDS = frozenset({
    "spawn", "status", "result", "await", "send", "cancel", "logs", "doctor", "gc",
})
_REMOTE_PROMPT_COMMANDS = frozenset({"spawn", "send"})
_REMOTE_INSTALL_HINT = (
    "codexspin: remote codexspin not found; install it there with: uv tool install codexspin"
)


class _Arg(str):
    """An argv token that remembers its original position through argparse."""

    def __new__(cls, value: str, index: int):
        token = super().__new__(cls, value)
        token.argv_index = index
        return token


def _forwarded_argv(argv: list[str], prompt_index: int | None) -> list[str]:
    """Remove --host while preserving every other original argv token."""
    forwarded = []
    parse_options = True
    index = 0
    while index < len(argv):
        value = argv[index]
        if parse_options and value == "--":
            parse_options = False
        elif parse_options and value == "--host":
            index += 2
            continue
        elif parse_options and value.startswith("--host="):
            index += 1
            continue
        forwarded.append("-" if index == prompt_index else value)
        index += 1
    return forwarded


def _run_remote(args, argv: list[str]) -> int:
    command_name = str(args.cmd)
    if command_name not in _REMOTE_COMMANDS:
        raise SystemExit(f"codexspin: --host is not supported for {command_name}")

    prompt = None
    prompt_index = None
    if command_name in _REMOTE_PROMPT_COMMANDS:
        prompt = sys.stdin.read() if args.prompt == "-" else str(args.prompt)
        prompt_index = args.prompt.argv_index

    forwarded = _forwarded_argv(argv, prompt_index)
    ssh_bin = os.environ.get("CODEXSPIN_SSH_BIN", "ssh")
    command = [ssh_bin, str(args.remote_host), _remote_command(["codexspin", *forwarded])]
    try:
        if prompt is None:
            completed = subprocess.run(command)
        else:
            completed = subprocess.run(command, input=prompt, text=True)
    except FileNotFoundError:
        print(f"codexspin: ssh binary not found: {ssh_bin}", file=sys.stderr)
        return 127
    if completed.returncode == 127:
        print(_REMOTE_INSTALL_HINT, file=sys.stderr)
    return completed.returncode


def _remote_command(parts: list[str]) -> str:
    """One shell-quoted command string for ssh: OpenSSH joins argv with spaces
    and lets the remote shell reparse, so every token must be quoted. When the
    local job root is overridden, the remote must use the same root — the
    rsynced paths assume it."""
    command = shlex.join(parts)
    home = os.environ.get("CODEXSPIN_HOME")
    if home:
        command = f"CODEXSPIN_HOME={shlex.quote(home)} {command}"
    return command


def _add_host_argument(parser: argparse.ArgumentParser) -> None:
    # dest is remote_host so it never collides with positional args named
    # "host" (e.g. handoff's target machine).
    parser.add_argument("--host", dest="remote_host", metavar="NAME",
                        help="run this command on NAME over ssh")


_HANDOFF_PROMPT = (
    "You were handed off to another machine mid-task. "
    "Re-read your prior context and continue to completion."
)


def launch_runner(jd: Path, resume: bool = False) -> int:
    """Double-fork so the runner is orphaned to init: it never becomes a
    zombie child of the spawning process, keeping pid liveness checks honest."""
    args = [sys.executable, "-m", "codexspin.runner", str(jd)]
    if resume:
        args.append("--resume")
    out = subprocess.run(
        [sys.executable, "-c", _DETACH, str(jd / "runner.out"), *args],
        capture_output=True, text=True, cwd=str(jd), check=True,
    )
    return int(out.stdout.strip())


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def create_worktree(cwd: str, job_id: str) -> dict:
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise SystemExit(f"codexspin: --worktree requires a git repository at {cwd}")
    repo_root = top.stdout.strip()
    # The common git dir survives even if the spawning worktree is later
    # removed — record it so gc can always clean up.
    common = git(repo_root, "rev-parse", "--git-common-dir")
    common_dir = common.stdout.strip() if common.returncode == 0 else ""
    if common_dir and not os.path.isabs(common_dir):
        common_dir = os.path.normpath(os.path.join(repo_root, common_dir))
    wt_path = str(jobs_root().parent / "worktrees" / job_id)
    branch = f"codexspin/{job_id}"
    Path(wt_path).parent.mkdir(parents=True, exist_ok=True)
    added = git(repo_root, "worktree", "add", "-b", branch, wt_path, "HEAD")
    if added.returncode != 0:
        raise SystemExit(f"codexspin: worktree add failed:\n{added.stderr.strip()}")
    return {"repo_root": repo_root, "worktree": wt_path, "branch": branch,
            "git_common_dir": common_dir}


def cmd_spawn(args) -> int:
    sandbox = "danger-full-access" if args.yolo else args.sandbox
    # realpath: symlinked paths (macOS /tmp!) must match git's physical
    # toplevel or the worktree-relative math escapes the worktree.
    cwd = os.path.realpath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise SystemExit(f"codexspin: cwd does not exist: {cwd}")
    if args.max_minutes is not None and args.max_minutes <= 0:
        raise SystemExit("codexspin: --max-minutes must be positive")
    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("codexspin: empty prompt")

    job_id = new_job_id(args.name)
    wt = create_worktree(cwd, job_id) if args.worktree else {}
    if wt:
        # Preserve a requested subdirectory: -C repo/pkg maps to <worktree>/pkg.
        rel = os.path.relpath(cwd, wt["repo_root"])
        cwd = os.path.normpath(os.path.join(wt["worktree"], rel))
    jd = job_dir(job_id)
    jd.mkdir(parents=True)
    write_json(jd / "job.json", {
        "job_id": job_id,
        "prompt": prompt,
        "cwd": cwd,
        "sandbox": sandbox,
        "model": args.model,
        "effort": args.effort,
        "max_minutes": args.max_minutes,
        "created_at": time.time(),
        **wt,
    })
    write_json(jd / "state.json", {
        "job_id": job_id,
        "phase": "starting",
        "cwd": cwd,
        "sandbox": sandbox,
        "prompt_preview": " ".join(prompt.split())[:120],
        "started_at": time.time(),
        "activity": "launching runner",
        **({"branch": wt["branch"], "worktree": wt["worktree"], "repo_root": wt["repo_root"],
            "git_common_dir": wt["git_common_dir"]} if wt else {}),
    })
    pid = launch_runner(jd)
    state = read_json(jd / "state.json") or {}
    state["runner_pid"] = pid
    write_json(jd / "state.json", state)
    print(job_id)
    return 0


def use_color() -> bool:
    forced = os.environ.get("CODEXSPIN_COLOR")
    if forced is not None:
        return forced not in ("0", "false", "no")
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


class Style:
    """ANSI styling that collapses to plain text when color is off."""

    def __init__(self, enabled: bool):
        self.on = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def red(self, t): return self._wrap("31", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def cyan(self, t): return self._wrap("36", t)
    def gray(self, t): return self._wrap("90", t)


PHASE_GLYPH = {
    "starting": ("◌", "cyan"), "running": ("●", "cyan"), "done": ("✓", "green"),
    "failed": ("✗", "red"), "cancelled": ("■", "gray"), "died": ("☠", "red"),
    "timeout": ("⏱", "yellow"),
}


def styled_sandbox(st: Style, sandbox: str) -> str:
    if sandbox == "danger-full-access":
        return st.red(st.bold("yolo"))
    if sandbox == "workspace-write":
        return st.yellow("write")
    return st.green(sandbox or "?")


def quota_line(st: Style, quota: dict, fancy: bool) -> str:
    mins = quota.get("window_mins") or 0
    if mins >= 1440:
        window = f"{round(mins / 1440)}d"
    elif mins >= 60:
        window = f"{round(mins / 60)}h"
    else:
        window = f"{mins}m"
    pct = quota.get("used_percent") or 0
    text = (f"codex quota: {pct}% of {window} window used"
            f" (plan: {quota.get('plan', '?')})")
    if not fancy:
        return text
    filled = min(10, round(pct / 10))
    bar = "▓" * filled + "░" * (10 - filled)
    paint = st.green if pct < 70 else (st.yellow if pct < 90 else st.red)
    return f"{paint(bar)} {paint(text)}"


def print_job_fancy(st: Style, s: dict, width: int) -> None:
    phase = s.get("phase", "?")
    glyph, color = PHASE_GLYPH.get(phase, ("?", "gray"))
    paint = getattr(st, color)
    started = s.get("started_at") or 0
    end = s.get("finished_at") or time.time()
    finished = phase in TERMINAL_PHASES
    head = (f"{paint(glyph)} {st.bold(s['job_id']):{34 + (8 if st.on else 0)}s} "
            f"{paint(f'{phase:9s}')} {fmt_elapsed(end - started):>7s}  "
            f"{styled_sandbox(st, s.get('sandbox', '?'))}")
    if s.get("model"):
        model_effort = f"{s['model']}/{s.get('effort', '?')}"
        head += f"  {st.dim(model_effort)}"
    print(head)
    print(f"  {st.dim(s.get('prompt_preview', '')[:width - 4])}")
    if not finished:
        print(f"  {st.cyan('↳ ' + str(s.get('activity', ''))[:width - 6])}")
    meta = []
    if s.get("branch"):
        meta.append(f"branch {s['branch']}")
    if s.get("thread_id"):
        meta.append(f"resume: codex resume {s['thread_id']}")
    if meta:
        print(f"  {st.gray('  '.join(meta))}")
    print()


def print_job_plain(s: dict) -> None:
    started = s.get("started_at") or 0
    end = s.get("finished_at") or time.time()
    line = (
        f"{s['job_id']:34s} {s.get('phase', '?'):9s} {fmt_elapsed(end - started):>7s}  "
        f"[{s.get('sandbox', '?')}] {Path(s.get('cwd', '')).name}"
    )
    if s.get("model"):
        line += f"  {s['model']}/{s.get('effort', '?')}"
    print(line)
    print(f"  {s.get('prompt_preview', '')}")
    if s.get("phase") not in TERMINAL_PHASES:
        print(f"  ↳ {s.get('activity', '')}")
    if s.get("branch"):
        print(f"  branch: {s['branch']}  worktree: {s.get('worktree', '')}")
    if s.get("thread_id"):
        print(f"  resume: codex resume {s['thread_id']}")


def cmd_status(args) -> int:
    if args.job:
        states = [s for s in [load_state(resolve_job_id(args.job))] if s]
    else:
        states = list_jobs()
        if not args.all:
            cutoff = time.time() - 24 * 3600
            states = [s for s in states
                      if s.get("phase") not in TERMINAL_PHASES or (s.get("started_at") or 0) > cutoff]
    if args.json:
        print(json.dumps(states, indent=2))
        return 0
    if not states:
        print("no jobs (use --all to include old finished jobs)")
        return 0
    fancy = use_color()
    st = Style(fancy)
    width = shutil.get_terminal_size((120, 24)).columns if fancy else 120
    quota = None
    for s in states:
        if fancy:
            print_job_fancy(st, s, width)
        else:
            print_job_plain(s)
        q = s.get("quota")
        if q and (quota is None or (q.get("at") or 0) > (quota.get("at") or 0)):
            quota = q
    if quota:
        prefix = "" if fancy else "\n"
        print(f"{prefix}{quota_line(st, quota, fancy)}")
    return 0


def cmd_result(args) -> int:
    job_id = resolve_job_id(args.job)
    result = read_json(job_dir(job_id) / "result.json")
    state = load_state(job_id)
    if result is None:
        phase = (state or {}).get("phase", "unknown")
        print(f"codexspin: {job_id} has no result yet (phase: {phase})", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["phase"] == "done" else 1
    print(f"# {job_id} — {result['phase']}")
    if result.get("duration_ms"):
        print(f"duration: {fmt_elapsed(result['duration_ms'] / 1000)}  commands: {result.get('command_count', 0)}")
    if result.get("touched_files"):
        print("touched files:")
        for f in result["touched_files"]:
            print(f"  {f}")
    if result.get("error"):
        print(f"error: {result['error'].get('message', '')}")
    print()
    print(result.get("final_message") or "(no final message)")
    return 0 if result["phase"] == "done" else 1


def cmd_await(args) -> int:
    job_ids = [resolve_job_id(j) for j in args.job]
    deadline = time.time() + args.timeout if args.timeout else None
    pending = set(job_ids)
    rc = 0
    while pending:
        for job_id in sorted(pending):
            state = load_state(job_id) or {}
            if state.get("phase") in TERMINAL_PHASES:
                pending.discard(job_id)
                print(f"--- {job_id}: {state.get('phase')} ---")
                sub = argparse.Namespace(job=job_id, json=False)
                if cmd_result(sub) != 0:
                    rc = 1
                break
        else:
            if deadline and time.time() > deadline:
                print(f"codexspin: timed out waiting for: {', '.join(sorted(pending))}", file=sys.stderr)
                return 2
            time.sleep(0.5)
    return rc


def cmd_send(args) -> int:
    job_id = resolve_job_id(args.job)
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        raise SystemExit("codexspin: empty prompt")
    jd = job_dir(job_id)
    # Exclusive lock over check-then-launch: two concurrent sends must not
    # put two runners on the same thread.
    with open(jd / "cli.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(job_id) or {}
        if state.get("phase") not in TERMINAL_PHASES:
            raise SystemExit(f"codexspin: {job_id} is still {state.get('phase', 'unknown')}; await or cancel it first")
        if not state.get("thread_id"):
            raise SystemExit(f"codexspin: {job_id} has no thread to resume")
        spec = read_json(jd / "job.json") or {}
        spec["prompt"] = prompt
        write_json(jd / "job.json", spec)
        # Invalidate the previous turn's result so `result` reports "no result
        # yet" during the new turn; history stays in results.jsonl.
        (jd / "result.json").unlink(missing_ok=True)
        state.update(phase="starting", activity="resuming thread",
                     prompt_preview=" ".join(prompt.split())[:120], started_at=time.time())
        state.pop("finished_at", None)
        write_json(jd / "state.json", state)
        pid = launch_runner(jd, resume=True)
        state["runner_pid"] = pid
        write_json(jd / "state.json", state)
    print(job_id)
    return 0


def find_session_rollout(thread_id: str) -> Path:
    sessions = Path.home() / ".codex" / "sessions"
    if sessions.is_dir():
        matches = [path for path in sessions.rglob("*")
                   if path.is_file() and thread_id in path.name]
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime)
    raise SystemExit(
        f"codexspin: no Codex session rollout file found for thread {thread_id} under {sessions}"
    )


def run_handoff_command(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, input=input_text, capture_output=True, text=True)
    except OSError as exc:
        raise SystemExit(f"codexspin: cannot run {command[0]}: {exc}") from exc


def remote_codexspin_missing(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == 127 or "command not found" in output or "codexspin: not found" in output


def remote_install_error(host: str) -> SystemExit:
    return SystemExit(
        f"codexspin: codexspin is not installed on {host}; install it there and ensure it is on PATH"
    )


def command_error(prefix: str, result: subprocess.CompletedProcess) -> SystemExit:
    detail = (result.stderr or result.stdout).strip()
    return SystemExit(f"codexspin: {prefix}: {detail or f'exit {result.returncode}'}")


def cmd_handoff(args) -> int:
    job_id = resolve_job_id(args.job)
    with open(job_dir(job_id) / "cli.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _handoff_locked(args, job_id)


def _handoff_locked(args, job_id: str) -> int:
    state = load_state(job_id)
    if state is None:
        raise SystemExit(f"codexspin: cannot read state for {job_id}")

    # A job that has not recorded a thread yet cannot be resumed anywhere;
    # cancelling it first would just destroy it with nothing to hand off.
    if not state.get("thread_id"):
        raise SystemExit(f"codexspin: {job_id} has no thread yet ({state.get('phase', '?')}); "
                         "wait for it to start or cancel it yourself")

    if state.get("phase") not in TERMINAL_PHASES:
        cmd_cancel(argparse.Namespace(job=job_id, hard=False))
        state = load_state(job_id) or {}
    if state.get("phase") not in TERMINAL_PHASES:
        raise SystemExit(f"codexspin: {job_id} did not reach a terminal phase after cancellation")

    # load_state can synthesize phase=died for a vanished runner. Persist that
    # terminal phase so the state copied to the remote can always be resumed.
    state_path = job_dir(job_id) / "state.json"
    disk_state = read_json(state_path) or {}
    if disk_state.get("phase") not in TERMINAL_PHASES:
        disk_state.update(state)
        disk_state.setdefault("finished_at", time.time())
        write_json(state_path, disk_state)
        state = disk_state

    thread_id = state.get("thread_id")
    if not thread_id:
        raise SystemExit(f"codexspin: {job_id} has no thread_id to hand off")
    rollout = find_session_rollout(thread_id)

    spec = read_json(job_dir(job_id) / "job.json") or {}
    cwd_tree = Path(spec.get("worktree") or spec.get("cwd") or state.get("cwd") or "")
    if not cwd_tree.is_absolute() or not cwd_tree.is_dir():
        raise SystemExit(f"codexspin: job cwd tree does not exist: {cwd_tree}")

    prompt = _HANDOFF_PROMPT if args.prompt is None else args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("codexspin: empty prompt")

    ssh_bin = os.environ.get("CODEXSPIN_SSH_BIN", "ssh")
    rsync_bin = os.environ.get("CODEXSPIN_RSYNC_BIN", "rsync")
    probe = run_handoff_command([ssh_bin, args.host, _remote_command(["codexspin", "--help"])])
    if probe.returncode != 0:
        if remote_codexspin_missing(probe):
            raise remote_install_error(args.host)
        raise command_error(f"could not reach codexspin on {args.host}", probe)

    sources = [cwd_tree, rollout, job_dir(job_id).resolve()]
    # A linked worktree's .git file points into the main repo's git dir;
    # without it the remote tree is not a repository.
    common_dir = spec.get("git_common_dir")
    if spec.get("worktree") and common_dir and os.path.isdir(common_dir):
        sources.append(Path(common_dir))
    for source in sources:
        # --no-implied-dirs: with --relative, rsync otherwise tries to copy
        # attributes (times, perms) onto root-owned implied parents like
        # /Users or /private/tmp and dies with EPERM.
        copied = run_handoff_command([
            rsync_bin, "--archive", "--relative", "--no-implied-dirs", "--rsh", ssh_bin,
            "--", str(source), f"{args.host}:/",
        ])
        if copied.returncode != 0:
            raise command_error(f"rsync failed for {source}", copied)

    resumed = run_handoff_command(
        [ssh_bin, args.host, _remote_command(["codexspin", "send", job_id, "-"])],
        input_text=prompt,
    )
    if resumed.returncode != 0:
        if remote_codexspin_missing(resumed):
            raise remote_install_error(args.host)
        raise command_error(f"remote resume failed on {args.host}", resumed)

    local_state = read_json(state_path) or state
    local_state["handed_off_to"] = args.host
    write_json(state_path, local_state)
    print(job_id)
    print(f"ssh {args.host} codexspin status {job_id}")
    return 0


def cmd_cancel(args) -> int:
    job_id = resolve_job_id(args.job)
    state = load_state(job_id) or {}
    pid = state.get("runner_pid")
    if state.get("phase") in TERMINAL_PHASES or not pid:
        print(f"codexspin: {job_id} is not running (phase: {state.get('phase', 'unknown')})")
        return 0
    if not pid_is_runner(pid):
        state.update(phase="died", activity="runner gone before cancel", finished_at=time.time())
        write_json(job_dir(job_id) / "state.json", state)
        print(f"codexspin: {job_id} runner already gone; marked died")
        return 0
    try:
        if args.hard:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Wait briefly for the runner to write a terminal state; if it died
    # without one (e.g. SIGTERM landed during interpreter startup), record
    # the cancellation ourselves so the job doesn't linger as "died".
    deadline = time.time() + (0 if args.hard else 6)
    while time.time() < deadline:
        state = load_state(job_id) or {}
        if state.get("phase") in TERMINAL_PHASES:
            break
        time.sleep(0.2)
    state = load_state(job_id) or state
    if state.get("phase") not in ("done", "failed", "cancelled"):
        state.update(phase="cancelled", activity="killed", finished_at=time.time())
        write_json(job_dir(job_id) / "state.json", state)
    print(f"cancelled {job_id}")
    return 0


def cmd_logs(args) -> int:
    job_id = resolve_job_id(args.job)
    jd = job_dir(job_id)
    events = (jd / "events.jsonl")
    if not events.exists():
        print("(no events yet)")
        return 0
    lines = events.read_text().splitlines()[-args.lines:]
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "?")
        params = msg.get("params", {})
        item = params.get("item") or {}
        if method in ("item/started", "item/completed"):
            print(f"{method:16s} {item.get('type', ''):18s} {json.dumps(item)[:140]}")
        else:
            print(f"{method:16s} {json.dumps(params)[:140]}")
    return 0


def remove_worktree(state: dict) -> bool:
    """Remove a job's worktree only when it has no uncommitted work; committed
    work survives on the codexspin/<job-id> branch. Returns False to keep."""
    wt = state.get("worktree")
    if not wt or not os.path.isdir(wt):
        return True
    dirty = git(wt, "status", "--porcelain")
    if dirty.returncode != 0 or dirty.stdout.strip():
        return False
    # Prefer the common git dir — it outlives the worktree we spawned from.
    for repo in (state.get("git_common_dir"), state.get("repo_root")):
        if repo and os.path.isdir(repo):
            return git(repo, "worktree", "remove", wt).returncode == 0
    # The repository itself is gone; the admin entry died with it.
    shutil.rmtree(wt, ignore_errors=True)
    return True


def cmd_gc(args) -> int:
    cutoff = time.time() - args.keep_days * 24 * 3600
    removed, kept = 0, []
    for state in list_jobs():
        if state.get("phase") in TERMINAL_PHASES and (state.get("finished_at") or state.get("started_at") or 0) < cutoff:
            if not remove_worktree(state):
                kept.append(state["job_id"])
                continue
            shutil.rmtree(job_dir(state["job_id"]), ignore_errors=True)
            removed += 1
    print(f"removed {removed} finished job(s) older than {args.keep_days}d")
    for job_id in kept:
        print(f"kept {job_id}: worktree has uncommitted changes")
    return 0


def cmd_doctor(args) -> int:
    from .appserver import AppServerClient, AppServerError

    try:
        version = subprocess.run([os.environ.get("CODEXSPIN_CODEX_BIN", "codex"), "--version"],
                                 capture_output=True, text=True)
    except OSError:
        print("codex binary: NOT FOUND on PATH")
        return 1
    if version.returncode != 0:
        print(f"codex binary: FAILED — {version.stderr.strip() or version.stdout.strip()}")
        return 1
    print(f"codex binary: {version.stdout.strip()}")
    try:
        client = AppServerClient(cwd=os.getcwd())
        client.initialize()
        account_resp = client.request("account/read", {"refreshToken": False}, timeout=30)
        config = client.request("config/read", {"includeLayers": False, "cwd": os.getcwd()},
                                timeout=30).get("config") or {}
        client.close()
    except (AppServerError, OSError) as exc:
        print(f"app-server: FAILED — {exc}")
        return 1
    print("app-server: ok")
    account = account_resp.get("account")
    if not account:
        if account_resp.get("requiresOpenaiAuth"):
            print("auth: NOT LOGGED IN — run `codex login`")
            return 1
        print("auth: none required (custom provider)")
    else:
        kind = account.get("type", "?")
        who = account.get("email") or account.get("planType") or ""
        print(f"auth: {kind} {who}".rstrip())
    print(f"default model: {config.get('model', '?')} / {config.get('model_reasoning_effort', '?')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="codexspin", description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spawn", help="spawn a detached codex job")
    p.add_argument("prompt", help="task prompt ('-' reads stdin)")
    p.add_argument("-s", "--sandbox", choices=SANDBOX_MODES, default="workspace-write")
    p.add_argument("--yolo", action="store_true", help="shortcut for --sandbox danger-full-access")
    p.add_argument("-m", "--model", default=None)
    p.add_argument("-e", "--effort", default=None,
                   choices=["none", "minimal", "low", "medium", "high", "xhigh"])
    p.add_argument("-C", "--cwd", default=None, help="working directory (default: current)")
    p.add_argument("-n", "--name", default=None, help="job name used in the job id")
    p.add_argument("-w", "--worktree", action="store_true",
                   help="run in a fresh git worktree (branch codexspin/<job-id>)")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="interrupt the job after this many minutes (phase: timeout)")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_spawn)

    p = sub.add_parser("status", help="show jobs (running + last 24h by default)")
    p.add_argument("job", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("result", help="print a job's result")
    p.add_argument("job")
    p.add_argument("--json", action="store_true")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_result)

    p = sub.add_parser("await", help="block until job(s) finish, print results")
    p.add_argument("job", nargs="+")
    p.add_argument("--timeout", type=float, default=None)
    _add_host_argument(p)
    p.set_defaults(fn=cmd_await)

    p = sub.add_parser("send", help="follow-up turn on a finished job's thread")
    p.add_argument("job")
    p.add_argument("prompt", help="follow-up prompt ('-' reads stdin)")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("handoff", help="copy a job to another machine and resume it there")
    p.add_argument("job")
    p.add_argument("host")
    p.add_argument("prompt", nargs="?", help="resume prompt ('-' reads stdin)")
    p.set_defaults(fn=cmd_handoff)

    p = sub.add_parser("cancel", help="interrupt a running job")
    p.add_argument("job")
    p.add_argument("--hard", action="store_true", help="SIGKILL the runner process group")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("logs", help="show recent job events")
    p.add_argument("job")
    p.add_argument("-n", "--lines", type=int, default=40)
    _add_host_argument(p)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("doctor", help="check codex binary, app-server handshake, auth, defaults")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("gc", help="delete old finished jobs")
    p.add_argument("--keep-days", type=int, default=7)
    _add_host_argument(p)
    p.set_defaults(fn=cmd_gc)

    args = parser.parse_args([_Arg(value, index) for index, value in enumerate(raw_argv)])
    if getattr(args, "remote_host", None) is not None:
        return _run_remote(args, raw_argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
