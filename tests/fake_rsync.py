#!/usr/bin/env python3
"""Local rsync stand-in that maps remote / beneath FAKE_RSYNC_REMOTE_ROOT."""

import json
import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    try:
        separator = args.index("--")
        operands = args[separator + 1:]
    except ValueError:
        operands = [arg for arg in args if not arg.startswith("-")]
    if len(operands) < 2:
        print("fake rsync: missing source or destination", file=sys.stderr)
        return 2

    sources, destination = operands[:-1], operands[-1]
    if not destination.endswith(":/"):
        print(f"fake rsync: unsupported destination {destination}", file=sys.stderr)
        return 2
    remote_root = Path(os.environ["FAKE_RSYNC_REMOTE_ROOT"])
    log_path = os.environ.get("FAKE_RSYNC_LOG")

    for raw_source in sources:
        source = Path(raw_source)
        if not source.is_absolute() or not source.exists():
            print(f"fake rsync: source does not exist: {source}", file=sys.stderr)
            return 23
        target = remote_root / source.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        if log_path:
            with open(log_path, "a") as log:
                log.write(json.dumps({
                    "source": str(source),
                    "destination": destination,
                    "target": str(target),
                }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
