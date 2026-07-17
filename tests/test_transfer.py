import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexspin import cli
import codexspin.transfer as transfer_module
from codexspin.transfer import (
    SESSION_METADATA_ENV,
    TRANSCRIPT_PATH_ENV,
    TransferError,
    resolve_claude_session_path,
)


ROOT = Path(__file__).parents[1]
FAKE_CODEX = str(Path(__file__).with_name("fake_codex.py"))
IMPORTED_THREAD_ID = "fake-imported-thread-0001"


@pytest.fixture
def transfer_case(tmp_path, monkeypatch):
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    source = projects / "sample project" / "claude-session.jsonl"
    source.parent.mkdir()
    source.write_text('{"type":"user","message":"keep this context"}\n')
    cwd = tmp_path / "working tree"
    cwd.mkdir()
    import_file = tmp_path / "import-params.json"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CODEXSPIN_CODEX_BIN", FAKE_CODEX)
    monkeypatch.setenv("CODEXSPIN_TRANSFER_TIMEOUT", "0.5")
    monkeypatch.setenv("FAKE_MODE", "transfer")
    monkeypatch.setenv("FAKE_CODEX_IMPORT_FILE", str(import_file))
    monkeypatch.delenv(TRANSCRIPT_PATH_ENV, raising=False)
    monkeypatch.delenv(SESSION_METADATA_ENV, raising=False)
    monkeypatch.chdir(cwd)
    return SimpleNamespace(
        home=home,
        projects=projects,
        source=source,
        cwd=cwd,
        import_file=import_file,
    )


def expected_params(case):
    return {
        "migrationItems": [
            {
                "itemType": "SESSIONS",
                "description": "Transfer Claude session claude-session.jsonl",
                "cwd": None,
                "details": {
                    "plugins": [],
                    "sessions": [
                        {
                            "path": str(case.source),
                            "cwd": str(case.cwd),
                            "title": None,
                        }
                    ],
                    "mcpServers": [],
                    "hooks": [],
                    "subagents": [],
                    "commands": [],
                },
            }
        ]
    }


def expected_result(case):
    return {
        "threadId": IMPORTED_THREAD_ID,
        "resumeCommand": f"codex resume {IMPORTED_THREAD_ID}",
        "sourcePath": str(case.source),
        "sessionId": "claude-session",
    }


def test_transfer_explicit_source_sends_exact_payload_and_prints_resume(
    transfer_case, capsys
):
    case = transfer_case

    assert cli.main(["transfer", "--source", str(case.source)]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        "Transferred the Claude session into a Codex thread with visible turn history.\n"
        f"Codex session ID: {IMPORTED_THREAD_ID}\n"
        f"Resume in Codex: codex resume {IMPORTED_THREAD_ID}\n"
    )
    assert json.loads(case.import_file.read_text()) == expected_params(case)


def test_transfer_does_not_advertise_ignored_cwd_override(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["transfer", "--help"])

    assert raised.value.code == 0
    assert "--cwd" not in capsys.readouterr().out


