"""Job state on disk.

~/.codexspin/jobs/<job-id>/
  job.json      spawn spec (prompt, cwd, sandbox, model, effort, name)
  state.json    live state, atomically replaced (phase, pids, thread id, activity)
  events.jsonl  every app-server notification, one per line
  result.json   terminal result of the latest turn
  results.jsonl append-only history, one line per finished turn
  runner.log    runner diagnostics
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import string
import time
import uuid
from pathlib import Path

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
TERMINAL_PHASES = ("done", "failed", "cancelled", "died", "timeout")


def jobs_root() -> Path:
    root = os.environ.get("CODEXSPIN_HOME") or os.path.expanduser("~/.codexspin")
    return Path(root) / "jobs"


def new_job_id(name: str | None) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "job").lower()).strip("-") or "job"
    stamp = time.strftime("%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{slug}-{stamp}-{suffix}"


def job_dir(job_id: str) -> Path:
    return jobs_root() / job_id


def write_json(path: Path, data: dict) -> None:
    # Writer-unique tmp name: concurrent writers (CLI vs runner, or two runner
    # threads) must never share a tmp path or the renames race.
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def pid_is_runner(pid: int | None) -> bool:
    """True only if pid is alive AND is actually a codexspin runner — guards
    against PID reuse making a dead job look alive (or getting signalled)."""
    if not pid_alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return b"codexspin" in cmdline.read_bytes()
        except OSError:
            return True  # alive but unreadable; don't misreport as died
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return True  # alive but unverifiable; don't misreport as died
    if out.returncode != 0 or not out.stdout.strip():
        return True  # ps lacks -p/-o (e.g. BusyBox); trust liveness
    return "codexspin.runner" in out.stdout


def load_state(job_id: str) -> dict | None:
    state = read_json(job_dir(job_id) / "state.json")
    if state is None:
        return None
    # A runner that died without reaching a terminal phase leaves phase=running
    # behind; surface that as its own phase instead of pretending it works.
    # Grace period: right after spawn the runner may not have written its pid yet.
    launching = not state.get("runner_pid") and time.time() - (state.get("started_at") or 0) < 10
    if state.get("phase") not in TERMINAL_PHASES and not launching and not pid_is_runner(state.get("runner_pid")):
        state["phase"] = "died"
    return state


def list_jobs() -> list[dict]:
    root = jobs_root()
    if not root.is_dir():
        return []
    states = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        state = load_state(entry.name)
        if state:
            states.append(state)
    states.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
    return states


def resolve_job_id(prefix: str) -> str:
    """Accept a full job id or an unambiguous prefix."""
    root = jobs_root()
    if (root / prefix).is_dir():
        return prefix
    matches = [e.name for e in root.iterdir() if e.is_dir() and e.name.startswith(prefix)] if root.is_dir() else []
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"codexspin: no job matches '{prefix}'")
    raise SystemExit(f"codexspin: '{prefix}' is ambiguous: {', '.join(sorted(matches))}")


def fmt_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
