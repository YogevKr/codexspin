#!/usr/bin/env python3
"""Fake `codex app-server` speaking the verified protocol on stdio.

Selected behavior via FAKE_MODE env var:
  ok       (default) one command item + agentMessage "FAKE-DONE", turn completes
  fail     turn completes with status=failed and an error notification
  slow     waits ~20s before completing; turn/interrupt short-circuits it
  hang     never answers thread/start
  die      answers turn/start, then the whole server exits mid-turn
  retryerr emits a willRetry error notification, then completes normally
  subagent a delegated sub-agent thread runs and is interrupted mid-turn,
           then the job's own thread does its work and completes normally
  transfer imports a Claude transcript and records the imported thread ledger
  transfer_unsupported rejects externalAgentConfig/import as an unknown RPC
  transfer_no_ledger reports an idempotent completion without a ledger record
  transfer_idempotent reports an idempotent completion with a ledger record
  transfer_failure reports a typed session-import failure despite a stale ledger
  transfer_timeout answers the import RPC but never reports completion
  transfer_delayed_timeout delays that RPC response and never reports completion
  transfer_wrong_id reports completion for a different import request
"""
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

MODE = os.environ.get("FAKE_MODE", "ok")
ARGV_FILE = os.environ.get("FAKE_CODEX_ARGV_FILE")
if ARGV_FILE:
    with open(ARGV_FILE, "w") as fh:
        json.dump(sys.argv[1:], fh)
THREAD_ID = "fake-thread-0001"
TURN_ID = "fake-turn-0001"
SUB_THREAD_ID = "fake-subthread-0001"
SUB_TURN_ID = "fake-subturn-0001"
IMPORTED_THREAD_ID = "fake-imported-thread-0001"
IMPORT_ID = "fake-import-0001"
interrupted = threading.Event()


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def notify(method, params):
    send({"method": method, "params": params})


