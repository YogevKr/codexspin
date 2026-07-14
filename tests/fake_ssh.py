#!/usr/bin/env python3
"""Fake ssh that runs the requested codexspin command on the local machine."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    record = os.environ.get("FAKE_SSH_ARGV_FILE")
    if record:
        Path(record).write_text(json.dumps(argv))

    if len(argv) < 2 or argv[1] != "codexspin":
        print("fake ssh: expected <host> codexspin <args...>", file=sys.stderr)
        return 2
    if os.environ.get("FAKE_SSH_MISSING_CODEXSPIN"):
        print("sh: codexspin: command not found", file=sys.stderr)
        return 127

    env = os.environ.copy()
    env["CODEXSPIN_HOME"] = env["FAKE_SSH_REMOTE_HOME"]
    os.execvpe("codexspin", argv[1:], env)


if __name__ == "__main__":
    raise SystemExit(main())