def test_transfer_uses_session_environment_and_prints_exact_json(
    transfer_case, monkeypatch, capsys
):
    case = transfer_case
    monkeypatch.setenv(TRANSCRIPT_PATH_ENV, str(case.source))

    assert cli.main(["transfer", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == json.dumps(expected_result(case), indent=2) + "\n"
    assert json.loads(case.import_file.read_text()) == expected_params(case)


def test_transfer_uses_session_start_metadata(transfer_case, monkeypatch, capsys):
    case = transfer_case
    metadata = case.home / "session-start.json"
    metadata.write_text(json.dumps({"transcript_path": str(case.source)}))
    monkeypatch.setenv(SESSION_METADATA_ENV, str(metadata))

    assert cli.main(["transfer", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected_result(case)


def test_source_validation_rejects_missing_invalid_outside_and_symlink_escape(
    transfer_case, monkeypatch
):
    case = transfer_case
    monkeypatch.delenv(TRANSCRIPT_PATH_ENV, raising=False)

    with pytest.raises(TransferError, match="could not identify the current Claude transcript"):
        resolve_claude_session_path(None, str(case.cwd))

    wrong_type = case.projects / "not-a-transcript.txt"
    wrong_type.write_text("not jsonl")
    with pytest.raises(TransferError, match="must be a JSONL file"):
        resolve_claude_session_path(str(wrong_type), str(case.cwd))

    missing = case.projects / "missing.jsonl"
    with pytest.raises(TransferError, match="Claude session file not found"):
        resolve_claude_session_path(str(missing), str(case.cwd))

    outside = case.home / "outside.jsonl"
    outside.write_text("{}\n")
    with pytest.raises(TransferError, match="only from"):
        resolve_claude_session_path(str(outside), str(case.cwd))

    escape = case.projects / "escape.jsonl"
    escape.symlink_to(outside)
    with pytest.raises(TransferError, match="only from"):
        resolve_claude_session_path(str(escape), str(case.cwd))


def test_transfer_reports_unsupported_import_rpc(transfer_case, monkeypatch):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_unsupported")

    with pytest.raises(SystemExit) as raised:
        cli.main(["transfer", "--source", str(case.source)])

    assert str(raised.value) == (
        "codexspin: this Codex version does not support Claude session transfer; update "
        "Codex with `npm install -g @openai/codex@latest`, then retry"
    )


def test_transfer_requires_matching_import_ledger_record(transfer_case, monkeypatch):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_no_ledger")

    with pytest.raises(SystemExit) as raised:
        cli.main(["transfer", "--source", str(case.source)])

    assert str(raised.value) == (
        "codexspin: Codex reported that the Claude import completed, but did not record an "
        "imported thread. Check the Codex app-server logs for the underlying import error."
    )


def test_transfer_reuses_idempotent_import_from_ledger(transfer_case, monkeypatch, capsys):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_idempotent")

    assert cli.main(["transfer", "--source", str(case.source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected_result(case)


def test_transfer_surfaces_completion_failure_before_stale_ledger(
    transfer_case, monkeypatch
):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_failure")

    with pytest.raises(SystemExit) as raised:
        cli.main(["transfer", "--source", str(case.source)])

    assert str(raised.value) == (
        "codexspin: Codex could not import the Claude session: "
        "session_prepare: no importable Claude messages"
    )


def test_transfer_returns_persisted_thread_when_ledger_update_fails(
    transfer_case, monkeypatch, capsys
):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_ledger_failure")

    assert cli.main(["transfer", "--source", str(case.source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected_result(case)


def test_transfer_completion_timeout_is_fast_and_actionable(transfer_case, monkeypatch):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_timeout")
    monkeypatch.setenv("CODEXSPIN_TRANSFER_TIMEOUT", "0.02")
    started = time.monotonic()

    with pytest.raises(SystemExit) as raised:
        cli.main(["transfer", "--source", str(case.source)])

    assert time.monotonic() - started < 2
    assert str(raised.value) == (
        "codexspin: timed out waiting for Codex to finish importing the Claude session"
    )


def test_transfer_uses_one_deadline_for_rpc_and_completion(transfer_case, monkeypatch):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_delayed_timeout")
    monkeypatch.setenv("FAKE_IMPORT_RESPONSE_DELAY", "0.4")
    monkeypatch.setenv("CODEXSPIN_TRANSFER_TIMEOUT", "0.5")
    started = time.monotonic()

    with pytest.raises(SystemExit, match="timed out waiting for Codex"):
        cli.main(["transfer", "--source", str(case.source)])

    # A separate full completion wait would take roughly 0.9s, plus cleanup.
    assert time.monotonic() - started < 0.8


def test_transfer_ignores_completion_for_another_import(transfer_case, monkeypatch):
    case = transfer_case
    monkeypatch.setenv("FAKE_MODE", "transfer_wrong_id")
    monkeypatch.setenv("CODEXSPIN_TRANSFER_TIMEOUT", "0.02")

    with pytest.raises(SystemExit, match="timed out waiting for Codex"):
        cli.main(["transfer", "--source", str(case.source)])


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", "-inf"])
def test_transfer_rejects_non_finite_or_non_positive_timeout(
    transfer_case, monkeypatch, timeout
):
    case = transfer_case
    monkeypatch.setenv("CODEXSPIN_TRANSFER_TIMEOUT", timeout)

    with pytest.raises(SystemExit, match="must be a finite positive number"):
        cli.main(["transfer", "--source", str(case.source)])


def test_transfer_reports_transcript_read_failure(transfer_case, monkeypatch):
    case = transfer_case

    def fail_digest(_source):
        raise OSError("transcript disappeared")

    monkeypatch.setattr(transfer_module, "_source_sha256", fail_digest)
    with pytest.raises(SystemExit, match="could not read the Claude session before import"):
        cli.main(["transfer", "--source", str(case.source)])


def test_session_start_persists_metadata_without_codexspin_cli(tmp_path):
    env_file = tmp_path / "claude env"
    transcript = tmp_path / "Claude's session.jsonl"
    payload = json.dumps({"transcript_path": str(transcript), "session_id": "ignored"})
    plugin_data = tmp_path / "plugin data"
    plugin_data.mkdir()
    stale_metadata = plugin_data / "session-start.stale"
    stale_metadata.write_text("stale")
    os.utime(stale_metadata, (0, 0))
    env = {
        **os.environ,
        "HOME": str(tmp_path / "empty home"),
        "PATH": "/usr/bin:/bin",
        "CLAUDE_ENV_FILE": str(env_file),
        "CLAUDE_PLUGIN_DATA": str(plugin_data),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
    }

    hook = subprocess.run(
        [str(ROOT / "hooks" / "session-start.sh")],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert hook.returncode == 0
    assert hook.stdout == ""
    resolved = subprocess.run(
        [
            "bash",
            "-c",
            f'. "$1"; printf %s "${{{SESSION_METADATA_ENV}}}"',
            "_",
            str(env_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    metadata_path = Path(resolved.stdout)
    assert metadata_path.parent == plugin_data
    assert "XXXXXX" not in metadata_path.name
    assert json.loads(metadata_path.read_text()) == json.loads(payload)
    assert not stale_metadata.exists()


def test_plugin_hook_and_transfer_command_contract():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    assert hooks["hooks"]["SessionStart"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh"',
                    "timeout": 10,
                }
            ]
        }
    ]
    hook_script = (ROOT / "hooks" / "session-start.sh").read_text().splitlines()
    assert '. "$PLUGIN_ROOT/scripts/find-codexspin.sh"' in hook_script
    assert any(SESSION_METADATA_ENV in line for line in hook_script)
    assert "_session-start" not in "\n".join(hook_script)

    command = (ROOT / "commands" / "transfer.md").read_text().splitlines()
    assert "disable-model-invocation: true" in command
    assert "allowed-tools: Bash(bash:*)" in command
    assert command.count('!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/transfer.sh"`') == 1
    assert "$ARGUMENTS" not in "\n".join(command)
    assert command[-1] == (
        "Present the command output to the user exactly as returned. Preserve the Codex "
        "session ID and the `codex resume <session-id>` command."
    )


def test_plugin_transfer_wrapper_finds_cli_outside_path(tmp_path):
    home = tmp_path / "home with spaces"
    fake_bin = home / ".local" / "bin" / "codexspin"
    fake_bin.parent.mkdir(parents=True)
    fake_codex = fake_bin.with_name("codex")
    fake_codex.write_text("#!/bin/bash\nexit 0\n")
    fake_codex.chmod(0o755)
    fake_bin.write_text(
        "#!/bin/bash\n"
        "if [ \"$1\" = transfer ] && [ \"${2:-}\" = --help ]; then exit 0; fi\n"
        "command -v codex >/dev/null || exit 9\n"
        "printf '%s\\n' \"$@\"\n"
    )
    fake_bin.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
    }

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "transfer.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "transfer\n"
    assert result.stderr == ""
