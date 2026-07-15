import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from codexspin import cli, jobs
from codexspin.appserver import AppServerClient, AppServerError
from codexspin.runner import Runner

FAKE = str(Path(__file__).parent / "fake_codex.py")
FAKE_SSH = str(Path(__file__).parent / "fake_ssh.py")
FAKE_RSYNC = str(Path(__file__).parent / "fake_rsync.py")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEXSPIN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE)
    monkeypatch.setenv("CODEXSPIN_SSH_BIN", FAKE_SSH)
    monkeypatch.setenv("FAKE_SSH_REMOTE_HOME", str(tmp_path / "remote-home"))
    monkeypatch.setenv("CODEXSPIN_STARTUP_TIMEOUT", "5")
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.delenv("FAKE_SSH_ARGV_FILE", raising=False)
    monkeypatch.delenv("FAKE_SSH_MISSING_CODEXSPIN", raising=False)
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


def wait_remote_terminal(remote_home: Path, job_id: str, timeout: float = 15) -> dict:
    state_path = remote_home / "jobs" / job_id / "state.json"
    deadline = time.time() + timeout
    state = None
    while time.time() < deadline:
        state = jobs.read_json(state_path)
        if state and state.get("phase") in jobs.TERMINAL_PHASES:
            return state
        time.sleep(0.1)
    raise AssertionError(f"remote job {job_id} never reached a terminal phase: {state}")


def test_remote_spawn_status_and_prompt_round_trip(capfd, monkeypatch, tmp_path):
    remote_home = Path(os.environ["FAKE_SSH_REMOTE_HOME"])
    remote_cwd = tmp_path / "remote cwd"
    remote_cwd.mkdir()
    argv_file = tmp_path / "ssh-argv.json"
    monkeypatch.setenv("FAKE_SSH_ARGV_FILE", str(argv_file))
    prompt = 'Keep "double quotes", single quotes, and $variables.\nSecond line: `literal`.'

    rc = cli.main([
        "spawn", "-n", "remote", "--host", "testbox", "-C", str(remote_cwd), prompt,
    ])
    out = capfd.readouterr().out
    assert rc == 0
    job_id = out.strip().splitlines()[-1]
    assert job_id.startswith("remote-")
    recorded = json.loads(argv_file.read_text())
    assert recorded[0] == "testbox"
    # ssh receives ONE shell-quoted command string; split it like a shell would
    remote_tokens = shlex.split(" ".join(recorded[1:]))
    remote_tokens = [t for t in remote_tokens if not t.startswith("CODEXSPIN_HOME=")]
    assert remote_tokens == ["codexspin", "spawn", "-n", "remote", "-C", str(remote_cwd), "-"]
    assert not (Path(os.environ["CODEXSPIN_HOME"]) / "jobs" / job_id).exists()

    state = wait_remote_terminal(remote_home, job_id)
    assert state["phase"] == "done"
    spec_path = remote_home / "jobs" / job_id / "job.json"
    spec = json.loads(spec_path.read_text())
    assert spec["prompt"] == prompt
    assert spec["cwd"] == str(remote_cwd)

    rc = cli.main(["status", "--host", "testbox", job_id])
    out = capfd.readouterr().out
    assert rc == 0
    assert job_id in out

    follow_up = 'Now preserve "this" too.\nAnd this apostrophe: it\'s exact.'
    rc = cli.main(["send", job_id, follow_up, "--host", "testbox"])
    assert rc == 0
    capfd.readouterr()
    recorded = json.loads(argv_file.read_text())
    remote_tokens = [t for t in shlex.split(" ".join(recorded[1:]))
                     if not t.startswith("CODEXSPIN_HOME=")]
    assert remote_tokens == ["codexspin", "send", job_id, "-"]
    wait_remote_terminal(remote_home, job_id)
    assert json.loads(spec_path.read_text())["prompt"] == follow_up


