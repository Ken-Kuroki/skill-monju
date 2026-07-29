#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run three independent Cursor CLI reviews in parallel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Reviewer:
    ordinal: int
    key: str
    display_name: str
    model_id: str
    allowed_reported_models: tuple[str, ...]


REVIEWERS = (
    Reviewer(
        1,
        "kimi-k3",
        "Kimi K3",
        "kimi-k3-max",
        ("kimi-k3-max", "Kimi K3 Max"),
    ),
    Reviewer(
        2,
        "grok-4-5",
        "Grok 4.5",
        "cursor-grok-4.5-high",
        ("cursor-grok-4.5-high", "Cursor Grok 4.5 High"),
    ),
    Reviewer(
        3,
        "fable-5",
        "Claude Fable 5",
        "claude-fable-5-thinking-max",
        ("claude-fable-5-thinking-max", "Fable 5 300K Max"),
    ),
)

REASONING_POLICY = {
    "mode": "highest-supported-reasoning-only",
    "kimi_k3": "kimi-k3-max",
    "grok_4_5": "cursor-grok-4.5-high",
    "claude_fable_5": "claude-fable-5-thinking-max",
    "fast": False,
    "parallel_compute_quality_variants": False,
    "top_level_reviewers": 3,
}

PROMPT_ENVELOPE = """\
You are one of three independent reviewers in a review-only workflow named Monju.

Mandatory rules:
1. Do not create, edit, delete, rename, or format any file.
2. Do not run commands that mutate the repository, workspace, environment, or external systems.
3. Do not launch subagents or delegate any part of this review.
4. Inspect only the evidence necessary to review the stated scope.
5. Follow the shared review brief exactly. Do not invent missing facts.
6. Cite concrete file paths and line numbers or document sections whenever possible.
7. Prioritize correctness, risk, omissions, and actionable improvements. Avoid filler.
8. Use your full configured reasoning effort. Do not trade review quality for speed.
9. Actively assess whether additional experiments would materially increase confidence
   or resolve an important uncertainty. Propose useful experiments, but never execute them.
10. Apply YAGNI and optimize for clear, direct, maintainable code in the stated current
    scope. Prefer the smallest coherent correction over speculative abstraction, broad
    compatibility, or defensive machinery.
11. Do not omit marginal findings, but classify premature generalization, support for
    unstated environments, disproportionate robustness, and low-impact unlikely edge cases
    as "Usually defer (YAGNI)" unless concrete evidence makes them worth doing now.
12. Do not use YAGNI to downgrade a likely material correctness failure, security issue,
    irreversible data loss, explicit requirement or compatibility promise, or a problem
    whose small, localized correction clearly improves current readability and reduces
    conceptual complexity. Defer purely stylistic preferences or readability rewrites
    whose churn and new machinery outweigh their present benefit.

Return Markdown with exactly these top-level sections:
# Verdict
# Findings
# Gaps and uncertainties
# Proposed experiments
# Recommended next actions

Inside # Findings, use exactly these second-level sections:
## Act now
## Usually defer (YAGNI)

For each finding, state severity (Critical, High, Medium, or Low), evidence, impact,
a concrete correction, and why its disposition is proportionate to current requirements,
likelihood, impact, implementation cost, and added complexity. Put uncertain or merely
future-proofing work under "Usually defer (YAGNI)" rather than promoting it through
worst-case reasoning. If there are no findings in a subsection, say so explicitly.

In # Recommended next actions, include only Act-now work by default. Mention a deferred
item only when it is a prerequisite, a very cheap adjacent cleanup, or the review brief
explicitly asks for future-proofing. Favor readability and a small conceptual surface over
maximum theoretical robustness; this is ordinary production software, not spacecraft code.

In # Proposed experiments, write "No additional experiment is warranted." only when
the available evidence is sufficient and an experiment would not materially improve
the review. Do not propose an experiment merely to explore low-value future-proofing.
Otherwise, provide each proposed experiment with all of these fields:
- Disposition: Act now or Usually defer (YAGNI)
- Question or uncertainty addressed
- Exact procedure and commands, where applicable
- Preconditions and required inputs
- Expected writes, side effects, or external interactions
- Possible outcomes: at least two materially distinct outcomes, plus an inconclusive
  or failed outcome when plausible
- Interpretation: what each possible outcome would support, refute, or leave unresolved
- Risks and approval requirements

Classify an experiment as Usually defer when its expected decision value does not
justify its cost, side effects, or added complexity in the current scope.
Keep proposed outcomes clearly hypothetical. Never describe an experiment as performed
or an outcome as observed unless that evidence is already present in the review brief.

<BEGIN_MONJU_REVIEW_BRIEF>
{review_brief}
<END_MONJU_REVIEW_BRIEF>
"""

MAX_EFFECTIVE_PROMPT_BYTES = 100_000
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
NOTIFICATION_WEBHOOK_ENV = "MONJU_NOTIFY_WEBHOOK_URL"
NOTIFICATION_TIMEOUT_SECONDS = 10
INTERNAL_WORKER_ENV = "MONJU_INTERNAL_WORKER"


@dataclass
class ReviewResult:
    reviewer: str
    requested_model: str
    reported_model: str | None
    model_verified: bool
    session_id: str | None
    status: str
    exit_code: int
    timed_out: bool
    started_at: str
    completed_at: str
    duration_seconds: float
    markdown_file: str
    events_file: str
    stderr_file: str
    command: list[str]
    error: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NotificationResult:
    requested_mode: str
    backend: str | None
    status: str
    attempted_at: str | None
    error: str | None


