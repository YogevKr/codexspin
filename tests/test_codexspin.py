import json
import os
import signal
import time
from pathlib import Path

import pytest

from codexspin import cli, jobs

FAKE = str(Path(__file__).parent / "fake_codex.py")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEXSPIN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE)
    monkeypatch.setenv("CODEXSPIN_STARTUP_TIMEOUT", "5")
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.chdir(tmp_path)
    yield


def spawn(capsys, *extra) -> str:
    rc = cli.main(["spawn", *extra, "do the thing"])
    assert rc == 0
    return capsys.readouterr().out.strip().splitlines()[-1]


def wait_terminal(job_id: str, timeout: float = 15) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = jobs.load_state(job_id)
        if state and state.get("phase") in jobs.TERMINAL_PHASES:
            return state
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached a terminal phase: {jobs.load_state(job_id)}")


def test_spawn_completes_and_result(capsys):
    job_id = spawn(capsys, "-n", "demo")
    assert job_id.startswith("demo-")
    state = wait_terminal(job_id)
    assert state["phase"] == "done"
    assert state["thread_id"] == "fake-thread-0001"

    rc = cli.main(["result", job_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAKE-DONE" in out
    assert "src/example.py" in out

    result = json.loads((jobs.job_dir(job_id) / "result.json").read_text())
    assert result["command_count"] == 1
    assert result["touched_files"] == ["src/example.py"]


def test_failed_turn(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    job_id = spawn(capsys)
    state = wait_terminal(job_id)
    assert state["phase"] == "failed"
    rc = cli.main(["result", job_id])
    out = capsys.readouterr().out
    assert rc == 1
    assert "fake model exploded" in out


def test_startup_hang_times_out(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    job_id = spawn(capsys)
    state = wait_terminal(job_id, timeout=20)
    assert state["phase"] == "failed"
    result = json.loads((jobs.job_dir(job_id) / "result.json").read_text())
    assert "timed out" in result["error"]["message"]


def test_cancel(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    job_id = spawn(capsys)
    deadline = time.time() + 10
    while time.time() < deadline:
        state = jobs.load_state(job_id) or {}
        if state.get("phase") == "running":
            break
        time.sleep(0.1)
    assert (jobs.load_state(job_id) or {}).get("phase") == "running"

    rc = cli.main(["cancel", job_id])
    assert rc == 0
    state = wait_terminal(job_id)
    assert state["phase"] == "cancelled"


def test_send_resumes_thread(capsys):
    job_id = spawn(capsys)
    wait_terminal(job_id)

    rc = cli.main(["send", job_id, "and another thing"])
    assert rc == 0
    capsys.readouterr()
    state = wait_terminal(job_id)
    assert state["phase"] == "done"
    spec = json.loads((jobs.job_dir(job_id) / "job.json").read_text())
    assert spec["prompt"] == "and another thing"
    history = (jobs.job_dir(job_id) / "results.jsonl").read_text().strip().splitlines()
    assert len(history) == 2


def test_send_refuses_running_job(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    job_id = spawn(capsys)
    with pytest.raises(SystemExit, match="still"):
        cli.main(["send", job_id, "nope"])
    cli.main(["cancel", job_id])
    wait_terminal(job_id)


def test_dead_runner_detected(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    job_id = spawn(capsys)
    deadline = time.time() + 10
    while time.time() < deadline:
        state = jobs.load_state(job_id) or {}
        if state.get("phase") == "running":
            break
        time.sleep(0.1)
    pid = (jobs.load_state(job_id) or {}).get("runner_pid")
    os.killpg(os.getpgid(pid), signal.SIGKILL)
    time.sleep(0.3)
    assert (jobs.load_state(job_id) or {}).get("phase") == "died"


def test_status_await_and_yolo_spec(capsys):
    job_a = spawn(capsys, "-n", "alpha")
    job_b = spawn(capsys, "-n", "beta", "--yolo")
    spec_b = json.loads((jobs.job_dir(job_b) / "job.json").read_text())
    assert spec_b["sandbox"] == "danger-full-access"

    rc = cli.main(["await", job_a, job_b, "--timeout", "20"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"--- {job_a}: done ---" in out
    assert f"--- {job_b}: done ---" in out

    cli.main(["status", "--all"])
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "codex resume fake-thread-0001" in out


def test_job_id_prefix_resolution(capsys):
    job_id = spawn(capsys, "-n", "uniqueprefix")
    wait_terminal(job_id)
    assert jobs.resolve_job_id("uniqueprefix") == job_id
    with pytest.raises(SystemExit, match="no job matches"):
        jobs.resolve_job_id("nonexistent")


def test_appserver_death_midturn_fails_job(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "die")
    job_id = spawn(capsys)
    state = wait_terminal(job_id)
    assert state["phase"] == "failed"
    result = json.loads((jobs.job_dir(job_id) / "result.json").read_text())
    assert "exited unexpectedly" in result["error"]["message"]


def test_willretry_error_then_success_is_done(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "retryerr")
    job_id = spawn(capsys)
    state = wait_terminal(job_id)
    assert state["phase"] == "done"
    rc = cli.main(["result", job_id])
    assert rc == 0
    assert "FAKE-DONE" in capsys.readouterr().out


def test_cancel_during_startup(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("CODEXSPIN_STARTUP_TIMEOUT", "60")
    job_id = spawn(capsys)
    deadline = time.time() + 10
    while time.time() < deadline:
        if (jobs.load_state(job_id) or {}).get("activity") == "starting app-server":
            break
        time.sleep(0.1)
    rc = cli.main(["cancel", job_id])
    assert rc == 0
    state = wait_terminal(job_id)
    assert state["phase"] == "cancelled"
    # runner must actually be gone, not grinding toward the startup timeout
    time.sleep(0.5)
    assert not jobs.pid_is_runner(state.get("runner_pid"))


def test_send_invalidates_previous_result(capsys, monkeypatch):
    job_id = spawn(capsys)
    wait_terminal(job_id)
    monkeypatch.setenv("FAKE_MODE", "slow")
    cli.main(["send", job_id, "again"])
    capsys.readouterr()
    rc = cli.main(["result", job_id])
    assert rc == 3  # no stale result served mid-turn
    cli.main(["cancel", job_id])
    wait_terminal(job_id)


def test_result_json_exit_code_on_failure(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    job_id = spawn(capsys)
    wait_terminal(job_id)
    rc = cli.main(["result", job_id, "--json"])
    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["phase"] == "failed"
