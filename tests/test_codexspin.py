import json
import os
import signal
import subprocess
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


def make_repo(path):
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_worktree_spawn_and_gc(capsys, tmp_path):
    repo = make_repo(tmp_path / "repo")
    rc = cli.main(["spawn", "-w", "-C", str(repo), "-n", "wt", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    spec = json.loads((jobs.job_dir(job_id) / "job.json").read_text())
    assert spec["branch"] == f"codexspin/{job_id}"
    assert spec["cwd"] == spec["worktree"] != str(repo)
    assert Path(spec["worktree"]).is_dir()
    wait_terminal(job_id)

    cli.main(["status", job_id])
    out = capsys.readouterr().out
    assert f"branch: codexspin/{job_id}" in out

    # dirty worktree survives gc; clean one is removed
    (Path(spec["worktree"]) / "wip.txt").write_text("uncommitted\n")
    cli.main(["gc", "--keep-days", "0"])
    assert "kept" in capsys.readouterr().out
    assert Path(spec["worktree"]).is_dir()

    (Path(spec["worktree"]) / "wip.txt").unlink()
    cli.main(["gc", "--keep-days", "0"])
    capsys.readouterr()
    assert not Path(spec["worktree"]).is_dir()
    assert not jobs.job_dir(job_id).is_dir()
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert f"codexspin/{job_id}" in branches  # committed work survives on the branch


def test_worktree_requires_git_repo(tmp_path):
    with pytest.raises(SystemExit, match="requires a git repository"):
        cli.main(["spawn", "-w", "-C", str(tmp_path), "nope"])


def test_max_minutes_timeout(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    rc = cli.main(["spawn", "--max-minutes", "0.03", "timeout me"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    state = wait_terminal(job_id)
    assert state["phase"] == "timeout"
    result = json.loads((jobs.job_dir(job_id) / "result.json").read_text())
    assert "max runtime" in result["error"]["message"]


def test_status_shows_model_and_quota(capsys):
    job_id = spawn(capsys)
    wait_terminal(job_id)
    cli.main(["status", job_id])
    out = capsys.readouterr().out
    assert "fake-model-1/medium" in out
    assert "codex quota: 42%" in out
    assert "plan: pro" in out


def test_doctor(capsys):
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "app-server: ok" in out
    assert "chatgpt fake@test.local" in out
    assert "fake-model-1" in out


def test_doctor_missing_binary(capsys, monkeypatch):
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", "/nonexistent/codex")
    rc = cli.main(["doctor"])
    assert rc == 1
    assert "NOT FOUND" in capsys.readouterr().out


def test_doctor_logged_out(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "noauth")
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT LOGGED IN" in out


def test_worktree_preserves_subdir(capsys, tmp_path):
    repo = make_repo(tmp_path / "mono")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "b.txt").write_text("b\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "pkg"], cwd=repo, check=True)
    rc = cli.main(["spawn", "-w", "-C", str(repo / "pkg"), "-n", "sub", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    spec = json.loads((jobs.job_dir(job_id) / "job.json").read_text())
    assert spec["cwd"] == str(Path(spec["worktree"]) / "pkg")
    wait_terminal(job_id)


def test_timeout_covers_startup(capsys, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("CODEXSPIN_STARTUP_TIMEOUT", "60")
    rc = cli.main(["spawn", "--max-minutes", "0.02", "stall out"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    state = wait_terminal(job_id, timeout=15)
    assert state["phase"] == "timeout"


def test_worktree_via_symlinked_path(capsys, tmp_path):
    repo = make_repo(tmp_path / "realrepo")
    link = tmp_path / "linkrepo"
    os.symlink(repo, link)
    rc = cli.main(["spawn", "-w", "-C", str(link), "-n", "sym", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    spec = json.loads((jobs.job_dir(job_id) / "job.json").read_text())
    assert spec["cwd"] == spec["worktree"]  # no ../ escape
    assert spec["git_common_dir"].endswith(".git")
    wait_terminal(job_id)


def test_max_minutes_rejects_zero(tmp_path):
    with pytest.raises(SystemExit, match="must be positive"):
        cli.main(["spawn", "--max-minutes", "0", "nope"])


def test_fancy_status_output(capsys, monkeypatch):
    monkeypatch.setenv("CODEXSPIN_COLOR", "1")
    job_id = spawn(capsys)
    wait_terminal(job_id)
    cli.main(["status", job_id])
    out = capsys.readouterr().out
    assert "\033[" in out            # ANSI styling active
    assert "✓" in out                # done glyph
    assert "▓" in out                # quota bar
    monkeypatch.setenv("CODEXSPIN_COLOR", "0")
    cli.main(["status", job_id])
    out = capsys.readouterr().out
    assert "\033[" not in out        # plain when disabled
    assert "codex quota: 42%" in out


def test_quota_window_formatting(capsys):
    job_id = spawn(capsys)
    wait_terminal(job_id)
    state_path = jobs.job_dir(job_id) / "state.json"
    state = json.loads(state_path.read_text())
    state["quota"] = {"used_percent": 12, "window_mins": 300, "plan": "pro", "at": time.time()}
    state_path.write_text(json.dumps(state))
    cli.main(["status", job_id])
    assert "12% of 5h window" in capsys.readouterr().out
