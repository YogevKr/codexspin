"""Minimal JSON-RPC client for `codex app-server` (NDJSON over stdio).

Protocol verified against codex-cli 0.144.1:
  initialize -> initialized (notify) -> thread/start -> turn/start
  notifications: thread/started, turn/started, item/started, item/completed,
  turn/completed, error, thread/status/changed, account/rateLimits/updated, ...
  cancel: turn/interrupt {threadId, turnId}
  follow-up: thread/resume {threadId, cwd, approvalPolicy, sandbox} + turn/start
  Claude transfer: externalAgentConfig/import + externalAgentConfig/import/completed
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any, Callable

CLIENT_INFO = {"title": "codexspin", "name": "codexspin", "version": "0.2.0"}
CAPABILITIES = {
    "experimentalApi": False,
    "requestAttestation": False,
    "optOutNotificationMethods": [
        "item/agentMessage/delta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
    ],
}


class AppServerError(Exception):
    def __init__(self, message: str, data: Any = None):
        super().__init__(message)
        self.data = data


class AppServerClient:
    """One spawned `codex app-server` process, one client."""

    def __init__(self, cwd: str, env: dict[str, str] | None = None,
                 config_overrides: list[str] | None = None):
        codex_bin = os.environ.get("CODEXSPIN_CODEX_BIN", "codex")
        overrides = [arg for override in (config_overrides or []) for arg in ("-c", override)]
        self.proc = subprocess.Popen(
            [codex_bin, *overrides, "app-server"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, dict] = {}
        self._responses: dict[int, dict] = {}
        self._response_cv = threading.Condition()
        self.notification_handler: Callable[[dict], None] | None = None
        self.on_close: Callable[[], None] | None = None
        self.stderr_tail: list[str] = []
        self.closed = False

        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def initialize(self) -> dict:
        result = self.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": CAPABILITIES})
        self.notify("initialized", {})
        return result

    def request(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
        self._send({"id": msg_id, "method": method, "params": params})
        with self._response_cv:
            ok = self._response_cv.wait_for(
                lambda: msg_id in self._responses or self.closed, timeout=timeout
            )
        if msg_id not in self._responses:
            if self.closed:
                raise AppServerError(f"app-server exited before responding to {method}")
            if not ok:
                raise AppServerError(f"timed out waiting for {method} response ({timeout}s)")
        resp = self._responses.pop(msg_id)
        if resp.get("error"):
            err = resp["error"]
            raise AppServerError(err.get("message", f"{method} failed"), data=err)
        return resp.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def close(self) -> None:
        self.closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        # Process exit closes both pipes. Drain their reader threads before a
        # caller tears down notification-owned resources such as event logs.
        current = threading.current_thread()
        for reader in (self._stdout_thread, self._stderr_thread):
            if reader is not current:
                reader.join(timeout=5)

    def _send(self, message: dict) -> None:
        if self.closed or not self.proc.stdin:
            raise AppServerError("app-server connection is closed")
        line = json.dumps(message) + "\n"
        with self._lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                # The app-server died between our closed-check and this write.
                # Surface it as a typed error so callers (the runner) fail
                # cleanly instead of crashing on an OSError. Wake any other
                # requests already waiting for responses from the dead process.
                with self._response_cv:
                    self.closed = True
                    self._response_cv.notify_all()
                raise AppServerError(f"app-server connection lost: {exc}") from exc

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and "method" in msg:
                # Server-initiated request (e.g. approval ask). We run
                # approvalPolicy=never, so reject anything that slips through.
                try:
                    self._send({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}})
                except AppServerError:
                    pass
            elif "id" in msg:
                with self._response_cv:
                    self._responses[msg["id"]] = msg
                    self._response_cv.notify_all()
            elif "method" in msg and self.notification_handler:
                try:
                    self.notification_handler(msg)
                except Exception as exc:  # noqa: BLE001 — handler faults close the client cleanly
                    self.stderr_tail.append(f"[notification handler error] {exc}")
                    break
        self.closed = True
        with self._response_cv:
            self._response_cv.notify_all()
        if self.on_close:
            self.on_close()

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail.append(line.rstrip())
            if len(self.stderr_tail) > 50:
                self.stderr_tail.pop(0)
