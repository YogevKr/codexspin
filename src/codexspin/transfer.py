"""Import a Claude Code transcript as a persistent Codex thread."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

from .appserver import AppServerClient, AppServerError


TRANSCRIPT_PATH_ENV = "CODEXSPIN_TRANSCRIPT_PATH"
SESSION_METADATA_ENV = "CODEXSPIN_SESSION_METADATA"
IMPORT_COMPLETED_METHOD = "externalAgentConfig/import/completed"
DEFAULT_IMPORT_TIMEOUT = 120.0
IMPORT_TIMEOUT_ERROR = "timed out waiting for Codex to finish importing the Claude session"


class TransferError(Exception):
    """A user-actionable Claude-to-Codex transfer failure."""


def resolve_claude_session_path(source: str | None, cwd: str) -> Path:
    """Resolve and validate a Claude transcript accepted by Codex's importer."""
    requested = source or os.environ.get(TRANSCRIPT_PATH_ENV)
    if not requested and os.environ.get(SESSION_METADATA_ENV):
        metadata_path = Path(os.environ[SESSION_METADATA_ENV]).expanduser()
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TransferError(
                "could not read the current Claude session metadata; retry with "
                "--source <path-to-claude-jsonl>"
            ) from exc
        requested = metadata.get("transcript_path") if isinstance(metadata, dict) else None
    if not requested:
        raise TransferError(
            "could not identify the current Claude transcript; retry with "
            "--source <path-to-claude-jsonl>"
        )

    requested_path = Path(requested).expanduser()
    if not requested_path.is_absolute():
        requested_path = Path(cwd) / requested_path
    if requested_path.suffix != ".jsonl":
        raise TransferError(f"Claude session source must be a JSONL file: {requested_path}")

    projects_dir = Path.home() / ".claude" / "projects"
    try:
        source_path = requested_path.resolve(strict=True)
        projects_path = projects_dir.resolve(strict=True)
    except OSError as exc:
        raise TransferError(f"Claude session file not found: {requested_path}") from exc

    if source_path == projects_path or not source_path.is_relative_to(projects_path):
        raise TransferError(
            f"Codex can import Claude sessions only from {projects_dir}: {source_path}"
        )
    if not source_path.is_file():
        raise TransferError(f"Claude session file not found: {source_path}")
    return source_path


def _migration_params(source_path: Path, cwd: str) -> dict:
    return {
        "migrationItems": [
            {
                "itemType": "SESSIONS",
                "description": f"Transfer Claude session {source_path.name}",
                "cwd": None,
                "details": {
                    "plugins": [],
                    "sessions": [{"path": str(source_path), "cwd": cwd, "title": None}],
                    "mcpServers": [],
                    "hooks": [],
                    "subagents": [],
                    "commands": [],
                },
            }
        ]
    }


def _source_sha256(source_path: Path) -> str:
    with source_path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _imported_thread_id(source_path: Path, content_sha256: str) -> str | None:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    ledger_path = codex_home / "external_agent_session_imports.json"
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    matches = [
        record.get("imported_thread_id")
        for record in ledger.get("records", [])
        if isinstance(record, dict)
        and record.get("source_path") == str(source_path)
        and record.get("content_sha256") == content_sha256
        and isinstance(record.get("imported_thread_id"), str)
    ] if isinstance(ledger, dict) and isinstance(ledger.get("records"), list) else []
    return matches[-1] if matches else None


def _thread_id_from_completion(completion: dict, source_path: Path) -> str | None:
    results = completion.get("itemTypeResults")
    if not isinstance(results, list):
        raise TransferError("Codex returned an invalid session-transfer completion")
    session_results = [
        result
        for result in results
        if isinstance(result, dict) and result.get("itemType") == "SESSIONS"
    ]
    if not session_results:
        raise TransferError("Codex did not report a result for the Claude session import")

    failures = []
    successes = []
    for result in session_results:
        result_failures = result.get("failures")
        if isinstance(result_failures, list):
            failures.extend(item for item in result_failures if isinstance(item, dict))
        result_successes = result.get("successes")
        if isinstance(result_successes, list):
            successes.extend(item for item in result_successes if isinstance(item, dict))
    # Codex records the persisted thread as a success before updating its
    # deduplication ledger. A later ledger failure therefore produces a mixed
    # result, but the success target is already valid and resumable.
    for success in successes:
        if success.get("source") not in (None, str(source_path)):
            continue
        thread_id = success.get("target")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    if failures:
        details = []
        for failure in failures:
            stage = failure.get("failureStage")
            message = failure.get("message") or "unknown import error"
            details.append(f"{stage}: {message}" if stage else str(message))
        raise TransferError(f"Codex could not import the Claude session: {'; '.join(details)}")
    if successes:
        raise TransferError("Codex reported a session import success without a thread ID")
    # An unchanged transcript that was imported before is an idempotent no-op;
    # Codex reports neither a success nor a failure, so resolve its ledger entry.
    return None