def test_remote_result_preserves_failed_exit_code(capfd, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    remote_home = Path(os.environ["FAKE_SSH_REMOTE_HOME"])

    rc = cli.main(["spawn", "--host", "testbox", "fail remotely"])
    job_id = capfd.readouterr().out.strip().splitlines()[-1]
    assert rc == 0
    assert wait_remote_terminal(remote_home, job_id)["phase"] == "failed"

    rc = cli.main(["result", "--host", "testbox", job_id])
    captured = capfd.readouterr()
    assert rc == 1
    assert "fake model exploded" in captured.out
    assert "remote codexspin not found" not in captured.err


def test_remote_missing_codexspin_prints_install_hint(capfd, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MISSING_CODEXSPIN", "1")

    rc = cli.main(["status", "--host", "testbox"])
    captured = capfd.readouterr()
    assert rc == 127
    assert "codexspin: command not found" in captured.err
    hints = [line for line in captured.err.splitlines() if "uv tool install codexspin" in line]
    assert hints == [
        "codexspin: remote codexspin not found; install it there with: uv tool install codexspin",
    ]


def test_remote_doctor_and_gc(capfd):
    assert cli.main(["doctor", "--host", "testbox"]) == 0
    assert "app-server: ok" in capfd.readouterr().out
    assert cli.main(["gc", "--host", "testbox", "--keep-days", "0"]) == 0
    assert "removed 0 finished job(s)" in capfd.readouterr().out



def prepare_handoff(tmp_path, monkeypatch, *, create_rollout=True):
    local_home = tmp_path / "user-home"
    remote_root = tmp_path / "remote-root"
    local_codexspin_home = Path(os.environ["CODEXSPIN_HOME"])
    remote_codexspin_home = remote_root / local_codexspin_home.relative_to("/")
    remote_home = remote_root / local_home.relative_to("/")
    rsync_log = tmp_path / "rsync.jsonl"

    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.setenv("CODEXSPIN_SSH_BIN", FAKE_SSH)
    monkeypatch.setenv("CODEXSPIN_RSYNC_BIN", FAKE_RSYNC)
    monkeypatch.setenv("FAKE_SSH_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_REMOTE_CODEXSPIN_HOME", str(remote_codexspin_home))
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(remote_home))
    monkeypatch.setenv("FAKE_REMOTE_MODE", "ok")
    monkeypatch.setenv("FAKE_RSYNC_REMOTE_ROOT", str(remote_root))
    monkeypatch.setenv("FAKE_RSYNC_LOG", str(rsync_log))

    project = tmp_path / "project"
    project.mkdir()
    (project / "work.txt").write_text("warm worktree context\n")
    rollout = local_home / ".codex" / "sessions" / "2026" / "07" / "14" / (
        "rollout-2026-07-14T10-00-00-fake-thread-0001.jsonl"
    )
    if create_rollout:
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"warm":"context"}\n')
    return project, rollout, remote_root, remote_codexspin_home, rsync_log


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


