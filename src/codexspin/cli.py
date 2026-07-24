"""codexspin — spin and manage parallel Codex sessions.

  codexspin spawn [-s SANDBOX | --yolo] [-w|--worktree] [--max-minutes N] [-m MODEL] [-e EFFORT] [-C DIR] [-n NAME] "prompt"
  codexspin run   <same options as spawn> "prompt"   # foreground: spawn + wait + print
  codexspin status [JOB]
  codexspin result JOB [--json]
  codexspin await JOB [JOB...] [--timeout SECS]
  codexspin send JOB "follow-up"
  codexspin transfer [--source CLAUDE_JSONL]
  codexspin handoff JOB HOST ["follow-up"]
  codexspin cancel JOB [--hard]
  codexspin logs JOB [-n LINES]
  codexspin archive JOB [JOB...]
  codexspin doctor
  codexspin gc [--keep-days N]

Add --host NAME to run any command above on NAME over ssh.
"""

from __future__ import annotations

import argparse
import shlex
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .jobs import (
    SANDBOX_MODES,
    attention_record,
    pid_is_runner,
    TERMINAL_PHASES,
    current_session_id,
    exclusive_lock,
    fmt_elapsed,
    is_archived,
    job_dir,
    list_jobs,
    load_state,
    mark_archived,
    mark_viewed,
    new_job_id,
    owned_by_other_session,
    read_json,
    resolve_job_id,
    write_json,
)
from .transfer import TransferError, transfer_claude_session


_DETACH = """
import subprocess, sys
out = open(sys.argv[1], "a")
p = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=out,
                     stderr=out, start_new_session=True)
print(p.pid)
"""

_REMOTE_COMMANDS = frozenset({
    "spawn", "run", "status", "result", "await", "send", "cancel", "logs", "archive",
    "doctor", "gc",
})
_REMOTE_PROMPT_COMMANDS = frozenset({"spawn", "run", "send"})
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


def validate_host(host: str) -> str:
    """Reject hosts that ssh/rsync would parse as an option (e.g.
    -oProxyCommand=…) — argument injection into the transport."""
    if not host or host.startswith("-"):
        raise SystemExit(f"codexspin: invalid host {host!r} (must not start with '-')")
    return host


def _run_remote(args, argv: list[str]) -> int:
    command_name = str(args.cmd)
    if command_name not in _REMOTE_COMMANDS:
        raise SystemExit(f"codexspin: --host is not supported for {command_name}")
    host = validate_host(str(args.remote_host))

    prompt = None
    prompt_index = None
    if command_name in _REMOTE_PROMPT_COMMANDS:
        prompt = sys.stdin.read() if args.prompt == "-" else str(args.prompt)
        prompt_index = args.prompt.argv_index

    forwarded = _forwarded_argv(argv, prompt_index)
    ssh_bin = os.environ.get("CODEXSPIN_SSH_BIN", "ssh")
    command = [ssh_bin, host, _remote_command(["codexspin", *forwarded])]
    try:
        if prompt is None:
            # DEVNULL, not inherited: ssh must not swallow the caller's stdin
            # (e.g. a shell loop feeding lines to `codexspin await --host`).
            completed = subprocess.run(command, stdin=subprocess.DEVNULL)
        else:
            completed = subprocess.run(command, input=prompt, text=True)
    except KeyboardInterrupt:
        return 130
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
    # ssh strips arbitrary env vars, so session ownership must ride the command
    # line or remote spawns come back untagged and remote status unscoped.
    session = current_session_id()
    if session:
        command = f"CLAUDE_CODE_SESSION_ID={shlex.quote(session)} {command}"
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

# codexspin's app-server JSON-RPC assumptions were verified against this
# codex-cli minor. A different minor may have drifted the protocol shapes.
TESTED_CODEX_MINOR = "0.144"


def _codex_minor(version_output: str) -> str | None:
    m = re.search(r"(\d+)\.(\d+)", version_output)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _herdr_setup(cwd: str, label: str) -> dict:
    """Create a herdr workspace pane on `cwd` so the job's runner can report a
    NATIVE codex agent into herdr's panel (see runner._herdr_maybe_report). The
    pane is a plain shell in the job's worktree — click the agent to land there.
    Best-effort: returns {} (job proceeds normally, un-mirrored) if herdr is
    absent or errors. Returns {herdr_pane_id, herdr_bin} to fold into job.json."""
    import shutil
    herdr = shutil.which("herdr")
    if not herdr:
        return {}
    try:
        out = subprocess.run(
            [herdr, "workspace", "create", "--cwd", cwd, "--label", label, "--no-focus"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return {}
        pane = ((json.loads(out.stdout or "{}").get("result") or {})
                .get("root_pane") or {}).get("pane_id")
        return {"herdr_pane_id": pane, "herdr_bin": herdr} if pane else {}
    except Exception:
        return {}


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


def git_common_dir(cwd: str) -> str:
    """Absolute path to the repository's shared git dir — where the index and
    the index.lock every commit takes actually live — or "" if cwd is not in a
    work tree. For a linked worktree this resolves to the MAIN repo's .git,
    which sits OUTSIDE the worktree directory."""
    common = git(cwd, "rev-parse", "--git-common-dir")
    if common.returncode != 0:
        return ""
    path = common.stdout.strip()
    if path and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(cwd, path))
    return path


def git_toplevel(cwd: str) -> str:
    """Absolute Git worktree root without pathname quoting."""
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
    )
    if result.returncode != 0:
        return ""
    return os.path.realpath(os.fsdecode(result.stdout.rstrip(b"\r\n")))