def import_claude_session(source_path: Path, cwd: str) -> str:
    """Run Codex's native external-agent importer and return its thread id."""
    try:
        timeout = float(os.environ.get("CODEXSPIN_TRANSFER_TIMEOUT", DEFAULT_IMPORT_TIMEOUT))
    except ValueError as exc:
        raise TransferError("CODEXSPIN_TRANSFER_TIMEOUT must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise TransferError("CODEXSPIN_TRANSFER_TIMEOUT must be a finite positive number")

    try:
        source_sha256 = _source_sha256(source_path)
    except OSError as exc:
        raise TransferError(f"could not read the Claude session before import: {source_path}") from exc

    completion_cv = threading.Condition()
    completions: dict[str, dict] = {}

    def on_notification(message: dict) -> None:
        if message.get("method") != IMPORT_COMPLETED_METHOD:
            return
        params = message.get("params")
        import_id = params.get("importId") if isinstance(params, dict) else None
        if isinstance(import_id, str):
            with completion_cv:
                completions[import_id] = params
                completion_cv.notify_all()

    def on_close() -> None:
        with completion_cv:
            completion_cv.notify_all()

    client: AppServerClient | None = None
    try:
        client = AppServerClient(cwd=cwd)
        client.notification_handler = on_notification
        client.on_close = on_close
        client.initialize()
        deadline = time.monotonic() + timeout
        try:
            response = client.request(
                "externalAgentConfig/import",
                _migration_params(source_path, cwd),
                timeout=timeout,
            )
        except AppServerError as exc:
            code = exc.data.get("code") if isinstance(exc.data, dict) else None
            if code == -32601:
                raise TransferError(
                    "this Codex version does not support Claude session transfer; update "
                    "Codex with `npm install -g @openai/codex@latest`, then retry"
                ) from exc
            if time.monotonic() >= deadline:
                raise TransferError(IMPORT_TIMEOUT_ERROR) from exc
            raise

        import_id = response.get("importId")
        if not isinstance(import_id, str) or not import_id:
            raise TransferError(
                "Codex returned an invalid session-transfer response; update Codex with "
                "`npm install -g @openai/codex@latest`, then retry"
            )

        remaining = max(0.0, deadline - time.monotonic())
        with completion_cv:
            completion_cv.wait_for(
                lambda: import_id in completions or client.closed,
                timeout=remaining,
            )
            completion = completions.get(import_id)
        if completion is None:
            if client.closed:
                raise TransferError(
                    "Codex app-server exited before completing the Claude session import"
                )
            raise TransferError(IMPORT_TIMEOUT_ERROR)

        thread_id = _thread_id_from_completion(completion, source_path)
        if not thread_id:
            thread_id = _imported_thread_id(source_path, source_sha256)
        if not thread_id:
            stderr = "\n".join(client.stderr_tail[-10:]).strip()
            detail = (
                f"\n{stderr}"
                if stderr
                else " Check the Codex app-server logs for the underlying import error."
            )
            raise TransferError(
                "Codex reported that the Claude import completed, but did not record an "
                f"imported thread.{detail}"
            )
        return thread_id
    except FileNotFoundError as exc:
        raise TransferError(
            "Codex CLI is not installed; install it with `npm install -g @openai/codex`"
        ) from exc
    except AppServerError as exc:
        raise TransferError(f"Codex app-server failed during Claude session transfer: {exc}") from exc
    finally:
        if client:
            client.close()


def transfer_claude_session(source: str | None, cwd: str) -> dict:
    cwd_path = Path(cwd).expanduser().resolve()
    if not cwd_path.is_dir():
        raise TransferError(f"working directory does not exist: {cwd_path}")
    source_path = resolve_claude_session_path(source, str(cwd_path))
    thread_id = import_claude_session(source_path, str(cwd_path))
    return {
        "threadId": thread_id,
        "resumeCommand": f"codex resume {thread_id}",
        "sourcePath": str(source_path),
        "sessionId": source_path.stem,
    }
