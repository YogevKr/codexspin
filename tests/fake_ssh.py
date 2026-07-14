#!/usr/bin/env python3
"""Local ssh stand-in that runs codexspin against a second state home."""

import json
import os
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("fake ssh: missing host or command", file=sys.stderr)
        return 2
    host, command = sys.argv[1], sys.argv[2:]
    log_path = os.environ.get("FAKE_SSH_LOG")
    if log_path:
        with open(log_path, "a") as log:
            log.write(json.dumps({"host": host, "command": command}) + "\n")
    if os.environ.get("FAKE_SSH_CODEXSPIN_MISSING"):
        print("sh: codexspin: command not found", file=sys.stderr)
        return 127
    if command[0] != "codexspin":
        print(f"fake ssh: unsupported command: {' '.join(command)}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["CODEXSPIN_HOME"] = os.environ["FAKE_REMOTE_CODEXSPIN_HOME"]
    if os.environ.get("FAKE_REMOTE_HOME"):
        env["HOME"] = os.environ["FAKE_REMOTE_HOME"]
    if os.environ.get("FAKE_REMOTE_MODE"):
        env["FAKE_MODE"] = os.environ["FAKE_REMOTE_MODE"]
    python = os.environ.get("FAKE_SSH_PYTHON", sys.executable)
    return subprocess.run([python, "-m", "codexspin.cli", *command[1:]], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