def git_primary_worktree(cwd: str) -> str:
    """Primary checkout root, even when cwd belongs to a linked worktree."""
    listed = subprocess.run(
        ["git", "-C", cwd, "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
    )
    if listed.returncode == 0:
        # Git lists the primary record first. A bare first record means there
        # is no primary checkout; later records are all linked worktrees and
        # must not contain child worktrees.
        first = listed.stdout.split(b"\0\0", 1)[0].split(b"\0")
        prefix = b"worktree "
        raw_path = next((field[len(prefix):] for field in first
                         if field.startswith(prefix)), b"")
        path = os.fsdecode(raw_path)
        if path and b"bare" not in first:
            return os.path.realpath(path)
        if path:
            return ""
    return git_toplevel(cwd)


def _is_within(path: str, root: str) -> bool:
    """True if path is inside root (both fully resolved). Used to skip the
    sandbox widening when a git dir already sits under cwd and is thus already
    writable."""
    try:
        root_real = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:  # different drives (Windows): not comparable
        return False


def create_worktree(cwd: str, job_id: str) -> dict:
    repo_root = git_toplevel(cwd)
    if not repo_root:
        raise SystemExit(f"codexspin: --worktree requires a git repository at {cwd}")
    # The common git dir survives even if the spawning worktree is later
    # removed — record it so gc can always clean up.
    common_dir = git_common_dir(repo_root)
    # Always use the primary checkout. If cwd is itself a linked worktree,
    # nesting a child inside it would let GC of the parent delete the child.
    primary_root = git_primary_worktree(repo_root)
    # Bare common repositories have no primary checkout. Keep the child next
    # to the Git database rather than inside it or inside a removable linked
    # worktree.
    wt_root = ((Path(primary_root) / ".worktrees") if primary_root else
               (Path(common_dir).parent / ".worktrees"))
    try:
        wt_root.mkdir()
    except FileExistsError:
        pass
    expected_parent = os.path.realpath(wt_root.parent)
    resolved_root = os.path.realpath(wt_root)
    if (wt_root.is_symlink() or not wt_root.is_dir()
            or os.path.dirname(resolved_root) != expected_parent):
        raise SystemExit(f"codexspin: unsafe worktree root: {wt_root}")
    # A repo-local checkout should not make the parent repository dirty. Keep
    # this machine-local: do not edit the project's tracked .gitignore.
    if common_dir:
        exclude = Path(common_dir) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        # Git permits info/exclude to be a symlink (often to a shared file).
        # Serialize through our own no-follow lock, then open the actual Git
        # file normally so that supported configuration keeps working.
        with exclusive_lock(exclude.parent / "codexspin-exclude.lock"):
            with open(exclude, "a+") as fh:
                fh.seek(0)
                contents = fh.read()
                pattern = "/.worktrees/"
                if pattern not in contents.splitlines():
                    fh.seek(0, os.SEEK_END)
                    if contents and not contents.endswith("\n"):
                        fh.write("\n")
                    fh.write(pattern + "\n")
                    fh.flush()
    wt_path = str(wt_root / job_id)
    if Path(wt_path).exists() or Path(wt_path).is_symlink():
        raise SystemExit(f"codexspin: worktree path already exists: {wt_path}")
    branch = f"codexspin/{job_id}"
    added = git(repo_root, "worktree", "add", "-b", branch, wt_path, "HEAD")
    if added.returncode != 0:
        raise SystemExit(f"codexspin: worktree add failed:\n{added.stderr.strip()}")
    return {"repo_root": repo_root, "worktree": wt_path, "branch": branch,
            "git_common_dir": common_dir}


def _create_job(args) -> str:
    """Create the job dir, launch the detached runner, return the job id.
    Shared by `spawn` (prints the id) and `run` (waits on it)."""
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
    session_id = current_session_id()
    wt = create_worktree(cwd, job_id) if args.worktree else {}
    if wt:
        # Preserve a requested subdirectory: -C repo/pkg maps to <worktree>/pkg.
        rel = os.path.relpath(cwd, wt["repo_root"])
        cwd = os.path.normpath(os.path.join(wt["worktree"], rel))
    jd = job_dir(job_id)
    jd.mkdir(parents=True)
    writable_roots = list(args.writable_root or [])
    if sandbox == "workspace-write":
        # A linked worktree's git metadata (index, and the index.lock every
        # commit takes) lives outside the tree, so a sandbox scoped to cwd
        # cannot commit. Widen it to the shared git dir when that dir sits
        # outside cwd. Covers codexspin's own -w worktrees AND jobs pointed at
        # a pre-existing ("externally-managed") worktree via -C — the latter
        # never went through create_worktree, so resolve from cwd here. A
        # normal repo whose .git is already inside cwd needs nothing.
        common = wt.get("git_common_dir") or git_common_dir(cwd)
        if common and os.path.isdir(common) and not _is_within(common, cwd):
            writable_roots.append(common)
    herdr_on = getattr(args, "herdr", False) or \
        os.environ.get("CODEXSPIN_HERDR") in ("1", "true", "yes")
    herdr_info = _herdr_setup(cwd, f"cs:{args.name or job_id}") if herdr_on else {}
    write_json(jd / "job.json", {
        "job_id": job_id,
        "prompt": prompt,
        "cwd": cwd,
        "sandbox": sandbox,
        "model": args.model,
        "effort": args.effort,
        "max_minutes": args.max_minutes,
        "writable_roots": [os.path.realpath(r) for r in writable_roots],
        "created_at": time.time(),
        **({"session_id": session_id} if session_id else {}),
        **wt,
        **herdr_info,
    })
    write_json(jd / "state.json", {
        "job_id": job_id,
        "phase": "starting",
        "cwd": cwd,
        "sandbox": sandbox,
        "generation": 1,
        "attention_tracked": True,
        "prompt_preview": " ".join(prompt.split())[:120],
        "started_at": time.time(),
        "activity": "launching runner",
        **({"session_id": session_id} if session_id else {}),
        **({"branch": wt["branch"], "worktree": wt["worktree"], "repo_root": wt["repo_root"],
            "git_common_dir": wt["git_common_dir"]} if wt else {}),
    })
    pid = launch_runner(jd)
    state = read_json(jd / "state.json") or {}
    state["runner_pid"] = pid
    write_json(jd / "state.json", state)
    return job_id


def cmd_spawn(args) -> int:
    print(_create_job(args))
    return 0


def _await_and_print(job_id: str, json_out: bool, timeout: float | None) -> int:
    """Block until a job is terminal, then print its result. The job stays
    detached: Ctrl-C or a timeout leaves it running to re-attach later."""
    deadline = time.time() + timeout if timeout else None
    try:
        while True:
            state = load_state(job_id) or {}
            if state.get("phase") in TERMINAL_PHASES:
                break
            if deadline and time.time() > deadline:
                print(f"codexspin: {job_id} still running after {timeout}s — job continues; "
                      f"`codexspin await {job_id}`", file=sys.stderr)
                return 2
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\ncodexspin: detached — {job_id} still running "
              f"(`codexspin await {job_id}` to re-attach, `codexspin cancel {job_id}` to stop)",
              file=sys.stderr)
        return 130
    return cmd_result(argparse.Namespace(job=job_id, json=json_out))


