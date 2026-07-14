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
"""

from __future__ import annotations

import argparse
import fcntl
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
    quota = None
    for s in states:
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
        q = s.get("quota")
        if q and (quota is None or (q.get("at") or 0) > (quota.get("at") or 0)):
            quota = q
    if quota:
        mins = quota.get("window_mins") or 0
        if mins >= 1440:
            window = f"{round(mins / 1440)}d"
        elif mins >= 60:
            window = f"{round(mins / 60)}h"
        else:
            window = f"{mins}m"
        print(f"\ncodex quota: {quota.get('used_percent')}% of {window} window used"
              f" (plan: {quota.get('plan', '?')})")
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
    state = load_state(job_id)
    if state is None:
        raise SystemExit(f"codexspin: cannot read state for {job_id}")

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
    probe = run_handoff_command([ssh_bin, args.host, "codexspin", "--help"])
    if probe.returncode != 0:
        if remote_codexspin_missing(probe):
            raise remote_install_error(args.host)
        raise command_error(f"could not reach codexspin on {args.host}", probe)

    for source in (cwd_tree, rollout, job_dir(job_id).resolve()):
        copied = run_handoff_command([
            rsync_bin, "--archive", "--relative", "--", str(source), f"{args.host}:/",
        ])
        if copied.returncode != 0:
            raise command_error(f"rsync failed for {source}", copied)

    resumed = run_handoff_command(
        [ssh_bin, args.host, "codexspin", "send", job_id, "-"], input_text=prompt,
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
    parser = argparse.ArgumentParser(prog="codexspin", description=__doc__,
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
    p.set_defaults(fn=cmd_spawn)

    p = sub.add_parser("status", help="show jobs (running + last 24h by default)")
    p.add_argument("job", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("result", help="print a job's result")
    p.add_argument("job")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_result)

    p = sub.add_parser("await", help="block until job(s) finish, print results")
    p.add_argument("job", nargs="+")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(fn=cmd_await)

    p = sub.add_parser("send", help="follow-up turn on a finished job's thread")
    p.add_argument("job")
    p.add_argument("prompt", help="follow-up prompt ('-' reads stdin)")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("handoff", help="copy a job to another machine and resume it there")
    p.add_argument("job")
    p.add_argument("host")
    p.add_argument("prompt", nargs="?", help="resume prompt ('-' reads stdin)")
    p.set_defaults(fn=cmd_handoff)

    p = sub.add_parser("cancel", help="interrupt a running job")
    p.add_argument("job")
    p.add_argument("--hard", action="store_true", help="SIGKILL the runner process group")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("logs", help="show recent job events")
    p.add_argument("job")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("doctor", help="check codex binary, app-server handshake, auth, defaults")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("gc", help="delete old finished jobs")
    p.add_argument("--keep-days", type=int, default=7)
    p.set_defaults(fn=cmd_gc)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
