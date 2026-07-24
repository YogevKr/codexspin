"""Shared test-suite guards.

`CODEXSPIN_HERDR` may be exported globally on a developer's machine (it makes
every `codexspin spawn` mirror the job into herdr's agent panel). The test suite
spawns real runners, so without this the tests would create herdr workspaces on
the live server and steal focus — plus add a ~10s-per-spawn stall hitting the
herdr socket. Force the mirroring OFF for every test so the suite never touches a
live herdr.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_live_herdr(monkeypatch):
    monkeypatch.delenv("CODEXSPIN_HERDR", raising=False)