def cmd_run(args) -> int:
    """spawn + await + result in one foreground command."""
    job_id = _create_job(args)
    print(f"codexspin: running {job_id}", file=sys.stderr)
    return _await_and_print(job_id, args.json, args.timeout)


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
STALE_SECONDS = 180
ATTENTION_STATES = frozenset({"urgent", "quiet", "review"})
ATTENTION_ORDER = {"urgent": 0, "quiet": 1, "review": 2}


def _heartbeat_line(s: dict, now: float | None = None) -> tuple[str, bool]:
    # Cheap: one stat, never a full read. Age comes from events.jsonl mtime
    # (bumped on every appended notification); count is persisted in state by
    # the runner so status never rescans an append-only log.
    now = time.time() if now is None else now
    try:
        stat = (job_dir(s["job_id"]) / "events.jsonl").stat()
    except OSError:
        stat = None
    if stat is None or stat.st_size == 0:
        age = int(max(0, now - (s.get("started_at") or now)))
        if age > STALE_SECONDS:
            quiet_age = f"{age // 60}m" if age < 3600 else f"{age // 3600}h"
            return f"⚠ quiet {quiet_age} — no events yet", True
        return "heartbeat: no events yet", False
    event_count = s.get("event_count", 0)
    # events.jsonl spans every resumed turn. A fresh turn starts healthy even
    # when the previous turn's last event is old.
    baseline = max(stat.st_mtime, s.get("started_at") or 0)
    age = int(max(0, now - baseline))
    if age > STALE_SECONDS:
        quiet_age = f"{age // 60}m" if age < 3600 else f"{age // 3600}h"
        return (f"⚠ quiet {quiet_age} — last event {age}s ago · "
                f"{event_count} events"), True
    return f"heartbeat: last event {age}s ago · {event_count} events", False


def _is_quiet(s: dict, now: float | None = None) -> bool:
    """Whether a live job has gone longer than the heartbeat threshold."""
    return _heartbeat_line(s, now)[1]