class RunInterrupted(Exception):
    """Raised when the foreground review supervisor receives SIGTERM."""

    def __init__(self, signum: int):
        super().__init__(f"received signal {signum}")
        self.signum = signum


class ProcessCoordinator:
    """Track live reviewer processes so the supervisor can terminate them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[bytes]] = {}

    def register(self, ordinal: int, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes[ordinal] = proc

    def unregister(self, ordinal: int, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._processes.get(ordinal) is proc:
                self._processes.pop(ordinal, None)

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for proc in processes:
            terminate_process(proc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the same read-only review through Kimi K3, Grok 4.5, "
            "and Claude Fable 5 in parallel."
        )
    )
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--agent-bin", default="agent")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument(
        "--notify",
        choices=("none", "auto", "desktop", "webhook"),
        default="none",
        help=(
            "Send a best-effort terminal-status notification. 'auto' uses "
            f"{NOTIFICATION_WEBHOOK_ENV} when set, otherwise the local desktop."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Create a unique run directory and empty review-brief file, then exit.",
    )
    mode.add_argument(
        "--background",
        action="store_true",
        help="Start the review supervisor in the background and return immediately.",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Print the current status of --run-dir without waiting.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print exact reviewer command templates without invoking Cursor.",
    )
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def notification_pending(mode: str) -> dict[str, Any]:
    if mode == "none":
        return asdict(
            NotificationResult(
                requested_mode=mode,
                backend=None,
                status="disabled",
                attempted_at=None,
                error=None,
            )
        )
    return asdict(
        NotificationResult(
            requested_mode=mode,
            backend=None,
            status="pending",
            attempted_at=None,
            error=None,
        )
    )


def completion_message(run_id: str, status: str, run_dir: Path) -> str:
    return f"Monju review {status}\nRun: {run_id}\nResults: {run_dir}"


def run_desktop_notification(title: str, message: str) -> str:
    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        if not executable:
            raise RuntimeError("osascript was not found")
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run"
        )
        command = [executable, "-e", script, title, message]
        backend = "macos"
    elif sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        if not executable:
            raise RuntimeError(
                "notify-send was not found; install the libnotify command-line tools"
            )
        command = [executable, "--app-name=Monju", title, message]
        backend = "linux"
    elif os.name == "nt":
        executable = shutil.which("msg.exe") or shutil.which("msg")
        if not executable:
            raise RuntimeError("msg.exe was not found")
        username = os.environ.get("USERNAME", "").strip()
        if not username:
            raise RuntimeError("USERNAME is not set for Windows session notification")
        command = [
            executable,
            username,
            f"/time:{NOTIFICATION_TIMEOUT_SECONDS}",
            message,
        ]
        backend = "windows"
    else:
        raise RuntimeError(f"desktop notifications are unsupported on {sys.platform}")

    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=NOTIFICATION_TIMEOUT_SECONDS,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stdout.strip()[-1000:] or "no diagnostic output"
        raise RuntimeError(
            f"{backend} notification command exited with status "
            f"{result.returncode}: {detail}"
        )
    return backend


def run_webhook_notification(
    webhook_url: str,
    run_id: str,
    status: str,
    run_dir: Path,
    message: str,
) -> str:
    payload = {
        "event": "monju.review.completed",
        "source": "monju",
        "run_id": run_id,
        "status": status,
        "run_dir": str(run_dir),
        "text": message,
        "content": message,
    }
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Monju notification",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=NOTIFICATION_TIMEOUT_SECONDS,
        ) as response:
            status_code = response.getcode()
    except (OSError, urllib.error.URLError) as exc:
        detail = str(exc).replace(webhook_url, "<redacted-webhook-url>")
        raise RuntimeError(f"webhook request failed: {detail}") from exc
    if not 200 <= status_code < 300:
        raise RuntimeError(f"webhook returned HTTP {status_code}")
    return "webhook"


def send_completion_notification(
    mode: str,
    run_id: str,
    status: str,
    run_dir: Path,
) -> NotificationResult:
    if mode == "none":
        return NotificationResult(mode, None, "disabled", None, None)

    attempted_at = iso_now()
    webhook_url = os.environ.get(NOTIFICATION_WEBHOOK_ENV, "").strip()
    selected = "webhook" if mode == "auto" and webhook_url else mode
    selected = "desktop" if selected == "auto" else selected
    try:
        message = completion_message(run_id, status, run_dir)
        if selected == "webhook":
            if not webhook_url:
                raise RuntimeError(
                    f"{NOTIFICATION_WEBHOOK_ENV} is not set for webhook notification"
                )
            backend = run_webhook_notification(
                webhook_url,
                run_id,
                status,
                run_dir,
                message,
            )
        else:
            backend = run_desktop_notification("Monju review complete", message)
        return NotificationResult(mode, backend, "sent", attempted_at, None)
    except Exception as exc:  # noqa: BLE001 - notifications are best effort.
        return NotificationResult(
            mode,
            selected,
            "failed",
            attempted_at,
            f"{type(exc).__name__}: {exc}",
        )


def record_completion_notification(
    run_dir: Path,
    run_id: str,
    result: NotificationResult,
) -> None:
    path = manifest_path(run_dir, run_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["notification"] = asdict(result)
        write_json_atomic(path, payload)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"could not record notification result: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def notify_terminal_status(
    mode: str,
    run_dir: Path,
    run_id: str,
    status: str,
) -> None:
    result = send_completion_notification(mode, run_id, status, run_dir)
    record_completion_notification(run_dir, run_id, result)
    if result.status == "failed":
        print(
            f"notification failed ({result.backend}): {result.error}",
            file=sys.stderr,
        )
    else:
        print(
            f"NOTIFICATION={result.status}"
            + (f" BACKEND={result.backend}" if result.backend else "")
        )


def windows_process_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        return True

    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def detached_process_options() -> dict[str, Any]:
    if os.name == "nt":
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_process_group = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0x00000200,
        )
        return {"creationflags": detached_process | new_process_group}
    return {"start_new_session": True}


def make_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    nonce = secrets.token_hex(4)
    return f"monju-{stamp}-p{os.getpid()}-r{nonce}"


def normalize_model_name(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def has_forbidden_variant(value: str) -> bool:
    lowered = value.lower()
    lowered = re.sub(r"fast\s*[:=]\s*(false|off|0)", "", lowered)
    normalized = normalize_model_name(lowered)
    forbidden = (
        "fast",
        "ultracode",
        "mini",
        "lite",
        "flash",
        "turbo",
        "nano",
    )
    if any(token in normalized for token in forbidden):
        return True
    raw_tokens = set(re.findall(r"[a-z0-9]+", lowered))
    return bool(raw_tokens.intersection({"auto", "low", "base"}))


def model_matches(reviewer: Reviewer, reported_model: str | None) -> bool:
    if not reported_model or has_forbidden_variant(reported_model):
        return False
    normalized_reported = normalize_model_name(reported_model)
    allowed = {
        normalize_model_name(value) for value in reviewer.allowed_reported_models
    }
    return normalized_reported in allowed


def validate_quality_policy() -> None:
    if len(REVIEWERS) != 3:
        raise RuntimeError("Monju must run exactly three top-level reviewers")
    for reviewer in REVIEWERS:
        if has_forbidden_variant(reviewer.model_id):
            raise RuntimeError(
                f"forbidden speed/parallel-compute variant configured for "
                f"{reviewer.display_name}: {reviewer.model_id}"
            )
        allowed = {
            normalize_model_name(value)
            for value in reviewer.allowed_reported_models
        }
        if normalize_model_name(reviewer.model_id) not in allowed:
            raise RuntimeError(
                f"requested model ID is missing from the reported-model allowlist "
                f"for {reviewer.display_name}"
            )
    expected_specs = {
        "Kimi K3": "kimi-k3-max",
        "Grok 4.5": "cursor-grok-4.5-high",
        "Claude Fable 5": "claude-fable-5-thinking-max",
    }
    actual_specs = {reviewer.display_name: reviewer.model_id for reviewer in REVIEWERS}
    if actual_specs != expected_specs:
        raise RuntimeError(
            "maximum-reasoning model specifications changed unexpectedly: "
            f"{actual_specs}"
        )


def resolve_executable(value: str) -> str:
    expanded = os.path.expanduser(value)
    explicit_path = (
        os.path.isabs(expanded)
        or expanded.startswith(f".{os.sep}")
        or os.sep in expanded
    )
    if explicit_path:
        resolved = Path(expanded).resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise FileNotFoundError(f"Cursor CLI is not executable: {resolved}")
        return str(resolved)

    located = shutil.which(value)
    if not located:
        raise FileNotFoundError(
            f"Cursor CLI executable '{value}' was not found in PATH. "
            "Install Cursor CLI or pass --agent-bin with an absolute path."
        )
    return str(Path(located).resolve())


def build_command(
    agent_bin: str, reviewer: Reviewer, effective_prompt: str
) -> list[str]:
    return [
        agent_bin,
        "-p",
        "--mode=ask",
        "--model",
        reviewer.model_id,
        "--output-format",
        "stream-json",
        effective_prompt,
    ]


def ensure_prompt_size(effective_prompt: str) -> None:
    size = len(effective_prompt.encode("utf-8"))
    if size > MAX_EFFECTIVE_PROMPT_BYTES:
        raise SystemExit(
            f"effective prompt is {size} bytes; the safe argv limit is "
            f"{MAX_EFFECTIVE_PROMPT_BYTES} bytes. Narrow the review brief."
        )


def send_process_group_signal(proc: subprocess.Popen[bytes], signum: int) -> None:
    if proc.poll() is not None and os.name != "posix":
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signum)
        elif signum == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        send_process_group_signal(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

    if os.name == "posix":
        # The direct child may exit while a helper in its process group survives.
        send_process_group_signal(proc, signal.SIGKILL)
    elif proc.poll() is None:
        proc.kill()

    try:
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass


def parse_event_stream(
    path: Path,
) -> tuple[str | None, str | None, str | None, str | None, list[str]]:
    reported_model: str | None = None
    session_id: str | None = None
    final_text: str | None = None
    stream_error: str | None = None
    warnings: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event: dict[str, Any] = json.loads(stripped)
            except json.JSONDecodeError as exc:
                warnings.append(f"ignored non-JSON event line {line_number}: {exc}")
                continue

            if session_id is None and isinstance(event.get("session_id"), str):
                session_id = event["session_id"]

            if (
                event.get("type") == "system"
                and event.get("subtype") == "init"
                and isinstance(event.get("model"), str)
            ):
                reported_model = event["model"]

            if event.get("type") == "result":
                if event.get("subtype") == "success" and isinstance(
                    event.get("result"), str
                ):
                    final_text = event["result"]
                elif event.get("is_error"):
                    stream_error = str(
                        event.get("result") or "Cursor reported an error"
                    )

    return reported_model, session_id, final_text, stream_error, warnings


def markdown_report(
    reviewer: Reviewer,
    result: ReviewResult,
    final_text: str | None,
    stderr_text: str,
) -> str:
    lines = [
        f"# Monju review — {reviewer.display_name}",
        "",
        f"- Requested model: `{result.requested_model}`",
        f"- Reported model: `{result.reported_model or 'not reported'}`",
        f"- Model verified: `{'yes' if result.model_verified else 'no'}`",
        f"- Status: `{result.status}`",
        f"- Session ID: `{result.session_id or 'not reported'}`",
        f"- Started: `{result.started_at}`",
        f"- Completed: `{result.completed_at}`",
        f"- Duration: `{result.duration_seconds:.3f}s`",
        "",
    ]
    if result.warnings:
        lines.extend(["## Execution notes", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
        lines.append("")
    if result.status != "success":
        lines.extend(
            [
                "## Execution warning",
                "",
                result.error or "The requested reviewer did not complete successfully.",
                "",
                "Do not treat the review text below as a valid result for the requested model.",
                "",
            ]
        )
    if final_text:
        lines.extend([final_text.rstrip(), ""])
    else:
        lines.extend(
            [
                "## Review unavailable",
                "",
                result.error or "Cursor did not emit a final review.",
                "",
            ]
        )
        if stderr_text.strip():
            excerpt = stderr_text.strip()[-4000:]
            lines.extend(["### stderr excerpt", "", "```text", excerpt, "```", ""])
    return "\n".join(lines)


def run_reviewer(
    agent_bin: str,
    reviewer: Reviewer,
    effective_prompt: str,
    staging_dir: Path,
    run_id: str,
    workspace: Path,
    timeout_seconds: int,
    coordinator: ProcessCoordinator,
) -> ReviewResult:
    stem = f"{run_id}-{reviewer.ordinal:02d}-{reviewer.key}"
    events_path = staging_dir / f"{stem}.events.jsonl"
    stderr_path = staging_dir / f"{stem}.stderr.log"
    markdown_path = staging_dir / f"{stem}.md"
    command = build_command(agent_bin, reviewer, effective_prompt)
    started_at = iso_now()
    started_monotonic = time.monotonic()
    timed_out = False
    launch_error: str | None = None
    exit_code = 127
    reviewer_environment = os.environ.copy()
    reviewer_environment.pop(NOTIFICATION_WEBHOOK_ENV, None)
    reviewer_environment.pop(INTERNAL_WORKER_ENV, None)

    with events_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        try:
            proc = subprocess.Popen(
                command,
                cwd=workspace,
                env=reviewer_environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            coordinator.register(reviewer.ordinal, proc)
            try:
                try:
                    exit_code = proc.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process(proc)
                    exit_code = proc.returncode if proc.returncode is not None else 124
            finally:
                coordinator.unregister(reviewer.ordinal, proc)
        except OSError as exc:
            launch_error = f"failed to launch Cursor CLI: {exc}"

    completed_at = iso_now()
    duration = time.monotonic() - started_monotonic
    (
        reported_model,
        session_id,
        final_text,
        stream_error,
        warnings,
    ) = parse_event_stream(events_path)
    verified = model_matches(reviewer, reported_model)
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")

    error_parts: list[str] = []
    if launch_error:
        error_parts.append(launch_error)
    if timed_out:
        error_parts.append(f"review timed out after {timeout_seconds} seconds")
    if exit_code != 0:
        error_parts.append(f"Cursor CLI exited with status {exit_code}")
    if not reported_model:
        error_parts.append("Cursor emitted no system/init model")
    elif has_forbidden_variant(reported_model):
        error_parts.append(f"reported a forbidden model variant: '{reported_model}'")
    elif not verified:
        error_parts.append(
            f"reported model '{reported_model}' does not match the required "
            f"family and quality for '{reviewer.display_name}'"
        )
    if not final_text:
        error_parts.append("Cursor emitted no successful final result")
    if stream_error:
        error_parts.append(stream_error)

    status = "success" if not error_parts else "failed"
    result = ReviewResult(
        reviewer=reviewer.display_name,
        requested_model=reviewer.model_id,
        reported_model=reported_model,
        model_verified=verified,
        session_id=session_id,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        markdown_file=markdown_path.name,
        events_file=events_path.name,
        stderr_file=stderr_path.name,
        command=command[:-1] + ["<effective-prompt>"],
        error="; ".join(error_parts) if error_parts else None,
        warnings=warnings,
    )
    markdown_path.write_text(
        markdown_report(reviewer, result, final_text, stderr_text),
        encoding="utf-8",
    )
    return result


def make_worker_failure_result(
    reviewer: Reviewer,
    staging_dir: Path,
    run_id: str,
    agent_bin: str,
    effective_prompt: str,
    exc: BaseException,
) -> ReviewResult:
    stem = f"{run_id}-{reviewer.ordinal:02d}-{reviewer.key}"
    markdown_path = staging_dir / f"{stem}.md"
    events_path = staging_dir / f"{stem}.events.jsonl"
    stderr_path = staging_dir / f"{stem}.stderr.log"
    error = f"review worker crashed: {type(exc).__name__}: {exc}"
    timestamp = iso_now()
    command = build_command(agent_bin, reviewer, effective_prompt)
    result = ReviewResult(
        reviewer=reviewer.display_name,
        requested_model=reviewer.model_id,
        reported_model=None,
        model_verified=False,
        session_id=None,
        status="failed",
        exit_code=70,
        timed_out=False,
        started_at=timestamp,
        completed_at=timestamp,
        duration_seconds=0.0,
        markdown_file=markdown_path.name,
        events_file=events_path.name,
        stderr_file=stderr_path.name,
        command=command[:-1] + ["<effective-prompt>"],
        error=error,
    )

    try:
        events_path.touch(exist_ok=True)
    except OSError:
        pass
    try:
        with stderr_path.open("a", encoding="utf-8") as stream:
            stream.write(error + "\n")
    except OSError:
        pass
    try:
        markdown_path.write_text(
            markdown_report(reviewer, result, None, error),
            encoding="utf-8",
        )
    except OSError:
        pass
    return result


def classify_status(results: list[ReviewResult], interrupted: bool = False) -> str:
    if interrupted:
        return "interrupted"
    successes = sum(result.status == "success" for result in results)
    if successes == len(results):
        return "success"
    if successes == 0:
        return "failure"
    return "partial_failure"


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, PRIVATE_FILE_MODE)
    os.replace(temporary, path)


def manifest_path(run_dir: Path, run_id: str) -> Path:
    return run_dir / f"{run_id}-manifest.json"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def write_running_manifest(
    run_dir: Path,
    run_id: str,
    workspace: Path,
    prompt_file: Path,
    effective_prompt: str,
    started_at: str,
    notification_mode: str,
) -> None:
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "workspace": str(workspace),
        "source_prompt_file": str(prompt_file),
        "effective_prompt_sha256": hashlib.sha256(
            effective_prompt.encode("utf-8")
        ).hexdigest(),
        "reasoning_policy": REASONING_POLICY,
        "parallel_reviewer_count": len(REVIEWERS),
        "notification": notification_pending(notification_mode),
        "results": [],
    }
    write_json_atomic(manifest_path(run_dir, run_id), payload)


def write_final_manifest(
    staging_dir: Path,
    run_id: str,
    workspace: Path,
    prompt_file: Path,
    effective_prompt: str,
    results: list[ReviewResult],
    started_at: str,
    interrupted_signal: int | None,
    notification_mode: str,
) -> str:
    status = classify_status(results, interrupted_signal is not None)
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "completed_at": iso_now(),
        "workspace": str(workspace),
        "source_prompt_file": str(prompt_file),
        "effective_prompt_sha256": hashlib.sha256(
            effective_prompt.encode("utf-8")
        ).hexdigest(),
        "reasoning_policy": REASONING_POLICY,
        "parallel_reviewer_count": len(REVIEWERS),
        "interrupted_signal": interrupted_signal,
        "notification": notification_pending(notification_mode),
        "results": [asdict(result) for result in results],
    }
    write_json_atomic(staging_dir / f"{run_id}-manifest.json", payload)
    return status


def copy_prompt_artifacts(
    destination: Path,
    run_id: str,
    review_brief: str,
    effective_prompt: str,
) -> tuple[Path, Path]:
    brief_path = destination / f"{run_id}-00-review-brief.md"
    effective_path = destination / f"{run_id}-00-effective-prompt.md"
    atomic_write_text(brief_path, review_brief.rstrip() + "\n")
    atomic_write_text(effective_path, effective_prompt.rstrip() + "\n")
    return brief_path, effective_path


def create_run_directory(output_root: Path) -> tuple[str, Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    for _ in range(10):
        run_id = make_run_id()
        run_dir = output_root / run_id
        try:
            run_dir.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            continue
        prompt_file = run_dir / f"{run_id}-00-review-brief.md"
        prompt_file.touch(mode=PRIVATE_FILE_MODE)
        return run_id, run_dir, prompt_file
    raise RuntimeError("could not allocate a collision-free Monju run directory")


def validate_run_dir(run_dir: Path) -> tuple[str, Path]:
    resolved = run_dir.expanduser().resolve()
    if not resolved.is_dir():
        raise SystemExit(f"run directory does not exist: {resolved}")
    run_id = resolved.name
    if not re.fullmatch(r"monju-[A-Za-z0-9._-]+", run_id):
        raise SystemExit(f"invalid Monju run directory name: {run_id}")
    return run_id, resolved


def default_prompt_path(run_dir: Path, run_id: str) -> Path:
    return run_dir / f"{run_id}-00-review-brief.md"


def resolve_output_root(args: argparse.Namespace, workspace: Path | None) -> Path:
    if args.output_root is None:
        if workspace is None:
            raise SystemExit("--output-root requires --workspace when omitted")
        return (workspace / "monju-reviews").resolve()
    output_root = args.output_root.expanduser()
    if output_root.is_absolute():
        return output_root.resolve()
    if workspace is None:
        raise SystemExit("relative --output-root requires --workspace")
    return (workspace / output_root).resolve()


def resolve_workspace(args: argparse.Namespace) -> Path:
    if args.workspace is None:
        raise SystemExit("--workspace is required")
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace is not a directory: {workspace}")
    return workspace


def resolve_prompt_file(
    args: argparse.Namespace,
    run_dir: Path | None,
    run_id: str | None,
) -> Path:
    if args.prompt_file is not None:
        prompt_file = args.prompt_file.expanduser().resolve()
    elif run_dir is not None and run_id is not None:
        prompt_file = default_prompt_path(run_dir, run_id)
    else:
        raise SystemExit("--prompt-file is required")
    if not prompt_file.is_file():
        raise SystemExit(f"prompt file is not a regular file: {prompt_file}")
    return prompt_file


def read_review_brief(prompt_file: Path) -> tuple[str, str]:
    review_brief = prompt_file.read_text(encoding="utf-8").strip()
    if not review_brief:
        raise SystemExit(f"prompt file is empty: {prompt_file}")
    effective_prompt = PROMPT_ENVELOPE.format(review_brief=review_brief)
    ensure_prompt_size(effective_prompt)
    return review_brief, effective_prompt


def ensure_file_credentials_default() -> None:
    os.environ.setdefault("AGENT_CLI_CREDENTIAL_STORE", "file")


def verify_authentication(agent_bin: str) -> None:
    try:
        result = subprocess.run(
            [agent_bin, "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"could not verify Cursor CLI authentication: {exc}") from exc
    output = result.stdout.strip()
    if result.returncode != 0 or re.search(r"\bnot logged in\b", output, re.IGNORECASE):
        detail = output[-2000:] if output else "no status output"
        raise SystemExit(
            "Cursor CLI file credential store is not authenticated.\n"
            "Run this yourself, complete the browser login, and retry:\n"
            "  AGENT_CLI_CREDENTIAL_STORE=file agent login\n"
            f"Status output:\n{detail}"
        )


def claim_run(run_dir: Path) -> Path:
    marker = run_dir / ".monju-started"
    try:
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            PRIVATE_FILE_MODE,
        )
    except FileExistsError as exc:
        raise SystemExit(f"run directory was already started: {run_dir}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(iso_now() + "\n")
    return marker


def publish_staging(staging_dir: Path, run_dir: Path) -> None:
    for source in sorted(staging_dir.iterdir()):
        destination = run_dir / source.name
        temporary = run_dir / f".{source.name}.{os.getpid()}.publishing"
        shutil.copy2(source, temporary)
        os.chmod(temporary, PRIVATE_FILE_MODE)
        os.replace(temporary, destination)


def write_artifact_failure_manifest(
    run_dir: Path,
    run_id: str,
    staging_dir: Path,
    exc: BaseException,
) -> None:
    current: dict[str, Any] = {}
    path = manifest_path(run_dir, run_id)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    staged_manifest = staging_dir / f"{run_id}-manifest.json"
    try:
        staged = json.loads(staged_manifest.read_text(encoding="utf-8"))
        if isinstance(staged, dict):
            current.update(staged)
    except (OSError, json.JSONDecodeError):
        pass
    current.update(
        {
            "schema_version": 2,
            "run_id": run_id,
            "status": "artifact_failure",
            "completed_at": iso_now(),
            "staging_preserved": str(staging_dir),
            "artifact_error": f"{type(exc).__name__}: {exc}",
        }
    )
    try:
        write_json_atomic(path, current)
    except OSError:
        pass


def write_supervisor_failure_manifest(
    run_dir: Path,
    run_id: str,
    exc: BaseException,
) -> None:
    path = manifest_path(run_dir, run_id)
    current: dict[str, Any] = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    staging_pointer = run_dir / f".{run_id}-staging"
    staging_preserved: str | None = None
    try:
        staging_preserved = staging_pointer.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    current.update(
        {
            "schema_version": 2,
            "run_id": run_id,
            "status": "supervisor_failure",
            "completed_at": iso_now(),
            "supervisor_error": f"{type(exc).__name__}: {exc}",
        }
    )
    if staging_preserved:
        current["staging_preserved"] = staging_preserved
    try:
        write_json_atomic(path, current)
    except OSError:
        pass


def execute_reviews(
    workspace: Path,
    prompt_file: Path,
    run_dir: Path,
    run_id: str,
    agent_bin: str,
    timeout_seconds: int,
    review_brief: str,
    effective_prompt: str,
    run_started_at: str,
    notification_mode: str,
) -> int:
    staging_parent = Path(tempfile.mkdtemp(prefix=f"{run_id}-staging-"))
    staging_dir = staging_parent / run_id
    staging_dir.mkdir(mode=PRIVATE_DIR_MODE)
    staging_pointer = run_dir / f".{run_id}-staging"
    atomic_write_text(staging_pointer, f"{staging_dir}\n")
    copy_prompt_artifacts(
        staging_dir,
        run_id,
        review_brief,
        effective_prompt,
    )

    results_by_ordinal: dict[int, ReviewResult] = {}
    results_lock = threading.Lock()
    coordinator = ProcessCoordinator()
    interrupted_signal: int | None = None

    def worker(reviewer: Reviewer) -> None:
        try:
            result = run_reviewer(
                agent_bin=agent_bin,
                reviewer=reviewer,
                effective_prompt=effective_prompt,
                staging_dir=staging_dir,
                run_id=run_id,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                coordinator=coordinator,
            )
        except Exception as exc:  # noqa: BLE001 - preserve every worker failure.
            result = make_worker_failure_result(
                reviewer,
                staging_dir,
                run_id,
                agent_bin,
                effective_prompt,
                exc,
            )
        finally:
            with results_lock:
                if "result" in locals():
                    results_by_ordinal[reviewer.ordinal] = result

    def sigterm_handler(signum: int, _frame: Any) -> None:
        raise RunInterrupted(signum)

    old_sigterm = signal.signal(signal.SIGTERM, sigterm_handler)
    threads = [
        threading.Thread(
            target=worker,
            args=(reviewer,),
            name=f"monju-{reviewer.key}",
        )
        for reviewer in REVIEWERS
    ]
    started_threads: list[threading.Thread] = []

    try:
        try:
            for thread in threads:
                thread.start()
                started_threads.append(thread)
            for thread in started_threads:
                thread.join()
        except KeyboardInterrupt:
            interrupted_signal = signal.SIGINT
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            coordinator.terminate_all()
        except RunInterrupted as exc:
            interrupted_signal = exc.signum
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            coordinator.terminate_all()
        finally:
            if interrupted_signal is not None:
                coordinator.terminate_all()
            for thread in started_threads:
                thread.join()
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)

    results: list[ReviewResult] = []
    for reviewer in REVIEWERS:
        result = results_by_ordinal.get(reviewer.ordinal)
        if result is None:
            result = make_worker_failure_result(
                reviewer,
                staging_dir,
                run_id,
                agent_bin,
                effective_prompt,
                RuntimeError("review worker produced no result"),
            )
        results.append(result)

    status = write_final_manifest(
        staging_dir,
        run_id,
        workspace,
        prompt_file,
        effective_prompt,
        results,
        run_started_at,
        interrupted_signal,
        notification_mode,
    )

    try:
        publish_staging(staging_dir, run_dir)
    except Exception as exc:  # noqa: BLE001 - preserve staging on publish failure.
        write_artifact_failure_manifest(run_dir, run_id, staging_dir, exc)
        notify_terminal_status(
            notification_mode,
            run_dir,
            run_id,
            "artifact_failure",
        )
        print(f"RUN_DIR={run_dir}")
        print("STATUS=artifact_failure")
        print(f"STAGING_PRESERVED={staging_dir}")
        print(f"ERROR={type(exc).__name__}: {exc}", file=sys.stderr)
        return 74

    try:
        shutil.rmtree(staging_parent)
    except Exception as exc:  # noqa: BLE001 - published results remain authoritative.
        print(
            f"warning: published reviews but could not remove staging "
            f"{staging_parent}: {exc}",
            file=sys.stderr,
        )
    else:
        try:
            staging_pointer.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
            print(
                f"warning: published reviews but could not remove staging pointer "
                f"{staging_pointer}: {exc}",
                file=sys.stderr,
            )
    notify_terminal_status(notification_mode, run_dir, run_id, status)
    print(f"RUN_DIR={run_dir}")
    print(f"STATUS={status}")
    for result in results:
        print(
            f"RESULT {result.reviewer}: {result.status} "
            f"{run_dir / result.markdown_file}"
        )

    if interrupted_signal == signal.SIGINT:
        return 130
    if interrupted_signal is not None:
        return 128 + interrupted_signal
    if status == "success":
        return 0
    if status == "partial_failure":
        return 3
    return 4


def prepare_run(args: argparse.Namespace) -> int:
    workspace = (
        args.workspace.expanduser().resolve() if args.workspace is not None else None
    )
    if workspace is not None and not workspace.is_dir():
        raise SystemExit(f"workspace is not a directory: {workspace}")
    output_root = resolve_output_root(args, workspace)
    run_id, run_dir, prompt_file = create_run_directory(output_root)
    print(f"RUN_ID={run_id}")
    print(f"RUN_DIR={run_dir}")
    print(f"PROMPT_FILE={prompt_file}")
    print("STATUS=prepared")
    return 0


def read_status(args: argparse.Namespace) -> int:
    if args.run_dir is None:
        raise SystemExit("--status requires --run-dir")
    run_id, run_dir = validate_run_dir(args.run_dir)
    path = manifest_path(run_dir, run_id)
    if not path.is_file():
        prompt_file = default_prompt_path(run_dir, run_id)
        status = "prepared" if prompt_file.is_file() else "unknown"
        print(f"RUN_DIR={run_dir}")
        print(f"STATUS={status}")
        return 0

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid manifest JSON: {path}: {exc}") from exc
    status = str(payload.get("status") or "unknown")
    pid_file = run_dir / f"{run_id}-runner.pid"
    runner_pid: str | None = None
    if pid_file.is_file():
        runner_pid = pid_file.read_text(encoding="utf-8").strip()
    if status == "running" and runner_pid:
        try:
            runner_running = process_is_running(int(runner_pid))
        except (OSError, ValueError):
            runner_running = False
        if not runner_running:
            status = "stale_running"
    print(f"RUN_DIR={run_dir}")
    print(f"STATUS={status}")
    print(f"MANIFEST={path}")
    if runner_pid:
        print(f"RUNNER_PID={runner_pid}")
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        markdown_file = result.get("markdown_file")
        if isinstance(markdown_file, str):
            print(
                f"RESULT {result.get('reviewer', 'unknown')}: "
                f"{result.get('status', 'unknown')} {run_dir / markdown_file}"
            )
    if payload.get("staging_preserved"):
        print(f"STAGING_PRESERVED={payload['staging_preserved']}")
    notification = payload.get("notification")
    if isinstance(notification, dict):
        notification_status = notification.get("status", "unknown")
        backend = notification.get("backend")
        print(
            f"NOTIFICATION={notification_status}"
            + (f" BACKEND={backend}" if backend else "")
        )
    return 0


def dry_run(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    run_id: str | None = None
    run_dir: Path | None = None
    if args.run_dir is not None:
        run_id, run_dir = validate_run_dir(args.run_dir)
    prompt_file = resolve_prompt_file(args, run_dir, run_id)
    _, _effective_prompt = read_review_brief(prompt_file)
    agent_bin = shutil.which(args.agent_bin) or args.agent_bin
    output_root = (
        resolve_output_root(args, workspace)
        if args.output_root is not None or run_dir is None
        else run_dir.parent
    )
    payload = {
        "workspace": str(workspace),
        "prompt_file": str(prompt_file),
        "run_dir": str(run_dir) if run_dir else None,
        "output_root": str(output_root),
        "parallel": True,
        "background": bool(args.background),
        "notification": {
            "mode": args.notify,
            "webhook_env": NOTIFICATION_WEBHOOK_ENV,
        },
        "commands": [
            {
                "reviewer": reviewer.display_name,
                "model_id": reviewer.model_id,
                "argv": build_command(agent_bin, reviewer, "<effective-prompt>"),
                "shell_display": shlex.join(
                    build_command(agent_bin, reviewer, "<effective-prompt>")
                ),
            }
            for reviewer in REVIEWERS
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_worker_command(
    workspace: Path,
    prompt_file: Path,
    run_dir: Path,
    agent_bin: str,
    timeout_seconds: int,
    notification_mode: str,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_worker",
        "--workspace",
        str(workspace),
        "--prompt-file",
        str(prompt_file),
        "--run-dir",
        str(run_dir),
        "--agent-bin",
        agent_bin,
        "--timeout-seconds",
        str(timeout_seconds),
        "--notify",
        notification_mode,
    ]


def launch_background(
    workspace: Path,
    prompt_file: Path,
    run_dir: Path,
    run_id: str,
    agent_bin: str,
    timeout_seconds: int,
    review_brief: str,
    effective_prompt: str,
    notification_mode: str,
) -> int:
    marker = claim_run(run_dir)
    started_at = iso_now()
    copy_prompt_artifacts(run_dir, run_id, review_brief, effective_prompt)
    write_running_manifest(
        run_dir,
        run_id,
        workspace,
        default_prompt_path(run_dir, run_id),
        effective_prompt,
        started_at,
        notification_mode,
    )

    stdout_path = run_dir / f"{run_id}-runner.stdout.log"
    stderr_path = run_dir / f"{run_id}-runner.stderr.log"
    worker_command = build_worker_command(
        workspace,
        default_prompt_path(run_dir, run_id),
        run_dir,
        agent_bin,
        timeout_seconds,
        notification_mode,
    )
    try:
        with (
            stdout_path.open("ab") as stdout_file,
            stderr_path.open("ab") as stderr_file,
        ):
            worker_environment = os.environ.copy()
            worker_environment[INTERNAL_WORKER_ENV] = "1"
            proc = subprocess.Popen(
                worker_command,
                cwd=workspace,
                env=worker_environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
                **detached_process_options(),
            )
    except OSError:
        marker.unlink(missing_ok=True)
        raise

    pid_path = run_dir / f"{run_id}-runner.pid"
    atomic_write_text(pid_path, f"{proc.pid}\n")
    print(f"RUN_DIR={run_dir}")
    print("STATUS=running")
    print(f"RUNNER_PID={proc.pid}")
    print(f"MANIFEST={manifest_path(run_dir, run_id)}")
    return 0


def run_requested_review(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    run_id: str
    run_dir: Path

    if args.run_dir is not None:
        run_id, run_dir = validate_run_dir(args.run_dir)
    else:
        output_root = resolve_output_root(args, workspace)
        run_id, run_dir, _ = create_run_directory(output_root)

    prompt_file = resolve_prompt_file(args, run_dir, run_id)
    canonical_prompt = default_prompt_path(run_dir, run_id)
    if args._worker:
        if prompt_file != canonical_prompt:
            raise SystemExit("internal worker requires the canonical run prompt")
        if not (run_dir / ".monju-started").is_file():
            raise SystemExit("internal worker requires a claimed run directory")
    review_brief, effective_prompt = read_review_brief(prompt_file)
    agent_bin = resolve_executable(args.agent_bin)
    verify_authentication(agent_bin)

    if args.background:
        return launch_background(
            workspace,
            canonical_prompt,
            run_dir,
            run_id,
            agent_bin,
            args.timeout_seconds,
            review_brief,
            effective_prompt,
            args.notify,
        )

    if not args._worker:
        claim_run(run_dir)
        copy_prompt_artifacts(
            run_dir,
            run_id,
            review_brief,
            effective_prompt,
        )
        write_running_manifest(
            run_dir,
            run_id,
            workspace,
            canonical_prompt,
            effective_prompt,
            iso_now(),
            args.notify,
        )

    return execute_reviews(
        workspace=workspace,
        prompt_file=canonical_prompt,
        run_dir=run_dir,
        run_id=run_id,
        agent_bin=agent_bin,
        timeout_seconds=args.timeout_seconds,
        review_brief=review_brief,
        effective_prompt=effective_prompt,
        run_started_at=iso_now(),
        notification_mode=args.notify,
    )


def main() -> int:
    os.umask(0o077)
    ensure_file_credentials_default()
    args = parse_args()
    validate_quality_policy()

    if args._worker and os.environ.get(INTERNAL_WORKER_ENV) != "1":
        raise SystemExit("--_worker is reserved for the detached Monju supervisor")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than zero")
    if args.prepare:
        return prepare_run(args)
    if args.status:
        return read_status(args)
    if args.dry_run:
        return dry_run(args)
    try:
        return run_requested_review(args)
    except SystemExit as exc:
        if not args._worker:
            raise
        if args.run_dir is not None:
            try:
                run_id, run_dir = validate_run_dir(args.run_dir)
                write_supervisor_failure_manifest(run_dir, run_id, exc)
                notify_terminal_status(
                    args.notify,
                    run_dir,
                    run_id,
                    "supervisor_failure",
                )
            except Exception as manifest_exc:  # noqa: BLE001 - diagnostics only.
                print(
                    f"could not record worker startup failure: {manifest_exc}",
                    file=sys.stderr,
                )
        print(f"worker startup failed: {exc}", file=sys.stderr)
        return 70
    except Exception as exc:  # noqa: BLE001 - record unexpected supervisor exits.
        if args.run_dir is not None:
            try:
                run_id, run_dir = validate_run_dir(args.run_dir)
                write_supervisor_failure_manifest(run_dir, run_id, exc)
                notify_terminal_status(
                    args.notify,
                    run_dir,
                    run_id,
                    "supervisor_failure",
                )
            except Exception as manifest_exc:  # noqa: BLE001 - diagnostics only.
                print(
                    f"could not record supervisor failure: {manifest_exc}",
                    file=sys.stderr,
                )
        print(f"Monju supervisor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