def run_turn():
    notify("turn/started", {"threadId": THREAD_ID, "turn": {"id": TURN_ID, "status": "inProgress"}})
    notify("account/rateLimits/updated", {"rateLimits": {
        "planType": "pro",
        "primary": {"usedPercent": 42, "windowDurationMins": 10080},
    }})
    if MODE == "die":
        os._exit(1)
    if MODE == "retryerr":
        # real 0.144 shape: willRetry sits at params level, next to the error
        notify("error", {"error": {"message": "transient stream error"}, "willRetry": True})
    if MODE == "subagent":
        # A delegated sub-agent: its own thread, its own turn lifecycle, all
        # multiplexed over this one connection. It ends before the main turn
        # has produced anything — the job must not end with it.
        notify("turn/started", {"threadId": SUB_THREAD_ID,
                                "turn": {"id": SUB_TURN_ID, "status": "inProgress"}})
        notify("item/completed", {"threadId": SUB_THREAD_ID, "turnId": SUB_TURN_ID,
                                  "item": {"type": "agentMessage", "id": "s1",
                                           "text": "SUB-AGENT-NOISE"}})
        notify("error", {"threadId": SUB_THREAD_ID, "error": {"message": "sub-agent hiccup"}})
        notify("turn/completed", {"threadId": SUB_THREAD_ID,
                                  "turn": {"id": SUB_TURN_ID, "status": "interrupted",
                                           "error": None}})
        # The real gap: the main agent keeps working for minutes after a
        # sub-agent ends. Without it the runner races on to the main turn's
        # own completion and the bug hides.
        time.sleep(2)
    if MODE == "slow":
        if interrupted.wait(timeout=20):
            notify("turn/completed", {"threadId": THREAD_ID,
                                      "turn": {"id": TURN_ID, "status": "interrupted", "error": None}})
            return
    notify("item/started", {"threadId": THREAD_ID, "turnId": TURN_ID,
                            "item": {"type": "commandExecution", "id": "i1", "command": "echo hi"}})
    notify("item/completed", {"threadId": THREAD_ID, "turnId": TURN_ID,
                              "item": {"type": "commandExecution", "id": "i1", "command": "echo hi", "exitCode": 0}})
    if MODE == "fail":
        notify("error", {"error": {"message": "fake model exploded"}})
        notify("turn/completed", {"threadId": THREAD_ID,
                                  "turn": {"id": TURN_ID, "status": "failed",
                                           "error": {"message": "fake model exploded"}}})
        return
    if MODE == "move":
        notify("item/completed", {"threadId": THREAD_ID, "turnId": TURN_ID,
                                  "item": {"type": "fileChange", "id": "i2", "changes": [
                                      {"path": "old.txt",
                                       "kind": {"type": "update", "move_path": "new.txt"}}]}})
    else:
        notify("item/completed", {"threadId": THREAD_ID, "turnId": TURN_ID,
                                  "item": {"type": "fileChange", "id": "i2",
                                           "changes": [{"path": "src/example.py", "kind": "edit"}]}})
    notify("item/completed", {"threadId": THREAD_ID, "turnId": TURN_ID,
                              "item": {"type": "agentMessage", "id": "i3", "text": "FAKE-DONE",
                                       "phase": "final_answer"}})
    notify("turn/completed", {"threadId": THREAD_ID,
                              "turn": {"id": TURN_ID, "status": "completed", "error": None,
                                       "durationMs": 1234}})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"id": msg_id, "result": {"userAgent": "fake-codex/0.0.0"}})
    elif method == "initialized":
        pass
    elif method in ("thread/start", "thread/resume"):
        if MODE == "hang":
            continue
        send({"id": msg_id, "result": {"thread": {"id": THREAD_ID, "ephemeral": False}}})
        notify("thread/started", {"thread": {"id": THREAD_ID}})
    elif method == "turn/start":
        send({"id": msg_id, "result": {"turn": {"id": TURN_ID, "status": "inProgress"}}})
        threading.Thread(target=run_turn, daemon=True).start()
    elif method == "turn/interrupt":
        interrupted.set()
        send({"id": msg_id, "result": {}})
        notify("turn/completed", {"threadId": THREAD_ID,
                                  "turn": {"id": TURN_ID, "status": "interrupted", "error": None}})
    elif method == "config/read":
        send({"id": msg_id, "result": {"config": {"model": "fake-model-1",
                                                  "model_reasoning_effort": "medium"}}})
    elif method == "account/read":
        if MODE == "noauth":
            send({"id": msg_id, "result": {"account": None, "requiresOpenaiAuth": True}})
        else:
            send({"id": msg_id, "result": {"account": {"type": "chatgpt", "email": "fake@test.local"}}})
    elif method == "externalAgentConfig/import":
        if MODE == "transfer_unsupported":
            send({"id": msg_id, "error": {"code": -32601, "message": "Method not found"}})
            continue

        params = msg.get("params") or {}
        import_file = os.environ.get("FAKE_CODEX_IMPORT_FILE")
        if import_file:
            Path(import_file).write_text(json.dumps(params))

        if MODE == "transfer_delayed_timeout":
            time.sleep(float(os.environ.get("FAKE_IMPORT_RESPONSE_DELAY", "0.15")))
        send({"id": msg_id, "result": {"importId": IMPORT_ID}})

        if MODE in ("transfer_timeout", "transfer_delayed_timeout"):
            continue
        session = params["migrationItems"][0]["details"]["sessions"][0]
        source = Path(session["path"])
        if MODE in ("transfer_idempotent", "transfer_failure"):
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            codex_home.mkdir(parents=True, exist_ok=True)
            ledger = {
                "records": [
                    {
                        "source_path": str(source),
                        "content_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "imported_thread_id": IMPORTED_THREAD_ID,
                    }
                ]
            }
            (codex_home / "external_agent_session_imports.json").write_text(
                json.dumps(ledger)
            )
        if MODE == "transfer_failure":
            successes = []
            failures = [{
                "itemType": "SESSIONS",
                "errorType": "invalid_session",
                "failureStage": "session_prepare",
                "message": "no importable Claude messages",
                "cwd": session["cwd"],
                "source": str(source),
            }]
        elif MODE in ("transfer_no_ledger", "transfer_idempotent"):
            successes = []
            failures = []
        else:
            successes = [{
                "itemType": "SESSIONS",
                "cwd": session["cwd"],
                "source": str(source),
                "target": IMPORTED_THREAD_ID,
            }]
            failures = ([{
                "itemType": "SESSIONS",
                "errorType": "io_error",
                "failureStage": "session_ledger_update",
                "message": "could not update import ledger",
                "cwd": None,
                "source": None,
            }] if MODE == "transfer_ledger_failure" else [])
        notify("externalAgentConfig/import/completed", {
            "importId": "different-import" if MODE == "transfer_wrong_id" else IMPORT_ID,
            "itemTypeResults": [{
                "itemType": "SESSIONS",
                "successes": successes,
                "failures": failures,
            }],
        })
    elif msg_id is not None:
        send({"id": msg_id, "result": {}})