def attention_state(s: dict, now: float | None = None) -> tuple[str, float | None]:
    """Derive presentation attention without changing execution phase.

    Terminal failures and successful completions need acknowledgement once per
    turn. Live jobs are either working or quiet. Cancelled and acknowledged
    terminal jobs are seen.
    """
    phase = s.get("phase")
    attention = attention_record(s["job_id"])
    seen_at = attention.get("viewed_at")
    if not isinstance(seen_at, (int, float)):
        seen_at = None
    if phase not in TERMINAL_PHASES:
        return ("quiet" if _is_quiet(s, now) else "working"), seen_at
    # Jobs created before attention tracking existed should not flood the
    # first inbox after an upgrade. Resuming one stamps attention_tracked and
    # its next completion becomes visible normally.
    if not s.get("attention_tracked") and not attention:
        return "seen", seen_at
    if is_archived(s):
        return "seen", seen_at
    generation = s.get("generation")
    viewed_generation = attention.get("viewed_generation")
    if isinstance(generation, int) and not isinstance(generation, bool):
        unseen = (not isinstance(viewed_generation, int)
                  or isinstance(viewed_generation, bool)
                  or generation > viewed_generation)
    elif phase != "died" and isinstance(s.get("turn_id"), str):
        unseen = attention.get("viewed_turn_id") != s["turn_id"]
    else:
        # Legacy jobs created before generation counters use timestamps as a
        # best-effort fallback. New jobs and every resumed turn are generation
        # based, which remains correct across host clock skew.
        finished_at = s.get("finished_at")
        viewed_finished_at = attention.get("viewed_result_finished_at")
        if isinstance(finished_at, (int, float)):
            unseen = (not isinstance(viewed_finished_at, (int, float))
                      or finished_at > viewed_finished_at)
        else:
            markers = [value for value in (
                viewed_finished_at, attention.get("viewed_run_started_at"),
            ) if isinstance(value, (int, float))]
            current_started_at = s.get("started_at")
            unseen = (not markers or not isinstance(current_started_at, (int, float))
                      or current_started_at > max(markers))
    if unseen and phase in {"failed", "died", "timeout"}:
        return "urgent", seen_at
    if unseen and phase == "done":
        return "review", seen_at
    return "seen", seen_at


def _with_attention(s: dict, now: float | None = None) -> dict:
    annotated = dict(s)
    attention, seen_at = attention_state(s, now)
    annotated["attention"] = attention
    if seen_at is not None:
        annotated["viewed_at"] = seen_at
    return annotated


def _terminal_reason(s: dict) -> str:
    # Prefer a persisted error (failed/timeout write one); otherwise give a
    # phase-specific cause — never leak an ambiguous activity like "finished".
    result = read_json(job_dir(s["job_id"]) / "result.json") or {}
    error = result.get("error") or {}
    message = error.get("message") if isinstance(error, dict) else None
    if message:
        return " ".join(str(message).split())
    phase = s.get("phase")
    if phase == "cancelled":
        return "cancelled"
    if phase == "died":
        return "runner exited without finishing (see codexspin logs)"
    if phase == "timeout":
        return "exceeded max runtime"
    return " ".join(str(s.get("activity") or "unknown").split())


