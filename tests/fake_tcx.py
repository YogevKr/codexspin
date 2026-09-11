#!/usr/bin/env python3
"""Fake `tcx run [--group G] -- ARGS...`.

Mirrors the real launcher: records its argv (FAKE_TCX_ARGV_FILE), then execs
the codex binary named by CODEXSPIN_CODEX_BIN with the TeamCodex provider
overrides prepended and the proxy token exported, exactly as `tcx run` does.
"""
import json
import os
import sys

argv = sys.argv[1:]
if os.environ.get("FAKE_TCX_ARGV_FILE"):
    with open(os.environ["FAKE_TCX_ARGV_FILE"], "w") as fh:
        json.dump(argv, fh)
if argv[:1] != ["run"] or "--" not in argv:
    sys.stderr.write(f"fake_tcx: unexpected argv {argv!r}\n")
    sys.exit(2)
rest = argv[argv.index("--") + 1:]
codex = os.environ["CODEXSPIN_CODEX_BIN"]
os.environ["TEAMCODEX_PROXY_TOKEN"] = "fake-proxy-token-0123456789"
os.execv(codex, [codex, "-c", 'model_provider="teamcodex"',
                 "-c", 'model_providers.teamcodex.base_url="http://127.0.0.1:4269/v1"',
                 *rest])
