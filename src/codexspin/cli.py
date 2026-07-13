"""codexspin — spin and manage parallel Codex sessions.

  codexspin spawn [-s SANDBOX | --yolo] [-m MODEL] [-e EFFORT] [-C DIR] [-n NAME] "prompt"
  codexspin status [JOB]
  codexspin result JOB [--json]
  codexspin await JOB [JOB...] [--timeout SECS]
  codexspin send JOB "follow-up"
  codexspin cancel JOB [--hard]
  codexspin logs JOB [-n LINES]
  codexspin gc [--keep-days N]
"""

from __future__ import annotations

import argparse
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


def cmd_spawn(args) -> int:
    sandbox = "danger-full-access" if args.yolo else args.sandbox
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise SystemExit(f"codexspin: cwd does not exist: {cwd}")
    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("codexspin: empty prompt")

    job_id = new_job_id(args.name)
    jd = job_dir(job_id)
    jd.mkdir(parents=True)
    write_json(jd / "job.json", {
        "job_id": job_id,
        "prompt": prompt,
        "cwd": cwd,
        "sandbox": sandbox,
        "model": args.model,
        "effort": args.effort,
        "created_at": time.time(),
    })
    write_json(jd / "state.json", {
        "job_id": job_id,
        "phase": "starting",
        "cwd": cwd,
        "sandbox": sandbox,
        "prompt_preview": " ".join(prompt.split())[:120],
        "started_at": time.time(),
        "activity": "launching runner",
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
    for s in states:
        started = s.get("started_at") or 0
        end = s.get("finished_at") or time.time()
        line = (
            f"{s['job_id']:34s} {s.get('phase', '?'):9s} {fmt_elapsed(end - started):>7s}  "
            f"[{s.get('sandbox', '?')}] {Path(s.get('cwd', '')).name}"
        )
        print(line)
        print(f"  {s.get('prompt_preview', '')}")
        if s.get("phase") not in TERMINAL_PHASES:
            print(f"  ↳ {s.get('activity', '')}")
        if s.get("thread_id"):
            print(f"  resume: codex resume {s['thread_id']}")
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
    jd = job_dir(job_id)
    state = load_state(job_id) or {}
    if state.get("phase") not in TERMINAL_PHASES:
        raise SystemExit(f"codexspin: {job_id} is still {state.get('phase', 'unknown')}; await or cancel it first")
    if not state.get("thread_id"):
        raise SystemExit(f"codexspin: {job_id} has no thread to resume")
    spec = read_json(jd / "job.json") or {}
    spec["prompt"] = args.prompt
    write_json(jd / "job.json", spec)
    # Invalidate the previous turn's result so `result` reports "no result
    # yet" during the new turn; history stays in results.jsonl.
    (jd / "result.json").unlink(missing_ok=True)
    state.update(phase="starting", activity="resuming thread",
                 prompt_preview=" ".join(args.prompt.split())[:120], started_at=time.time())
    state.pop("finished_at", None)
    write_json(jd / "state.json", state)
    pid = launch_runner(jd, resume=True)
    state["runner_pid"] = pid
    write_json(jd / "state.json", state)
    print(job_id)
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


def cmd_gc(args) -> int:
    cutoff = time.time() - args.keep_days * 24 * 3600
    removed = 0
    for state in list_jobs():
        if state.get("phase") in TERMINAL_PHASES and (state.get("finished_at") or state.get("started_at") or 0) < cutoff:
            shutil.rmtree(job_dir(state["job_id"]), ignore_errors=True)
            removed += 1
    print(f"removed {removed} finished job(s) older than {args.keep_days}d")
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
                   choices=["minimal", "low", "medium", "high", "xhigh"])
    p.add_argument("-C", "--cwd", default=None, help="working directory (default: current)")
    p.add_argument("-n", "--name", default=None, help="job name used in the job id")
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
    p.add_argument("prompt")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("cancel", help="interrupt a running job")
    p.add_argument("job")
    p.add_argument("--hard", action="store_true", help="SIGKILL the runner process group")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("logs", help="show recent job events")
    p.add_argument("job")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("gc", help="delete old finished jobs")
    p.add_argument("--keep-days", type=int, default=7)
    p.set_defaults(fn=cmd_gc)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
