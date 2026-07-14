#!/usr/bin/env python3
"""Fake ssh: runs the requested codexspin command locally against a second
CODEXSPIN_HOME to simulate a remote machine.

Supports both test conventions:
  FAKE_SSH_ARGV_FILE            write last argv (json) here
  FAKE_SSH_LOG                  append {"host", "command"} json lines here
  FAKE_SSH_MISSING_CODEXSPIN /
  FAKE_SSH_CODEXSPIN_MISSING    simulate codexspin absent on the remote (127)
  FAKE_SSH_REMOTE_HOME /
  FAKE_REMOTE_CODEXSPIN_HOME    CODEXSPIN_HOME on the "remote"
  FAKE_REMOTE_HOME              HOME on the "remote"
  FAKE_REMOTE_MODE              FAKE_MODE on the "remote"
  FAKE_SSH_PYTHON               run via this python -m codexspin.cli instead
                                of exec'ing the codexspin binary on PATH
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 2:
        print("fake ssh: missing host or command", file=sys.stderr)
        return 2
    host, command = argv[0], argv[1:]

    record = os.environ.get("FAKE_SSH_ARGV_FILE")
    if record:
        Path(record).write_text(json.dumps(argv))
    log_path = os.environ.get("FAKE_SSH_LOG")
    if log_path:
        with open(log_path, "a") as log:
            log.write(json.dumps({"host": host, "command": command}) + "\n")

    if os.environ.get("FAKE_SSH_MISSING_CODEXSPIN") or os.environ.get("FAKE_SSH_CODEXSPIN_MISSING"):
        print("sh: codexspin: command not found", file=sys.stderr)
        return 127
    if command[0] != "codexspin":
        print(f"fake ssh: unsupported command: {' '.join(command)}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    remote_home = os.environ.get("FAKE_REMOTE_CODEXSPIN_HOME") or os.environ.get("FAKE_SSH_REMOTE_HOME")
    if remote_home:
        env["CODEXSPIN_HOME"] = remote_home
    if os.environ.get("FAKE_REMOTE_HOME"):
        env["HOME"] = os.environ["FAKE_REMOTE_HOME"]
    if os.environ.get("FAKE_REMOTE_MODE"):
        env["FAKE_MODE"] = os.environ["FAKE_REMOTE_MODE"]

    python = os.environ.get("FAKE_SSH_PYTHON")
    if python:
        return subprocess.run([python, "-m", "codexspin.cli", *command[1:]], env=env).returncode
    os.execvpe("codexspin", command, env)


if __name__ == "__main__":
    raise SystemExit(main())
