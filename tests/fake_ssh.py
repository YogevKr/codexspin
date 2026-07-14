#!/usr/bin/env python3
"""Fake ssh: runs the requested codexspin command locally against a second
CODEXSPIN_HOME to simulate a remote machine.

Like real OpenSSH, the remote command arrives as argv joined into one string
that a shell would reparse — this fake shlex-splits it and honors leading
VAR=value assignments.

Env conventions:
  FAKE_SSH_ARGV_FILE            write last raw argv (json) here
  FAKE_SSH_LOG                  append {"host", "command"} json lines here
  FAKE_SSH_MISSING_CODEXSPIN /
  FAKE_SSH_CODEXSPIN_MISSING    simulate codexspin absent on the remote (127)
  FAKE_SSH_REMOTE_HOME /
  FAKE_REMOTE_CODEXSPIN_HOME    CODEXSPIN_HOME on the "remote" (wins over a
                                CODEXSPIN_HOME= assignment in the command)
  FAKE_REMOTE_HOME              HOME on the "remote"
  FAKE_REMOTE_MODE              FAKE_MODE on the "remote"
  FAKE_SSH_PYTHON               run via this python -m codexspin.cli instead
                                of exec'ing the codexspin binary on PATH
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 2:
        print("fake ssh: missing host or command", file=sys.stderr)
        return 2
    host = argv[0]
    # OpenSSH semantics: everything after the host is space-joined and
    # reparsed by the remote shell.
    command = shlex.split(" ".join(argv[1:]))

    env = os.environ.copy()
    while command and _ASSIGNMENT.match(command[0]):
        key, value = command.pop(0).split("=", 1)
        env[key] = value

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
    if not command or command[0] != "codexspin":
        print(f"fake ssh: unsupported command: {' '.join(command)}", file=sys.stderr)
        return 2

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
