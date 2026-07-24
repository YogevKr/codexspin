"""Detached per-job runner: owns one app-server process, drives one turn.

Invoked as `python -m codexspin.runner <job-dir> [--resume]` by the CLI,
detached into its own session. SIGTERM triggers turn/interrupt and marks the
job cancelled.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .appserver import AppServerClient, AppServerError
from .jobs import TERMINAL_PHASES, exclusive_lock, read_json, write_json

STARTUP_TIMEOUT = float(os.environ.get("CODEXSPIN_STARTUP_TIMEOUT", "180"))
EVENTS_MAX_BYTES = int(os.environ.get("CODEXSPIN_EVENTS_MAX_BYTES", "10000000"))
EVENTS_TERMINAL_BYTES = int(os.environ.get("CODEXSPIN_EVENTS_TERMINAL_BYTES", "1000000"))


class BoundedEventLog:
    """Two-segment active log, compacted to a small tail on completion."""

    def __init__(self, job_path: Path, max_bytes: int = EVENTS_MAX_BYTES,
                 terminal_bytes: int = EVENTS_TERMINAL_BYTES):
        self._mutex = threading.Lock()
        self._closed = False
        self._lock_context = exclusive_lock(job_path / "events.lock")
        self._process_lock = self._lock_context.__enter__()
        try:
            self.path = job_path / "events.jsonl"
            self.rotated = self.path.with_name(self.path.name + ".1")
            self.max_bytes = max(512, max_bytes)
            self.segment_bytes = max(1, self.max_bytes // 2)
            self.terminal_bytes = max(256, min(terminal_bytes, self.max_bytes))
            # Normalize legacy/unbounded logs before appending. Starting with
            # one segment leaves room for the next without exceeding the cap.
            self._compact(self.segment_bytes)
            self.file = open(self.path, "a", buffering=1)
        except Exception:
            self._lock_context.__exit__(*sys.exc_info())
            raise

    @staticmethod
    def _tail(paths: list[Path], limit: int) -> bytes:
        sizes = [path.stat().st_size if path.exists() else 0 for path in paths]
        total = sum(sizes)
        remaining = limit if total <= limit else limit + 1
        chunks = []
        for path, size in reversed(list(zip(paths, sizes))):
            if not size or not remaining:
                continue
            take = min(size, remaining)
            with open(path, "rb") as fh:
                fh.seek(size - take)
                chunks.append(fh.read(take))
            remaining -= take
        data = b"".join(reversed(chunks))
        if total <= limit:
            return data
        # Read one byte before the retained window. A newline proves the
        # window begins at a JSONL boundary; otherwise discard the partial
        # first record. Legacy logs may contain one final record larger than
        # the entire window, in which case retain a valid diagnostic marker.
        preceding, retained = data[:1], data[1:]
        if preceding == b"\n":
            return retained
        newline = retained.find(b"\n")
        if 0 <= newline < len(retained) - 1:
            return retained[newline + 1:]
        return (json.dumps({
            "method": "codexspin/event-truncated",
            "params": {"reason": "legacy event exceeds retained tail"},
        }) + "\n").encode()

    def _compact(self, limit: int) -> None:
        paths = [self.rotated, self.path]
        if sum(path.stat().st_size for path in paths if path.exists()) <= limit:
            return
        data = self._tail(paths, limit)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path)
        self.rotated.unlink(missing_ok=True)

    def _rotate(self) -> None:
        self.file.close()
        if self.path.exists():
            self.path.replace(self.rotated)
        self.file = open(self.path, "w", buffering=1)

    def write(self, msg: dict) -> None:
        with self._mutex:
            if self._closed:
                raise OSError("event log is closed")
            line = json.dumps(msg) + "\n"
            encoded_bytes = len(line.encode())
            record_limit = min(self.segment_bytes, self.terminal_bytes)
            if encoded_bytes > record_limit:
                line = json.dumps({
                    "method": "codexspin/event-truncated",
                    "params": {
                        "originalMethod": msg.get("method"),
                        "originalBytes": encoded_bytes,
                    },
                }) + "\n"
                encoded_bytes = len(line.encode())
            if self.file.tell() and self.file.tell() + encoded_bytes > self.segment_bytes:
                self._rotate()
            self.file.write(line)

    def close(self, terminal: bool = False) -> None:
        with self._mutex:
            if self._closed:
                return
            try:
                if not self.file.closed:
                    self.file.close()
                if terminal:
                    self._compact(self.terminal_bytes)
            finally:
                self._closed = True
                self._lock_context.__exit__(None, None, None)


class Runner:
    def __init__(self, job_path: Path, resume: bool):
        self.dir = job_path
        self.resume = resume
        self.spec = read_json(job_path / "job.json") or {}
        self.state = read_json(job_path / "state.json") or {}
        self.events = BoundedEventLog(job_path)
        self.log = open(job_path / "runner.log", "a", buffering=1)
        self.client: AppServerClient | None = None
        self.thread_id: str | None = self.state.get("thread_id") if resume else None
        self.turn_id: str | None = None
        self.turn_done = threading.Event()
        self.final_turn: dict = {}
        self.last_agent_message = ""
        self.turn_error: dict | None = None
        self.touched_files: list[str] = []
        self.command_count = 0
        self.event_count = self.state.get("event_count", 0)
        self.cancelled = False
        self.timed_out = False
        self._state_lock = threading.Lock()
        self._finished = False
        self._herdr_last_key: tuple | None = None

    def logline(self, msg: str) -> None:
        self.log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")

    def _herdr_maybe_report(self) -> None:
        """When the job was spawned with herdr mirroring (job.json carries
        herdr_pane_id + herdr_bin), surface it in herdr's agent panel as a NATIVE
        codex agent — same report-agent API the built-in codex integration uses,
        but driven from this job's real turn events and linked to its real codex
        thread. Best-effort and non-blocking: a herdr hiccup must never touch the
        job. Only fires on a state transition (working<->idle) or once the real
        thread id is known, so it stays ~3 calls per job, not per event."""
        pane = self.spec.get("herdr_pane_id")
        herdr = self.spec.get("herdr_bin")
        if not pane or not herdr:
            return
        desired = "idle" if self.state.get("phase") in TERMINAL_PHASES else "working"
        key = (desired, bool(self.thread_id))
        if key == self._herdr_last_key:
            return
        self._herdr_last_key = key
        args = [herdr, "pane", "report-agent", pane,
                "--source", "herdr:codexspin", "--agent", "codex",
                "--state", desired, "--seq", str(time.time_ns())]
        if self.thread_id:
            args += ["--agent-session-id", self.thread_id]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _herdr_close(self) -> None:
        """Close the job's herdr workspace once the runner is done, so finished
        jobs don't pile up in the agent panel. A short grace (CODEXSPIN_HERDR_
        CLOSE_DELAY seconds, default 3) lets the final done state + notification
        propagate first; set it negative to keep the pane open. The worktree is
        untouched — reopen it any time with `csws`. Best-effort."""
        pane = self.spec.get("herdr_pane_id")
        herdr = self.spec.get("herdr_bin")
        if not pane or not herdr:
            return
        try:
            delay = float(os.environ.get("CODEXSPIN_HERDR_CLOSE_DELAY", "3"))
        except ValueError:
            delay = 3.0
        if delay < 0:
            return
        if delay:
            time.sleep(delay)
        try:
            subprocess.run([herdr, "workspace", "close", pane.split(":", 1)[0]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            pass

    def set_state(self, **updates) -> None:
        # The deadline timer, the stdout notification thread, and the main
        # thread all write state; serialize so a snapshot is never taken
        # mid-mutation.
        with self._state_lock:
            if self._finished:
                return
            self.state.update(updates, updated_at=time.time(), event_count=self.event_count)
            write_json(self.dir / "state.json", dict(self.state))
            self._herdr_maybe_report()

    def is_own_thread(self, params: dict) -> bool:
        """Whether a notification speaks for this job.

        Codex runs delegated sub-agents as separate threads whose turn and item
        lifecycles are multiplexed over this same connection. Only the job's own
        thread ends the job or supplies its answer — a sub-agent finishing (or
        being interrupted) says nothing about the main turn. A payload carrying
        no threadId is treated as ours.
        """
        thread_id = params.get("threadId")
        return thread_id is None or thread_id == self.thread_id

    def on_notification(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "turn/completed" and self.is_own_thread(params):
            self.final_turn = params.get("turn") or {}

        self.event_count += 1
        try:
            self.events.write(msg)
        except OSError as exc:
            try:
                self.logline(f"failed to write notification event: {exc}")
            except OSError:
                pass

        try:
            if method == "turn/started" and not self.turn_id and self.is_own_thread(params):
                self.turn_id = (params.get("turn") or {}).get("id")
                self.set_state(phase="running", turn_id=self.turn_id, activity="turn started")
            elif method == "item/started":
                item = params.get("item") or {}
                desc = self.describe_item(item)
                if desc:
                    self.set_state(activity=desc)
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    # A sub-agent's report to its caller is not the job's answer.
                    if self.is_own_thread(params):
                        self.last_agent_message = item["text"]
                elif item.get("type") == "fileChange":
                    # Deliberately unfiltered by thread: a sub-agent's edits and
                    # commands land in the job's own tree, so they are the job's.
                    for change in item.get("changes") or []:
                        # A move/rename reports the OLD path plus kind.move_path
                        # for the new one; record the destination the user will
                        # actually find on disk.
                        kind = change.get("kind")
                        dest = kind.get("move_path") if isinstance(kind, dict) else None
                        path = dest or change.get("path")
                        if path and path not in self.touched_files:
                            self.touched_files.append(path)
                elif item.get("type") == "commandExecution":
                    self.command_count += 1
            elif method == "error":
                error = params.get("error") or {}
                # codex 0.144 puts willRetry at params level, next to the error;
                # accept the nested spot too in case the shape moves.
                if params.get("willRetry") or error.get("willRetry"):
                    self.set_state(activity=f"transient error, retrying: {str(error.get('message', ''))[:150]}")
                else:
                    # Surface any thread's error as activity, but only our own
                    # can fail the job — the main agent may handle a sub-agent's.
                    if self.is_own_thread(params):
                        self.turn_error = error
                    self.set_state(activity=f"error: {str(error.get('message', ''))[:200]}")
            elif method == "account/rateLimits/updated":
                limits = (params.get("rateLimits") or {})
                primary = limits.get("primary") or {}
                if primary.get("usedPercent") is not None:
                    self.set_state(quota={
                        "used_percent": primary.get("usedPercent"),
                        "window_mins": primary.get("windowDurationMins"),
                        "plan": limits.get("planType"),
                        "at": time.time(),
                    })
        except OSError as exc:
            try:
                self.logline(f"failed to update state for {method}: {exc}")
            except OSError:
                pass
        if method == "turn/completed" and self.is_own_thread(params):
            # Publish completion only after the final notification is logged
            # and reflected in state. Runner shutdown can now drain safely.
            self.turn_done.set()

    @staticmethod
    def describe_item(item: dict) -> str | None:
        kind = item.get("type")
        if kind == "commandExecution":
            return f"$ {str(item.get('command', ''))[:120]}"
        if kind == "fileChange":
            return "editing files"
        if kind == "webSearch":
            return f"web search: {str(item.get('query', ''))[:80]}"
        if kind == "mcpToolCall":
            return f"mcp: {item.get('server', '')}.{item.get('tool', '')}"
        if kind == "reasoning":
            return "thinking"
        if kind == "agentMessage":
            return "writing answer"
        return None

    def interrupt_turn(self, reason: str) -> None:
        self.logline(f"{reason}: interrupting turn")
        if self.client and self.thread_id and self.turn_id:
            try:
                self.client.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=10)
                # Give codex a moment to emit turn/completed(interrupted) and
                # finish writing the session rollout, so the thread stays
                # cleanly resumable; the notification sets turn_done for us.
                self.turn_done.wait(timeout=5)
            except AppServerError as exc:
                self.logline(f"turn/interrupt failed: {exc}")
        elif self.client:
            # Still starting up: no turn to interrupt. Kill the app-server so
            # the pending request aborts instead of continuing toward a turn
            # nobody wants (and that cancel already reported as cancelled).
            self.client.close()
        self.turn_done.set()

    def handle_sigterm(self, *_args) -> None:
        self.cancelled = True
        self.interrupt_turn("SIGTERM received")

    def handle_deadline(self) -> None:
        if self.turn_done.is_set():
            return
        self.timed_out = True
        self.set_state(activity="max runtime reached, interrupting")
        self.interrupt_turn("deadline reached")

    def check_aborted(self) -> None:
        """Between startup steps: stop immediately if the deadline or a cancel
        fired while no turn existed yet to interrupt."""
        if self.timed_out or self.cancelled:
            raise AppServerError("aborted during startup")

    def handle_client_close(self) -> None:
        if not self.turn_done.is_set():
            stderr = "\n".join(self.client.stderr_tail[-10:]) if self.client else ""
            if not self.cancelled and not self.turn_error:
                self.turn_error = {"message": "app-server exited unexpectedly", "stderr": stderr}
            self.logline("app-server closed before turn completion")
            self.turn_done.set()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.handle_sigterm)
        spec = self.spec
        # The runtime budget covers the WHOLE job including startup — a
        # stalled app-server handshake must not extend --max-minutes.
        deadline_timer = None
        if spec.get("max_minutes"):
            deadline_timer = threading.Timer(spec["max_minutes"] * 60, self.handle_deadline)
            deadline_timer.daemon = True
            deadline_timer.start()
        try:
            self.set_state(phase="starting", activity="starting app-server", runner_pid=os.getpid())
            overrides = []
            if spec.get("writable_roots"):
                # Per-job app-server => per-job sandbox roots. Lets codex git-commit
                # in linked worktrees whose metadata lives outside the tree.
                overrides.append(
                    f"sandbox_workspace_write.writable_roots={json.dumps(spec['writable_roots'])}")
            self.client = AppServerClient(cwd=spec["cwd"], config_overrides=overrides)
            self.client.notification_handler = self.on_notification
            self.client.on_close = self.handle_client_close
            self.client.initialize()

            self.check_aborted()
            try:
                config = (self.client.request("config/read",
                                              {"includeLayers": False, "cwd": spec["cwd"]},
                                              timeout=min(30.0, STARTUP_TIMEOUT)).get("config") or {})
            except AppServerError:
                config = {}
            self.set_state(
                model=spec.get("model") or config.get("model") or "?",
                effort=spec.get("effort") or config.get("model_reasoning_effort") or "?",
            )

            thread_params = {
                "cwd": spec["cwd"],
                "model": spec.get("model"),
                "approvalPolicy": "never",
                "sandbox": spec["sandbox"],
                "serviceName": "codexspin",
                "ephemeral": False,
            }
            if self.resume and self.thread_id:
                thread_params = {"threadId": self.thread_id, **{k: v for k, v in thread_params.items() if k != "serviceName" and k != "ephemeral"}}
                result = self.client.request("thread/resume", thread_params, timeout=STARTUP_TIMEOUT)
            else:
                result = self.client.request("thread/start", thread_params, timeout=STARTUP_TIMEOUT)
                self.thread_id = ((result.get("thread") or {}).get("id")) or result.get("threadId")
            if not self.thread_id:
                raise AppServerError(f"no thread id in response: {json.dumps(result)[:300]}")
            self.set_state(phase="starting", thread_id=self.thread_id, activity="thread ready")

            self.check_aborted()
            self.client.request("turn/start", {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": spec["prompt"], "text_elements": []}],
                "model": spec.get("model"),
                "effort": spec.get("effort"),
                "outputSchema": None,
            }, timeout=STARTUP_TIMEOUT)

            self.turn_done.wait()
        except AppServerError as exc:
            if self.cancelled:
                self.finish("cancelled")
                return 0
            if self.timed_out:
                self.finish("timeout", error={"message": f"exceeded max runtime of {spec.get('max_minutes')} minutes during startup"})
                return 1
            stderr = "\n".join(self.client.stderr_tail[-10:]) if self.client else ""
            self.logline(f"failed: {exc}\n{stderr}")
            self.finish("failed", error={"message": str(exc), "stderr": stderr})
            return 1
        finally:
            if deadline_timer:
                deadline_timer.cancel()
            if self.client:
                self.client.close()

        if self.cancelled:
            self.finish("cancelled")
            return 0
        if self.timed_out:
            self.finish("timeout", error={"message": f"exceeded max runtime of {spec.get('max_minutes')} minutes"})
            return 1
        # The turn's own terminal status is authoritative: error notifications
        # that codex recovered from must not fail a completed turn.
        status = self.final_turn.get("status")
        if status == "completed" and not self.final_turn.get("error"):
            self.finish("done")
            return 0
        error = self.final_turn.get("error") or self.turn_error or {"message": f"turn status: {status}"}
        self.finish("failed", error=error)
        return 1

    def finish(self, phase: str, error: dict | None = None) -> None:
        result = {
            "phase": phase,
            "final_message": self.last_agent_message,
            "error": error,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "touched_files": self.touched_files,
            "command_count": self.command_count,
            "duration_ms": self.final_turn.get("durationMs"),
            "generation": self.state.get("generation"),
            "started_at": self.state.get("started_at"),
            "finished_at": time.time(),
        }
        write_json(self.dir / "result.json", result)
        with open(self.dir / "results.jsonl", "a") as fh:
            fh.write(json.dumps(result) + "\n")
        # Terminal publication is a barrier: reader-thread notifications that
        # drain during client shutdown may still be logged, but can no longer
        # revert the execution phase to running.
        with self._state_lock:
            self._finished = True
            self.state.update(
                phase=phase,
                activity="finished",
                finished_at=result["finished_at"],
                updated_at=time.time(),
                event_count=self.event_count,
            )
            write_json(self.dir / "state.json", dict(self.state))
            self._herdr_maybe_report()
        self.logline(f"finished: {phase}")


def main() -> int:
    job_path = Path(sys.argv[1])
    resume = "--resume" in sys.argv[2:]
    runner = Runner(job_path, resume)
    try:
        return runner.run()
    finally:
        state = read_json(job_path / "state.json") or {}
        runner.events.close(terminal=state.get("phase") in {
            "done", "failed", "cancelled", "died", "timeout",
        })
        runner.log.close()
        runner._herdr_close()


if __name__ == "__main__":
    sys.exit(main())
