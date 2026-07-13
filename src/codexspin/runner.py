"""Detached per-job runner: owns one app-server process, drives one turn.

Invoked as `python -m codexspin.runner <job-dir> [--resume]` by the CLI,
detached into its own session. SIGTERM triggers turn/interrupt and marks the
job cancelled.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .appserver import AppServerClient, AppServerError
from .jobs import read_json, write_json

STARTUP_TIMEOUT = float(os.environ.get("CODEXSPIN_STARTUP_TIMEOUT", "180"))


class Runner:
    def __init__(self, job_path: Path, resume: bool):
        self.dir = job_path
        self.resume = resume
        self.spec = read_json(job_path / "job.json") or {}
        self.state = read_json(job_path / "state.json") or {}
        self.events = open(job_path / "events.jsonl", "a", buffering=1)
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
        self.cancelled = False

    def logline(self, msg: str) -> None:
        self.log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")

    def set_state(self, **updates) -> None:
        self.state.update(updates, updated_at=time.time())
        write_json(self.dir / "state.json", self.state)

    def on_notification(self, msg: dict) -> None:
        self.events.write(json.dumps(msg) + "\n")
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "turn/started" and not self.turn_id:
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
                self.last_agent_message = item["text"]
            elif item.get("type") == "fileChange":
                for change in item.get("changes") or []:
                    path = change.get("path")
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
                self.turn_error = error
                self.set_state(activity=f"error: {str(error.get('message', ''))[:200]}")
        elif method == "turn/completed":
            self.final_turn = params.get("turn") or {}
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

    def handle_sigterm(self, *_args) -> None:
        self.cancelled = True
        self.logline("SIGTERM received, interrupting turn")
        if self.client and self.thread_id and self.turn_id:
            try:
                self.client.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=10)
            except AppServerError as exc:
                self.logline(f"turn/interrupt failed: {exc}")
        elif self.client:
            # Still starting up: no turn to interrupt. Kill the app-server so
            # the pending request aborts instead of continuing toward a turn
            # nobody wants (and that cancel already reported as cancelled).
            self.client.close()
        self.turn_done.set()

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
        try:
            self.set_state(phase="starting", activity="starting app-server", runner_pid=os.getpid())
            self.client = AppServerClient(cwd=spec["cwd"])
            self.client.notification_handler = self.on_notification
            self.client.on_close = self.handle_client_close
            self.client.initialize()

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
            stderr = "\n".join(self.client.stderr_tail[-10:]) if self.client else ""
            self.logline(f"failed: {exc}\n{stderr}")
            self.finish("failed", error={"message": str(exc), "stderr": stderr})
            return 1
        finally:
            if self.client:
                self.client.close()

        if self.cancelled:
            self.finish("cancelled")
            return 0
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
            "finished_at": time.time(),
        }
        write_json(self.dir / "result.json", result)
        with open(self.dir / "results.jsonl", "a") as fh:
            fh.write(json.dumps(result) + "\n")
        self.set_state(phase=phase, activity="finished", finished_at=result["finished_at"])
        self.logline(f"finished: {phase}")


def main() -> int:
    job_path = Path(sys.argv[1])
    resume = "--resume" in sys.argv[2:]
    return Runner(job_path, resume).run()


if __name__ == "__main__":
    sys.exit(main())