def test_handoff_finished_job_resumes_remotely(capsys, tmp_path, monkeypatch):
    project, rollout, remote_root, remote_home, rsync_log = prepare_handoff(tmp_path, monkeypatch)
    rc = cli.main(["spawn", "-C", str(project), "-n", "handoff", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    assert wait_terminal(job_id)["phase"] == "done"

    rc = cli.main(["handoff", "handoff", "build-host"])
    assert rc == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        job_id,
        f"ssh build-host codexspin status {job_id}",
    ]
    assert wait_remote_terminal(remote_home, job_id)["phase"] == "done"

    remote_job = remote_home / "jobs" / job_id
    remote_spec = json.loads((remote_job / "job.json").read_text())
    assert remote_spec["prompt"] == (
        "You were handed off to another machine mid-task. "
        "Re-read your prior context and continue to completion."
    )
    assert len((remote_job / "results.jsonl").read_text().strip().splitlines()) == 2
    assert (remote_root / project.relative_to("/") / "work.txt").read_text() == "warm worktree context\n"
    assert (remote_root / rollout.relative_to("/")).read_text() == '{"warm":"context"}\n'

    transfers = [json.loads(line) for line in rsync_log.read_text().splitlines()]
    assert [entry["source"] for entry in transfers] == [
        str(project), str(rollout), str(jobs.job_dir(job_id)),
    ]
    assert all(entry["destination"] == "build-host:/" for entry in transfers)
    local_state = json.loads((jobs.job_dir(job_id) / "state.json").read_text())
    assert local_state["phase"] == "done"
    assert local_state["handed_off_to"] == "build-host"
    assert jobs.job_dir(job_id).is_dir()


def test_handoff_running_job_cancels_before_copy(capsys, tmp_path, monkeypatch):
    project, _, _, remote_home, _ = prepare_handoff(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_MODE", "slow")
    rc = cli.main(["spawn", "-C", str(project), "-n", "moving", "keep working"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    deadline = time.time() + 10
    while time.time() < deadline:
        if (jobs.load_state(job_id) or {}).get("phase") == "running":
            break
        time.sleep(0.1)
    assert (jobs.load_state(job_id) or {}).get("phase") == "running"

    rc = cli.main(["handoff", job_id, "other-host", "finish on the remote"])
    assert rc == 0
    capsys.readouterr()
    local_state = json.loads((jobs.job_dir(job_id) / "state.json").read_text())
    assert local_state["phase"] == "cancelled"
    assert local_state["handed_off_to"] == "other-host"
    assert wait_remote_terminal(remote_home, job_id)["phase"] == "done"
    remote_spec = json.loads((remote_home / "jobs" / job_id / "job.json").read_text())
    assert remote_spec["prompt"] == "finish on the remote"


def test_handoff_missing_session_file_is_clear(capsys, tmp_path, monkeypatch):
    project, _, _, remote_home, _ = prepare_handoff(
        tmp_path, monkeypatch, create_rollout=False,
    )
    rc = cli.main(["spawn", "-C", str(project), "-n", "orphan", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    wait_terminal(job_id)

    with pytest.raises(SystemExit, match="no Codex session rollout file found"):
        cli.main(["handoff", job_id, "build-host"])
    assert not (remote_home / "jobs" / job_id).exists()


def test_handoff_without_thread_id_is_clear(capsys, tmp_path, monkeypatch):
    project, _, _, _, _ = prepare_handoff(tmp_path, monkeypatch)
    rc = cli.main(["spawn", "-C", str(project), "-n", "threadless", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    wait_terminal(job_id)
    state_path = jobs.job_dir(job_id) / "state.json"
    state = json.loads(state_path.read_text())
    state.pop("thread_id")
    state_path.write_text(json.dumps(state))

    with pytest.raises(SystemExit, match="has no thread yet"):
        cli.main(["handoff", job_id, "build-host"])


def test_handoff_remote_missing_codexspin_has_install_hint(capsys, tmp_path, monkeypatch):
    project, _, _, _, _ = prepare_handoff(tmp_path, monkeypatch)
    rc = cli.main(["spawn", "-C", str(project), "-n", "missing", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    wait_terminal(job_id)
    monkeypatch.setenv("FAKE_SSH_CODEXSPIN_MISSING", "1")

    with pytest.raises(SystemExit, match="install it there and ensure it is on PATH"):
        cli.main(["handoff", job_id, "empty-host"])


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


def test_worktree_job_gets_git_writable_root(capsys, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    argv_file = tmp_path / "codex-argv.json"
    monkeypatch.setenv("FAKE_CODEX_ARGV_FILE", str(argv_file))
    rc = cli.main(["spawn", "-w", "-C", str(repo), "-n", "gitroot", "do the thing"])
    assert rc == 0
    job_id = capsys.readouterr().out.strip().splitlines()[-1]
    spec = json.loads((jobs.job_dir(job_id) / "job.json").read_text())
    assert spec["writable_roots"] == [spec["git_common_dir"]]
    wait_terminal(job_id)
    argv = json.loads(argv_file.read_text())
    assert argv[0] == "-c"
    assert json.loads(argv[1].split("=", 1)[1]) == [spec["git_common_dir"]]
    assert argv[-1] == "app-server"


def test_explicit_writable_root_flag(capsys, tmp_path):
    extra = tmp_path / "shared"
    extra.mkdir()
    job_id = spawn(capsys, "--writable-root", str(extra))
    spec = json.loads((jobs.job_dir(job_id) / "job.json").read_text())
    assert spec["writable_roots"] == [str(extra.resolve())]
    wait_terminal(job_id)


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


# ---- communication layer ----

def test_notification_handler_error_does_not_wedge_reader(monkeypatch):
    """A throwing notification handler closes the client without hanging."""
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE)
    monkeypatch.setenv("FAKE_MODE", "ok")
    client = AppServerClient(cwd=".")
    closed = threading.Event()
    client.on_close = closed.set
    try:
        client.initialize()
        client.notification_handler = lambda msg: (_ for _ in ()).throw(RuntimeError("disk full"))
        client.request("thread/start", {"cwd": ".", "approvalPolicy": "never",
                                        "sandbox": "read-only", "serviceName": "x", "ephemeral": True})
        assert closed.wait(timeout=3)
        client._stdout_thread.join(timeout=3)
        assert client.closed
        assert not client._stdout_thread.is_alive()
        assert any("handler error" in s for s in client.stderr_tail)
    finally:
        client.close()


def test_turn_completed_handler_error_closes_and_unblocks(monkeypatch):
    """A handler fault on the terminal notification still runs the close path."""
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE)
    monkeypatch.setenv("FAKE_MODE", "ok")
    client = AppServerClient(cwd=".")
    closed = threading.Event()
    saw_completed = threading.Event()
    client.on_close = closed.set

    def handler(msg):
        if msg.get("method") == "turn/completed":
            saw_completed.set()
            raise RuntimeError("terminal handler failed")

    try:
        client.initialize()
        client.notification_handler = handler
        r = client.request("thread/start", {"cwd": ".", "approvalPolicy": "never",
                                            "sandbox": "read-only", "serviceName": "x",
                                            "ephemeral": True})
        tid = (r.get("thread") or {}).get("id")
        client.request("turn/start", {"threadId": tid,
                       "input": [{"type": "text", "text": "hi", "text_elements": []}],
                       "model": None, "effort": None, "outputSchema": None}, timeout=15)
        assert closed.wait(timeout=5)
        assert saw_completed.is_set()
        assert client.closed
    finally:
        client.close()


def test_runner_notification_disk_errors_do_not_block_completion(tmp_path, monkeypatch):
    job_path = tmp_path / "runner-job"
    job_path.mkdir()
    (job_path / "job.json").write_text("{}")
    (job_path / "state.json").write_text("{}")
    runner = Runner(job_path, resume=False)
    runner.events.close()

    class BrokenEvents:
        def write(self, _line):
            raise OSError("events disk full")

    runner.events = BrokenEvents()

    def fail_state(**_updates):
        raise OSError("state disk full")

    monkeypatch.setattr(runner, "set_state", fail_state)
    try:
        runner.on_notification({
            "method": "turn/started",
            "params": {"turn": {"id": "turn-1"}},
        })
        runner.on_notification({
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "completed"}},
        })
        assert runner.turn_done.is_set()
        assert runner.final_turn == {"id": "turn-1", "status": "completed"}
        log = (job_path / "runner.log").read_text()
        assert "failed to write notification event: events disk full" in log
        assert "failed to update state for turn/started: state disk full" in log
    finally:
        runner.log.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_send_after_process_death_raises_typed(monkeypatch):
    """L1: a write to a dead app-server surfaces as AppServerError, not a bare
    OSError that would crash the runner before writing result.json."""
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE)
    client = AppServerClient(cwd=".")
    client.initialize()
    client.proc.kill()
    client.proc.wait()
    time.sleep(0.2)
    client.closed = False  # force the death-between-check-and-write window
    with pytest.raises(AppServerError):
        client.notify("initialized", {})


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_send_failure_wakes_pending_request(monkeypatch):
    """A broken send wakes a request already waiting on the response condition."""
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE)
    client = AppServerClient(cwd=".")
    handler_started = threading.Event()
    release_handler = threading.Event()
    request_waiting = threading.Event()
    request_finished = threading.Event()
    request_errors = []

    def blocking_handler(_msg):
        handler_started.set()
        release_handler.wait(timeout=15)

    try:
        client.initialize()
        client.notification_handler = blocking_handler
        client.request("thread/start", {"cwd": ".", "approvalPolicy": "never",
                                        "sandbox": "read-only", "serviceName": "x",
                                        "ephemeral": True})
        assert handler_started.wait(timeout=3)

        original_wait_for = client._response_cv.wait_for

        def observed_wait_for(predicate, timeout=None):
            request_waiting.set()
            return original_wait_for(predicate, timeout=timeout)

        monkeypatch.setattr(client._response_cv, "wait_for", observed_wait_for)

        def wait_for_response():
            try:
                client.request("never/answered", {}, timeout=30)
            except Exception as exc:  # noqa: BLE001 - asserted below
                request_errors.append(exc)
            finally:
                request_finished.set()

        waiter = threading.Thread(target=wait_for_response)
        waiter.start()
        assert request_waiting.wait(timeout=3)

        client.proc.kill()
        client.proc.wait(timeout=5)
        client.closed = False  # force the death-between-check-and-write window
        started = time.monotonic()
        with pytest.raises(AppServerError):
            client.notify("initialized", {})
        assert request_finished.wait(timeout=5)
        assert time.monotonic() - started < 5
        assert len(request_errors) == 1
        assert isinstance(request_errors[0], AppServerError)
    finally:
        release_handler.set()
        if "waiter" in locals():
            waiter.join(timeout=5)
        client.close()


def test_validate_host_rejects_option_injection():
    """R9: a host starting with '-' would become an ssh/rsync option."""
    for bad in ["-oProxyCommand=id", "--", "-lroot", ""]:
        with pytest.raises(SystemExit, match="invalid host"):
            cli.validate_host(bad)
    assert cli.validate_host("tmm") == "tmm"


def test_remote_host_equals_form_rejected(capsys):
    with pytest.raises(SystemExit, match="invalid host"):
        cli.main(["status", "--host=-oProxyCommand=echo pwned"])


def test_remote_nonprompt_does_not_consume_stdin(tmp_path, monkeypatch):
    """R3: a non-prompt remote command must not let ssh swallow caller stdin."""
    spy = tmp_path / "spyssh.sh"
    bytes_file = tmp_path / "bytes"
    spy.write_text("#!/bin/bash\ncat | wc -c | tr -d ' ' > \"$SPY_BYTES\"\nexit 0\n")
    spy.chmod(0o755)
    monkeypatch.setenv("CODEXSPIN_SSH_BIN", str(spy))
    monkeypatch.setenv("SPY_BYTES", str(bytes_file))
    r, w = os.pipe()
    os.write(w, b"SENTINEL-CALLER-STDIN\n")
    os.close(w)
    old = os.dup(0)
    os.dup2(r, 0)
    try:
        cli.main(["status", "--host", "tmm"])
    finally:
        os.dup2(old, 0)
        os.close(old)
        os.close(r)
    assert bytes_file.read_text().strip() == "0"