def _truncate_line(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[:max(0, width - 1)] + "…"


def styled_sandbox(st: Style, sandbox: str) -> str:
    if sandbox == "danger-full-access":
        return st.red(st.bold("yolo"))
    if sandbox == "workspace-write":
        return st.yellow("write")
    return st.green(sandbox or "?")


def styled_attention(st: Style, attention: str | None) -> str:
    if attention == "urgent":
        return st.red(st.bold("urgent"))
    if attention == "quiet":
        return st.yellow("quiet")
    if attention == "review":
        return st.green("review")
    return ""


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
    attention = styled_attention(st, s.get("attention"))
    if attention:
        head += f"  [{attention}]"
    print(head)
    print(f"  {st.dim(s.get('prompt_preview', '')[:width - 4])}")
    if not finished:
        print(f"  {st.cyan('↳ ' + str(s.get('activity', ''))[:width - 6])}")
        heartbeat, stale = _heartbeat_line(s)
        heartbeat = _truncate_line(heartbeat, width - 2)
        print(f"  {(st.yellow if stale else st.dim)(heartbeat)}")
    elif phase != "done":
        reason = _truncate_line(f"reason: {_terminal_reason(s)}", width - 2)
        print(f"  {paint(reason)}")
    meta = []
    if s.get("branch"):
        meta.append(f"branch {s['branch']}")
    if owned_by_other_session(s, current_session_id()):
        meta.append(f"session {s['session_id'][:8]}")
    if s.get("thread_id"):
        meta.append(f"resume: codex resume {s['thread_id']}")
    if meta:
        print(f"  {st.gray('  '.join(meta))}")
    print()


def print_job_plain(s: dict, width: int = 120) -> None:
    started = s.get("started_at") or 0
    end = s.get("finished_at") or time.time()
    line = (
        f"{s['job_id']:34s} {s.get('phase', '?'):9s} {fmt_elapsed(end - started):>7s}  "
        f"[{s.get('sandbox', '?')}] {Path(s.get('cwd', '')).name}"
    )
    if s.get("model"):
        line += f"  {s['model']}/{s.get('effort', '?')}"
    if s.get("attention") in ATTENTION_STATES:
        line += f"  [{s['attention']}]"
    print(line)
    print(f"  {s.get('prompt_preview', '')}")
    if s.get("phase") not in TERMINAL_PHASES:
        print(f"  ↳ {s.get('activity', '')}")
        heartbeat, _ = _heartbeat_line(s)
        print(f"  {_truncate_line(heartbeat, width - 2)}")
    elif s.get("phase") != "done":
        reason = _truncate_line(f"reason: {_terminal_reason(s)}", width - 2)
        print(f"  {reason}")
    if s.get("branch"):
        print(f"  branch: {s['branch']}  worktree: {s.get('worktree', '')}")
    if owned_by_other_session(s, current_session_id()):
        print(f"  session: {s['session_id'][:8]}")
    if s.get("thread_id"):
        print(f"  resume: codex resume {s['thread_id']}")


def _other_sessions_line(others: list[dict]) -> str:
    count = len(others)
    sessions = len({s.get("session_id") for s in others})
    running = sum(1 for s in others if s.get("phase") not in TERMINAL_PHASES)
    running_note = f", {running} running" if running else ""
    return (f"+ {count} job{'s' if count != 1 else ''} from {sessions} other "
            f"Claude session{'s' if sessions != 1 else ''}{running_note}"
            f" — codexspin status --all-sessions")


def _working_line(count: int) -> str:
    return f"+ {count} working job{'s' if count != 1 else ''} — codexspin status"


def _status_states(args) -> tuple[list[dict], list[dict], int]:
    others: list[dict] = []
    if args.job:
        # Explicit job id: cross-session escape hatch, never filtered.
        states = [s for s in [load_state(resolve_job_id(args.job))] if s]
    else:
        states = list_jobs()
        if not args.all:
            states = [s for s in states if not is_archived(s)]
        # Attention is an inbox, so unseen old results must not disappear at
        # the normal 24-hour status cutoff.
        if not args.all and not args.attention:
            cutoff = time.time() - 24 * 3600
            states = [s for s in states
                      if s.get("phase") not in TERMINAL_PHASES or (s.get("started_at") or 0) > cutoff]
        session = None if args.all_sessions else current_session_id()
        if session:
            others = [s for s in states if owned_by_other_session(s, session)]
            states = [s for s in states if not owned_by_other_session(s, session)]

    now = time.time()
    states = [_with_attention(s, now) for s in states]
    others = [_with_attention(s, now) for s in others]
    working = sum(1 for s in states if s["attention"] == "working")
    if args.attention:
        states = [s for s in states if s["attention"] in ATTENTION_STATES]
        states.sort(key=lambda s: ATTENTION_ORDER[s["attention"]])
        # Keep foreign-session summaries useful without counting acknowledged
        # terminal history that attention mode intentionally hides.
        others = [s for s in others
                  if s["attention"] in ATTENTION_STATES or s["attention"] == "working"]
    return states, others, working


def _print_status_once(args) -> int:
    states, others, working = _status_states(args)
    if args.json:
        print(json.dumps(states, indent=2))
        return 0
    if not states:
        printed = False
        if args.attention and working:
            print(_working_line(working))
            printed = True
        if others:
            if printed:
                print()
            print(_other_sessions_line(others))
            return 0
        if printed:
            return 0
        if args.attention:
            print("no jobs need attention")
        else:
            print("no jobs (use --all to include old finished jobs)")
        return 0
    fancy = use_color()
    st = Style(fancy)
    width = shutil.get_terminal_size((120, 24)).columns if fancy else 120
    for s in states:
        if fancy:
            print_job_fancy(st, s, width)
        else:
            print_job_plain(s, width)
    if args.attention and working:
        prefix = "" if fancy else "\n"
        print(f"{prefix}{st.gray(_working_line(working))}")
    # Quota is account-wide: hidden foreign jobs stay eligible for the freshest
    # snapshot even though their details are not rendered.
    quota = None
    if not args.attention:
        for s in [*states, *others]:
            q = s.get("quota")
            if q and (quota is None or (q.get("at") or 0) > (quota.get("at") or 0)):
                quota = q
        if quota:
            prefix = "" if fancy else "\n"
            print(f"{prefix}{quota_line(st, quota, fancy)}")
    if others:
        prefix = "" if fancy else "\n"
        print(f"{prefix}{st.gray(_other_sessions_line(others))}")
    return 0


def cmd_status(args) -> int:
    if args.watch and args.json:
        raise SystemExit("codexspin: --watch cannot be combined with --json")
    if not args.watch:
        return _print_status_once(args)

    first = True
    try:
        while True:
            if sys.stdout.isatty():
                print("\033[H\033[2J", end="")
            elif not first:
                print("\n---")
            _print_status_once(args)
            sys.stdout.flush()
            first = False
            time.sleep(1)
    except KeyboardInterrupt:
        return 130


def cmd_result(args) -> int:
    job_id = resolve_job_id(args.job)
    jd = job_dir(job_id)
    # Serialize with send's result invalidation + runner launch. Otherwise a
    # transient state carrying the prior dead runner PID can look synthesized-
    # died and acknowledge a generation that is actually about to run.
    with exclusive_lock(jd / "cli.lock"):
        result = read_json(jd / "result.json")
        state = load_state(job_id)
        phase = (state or {}).get("phase", "unknown")
        # Runner.finish writes result.json before publishing terminal state.
        # If completion landed between our two reads, pick up and present that
        # result instead of acknowledging a resultless outcome.
        if result is None and phase in TERMINAL_PHASES:
            result = read_json(jd / "result.json")
        if result is None and phase in TERMINAL_PHASES:
            # Keep this under cli.lock: otherwise a concurrent send can start
            # a new runner and make old-death compaction wait for its full turn.
            compact_terminal_events(job_id)
    if result is None:
        print(f"codexspin: {job_id} has no result yet (phase: {phase})", file=sys.stderr)
        sys.stderr.flush()
        # A synthesized died state may have no result.json. Acknowledge only
        # after its human-facing output was successfully delivered.
        if phase in TERMINAL_PHASES:
            mark_viewed(
                job_id,
                generation=(state or {}).get("generation"),
                turn_id=(state or {}).get("turn_id"),
                result_finished_at=(state or {}).get("finished_at"),
                run_started_at=(state or {}).get("started_at"),
            )
        return 3
    if args.json:
        print(json.dumps(result, indent=2))
    else:
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
    sys.stdout.flush()
    mark_viewed(
        job_id,
        generation=result.get("generation"),
        turn_id=result.get("turn_id"),
        result_finished_at=result.get("finished_at"),
        run_started_at=result.get("started_at"),
    )
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
    with exclusive_lock(jd / "cli.lock"):
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
        generation = state.get("generation")
        generation = generation if isinstance(generation, int) and generation > 0 else 0
        state.update(phase="starting", activity="resuming thread",
                     generation=generation + 1,
                     attention_tracked=True,
                     prompt_preview=" ".join(prompt.split())[:120], started_at=time.time())
        state.pop("finished_at", None)
        state.pop("turn_id", None)
        state.pop("runner_pid", None)
        write_json(jd / "state.json", state)
        pid = launch_runner(jd, resume=True)
        state["runner_pid"] = pid
        write_json(jd / "state.json", state)
    if getattr(args, "wait", False):
        print(f"codexspin: resuming {job_id}", file=sys.stderr)
        return _await_and_print(job_id, getattr(args, "json", False), getattr(args, "timeout", None))
    print(job_id)
    print(f"codexspin: {job_id} resumed (async) — `codexspin await {job_id}` for the result, "
          f"or use `send --wait`", file=sys.stderr)
    return 0


def cmd_transfer(args) -> int:
    try:
        result = transfer_claude_session(args.source, os.getcwd())
    except TransferError as exc:
        raise SystemExit(f"codexspin: {exc}") from exc

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Transferred the Claude session into a Codex thread with visible turn history.")
        print(f"Codex session ID: {result['threadId']}")
        print(f"Resume in Codex: {result['resumeCommand']}")
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
        # With no input_text, force DEVNULL so ssh/rsync never inherit and
        # consume the caller's stdin.
        stdin = None if input_text is not None else subprocess.DEVNULL
        return subprocess.run(command, input=input_text, stdin=stdin,
                              capture_output=True, text=True)
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
    with exclusive_lock(job_dir(job_id) / "cli.lock"):
        return _handoff_locked(args, job_id)


def _handoff_locked(args, job_id: str) -> int:
    validate_host(args.host)
    state = load_state(job_id)
    if state is None:
        raise SystemExit(f"codexspin: cannot read state for {job_id}")

    # A job that has not recorded a thread yet cannot be resumed anywhere;
    # cancelling it first would just destroy it with nothing to hand off.
    if not state.get("thread_id"):
        raise SystemExit(f"codexspin: {job_id} has no thread yet ({state.get('phase', '?')}); "
                         "wait for it to start or cancel it yourself")

    if state.get("phase") not in TERMINAL_PHASES:
        _cancel_locked(argparse.Namespace(job=job_id, hard=False), job_id)
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
    with exclusive_lock(job_dir(job_id) / "cli.lock"):
        return _cancel_locked(args, job_id)


def _cancel_locked(args, job_id: str) -> int:
    state = load_state(job_id) or {}
    pid = state.get("runner_pid")
    if state.get("phase") in TERMINAL_PHASES or not pid:
        if state.get("phase") in TERMINAL_PHASES:
            compact_terminal_events(job_id)
        print(f"codexspin: {job_id} is not running (phase: {state.get('phase', 'unknown')})")
        return 0
    if not pid_is_runner(pid):
        state.update(phase="died", activity="runner gone before cancel", finished_at=time.time())
        write_json(job_dir(job_id) / "state.json", state)
        compact_terminal_events(job_id)
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
    deadline = time.time() + (2 if args.hard else 6)
    while time.time() < deadline:
        state = load_state(job_id) or {}
        if state.get("phase") in TERMINAL_PHASES:
            break
        time.sleep(0.2)
    state = load_state(job_id) or state
    if state.get("phase") not in ("done", "failed", "cancelled"):
        state.update(phase="cancelled", activity="killed", finished_at=time.time())
        write_json(job_dir(job_id) / "state.json", state)
    compact_terminal_events(job_id)
    print(f"cancelled {job_id}")
    return 0


def _describe_event(msg: dict) -> str | None:
    """A human line for one event — the actual content, not the id-laden JSON.
    Returns None for events not worth showing in the readable log."""
    method = msg.get("method", "")
    p = msg.get("params") or {}
    if "outputDelta" in method:
        chunk = p.get("delta") or p.get("chunk") or p.get("text") or p.get("output") or ""
        chunk = chunk.rstrip("\n")
        return f"  ┆ {chunk}" if chunk else None
    item = p.get("item") or {}
    kind = item.get("type")
    if method == "item/started" and kind == "commandExecution":
        return f"$ {str(item.get('command', '')).strip()}"
    if method == "item/completed":
        if kind == "commandExecution":
            return f"↳ exit {item.get('exitCode', '?')}"
        if kind == "agentMessage" and item.get("text"):
            return f"« {item['text'].strip()}"
        if kind == "fileChange":
            paths = []
            for c in item.get("changes") or []:
                k = c.get("kind")
                paths.append((k.get("move_path") if isinstance(k, dict) else None) or c.get("path") or "?")
            return f"± {', '.join(paths)}" if paths else None
        if kind == "webSearch":
            return f"web: {item.get('query', '')}"
        if kind == "mcpToolCall":
            return f"mcp: {item.get('server', '')}.{item.get('tool', '')}"
    if method == "turn/started":
        return "— turn started"
    if method == "turn/completed":
        return f"— turn {(p.get('turn') or {}).get('status', 'completed')}"
    if method == "error":
        return f"! error: {(p.get('error') or {}).get('message', '')[:200]}"
    return None


def cmd_logs(args) -> int:
    job_id = resolve_job_id(args.job)
    events = job_dir(job_id) / "events.jsonl"
    rotated = events.with_name(events.name + ".1")
    contents = None
    # Rotation uses atomic renames. Retry a changing snapshot rather than
    # blocking live `logs` behind the runner's lifetime event-log lock.
    for _attempt in range(3):
        paths = [path for path in (rotated, events) if path.exists()]
        try:
            before = [(path, path.stat().st_ino, path.stat().st_size,
                       path.stat().st_mtime_ns) for path in paths]
            candidate = [(path, path.read_text()) for path in paths]
            after = [(path, path.stat().st_ino, path.stat().st_size,
                      path.stat().st_mtime_ns) for path in paths]
        except OSError:
            continue
        if before == after:
            contents = candidate
            break
    if contents is None:
        contents = []
        for path in (rotated, events):
            try:
                contents.append((path, path.read_text()))
            except OSError:
                continue
    if not contents:
        print("(no events yet)")
        return 0
    lines = []
    for _path, content in reversed(contents):
        lines[0:0] = content.splitlines()[-args.lines:]
        if len(lines) >= args.lines:
            lines = lines[-args.lines:]
            break
    for line in lines:
        if args.json:
            print(line)
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        desc = _describe_event(msg)
        if desc is not None:
            print(desc)
    return 0


def cmd_archive(args) -> int:
    archived = 0
    for prefix in args.job:
        job_id = resolve_job_id(prefix)
        jd = job_dir(job_id)
        with exclusive_lock(jd / "cli.lock"):
            state = load_state(job_id) or {}
            if state.get("phase") not in TERMINAL_PHASES:
                print(f"codexspin: {job_id} is {state.get('phase', 'unknown')}, not archiving")
                continue
            # Existing pre-cap jobs can be large. Archive keeps the structured
            # result and native resume id, but reduces raw diagnostics to the
            # same bounded terminal tail as newly completed jobs.
            compact_terminal_events(job_id)
            mark_viewed(
                job_id,
                generation=state.get("generation"),
                turn_id=state.get("turn_id"),
                result_finished_at=state.get("finished_at"),
                run_started_at=state.get("started_at"),
            )
            mark_archived(
                job_id,
                generation=state.get("generation"),
                result_finished_at=state.get("finished_at"),
            )
            archived += 1
            print(f"archived {job_id}")
    return 0 if archived == len(args.job) else 1


def compact_terminal_events(job_id: str) -> bool:
    """Apply the terminal cap when no runner still owns the active log.

    Returns False instead of waiting behind a stuck runner. Live logs are
    already bounded, and a runner that eventually exits compacts in finally.
    Callers serialize this check with send through cli.lock.
    """
    from .runner import BoundedEventLog

    state = read_json(job_dir(job_id) / "state.json") or {}
    if pid_is_runner(state.get("runner_pid")):
        return False
    event_log = BoundedEventLog(job_dir(job_id))
    event_log.close(terminal=True)
    return True


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
    if args.job:
        # Explicit: remove exactly these finished jobs, ignore all scoping.
        targets = []
        for prefix in args.job:
            state = load_state(resolve_job_id(prefix))
            if state and state.get("phase") in TERMINAL_PHASES:
                targets.append(state)
            elif state:
                print(f"codexspin: {state['job_id']} is {state.get('phase')}, not removing")
    else:
        # Sweep finished jobs older than keep-days — but ONLY those whose cwd is
        # under this directory, so `gc` in one project can't nuke another's (or
        # another session's) jobs. --everywhere opts into the machine-wide sweep.
        cutoff = time.time() - args.keep_days * 24 * 3600
        here = os.path.realpath(os.getcwd())
        targets = []
        for state in list_jobs():
            if state.get("phase") not in TERMINAL_PHASES:
                continue
            if (state.get("finished_at") or state.get("started_at") or 0) >= cutoff:
                continue
            job_cwd = os.path.realpath(state.get("repo_root") or state.get("cwd") or "/")
            if not args.everywhere and not (job_cwd == here or job_cwd.startswith(here + os.sep)):
                continue
            targets.append(state)

    removed, kept = 0, []
    for state in targets:
        if args.dry_run:
            print(f"would remove {state['job_id']} ({state.get('phase')})")
            continue
        if not remove_worktree(state):
            kept.append(state["job_id"])
            continue
        shutil.rmtree(job_dir(state["job_id"]), ignore_errors=True)
        removed += 1
    if args.dry_run:
        print(f"dry-run: {len(targets)} job(s) would be removed")
    else:
        scope = "" if (args.job or args.everywhere) else " under this directory"
        print(f"removed {removed} finished job(s){scope}")
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
    minor = _codex_minor(version.stdout)
    if minor and minor != TESTED_CODEX_MINOR:
        print(f"⚠ codexspin verified against codex {TESTED_CODEX_MINOR}.x; yours is "
              f"{minor}.x — app-server protocol shapes may have drifted, watch for "
              f"job failures and file an issue if so")
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

    def add_spawn_args(p):
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
        p.add_argument("--writable-root", action="append", metavar="DIR",
                       help="extra writable dir for the workspace-write sandbox (repeatable)")
        p.add_argument("--herdr", action="store_true",
                       help="show this job as a native codex agent in herdr's agent panel "
                            "(also enabled by CODEXSPIN_HERDR=1)")
        _add_host_argument(p)

    p = sub.add_parser("spawn", help="spawn a detached codex job")
    add_spawn_args(p)
    p.set_defaults(fn=cmd_spawn)

    p = sub.add_parser("run", help="spawn + wait + print result in one foreground command")
    add_spawn_args(p)
    p.add_argument("--timeout", type=float, default=None, help="give up waiting after N seconds (job keeps running)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="show jobs (running + last 24h by default)")
    p.add_argument("job", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--all-sessions", action="store_true",
                   help="include jobs owned by other Claude sessions "
                        "(default when not inside a Claude session)")
    p.add_argument("--attention", action="store_true",
                   help="show urgent, quiet, and completed-unseen jobs plus a working count")
    p.add_argument("--watch", action="store_true",
                   help="refresh status every second until interrupted")
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
    p.add_argument("--wait", action="store_true", help="block and print the result (like run)")
    p.add_argument("--timeout", type=float, default=None, help="with --wait, give up after N seconds")
    p.add_argument("--json", action="store_true", help="with --wait, print result as JSON")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser(
        "transfer", help="import a Claude Code session into a persistent Codex thread"
    )
    p.add_argument("--source", metavar="CLAUDE_JSONL",
                   help="Claude transcript (default: current plugin session)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_transfer)

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
    p.add_argument("--json", action="store_true", help="print raw event JSON instead of readable lines")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("archive", help="hide finished jobs without deleting resume metadata")
    p.add_argument("job", nargs="+", help="finished job id(s) to archive")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("doctor", help="check codex binary, app-server handshake, auth, defaults")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("gc", help="delete finished jobs (this project by default)")
    p.add_argument("job", nargs="*", help="specific job id(s) to remove; omit to sweep by age")
    p.add_argument("--keep-days", type=int, default=7)
    p.add_argument("--everywhere", action="store_true",
                   help="sweep jobs from all projects, not just this directory")
    p.add_argument("--dry-run", action="store_true", help="show what would be removed")
    _add_host_argument(p)
    p.set_defaults(fn=cmd_gc)

    args = parser.parse_args([_Arg(value, index) for index, value in enumerate(raw_argv)])
    if getattr(args, "remote_host", None) is not None:
        return _run_remote(args, raw_argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
