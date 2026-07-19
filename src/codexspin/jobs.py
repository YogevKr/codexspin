"""Job state on disk.

~/.codexspin/jobs/<job-id>/
  job.json      spawn spec (prompt, cwd, sandbox, model, effort, name)
  state.json    live state, atomically replaced (phase, pids, thread id, activity)
  events.jsonl  every app-server notification, one per line
  result.json   terminal result of the latest turn
  results.jsonl append-only history, one line per finished turn
  attention.json CLI-owned acknowledgement/archive state
  runner.log    runner diagnostics
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import subprocess
import string
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
TERMINAL_PHASES = ("done", "failed", "cancelled", "died", "timeout")


def jobs_root() -> Path:
    root = os.environ.get("CODEXSPIN_HOME") or os.path.expanduser("~/.codexspin")
    return Path(root) / "jobs"


def current_session_id() -> str | None:
    """The Claude Code session this process runs inside, if any.

    Claude Code exports this to every Bash call; the SessionStart hook sets it
    explicitly from its stdin payload. Absent in a plain terminal.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def owned_by_other_session(state: dict, session_id: str | None) -> bool:
    """Jobs without a session tag are shared; tagged jobs belong to one session."""
    owner = state.get("session_id")
    return bool(owner) and owner != session_id


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


@contextmanager
def exclusive_lock(path: Path):
    """Lock a job-owned file without truncating or following a symlink."""
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise OSError(f"refusing symlink lock file: {path}")
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield lock


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def attention_record(job_id: str) -> dict:
    """CLI-owned acknowledgement metadata for one job."""
    return read_json(job_dir(job_id) / "attention.json") or {}


def viewed_at(job_id: str) -> float | None:
    """When a human-facing command last presented this job's result.

    Attention state deliberately lives outside state.json: the runner owns and
    frequently replaces state.json, while result/status commands own this
    acknowledgement sidecar.
    """
    attention = attention_record(job_id)
    value = attention.get("viewed_at")
    return float(value) if isinstance(value, (int, float)) else None


def mark_viewed(job_id: str, *, generation: int | None = None,
                turn_id: str | None = None, result_finished_at: float | None = None,
                run_started_at: float | None = None, at: float | None = None) -> float:
    """Acknowledge one displayed turn/run without touching runner state.

    The generation markers prevent a late reader of an older result from
    acknowledging a newer concurrent turn, while viewed_at records when the
    presentation actually happened.
    """
    timestamp = time.time() if at is None else at
    with exclusive_lock(job_dir(job_id) / "attention.lock"):
        previous = attention_record(job_id)
        previous_viewed = previous.get("viewed_at")
        if not isinstance(previous_viewed, (int, float)):
            previous_viewed = 0
        # Viewing an archived result must not unarchive it. Preserve lifecycle
        # fields while updating only acknowledgement markers.
        record = {key: value for key, value in previous.items()
                  if key.startswith("archived_")}
        record["viewed_at"] = max(timestamp, previous_viewed)

        previous_generation = previous.get("viewed_generation")
        if not isinstance(previous_generation, int) or isinstance(previous_generation, bool):
            previous_generation = None
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = None
        if generation is not None:
            record["viewed_generation"] = max(generation, previous_generation or generation)
            if generation >= (previous_generation or generation):
                if isinstance(turn_id, str) and turn_id:
                    record["viewed_turn_id"] = turn_id
                elif isinstance(previous.get("viewed_turn_id"), str):
                    record["viewed_turn_id"] = previous["viewed_turn_id"]
            elif isinstance(previous.get("viewed_turn_id"), str):
                record["viewed_turn_id"] = previous["viewed_turn_id"]
        elif previous_generation is not None:
            record["viewed_generation"] = previous_generation
            if isinstance(previous.get("viewed_turn_id"), str):
                record["viewed_turn_id"] = previous["viewed_turn_id"]
        elif isinstance(turn_id, str) and turn_id:
            record["viewed_turn_id"] = turn_id
        elif isinstance(previous.get("viewed_turn_id"), str):
            record["viewed_turn_id"] = previous["viewed_turn_id"]

        for key, value in (
            ("viewed_result_finished_at", result_finished_at),
            ("viewed_run_started_at", run_started_at),
        ):
            prior_value = previous.get(key)
            values = [candidate for candidate in (prior_value, value)
                      if isinstance(candidate, (int, float))]
            if values:
                record[key] = max(values)
        write_json(job_dir(job_id) / "attention.json", record)
    return timestamp


def mark_archived(job_id: str, *, generation: int | None = None,
                  result_finished_at: float | None = None,
                  at: float | None = None) -> float:
    """Hide one terminal generation without deleting its resume metadata.

    A later ``send`` increments generation, which automatically brings the job
    back into normal status and attention views.
    """
    timestamp = time.time() if at is None else at
    with exclusive_lock(job_dir(job_id) / "attention.lock"):
        record = attention_record(job_id)
        record["archived_at"] = timestamp
        if isinstance(generation, int) and not isinstance(generation, bool):
            record["archived_generation"] = generation
        if isinstance(result_finished_at, (int, float)):
            record["archived_result_finished_at"] = result_finished_at
        if (not isinstance(generation, int) or isinstance(generation, bool)) and not isinstance(
                result_finished_at, (int, float)):
            # A pre-generation runner death can be synthesized from stale live
            # state, so it has no persisted finished_at either. This marker
            # applies only until send creates a real numeric generation.
            record["archived_legacy"] = True
        write_json(job_dir(job_id) / "attention.json", record)
    return timestamp


def is_archived(state: dict) -> bool:
    """Whether the current generation of a job is archived."""
    record = attention_record(state["job_id"])
    archived_generation = record.get("archived_generation")
    generation = state.get("generation")
    valid_generation = isinstance(generation, int) and not isinstance(generation, bool)
    if valid_generation:
        # Numeric generations are authoritative across hosts. Never compare a
        # resumed generation with a legacy timestamp from another clock.
        return (isinstance(archived_generation, int)
                and not isinstance(archived_generation, bool)
                and generation <= archived_generation)
    archived_finished_at = record.get("archived_result_finished_at")
    finished_at = state.get("finished_at")
    if (isinstance(archived_finished_at, (int, float))
            and isinstance(finished_at, (int, float))
            and finished_at <= archived_finished_at):
        return True
    return record.get("archived_legacy") is True


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
    if not prefix or Path(prefix).name != prefix or prefix in (".", ".."):
        raise SystemExit(f"codexspin: invalid job id {prefix!r}")
    candidate = root / prefix
    if candidate.is_dir() and not candidate.is_symlink():
        return prefix
    matches = [e.name for e in root.iterdir()
               if e.is_dir() and not e.is_symlink() and e.name.startswith(prefix)] if root.is_dir() else []
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
