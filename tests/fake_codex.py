#!/usr/bin/env python3
"""Fake `codex app-server` speaking the verified protocol on stdio.

Selected behavior via FAKE_MODE env var:
  ok       (default) one command item + agentMessage "FAKE-DONE", turn completes
  fail     turn completes with status=failed and an error notification
  slow     waits ~20s before completing; turn/interrupt short-circuits it
  hang     never answers thread/start
  die      answers turn/start, then the whole server exits mid-turn
  retryerr emits a willRetry error notification, then completes normally
"""
import json
import os
import sys
import threading

MODE = os.environ.get("FAKE_MODE", "ok")
THREAD_ID = "fake-thread-0001"
TURN_ID = "fake-turn-0001"
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
    elif msg_id is not None:
        send({"id": msg_id, "result": {}})
