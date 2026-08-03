#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run configurable independent OpenCode Go reviews in parallel."""

from __future__ import annotations

import argparse
import contextlib
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
    variant: str | None


REVIEWERS: tuple[Reviewer, ...] = ()
REVIEWER_CONFIG_HASH = ""
CATALOG_VERIFICATION: dict[str, Any] = {}
CONFIG_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 3
BACKEND = "opencode"
PROVIDER = "opencode-go"
MONJU_AGENT_NAME = "monju-review"
DEFAULT_REVIEWERS_FILE = Path(__file__).resolve().parents[1] / "reviewers.json"

PROMPT_ENVELOPE = """\
You are one independent reviewer in a parallel review-only workflow named Monju.

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
EXECUTION_MODE = "foreground_supervisor"
TMUX_EXECUTION_MODE = "tmux_supervisor"
TMUX_STARTUP_TIMEOUT_SECONDS = 20.0
DEFAULT_TMUX_BIN = "tmux"
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECOVERY_SOURCE = "preserved_event_streams"
TERMINAL_MARKER_SCHEMA_VERSION = 1
PUBLISHED_STATUSES = frozenset(
    {"success", "partial_failure", "failure", "interrupted"}
)
RECOVERABLE_FAILURE_STATUSES = frozenset(
    {"artifact_failure", "supervisor_failure"}
)
TERMINAL_STATUSES = PUBLISHED_STATUSES | RECOVERABLE_FAILURE_STATUSES
REQUIRED_REVIEW_HEADINGS = (
    "# Verdict",
    "# Findings",
    "## Act now",
    "## Usually defer (YAGNI)",
    "# Gaps and uncertainties",
    "# Proposed experiments",
    "# Recommended next actions",
)
IGNORED_STARTUP_STDERR_FRAGMENTS = (
    "warning: setlocale: LC_ALL: cannot change locale",
)


@dataclass
class ReviewResult:
    reviewer: str
    requested_model: str
    requested_variant: str | None
    reported_model: str | None
    model_verified: bool
    model_verification_source: str
    session_id: str | None
    status: str
    exit_code: int | None
    timed_out: bool | None
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None
    markdown_file: str
    events_file: str
    stderr_file: str
    command: list[str] | None
    error: str | None
    warnings: list[str] = field(default_factory=list)
    recovered: bool = False
    timing_source: dict[str, str | None] | None = None
    recovery_validation: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParsedEventStream:
    session_id: str | None
    final_text: str | None
    stream_error: str | None
    warnings: tuple[str, ...]
    malformed_lines: tuple[int, ...]
    event_count: int
    text_event_count: int


@dataclass(frozen=True)
class RecoveryStream:
    reviewer: Reviewer
    events_path: Path
    stderr_path: Path
    terminal_path: Path
    parsed: ParsedEventStream | None
    terminal: dict[str, Any] | None
    terminal_complete: bool
    validation_errors: tuple[str, ...]
    read_error: str | None


@dataclass(frozen=True)
class RecoveryAssessment:
    state: str
    staging_dir: Path | None
    streams: tuple[RecoveryStream, ...]
    terminal_results: int
    successful_results: int
    can_publish: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class NotificationResult:
    requested_mode: str
    backend: str | None
    status: str
    attempted_at: str | None
    error: str | None


class RunInterrupted(Exception):
    """Raised when a Monju process receives a handled termination signal."""

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


class SupervisorHeartbeat:
    """Emit bounded liveness output while the foreground supervisor waits."""

    def __init__(
        self,
        run_id: str,
        *,
        interval_seconds: float | None = None,
        output: Any = None,
    ) -> None:
        self.run_id = run_id
        self.interval_seconds = (
            HEARTBEAT_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        )
        if self.interval_seconds <= 0:
            raise ValueError("heartbeat interval must be greater than zero")
        self.output = sys.stdout if output is None else output
        self._stop_event = threading.Event()
        self._started_at: float | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"monju-heartbeat-{run_id}",
            daemon=False,
        )

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> SupervisorHeartbeat:
        self._started_at = time.monotonic()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.ident is not None:
            self._thread.join()

    def __enter__(self) -> SupervisorHeartbeat:
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            started_at = self._started_at
            if started_at is None:
                return
            elapsed_seconds = max(0, int(time.monotonic() - started_at))
            try:
                print(
                    f"MONJU_HEARTBEAT run_id={self.run_id} "
                    f"elapsed_seconds={elapsed_seconds}",
                    file=self.output,
                    flush=True,
                )
            except (BrokenPipeError, OSError, ValueError):
                return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the same read-only review through configured OpenCode Go models."
    )
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument(
        "--tmux-bin",
        default=DEFAULT_TMUX_BIN,
        help="tmux executable used by --tmux; defaults to PATH lookup.",
    )
    parser.add_argument(
        "--reviewers-file",
        type=Path,
        default=DEFAULT_REVIEWERS_FILE,
        help="Reviewer configuration JSON; defaults to the skill's reviewers.json.",
    )
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
        "--preflight",
        action="store_true",
        help=(
            "Check tmux access plus OpenCode state, authentication, models, "
            "and variants without starting a review."
        ),
    )
    mode.add_argument(
        "--tmux",
        action="store_true",
        help="Launch the foreground supervisor inside a user-owned tmux session.",
    )
    mode.add_argument(
        "--background",
        action="store_true",
        help=(
            "Deprecated and rejected: use --tmux for a tracked persistent "
            "supervisor instead."
        ),
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Print the current status of --run-dir without waiting.",
    )
    mode.add_argument(
        "--recover",
        action="store_true",
        help=(
            "Publish a dead supervisor's completed preserved event streams "
            "without invoking OpenCode or using the network."
        ),
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print exact reviewer command templates without invoking OpenCode.",
    )
    mode.add_argument("--reviewer-worker", type=Path, help=argparse.SUPPRESS)
    mode.add_argument("--tmux-supervisor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tmux-session", help=argparse.SUPPRESS)
    parser.add_argument("--tmux-webhook-file", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reviewers_payload(reviewers: tuple[Reviewer, ...]) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": reviewer.ordinal,
            "key": reviewer.key,
            "display_name": reviewer.display_name,
            "model_id": reviewer.model_id,
            "variant": reviewer.variant,
        }
        for reviewer in reviewers
    ]


def reviewer_config_hash(reviewers: tuple[Reviewer, ...]) -> str:
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "provider": PROVIDER,
        "reviewers": reviewers_payload(reviewers),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_reviewer_entries(entries: Any) -> tuple[Reviewer, ...]:
    if not isinstance(entries, list) or not entries:
        raise ValueError("reviewer configuration must contain at least one reviewer")
    reviewers: list[Reviewer] = []
    keys: set[str] = set()
    model_variants: set[tuple[str, str | None]] = set()
    for ordinal, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"reviewer {ordinal} must be a JSON object")
        key = entry.get("key")
        display_name = entry.get("display_name")
        model_id = entry.get("model_id")
        variant = entry.get("variant")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
            raise ValueError(f"reviewer {ordinal} has an invalid key")
        if key in keys:
            raise ValueError(f"duplicate reviewer key: {key}")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError(f"reviewer {key} has no display_name")
        if not isinstance(model_id, str) or not model_id.startswith(f"{PROVIDER}/"):
            raise ValueError(
                f"reviewer {key} model_id must start with '{PROVIDER}/'"
            )
        if variant is not None and (not isinstance(variant, str) or not variant.strip()):
            raise ValueError(f"reviewer {key} has an invalid variant")
        identity = (model_id, variant)
        if identity in model_variants:
            raise ValueError(f"duplicate model/variant configuration: {model_id}/{variant}")
        keys.add(key)
        model_variants.add(identity)
        reviewers.append(
            Reviewer(ordinal, key, display_name.strip(), model_id, variant)
        )
    return tuple(reviewers)


def load_reviewer_configuration(path: Path) -> tuple[tuple[Reviewer, ...], str]:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read reviewer configuration {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("reviewer configuration root must be a JSON object")
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise SystemExit(
            f"unsupported reviewer configuration schema: {payload.get('schema_version')!r}"
        )
    if payload.get("provider") != PROVIDER:
        raise SystemExit(f"reviewer provider must be '{PROVIDER}'")
    try:
        reviewers = validate_reviewer_entries(payload.get("reviewers"))
    except ValueError as exc:
        raise SystemExit(f"invalid reviewer configuration: {exc}") from exc
    return reviewers, reviewer_config_hash(reviewers)


def reviewers_from_manifest(payload: dict[str, Any]) -> tuple[Reviewer, ...]:
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION or payload.get("backend") != BACKEND:
        raise ValueError("manifest does not contain a recoverable OpenCode reviewer configuration")
    try:
        reviewers = validate_reviewer_entries(payload.get("reviewers"))
    except ValueError as exc:
        raise ValueError(f"invalid manifest reviewer configuration: {exc}") from exc
    for expected, reviewer in enumerate(reviewers, start=1):
        raw = payload["reviewers"][expected - 1]
        if raw.get("ordinal") != reviewer.ordinal:
            raise ValueError("manifest reviewer ordinals are invalid")
    expected_hash = payload.get("reviewer_config_sha256")
    actual_hash = reviewer_config_hash(reviewers)
    if expected_hash != actual_hash:
        raise ValueError("manifest reviewer configuration hash does not match")
    return reviewers


def configure_reviewers(reviewers: tuple[Reviewer, ...], config_hash: str) -> None:
    global REVIEWERS, REVIEWER_CONFIG_HASH
    REVIEWERS = reviewers
    REVIEWER_CONFIG_HASH = config_hash


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


def macos_notification_script() -> str:
    return (
        "function run(argv) {\n"
        "  const app = Application.currentApplication();\n"
        "  app.includeStandardAdditions = true;\n"
        "  app.displayNotification(argv[1], {withTitle: argv[0]});\n"
        "}"
    )


def run_desktop_notification(title: str, message: str) -> str:
    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        if not executable:
            raise RuntimeError("osascript was not found")
        script = macos_notification_script()
        command = [
            executable,
            "-l",
            "JavaScript",
            "-e",
            script,
            "--",
            title,
            message,
        ]
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
        ):
            pass
    except (OSError, urllib.error.URLError) as exc:
        detail = str(exc).replace(webhook_url, "<redacted-webhook-url>")
        raise RuntimeError(f"webhook request failed: {detail}") from exc
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
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def make_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    nonce = secrets.token_hex(4)
    return f"monju-{stamp}-p{os.getpid()}-r{nonce}"


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
            raise FileNotFoundError(f"OpenCode CLI is not executable: {resolved}")
        return str(resolved)

    located = shutil.which(value)
    if located:
        return str(Path(located).resolve())
    if value == "opencode":
        installer_path = Path.home() / ".opencode" / "bin" / "opencode"
        if installer_path.is_file() and os.access(installer_path, os.X_OK):
            return str(installer_path.resolve())
    raise FileNotFoundError(
        f"OpenCode CLI executable '{value}' was not found. Install OpenCode or "
        "pass --opencode-bin with an absolute path."
    )


def resolve_tmux_executable(value: str) -> str:
    expanded = os.path.expanduser(value)
    explicit_path = (
        os.path.isabs(expanded)
        or expanded.startswith(f".{os.sep}")
        or os.sep in expanded
    )
    if explicit_path:
        resolved = Path(expanded).resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise FileNotFoundError(f"tmux is not executable: {resolved}")
        return str(resolved)
    located = shutil.which(value)
    if located:
        return str(Path(located).resolve())
    raise FileNotFoundError(
        f"tmux executable '{value}' was not found. Install tmux or pass "
        "--tmux-bin with an absolute path."
    )


def run_tmux_capture(
    tmux_bin: str,
    arguments: list[str],
    *,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [tmux_bin, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not run tmux: {exc}") from exc


def tmux_missing_target(output: str) -> bool:
    normalized = output.lower()
    return any(
        fragment in normalized
        for fragment in (
            "can't find session",
            "no server running",
            "no sessions",
            "failed to connect to server",
        )
    )


def inspect_tmux_server(tmux_bin: str) -> str:
    result = run_tmux_capture(
        tmux_bin,
        ["list-sessions", "-F", "#{session_name}"],
    )
    output = strip_ansi(result.stdout).strip()
    if result.returncode == 0:
        return "existing"
    if tmux_missing_target(output):
        return "absent"
    if "operation not permitted" in output.lower() or "permission denied" in output.lower():
        raise SystemExit(
            "PREFLIGHT=tmux_state_unwritable\n"
            f"tmux socket access failed: {output or 'permission denied'}\n"
            "Rerun preflight and launch outside the filesystem sandbox."
        )
    raise SystemExit(
        "PREFLIGHT=tmux_unavailable\n"
        f"tmux server check failed: {output or f'exit status {result.returncode}'}"
    )


def tmux_session_is_running(tmux_bin: str, session_name: str) -> bool:
    result = run_tmux_capture(
        tmux_bin,
        ["has-session", "-t", f"={session_name}"],
    )
    if result.returncode == 0:
        return True
    output = strip_ansi(result.stdout).strip()
    if tmux_missing_target(output):
        return False
    raise RuntimeError(
        f"tmux session check failed: {output or f'exit status {result.returncode}'}"
    )


def build_command(opencode_bin: str, reviewer: Reviewer, workspace: Path) -> list[str]:
    command = [
        opencode_bin,
        "--pure",
        "run",
        "--format",
        "json",
        "--model",
        reviewer.model_id,
        "--agent",
        MONJU_AGENT_NAME,
        "--dir",
        str(workspace),
    ]
    if reviewer.variant is not None:
        command.extend(["--variant", reviewer.variant])
    return command


def ensure_prompt_size(effective_prompt: str) -> None:
    size = len(effective_prompt.encode("utf-8"))
    if size > MAX_EFFECTIVE_PROMPT_BYTES:
        raise SystemExit(
            f"effective prompt is {size} bytes; the configured safety limit is "
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


def inspect_event_stream(path: Path) -> ParsedEventStream:
    session_id: str | None = None
    text_parts: list[str] = []
    stream_error: str | None = None
    warnings: list[str] = []
    malformed_lines: list[int] = []
    event_count = 0
    text_event_count = 0

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as exc:
                warnings.append(f"non-JSON event line {line_number}: {exc}")
                malformed_lines.append(line_number)
                continue
            if not isinstance(decoded, dict):
                warnings.append(f"non-object event line {line_number}: {type(decoded).__name__}")
                malformed_lines.append(line_number)
                continue
            event: dict[str, Any] = decoded
            event_count += 1
            current_session = event.get("sessionID")
            if isinstance(current_session, str):
                if session_id is None:
                    session_id = current_session
                elif current_session != session_id:
                    malformed_lines.append(line_number)
                    warnings.append(f"event line {line_number} changed sessionID")

            event_type = event.get("type")
            if event_type == "text":
                part = event.get("part")
                text_value = part.get("text") if isinstance(part, dict) else None
                if isinstance(text_value, str) and text_value.strip():
                    text_parts.append(text_value)
                    text_event_count += 1
            elif event_type == "error":
                error = event.get("error")
                message: Any = None
                if isinstance(error, dict):
                    data = error.get("data")
                    if isinstance(data, dict):
                        message = data.get("message")
                    message = message or error.get("message") or error.get("name")
                stream_error = str(message or "OpenCode reported an error")

    return ParsedEventStream(
        session_id=session_id,
        final_text="\n\n".join(text_parts) if text_parts else None,
        stream_error=stream_error,
        warnings=tuple(warnings),
        malformed_lines=tuple(malformed_lines),
        event_count=event_count,
        text_event_count=text_event_count,
    )


def parsed_review_errors(
    reviewer: Reviewer,
    parsed: ParsedEventStream,
) -> list[str]:
    errors: list[str] = []
    if parsed.malformed_lines:
        lines = ", ".join(str(line) for line in parsed.malformed_lines[:10])
        suffix = "…" if len(parsed.malformed_lines) > 10 else ""
        errors.append(f"malformed OpenCode event stream at line(s) {lines}{suffix}")
    if not parsed.final_text:
        errors.append("OpenCode emitted no final review text")
    else:
        missing_headings = [
            heading
            for heading in REQUIRED_REVIEW_HEADINGS
            if re.search(
                rf"(?m)^{re.escape(heading)}[ \t]*$",
                parsed.final_text,
            )
            is None
        ]
        if missing_headings:
            errors.append(
                "OpenCode review omitted required section(s): "
                + ", ".join(missing_headings)
            )
    if parsed.stream_error:
        errors.append(parsed.stream_error)
    return errors


def opencode_agent_config() -> dict[str, Any]:
    return {
        "agent": {
            MONJU_AGENT_NAME: {
                "description": "Read-only Monju reviewer",
                "mode": "primary",
                "permission": {
                    "*": "deny",
                    "read": {
                        "*": "allow",
                        "*.env": "deny",
                        "*.env.*": "deny",
                        "*.env.example": "allow",
                    },
                    "glob": "allow",
                    "grep": "allow",
                },
            }
        }
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def terminal_marker_path(staging_dir: Path, run_id: str, reviewer: Reviewer) -> Path:
    stem = f"{run_id}-{reviewer.ordinal:02d}-{reviewer.key}"
    return staging_dir / f"{stem}.terminal.json"


def catalog_model_verified(reviewer: Reviewer) -> bool:
    models = CATALOG_VERIFICATION.get("models")
    if not isinstance(models, dict):
        return False
    entry = models.get(reviewer.model_id)
    if not isinstance(entry, dict):
        return False
    variants = entry.get("variants")
    if reviewer.variant is None:
        return True
    return isinstance(variants, list) and reviewer.variant in variants


def validate_terminal_marker(
    marker: Any,
    reviewer: Reviewer,
    run_id: str,
    events_path: Path,
    stderr_path: Path,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(marker, dict):
        return ["terminal marker is not a JSON object"]
    expected = {
        "schema_version": TERMINAL_MARKER_SCHEMA_VERSION,
        "run_id": run_id,
        "ordinal": reviewer.ordinal,
        "reviewer_key": reviewer.key,
        "model_id": reviewer.model_id,
        "variant": reviewer.variant,
        "reviewer_config_sha256": REVIEWER_CONFIG_HASH,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            errors.append(f"terminal marker {key} does not match launch configuration")
    if marker.get("completed") is not True:
        errors.append("terminal marker is not complete")
    for label, path in (("events", events_path), ("stderr", stderr_path)):
        try:
            size = path.stat().st_size
            digest = file_sha256(path)
        except OSError as exc:
            errors.append(f"could not verify {label} artifact: {exc}")
            continue
        if marker.get(f"{label}_size") != size or marker.get(f"{label}_sha256") != digest:
            errors.append(f"terminal marker {label} hash or size does not match")
    return errors


def run_reviewer_worker(task_path: Path) -> int:
    try:
        task = json.loads(task_path.resolve().read_text(encoding="utf-8"))
        reviewer = Reviewer(
            int(task["ordinal"]),
            str(task["key"]),
            str(task["display_name"]),
            str(task["model_id"]),
            task.get("variant"),
        )
        workspace = Path(task["workspace"]).resolve()
        prompt_path = Path(task["prompt_path"]).resolve()
        events_path = Path(task["events_path"]).resolve()
        stderr_path = Path(task["stderr_path"]).resolve()
        terminal_path = Path(task["terminal_path"]).resolve()
        opencode_bin = str(task["opencode_bin"])
        timeout_seconds = int(task["timeout_seconds"])
        config_hash = str(task["reviewer_config_sha256"])
        run_id = str(task["run_id"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"invalid reviewer worker task: {exc}", file=sys.stderr)
        return 64

    started_at = iso_now()
    started_monotonic = time.monotonic()
    exit_code = 127
    timed_out = False
    launch_error: str | None = None
    interrupted_signal: int | None = None
    proc: subprocess.Popen[bytes] | None = None
    environment = os.environ.copy()
    environment.pop(NOTIFICATION_WEBHOOK_ENV, None)
    environment["OPENCODE_CONFIG_CONTENT"] = canonical_json(opencode_agent_config())
    environment["OPENCODE_AUTO_SHARE"] = "false"

    def worker_signal(signum: int, _frame: Any) -> None:
        raise RunInterrupted(signum)

    old_sigterm = signal.signal(signal.SIGTERM, worker_signal)
    try:
        prompt = prompt_path.read_bytes()
        with events_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            try:
                proc = subprocess.Popen(
                    build_command(opencode_bin, reviewer, workspace),
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
                try:
                    proc.communicate(input=prompt, timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process(proc)
                exit_code = proc.returncode if proc.returncode is not None else 124
            except RunInterrupted as exc:
                interrupted_signal = exc.signum
                if proc is not None:
                    terminate_process(proc)
                exit_code = 128 + exc.signum
            except OSError as exc:
                launch_error = f"failed to launch OpenCode CLI: {exc}"
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)

    completed_at = iso_now()
    marker = {
        "schema_version": TERMINAL_MARKER_SCHEMA_VERSION,
        "completed": True,
        "run_id": run_id,
        "ordinal": reviewer.ordinal,
        "reviewer_key": reviewer.key,
        "model_id": reviewer.model_id,
        "variant": reviewer.variant,
        "reviewer_config_sha256": config_hash,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": time.monotonic() - started_monotonic,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "interrupted_signal": interrupted_signal,
        "launch_error": launch_error,
        "events_size": events_path.stat().st_size,
        "events_sha256": file_sha256(events_path),
        "stderr_size": stderr_path.stat().st_size,
        "stderr_sha256": file_sha256(stderr_path),
    }
    write_json_atomic(terminal_path, marker)
    return 0


def markdown_report(
    reviewer: Reviewer,
    result: ReviewResult,
    final_text: str | None,
    stderr_text: str,
) -> str:
    started_at = result.started_at or "not available"
    completed_at = result.completed_at or "not available"
    duration = (
        f"{result.duration_seconds:.3f}s"
        if result.duration_seconds is not None
        else "not available"
    )
    lines = [
        f"# Monju review — {reviewer.display_name}",
        "",
        f"- Requested model: `{result.requested_model}`",
        f"- Requested variant: `{result.requested_variant or 'default'}`",
        "- Reported model: `not exposed by OpenCode JSON output`",
        f"- Model verified: `{'yes' if result.model_verified else 'no'}`",
        f"- Verification source: `{result.model_verification_source}`",
        f"- Status: `{result.status}`",
        f"- Session ID: `{result.session_id or 'not reported'}`",
        f"- Started: `{started_at}`",
        f"- Completed: `{completed_at}`",
        f"- Duration: `{duration}`",
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
                result.error or "OpenCode did not emit a final review.",
                "",
            ]
        )
        if stderr_text.strip():
            excerpt = stderr_text.strip()[-4000:]
            lines.extend(["### stderr excerpt", "", "```text", excerpt, "```", ""])
    return "\n".join(lines)


def run_reviewer(
    opencode_bin: str,
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
    terminal_path = terminal_marker_path(staging_dir, run_id, reviewer)
    task_path = staging_dir / f"{stem}.worker-task.json"
    effective_prompt_path = staging_dir / f"{run_id}-00-effective-prompt.md"
    command = build_command(opencode_bin, reviewer, workspace)
    write_json_atomic(
        task_path,
        {
            **reviewers_payload((reviewer,))[0],
            "workspace": str(workspace),
            "prompt_path": str(effective_prompt_path),
            "events_path": str(events_path),
            "stderr_path": str(stderr_path),
            "terminal_path": str(terminal_path),
            "opencode_bin": opencode_bin,
            "timeout_seconds": timeout_seconds,
            "run_id": run_id,
            "reviewer_config_sha256": REVIEWER_CONFIG_HASH,
        },
    )
    worker_command = [sys.executable, str(Path(__file__).resolve()), "--reviewer-worker", str(task_path)]
    worker_exit = 127
    try:
        proc = subprocess.Popen(
            worker_command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        coordinator.register(reviewer.ordinal, proc)
        try:
            try:
                worker_exit = proc.wait(timeout=timeout_seconds + 30)
            except subprocess.TimeoutExpired:
                terminate_process(proc)
                worker_exit = proc.returncode if proc.returncode is not None else 124
        finally:
            coordinator.unregister(reviewer.ordinal, proc)
    except OSError:
        events_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)

    marker: dict[str, Any] | None = None
    marker_errors: list[str] = []
    try:
        decoded = json.loads(terminal_path.read_text(encoding="utf-8"))
        marker = decoded if isinstance(decoded, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        marker_errors.append(f"terminal marker unavailable: {exc}")
    if marker is not None:
        marker_errors.extend(
            validate_terminal_marker(marker, reviewer, run_id, events_path, stderr_path)
        )
    parsed = inspect_event_stream(events_path)
    verified = not marker_errors and catalog_model_verified(reviewer)
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")

    error_parts = list(marker_errors)
    if worker_exit != 0:
        error_parts.append(f"reviewer worker exited with status {worker_exit}")
    exit_code = marker.get("exit_code") if marker is not None else None
    timed_out = marker.get("timed_out") if marker is not None else None
    if marker is not None and marker.get("launch_error"):
        error_parts.append(str(marker["launch_error"]))
    if timed_out:
        error_parts.append(f"review timed out after {timeout_seconds} seconds")
    if exit_code != 0:
        error_parts.append(f"OpenCode CLI exited with status {exit_code}")
    if not catalog_model_verified(reviewer):
        error_parts.append("requested model or variant was not verified by preflight catalog")
    error_parts.extend(parsed_review_errors(reviewer, parsed))

    status = "success" if not error_parts else "failed"
    result = ReviewResult(
        reviewer=reviewer.display_name,
        requested_model=reviewer.model_id,
        requested_variant=reviewer.variant,
        reported_model=None,
        model_verified=verified,
        model_verification_source="preflight_catalog_and_worker_terminal_marker",
        session_id=parsed.session_id,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=marker.get("started_at") if marker is not None else None,
        completed_at=marker.get("completed_at") if marker is not None else None,
        duration_seconds=marker.get("duration_seconds") if marker is not None else None,
        markdown_file=markdown_path.name,
        events_file=events_path.name,
        stderr_file=stderr_path.name,
        command=command,
        error="; ".join(error_parts) if error_parts else None,
        warnings=list(parsed.warnings),
    )
    markdown_path.write_text(
        markdown_report(reviewer, result, parsed.final_text, stderr_text),
        encoding="utf-8",
    )
    return result


def make_worker_failure_result(
    reviewer: Reviewer,
    staging_dir: Path,
    run_id: str,
    opencode_bin: str,
    effective_prompt: str,
    exc: BaseException,
) -> ReviewResult:
    stem = f"{run_id}-{reviewer.ordinal:02d}-{reviewer.key}"
    markdown_path = staging_dir / f"{stem}.md"
    events_path = staging_dir / f"{stem}.events.jsonl"
    stderr_path = staging_dir / f"{stem}.stderr.log"
    error = f"review worker crashed: {type(exc).__name__}: {exc}"
    timestamp = iso_now()
    command = build_command(opencode_bin, reviewer, Path("<workspace>"))
    result = ReviewResult(
        reviewer=reviewer.display_name,
        requested_model=reviewer.model_id,
        requested_variant=reviewer.variant,
        reported_model=None,
        model_verified=False,
        model_verification_source="worker_failure",
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
        command=command,
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


def tmux_launch_marker_path(run_dir: Path, run_id: str) -> Path:
    return run_dir / f".{run_id}-tmux-session"


def tmux_webhook_handoff_path(run_dir: Path, run_id: str) -> Path:
    return run_dir / f".{run_id}-tmux-webhook"


def prepare_tmux_webhook_handoff(
    run_dir: Path,
    run_id: str,
    notification_mode: str,
) -> Path | None:
    if notification_mode not in {"auto", "webhook"}:
        return None
    webhook = os.environ.get(NOTIFICATION_WEBHOOK_ENV)
    if not webhook:
        return None
    path = tmux_webhook_handoff_path(run_dir, run_id)
    atomic_write_text(path, webhook)
    return path


def consume_tmux_webhook_handoff(
    configured_path: Path | None,
    run_dir: Path,
    run_id: str,
) -> None:
    if configured_path is None:
        return
    expected = tmux_webhook_handoff_path(run_dir, run_id)
    resolved = configured_path.expanduser().resolve()
    if resolved != expected.resolve():
        raise SystemExit("invalid tmux webhook handoff path")
    try:
        webhook = resolved.read_text(encoding="utf-8")
    finally:
        try:
            resolved.unlink()
        except OSError:
            pass
    if not webhook:
        raise SystemExit("tmux webhook handoff is empty")
    os.environ[NOTIFICATION_WEBHOOK_ENV] = webhook


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
    execution_mode: str = EXECUTION_MODE,
    tmux_session: str | None = None,
) -> None:
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend": BACKEND,
        "provider": PROVIDER,
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "workspace": str(workspace),
        "source_prompt_file": str(prompt_file),
        "effective_prompt_sha256": hashlib.sha256(
            effective_prompt.encode("utf-8")
        ).hexdigest(),
        "reviewers": reviewers_payload(REVIEWERS),
        "reviewer_config_sha256": REVIEWER_CONFIG_HASH,
        "catalog_verification": CATALOG_VERIFICATION,
        "parallel_reviewer_count": len(REVIEWERS),
        "execution_mode": execution_mode,
        "supervisor_pid": os.getpid(),
        "notification": notification_pending(notification_mode),
        "results": [],
    }
    if tmux_session is not None:
        payload["tmux_session"] = tmux_session
    write_json_atomic(manifest_path(run_dir, run_id), payload)


def write_final_manifest(
    staging_dir: Path,
    run_id: str,
    workspace: Path,
    prompt_file: Path,
    effective_prompt: str,
    results: list[ReviewResult],
    started_at: str | None,
    interrupted_signal: int | None,
    notification_mode: str,
    *,
    execution_mode: str = EXECUTION_MODE,
    tmux_session: str | None = None,
    supervisor_pid: int | None = None,
    completed_at: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> str:
    status = classify_status(results, interrupted_signal is not None)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend": BACKEND,
        "provider": PROVIDER,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at or iso_now(),
        "workspace": str(workspace),
        "source_prompt_file": str(prompt_file),
        "effective_prompt_sha256": hashlib.sha256(
            effective_prompt.encode("utf-8")
        ).hexdigest(),
        "reviewers": reviewers_payload(REVIEWERS),
        "reviewer_config_sha256": REVIEWER_CONFIG_HASH,
        "catalog_verification": CATALOG_VERIFICATION,
        "parallel_reviewer_count": len(REVIEWERS),
        "execution_mode": execution_mode,
        "supervisor_pid": os.getpid() if supervisor_pid is None else supervisor_pid,
        "interrupted_signal": interrupted_signal,
        "notification": notification_pending(notification_mode),
        "results": [asdict(result) for result in results],
    }
    if tmux_session is not None:
        payload["tmux_session"] = tmux_session
    if extra_fields:
        payload.update(extra_fields)
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


def opencode_state_directories() -> tuple[Path, Path]:
    return (
        Path.home() / ".local" / "share" / "opencode",
        Path.home() / ".cache" / "opencode",
    )


def verify_opencode_state_writable() -> tuple[Path, Path]:
    directories = opencode_state_directories()
    for target in directories:
        probe = target / f".monju-write-probe-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            target.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("Monju OpenCode state write probe\n")
            probe.unlink()
        except OSError as exc:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
            raise SystemExit(
                "PREFLIGHT=opencode_state_unwritable\n"
                f"OpenCode cannot write its state directory: {target}\n"
                f"{type(exc).__name__}: {exc}\n"
                "Rerun this preflight and the eventual review launch outside the filesystem sandbox."
            ) from exc
    return directories


def run_cli_capture(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"could not run OpenCode CLI preflight: {exc}") from exc


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def verify_opencode_authentication(opencode_bin: str) -> None:
    result = run_cli_capture([opencode_bin, "auth", "list"])
    output = strip_ansi(result.stdout)
    if result.returncode != 0 or "0 credentials" in output or "OpenCode Go" not in output:
        detail = output.strip()[-2000:] or "no authentication output"
        raise SystemExit(
            "PREFLIGHT=opencode_auth_required\n"
            "OpenCode Go authentication is not available. Run this yourself and retry:\n"
            f"  {shlex.quote(opencode_bin)} auth login --provider {PROVIDER}\n"
            f"Authentication output:\n{detail}"
        )


def parse_verbose_model_catalog(output: str) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    decoder = json.JSONDecoder()
    position = 0
    pattern = re.compile(rf"(?m)^({re.escape(PROVIDER)}/[^\s]+)\s*$")
    while match := pattern.search(output, position):
        cursor = match.end()
        while cursor < len(output) and output[cursor].isspace():
            cursor += 1
        try:
            metadata, end = decoder.raw_decode(output, cursor)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed verbose model catalog after {match.group(1)}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"model metadata for {match.group(1)} is not an object")
        variants = metadata.get("variants")
        models[match.group(1)] = {
            "status": metadata.get("status"),
            "variants": sorted(variants) if isinstance(variants, dict) else [],
        }
        position = end
    if not models:
        raise ValueError("OpenCode returned no parseable OpenCode Go models")
    return models


def verify_model_catalog(opencode_bin: str) -> dict[str, Any]:
    result = run_cli_capture([opencode_bin, "models", PROVIDER, "--verbose"], timeout=60)
    output = strip_ansi(result.stdout)
    if result.returncode != 0:
        raise SystemExit(
            "PREFLIGHT=opencode_models_unavailable\n"
            f"OpenCode Go model catalog failed:\n{output.strip()[-3000:]}"
        )
    try:
        models = parse_verbose_model_catalog(output)
    except ValueError as exc:
        raise SystemExit(f"PREFLIGHT=opencode_models_invalid\n{exc}") from exc
    errors: list[str] = []
    for reviewer in REVIEWERS:
        metadata = models.get(reviewer.model_id)
        if metadata is None:
            errors.append(f"model is unavailable: {reviewer.model_id}")
            continue
        if metadata.get("status") not in {None, "active"}:
            errors.append(f"model is not active: {reviewer.model_id}")
        if reviewer.variant is not None and reviewer.variant not in metadata["variants"]:
            errors.append(
                f"variant is unavailable: {reviewer.model_id}/{reviewer.variant}; "
                f"available={metadata['variants']}"
            )
    if errors:
        raise SystemExit("PREFLIGHT=reviewer_model_invalid\n" + "\n".join(errors))
    return {"verified_at": iso_now(), "provider": PROVIDER, "models": models}


def run_preflight_checks(workspace: Path, opencode_bin: str) -> dict[str, Any]:
    del workspace
    state_dirs = verify_opencode_state_writable()
    verify_opencode_authentication(opencode_bin)
    catalog = verify_model_catalog(opencode_bin)
    catalog["state_directories"] = [str(path) for path in state_dirs]
    global CATALOG_VERIFICATION
    CATALOG_VERIFICATION = catalog
    return catalog


def preflight_run(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    opencode_bin = resolve_executable(args.opencode_bin)
    tmux_bin = resolve_tmux_executable(args.tmux_bin)
    tmux_server = inspect_tmux_server(tmux_bin)
    catalog = run_preflight_checks(workspace, opencode_bin)
    print("PREFLIGHT=ok")
    print(f"WORKSPACE={workspace}")
    print(f"OPENCODE_BIN={opencode_bin}")
    print(f"TMUX_BIN={tmux_bin}")
    print(f"TMUX_SERVER={tmux_server}")
    print(f"PROVIDER={PROVIDER}")
    print(f"REVIEWERS={len(REVIEWERS)}")
    print(f"CATALOG_MODELS={len(catalog['models'])}")
    return 0


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


def cleanup_published_staging(
    staging_parent: Path,
    staging_pointer: Path,
) -> None:
    try:
        shutil.rmtree(staging_parent)
    except Exception as exc:  # noqa: BLE001 - published results remain authoritative.
        print(
            f"warning: published reviews but could not remove staging "
            f"{staging_parent}: {exc}",
            file=sys.stderr,
        )
        return
    try:
        staging_pointer.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
        print(
            f"warning: published reviews but could not remove staging pointer "
            f"{staging_pointer}: {exc}",
            file=sys.stderr,
        )


def terminal_exit_code(status: str) -> int:
    if status == "success":
        return 0
    if status == "partial_failure":
        return 3
    return 4


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
            "schema_version": MANIFEST_SCHEMA_VERSION,
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
            "schema_version": MANIFEST_SCHEMA_VERSION,
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
    opencode_bin: str,
    timeout_seconds: int,
    review_brief: str,
    effective_prompt: str,
    run_started_at: str,
    notification_mode: str,
    execution_mode: str = EXECUTION_MODE,
    tmux_session: str | None = None,
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
                opencode_bin=opencode_bin,
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
                opencode_bin,
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
    sighup = getattr(signal, "SIGHUP", None)
    old_sighup = signal.signal(sighup, sigterm_handler) if sighup is not None else None
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
        with SupervisorHeartbeat(run_id):
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
                signal.signal(exc.signum, signal.SIG_IGN)
                coordinator.terminate_all()
            finally:
                if interrupted_signal is not None:
                    coordinator.terminate_all()
                for thread in started_threads:
                    thread.join()
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        if sighup is not None and old_sighup is not None:
            signal.signal(sighup, old_sighup)

    results: list[ReviewResult] = []
    for reviewer in REVIEWERS:
        result = results_by_ordinal.get(reviewer.ordinal)
        if result is None:
            result = make_worker_failure_result(
                reviewer,
                staging_dir,
                run_id,
                opencode_bin,
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
        execution_mode=execution_mode,
        tmux_session=tmux_session,
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

    cleanup_published_staging(staging_parent, staging_pointer)
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
    return terminal_exit_code(status)


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


def has_meaningful_startup_stderr(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(
            fragment in stripped
            for fragment in IGNORED_STARTUP_STDERR_FRAGMENTS
        ):
            continue
        return True
    return False


def reviewer_artifact_paths(
    staging_dir: Path,
    run_id: str,
    reviewer: Reviewer,
) -> tuple[Path, Path, Path]:
    stem = f"{run_id}-{reviewer.ordinal:02d}-{reviewer.key}"
    return (
        staging_dir / f"{stem}.events.jsonl",
        staging_dir / f"{stem}.stderr.log",
        staging_dir / f"{stem}.md",
    )


def resolve_preserved_staging(run_dir: Path, run_id: str) -> Path:
    pointer = run_dir / f".{run_id}-staging"
    if not pointer.is_file():
        raise ValueError(f"staging pointer does not exist: {pointer}")
    try:
        raw_path = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"could not read staging pointer: {exc}") from exc
    if not raw_path:
        raise ValueError(f"staging pointer is empty: {pointer}")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValueError("staging pointer must contain an absolute path")
    try:
        staging_dir = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"could not resolve preserved staging path: {exc}") from exc
    expected_parent_prefix = f"{run_id}-staging-"
    if (
        staging_dir.name != run_id
        or not staging_dir.parent.name.startswith(expected_parent_prefix)
        or staging_dir == run_dir
    ):
        raise ValueError(
            "staging pointer does not identify a Monju-owned preserved staging "
            "directory"
        )
    if not staging_dir.is_dir():
        raise ValueError(f"preserved staging directory does not exist: {staging_dir}")
    return staging_dir


def assess_recovery(run_dir: Path, run_id: str) -> RecoveryAssessment:
    try:
        staging_dir = resolve_preserved_staging(run_dir, run_id)
    except ValueError as exc:
        return RecoveryAssessment(
            state="invalid",
            staging_dir=None,
            streams=(),
            terminal_results=0,
            successful_results=0,
            can_publish=False,
            errors=(str(exc),),
        )

    streams: list[RecoveryStream] = []
    assessment_errors: list[str] = []
    terminal_results = 0
    successful_results = 0
    invalid = False
    for reviewer in REVIEWERS:
        events_path, stderr_path, _ = reviewer_artifact_paths(
            staging_dir,
            run_id,
            reviewer,
        )
        parsed: ParsedEventStream | None = None
        terminal: dict[str, Any] | None = None
        terminal_path = terminal_marker_path(staging_dir, run_id, reviewer)
        read_error: str | None = None
        validation_error_list: list[str] = []
        terminal_complete = False
        if not events_path.is_file():
            read_error = f"event stream is missing for {reviewer.display_name}"
        else:
            try:
                parsed = inspect_event_stream(events_path)
            except OSError as exc:
                read_error = (
                    f"could not read event stream for {reviewer.display_name}: "
                    f"{type(exc).__name__}: {exc}"
                )
                invalid = True
            else:
                validation_error_list.extend(parsed_review_errors(reviewer, parsed))
        if terminal_path.is_file():
            try:
                decoded = json.loads(terminal_path.read_text(encoding="utf-8"))
                terminal = decoded if isinstance(decoded, dict) else None
            except (OSError, json.JSONDecodeError) as exc:
                validation_error_list.append(f"malformed terminal marker: {exc}")
                invalid = True
            if terminal is not None:
                marker_errors = validate_terminal_marker(
                    terminal, reviewer, run_id, events_path, stderr_path
                )
                if marker_errors:
                    validation_error_list.extend(marker_errors)
                    invalid = True
                else:
                    terminal_complete = True
                    terminal_results += 1
                    if terminal.get("timed_out"):
                        validation_error_list.append("review timed out")
                    if terminal.get("launch_error"):
                        validation_error_list.append(str(terminal["launch_error"]))
                    if terminal.get("exit_code") != 0:
                        validation_error_list.append(
                            f"OpenCode CLI exited with status {terminal.get('exit_code')}"
                        )
                    if not catalog_model_verified(reviewer):
                        validation_error_list.append(
                            "requested model or variant was not verified at launch"
                        )
        validation_errors = tuple(validation_error_list)
        if terminal_complete and validation_errors:
            invalid = True
        if terminal_complete and not validation_errors:
            successful_results += 1
        if read_error:
            assessment_errors.append(read_error)
        assessment_errors.extend(
            f"{reviewer.display_name}: {error}" for error in validation_errors
        )
        streams.append(
            RecoveryStream(
                reviewer=reviewer,
                events_path=events_path,
                stderr_path=stderr_path,
                terminal_path=terminal_path,
                parsed=parsed,
                terminal=terminal,
                terminal_complete=terminal_complete,
                validation_errors=validation_errors,
                read_error=read_error,
            )
        )

    can_publish = terminal_results == len(REVIEWERS)
    if invalid:
        state = "invalid"
    elif can_publish:
        state = "ready"
    else:
        state = "not_ready"
    return RecoveryAssessment(
        state=state,
        staging_dir=staging_dir,
        streams=tuple(streams),
        terminal_results=terminal_results,
        successful_results=successful_results,
        can_publish=can_publish,
        errors=tuple(assessment_errors),
    )


def diagnose_stale_run(run_dir: Path, run_id: str) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "reason": "external_termination_before_startup",
        "reviewers_with_events": 0,
        "initialized_reviewers": 0,
        "completed_reviewers": 0,
        "startup_error_reviewers": 0,
        "supervisor_stderr": False,
        "staging_preserved": None,
    }
    pointer = run_dir / f".{run_id}-staging"
    try:
        staging_dir = Path(pointer.read_text(encoding="utf-8").strip())
    except OSError:
        return diagnostics
    diagnostics["staging_preserved"] = str(staging_dir)

    event_bytes = 0
    for reviewer in REVIEWERS:
        stem = f"{run_id}-{reviewer.ordinal:02d}-{reviewer.key}"
        events_path = staging_dir / f"{stem}.events.jsonl"
        stderr_path = staging_dir / f"{stem}.stderr.log"
        try:
            event_size = events_path.stat().st_size
        except OSError:
            event_size = 0
        if event_size:
            diagnostics["reviewers_with_events"] += 1
            event_bytes += event_size
        try:
            parsed = inspect_event_stream(events_path)
        except OSError:
            parsed = None
        if parsed is not None and parsed.event_count:
            diagnostics["initialized_reviewers"] += 1
        terminal_path = terminal_marker_path(staging_dir, run_id, reviewer)
        if terminal_path.is_file():
            diagnostics["completed_reviewers"] += 1
        try:
            stderr_text = stderr_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            stderr_text = ""
        if (
            (parsed is not None and parsed.stream_error)
            or has_meaningful_startup_stderr(stderr_text)
        ):
            diagnostics["startup_error_reviewers"] += 1

    supervisor_stderr_path = run_dir / f"{run_id}-runner.stderr.log"
    try:
        supervisor_stderr = supervisor_stderr_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        supervisor_stderr = ""
    diagnostics["supervisor_stderr"] = has_meaningful_startup_stderr(
        supervisor_stderr
    )

    if diagnostics["completed_reviewers"]:
        diagnostics["reason"] = "external_termination_after_results"
    elif diagnostics["initialized_reviewers"]:
        diagnostics["reason"] = "external_termination_after_startup"
    elif (
        diagnostics["startup_error_reviewers"]
        or diagnostics["supervisor_stderr"]
        or event_bytes
    ):
        diagnostics["reason"] = "startup_failure"
    return diagnostics


def read_status(args: argparse.Namespace) -> int:
    if args.run_dir is None:
        raise SystemExit("--status requires --run-dir")
    run_id, run_dir = validate_run_dir(args.run_dir)
    path = manifest_path(run_dir, run_id)
    if not path.is_file():
        prompt_file = default_prompt_path(run_dir, run_id)
        tmux_alive = False
        tmux_error: str | None = None
        tmux_marker = tmux_launch_marker_path(run_dir, run_id)
        if prompt_file.is_file() and tmux_marker.is_file():
            try:
                tmux_bin = resolve_tmux_executable(
                    str(getattr(args, "tmux_bin", DEFAULT_TMUX_BIN))
                )
                tmux_alive = tmux_session_is_running(tmux_bin, run_id)
            except (FileNotFoundError, RuntimeError) as exc:
                tmux_error = str(exc)
        if tmux_alive:
            status = "tmux_starting"
        elif tmux_marker.is_file():
            status = "tmux_launch_failed"
        else:
            status = "prepared" if prompt_file.is_file() else "unknown"
        print(f"RUN_DIR={run_dir}")
        print(f"STATUS={status}")
        if tmux_alive:
            print(f"EXECUTION_MODE={TMUX_EXECUTION_MODE}")
            print(f"TMUX_SESSION={run_id}")
            print("TMUX_SESSION_ALIVE=yes")
        elif tmux_marker.is_file() and tmux_error is not None:
            print("TMUX_SESSION_ALIVE=unknown")
            print(f"TMUX_SESSION_REASON={tmux_error}")
        return 0

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid manifest JSON: {path}: {exc}") from exc
    status = str(payload.get("status") or "unknown")
    execution_mode = str(payload.get("execution_mode") or EXECUTION_MODE)
    tmux_session: str | None = None
    tmux_session_alive: bool | None = None
    tmux_session_error: str | None = None
    if execution_mode == TMUX_EXECUTION_MODE:
        raw_tmux_session = payload.get("tmux_session")
        if isinstance(raw_tmux_session, str) and raw_tmux_session == run_id:
            tmux_session = raw_tmux_session
            try:
                tmux_bin = resolve_tmux_executable(
                    str(getattr(args, "tmux_bin", DEFAULT_TMUX_BIN))
                )
                tmux_session_alive = tmux_session_is_running(
                    tmux_bin,
                    tmux_session,
                )
            except (FileNotFoundError, RuntimeError) as exc:
                tmux_session_error = str(exc)
        else:
            tmux_session_error = "manifest has no valid tmux session"
    pid_file = run_dir / f"{run_id}-runner.pid"
    runner_pid: str | None = None
    if pid_file.is_file():
        runner_pid = pid_file.read_text(encoding="utf-8").strip()
    elif isinstance(payload.get("supervisor_pid"), int):
        runner_pid = str(payload["supervisor_pid"])
    stale_diagnostics: dict[str, Any] | None = None
    recovery: RecoveryAssessment | None = None
    recovery_config_error: str | None = None
    recovery_candidate = status == "running" or status in RECOVERABLE_FAILURE_STATUSES
    if recovery_candidate:
        try:
            manifest_reviewers = reviewers_from_manifest(payload)
            configure_reviewers(manifest_reviewers, str(payload["reviewer_config_sha256"]))
            catalog = payload.get("catalog_verification")
            if not isinstance(catalog, dict):
                raise ValueError("running manifest has no catalog verification")
            global CATALOG_VERIFICATION
            CATALOG_VERIFICATION = catalog
        except (KeyError, ValueError) as exc:
            recovery_config_error = str(exc)
    if status == "running" and runner_pid:
        if tmux_session_alive is not None:
            runner_running = tmux_session_alive
        else:
            try:
                runner_running = process_is_running(int(runner_pid))
            except (OSError, ValueError):
                runner_running = False
        if not runner_running:
            status = "stale_running"
            if recovery_config_error is None:
                stale_diagnostics = diagnose_stale_run(run_dir, run_id)
                recovery = assess_recovery(run_dir, run_id)
    elif status in RECOVERABLE_FAILURE_STATUSES and recovery_config_error is None:
        recovery = assess_recovery(run_dir, run_id)
    print(f"RUN_DIR={run_dir}")
    print(f"STATUS={status}")
    print(f"MANIFEST={path}")
    if runner_pid:
        print(f"RUNNER_PID={runner_pid}")
    if execution_mode == TMUX_EXECUTION_MODE:
        if tmux_session is not None:
            print(f"TMUX_SESSION={tmux_session}")
        if tmux_session_alive is not None:
            print(
                "TMUX_SESSION_ALIVE="
                f"{'yes' if tmux_session_alive else 'no'}"
            )
        else:
            print("TMUX_SESSION_ALIVE=unknown")
        if tmux_session_error is not None:
            print(f"TMUX_SESSION_REASON={tmux_session_error}")
    if recovery_candidate and recovery_config_error is not None:
        print("RECOVERY=invalid")
        print(f"RECOVERY_REASON={recovery_config_error}")
    elif stale_diagnostics is not None:
        print(f"STALE_REASON={stale_diagnostics['reason']}")
        print(
            "STALE_PROGRESS="
            f"events:{stale_diagnostics['reviewers_with_events']}/"
            f"{len(REVIEWERS)} "
            f"initialized:{stale_diagnostics['initialized_reviewers']}/"
            f"{len(REVIEWERS)} "
            f"completed:{stale_diagnostics['completed_reviewers']}/"
            f"{len(REVIEWERS)} "
            f"startup_errors:{stale_diagnostics['startup_error_reviewers']}/"
            f"{len(REVIEWERS)} "
            "supervisor_stderr:"
            f"{int(stale_diagnostics['supervisor_stderr'])}"
        )
        staging_preserved = stale_diagnostics["staging_preserved"]
        if staging_preserved:
            print(f"STAGING_PRESERVED={staging_preserved}")
        if recovery is not None:
            print(f"RECOVERY={recovery.state}")
            print(
                f"RECOVERY_TERMINAL_RESULTS={recovery.terminal_results}/"
                f"{len(REVIEWERS)}"
            )
            print(
                f"RECOVERY_SUCCESSFUL_RESULTS={recovery.successful_results}/"
                f"{len(REVIEWERS)}"
            )
            print(
                "RECOVERY_CAN_PUBLISH="
                f"{'yes' if recovery.can_publish else 'no'}"
            )
    elif status in RECOVERABLE_FAILURE_STATUSES and recovery is not None:
        print(f"RECOVERY={recovery.state}")
        print(
            f"RECOVERY_TERMINAL_RESULTS={recovery.terminal_results}/"
            f"{len(REVIEWERS)}"
        )
        print(
            f"RECOVERY_SUCCESSFUL_RESULTS={recovery.successful_results}/"
            f"{len(REVIEWERS)}"
        )
        print(
            "RECOVERY_CAN_PUBLISH="
            f"{'yes' if recovery.can_publish else 'no'}"
        )
    elif status in PUBLISHED_STATUSES:
        print("RECOVERY=published")
    elif status != "running":
        print("RECOVERY=invalid")
    for result in payload.get("results", []) if status in PUBLISHED_STATUSES else []:
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


def read_manifest_for_recovery(run_dir: Path, run_id: str) -> dict[str, Any]:
    path = manifest_path(run_dir, run_id)
    if not path.is_file():
        raise ValueError(f"manifest does not exist: {path}")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not read manifest: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise ValueError("manifest root must be a JSON object")
    manifest_run_id = decoded.get("run_id")
    if manifest_run_id is not None and manifest_run_id != run_id:
        raise ValueError(
            f"manifest run_id does not match its directory: {manifest_run_id!r}"
        )
    return decoded


def recorded_supervisor_pid(
    payload: dict[str, Any],
    run_dir: Path,
    run_id: str,
) -> int:
    value = payload.get("supervisor_pid")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    pid_path = run_dir / f"{run_id}-runner.pid"
    try:
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw_pid)
    except (OSError, ValueError) as exc:
        raise ValueError("running manifest has no valid recorded supervisor PID") from exc
    if pid <= 0:
        raise ValueError("running manifest has no valid recorded supervisor PID")
    return pid


def validate_recovery_prompt(
    payload: dict[str, Any],
    run_dir: Path,
    run_id: str,
    staging_dir: Path,
) -> tuple[Path, str, str]:
    prompt_file = default_prompt_path(run_dir, run_id)
    review_brief, effective_prompt = read_review_brief(prompt_file)
    expected_hash = payload.get("effective_prompt_sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}",
        expected_hash,
    ):
        raise ValueError("running manifest has no valid effective prompt hash")
    calculated_hash = hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest()
    if calculated_hash != expected_hash:
        raise ValueError(
            "current review brief does not match the prompt recorded at launch"
        )
    staged_prompt = staging_dir / f"{run_id}-00-effective-prompt.md"
    try:
        staged_hash = hashlib.sha256(staged_prompt.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(
            f"could not read staged effective prompt: {type(exc).__name__}: {exc}"
        ) from exc
    if staged_hash != expected_hash:
        raise ValueError(
            "staged effective prompt does not match the prompt recorded at launch"
        )
    return prompt_file, review_brief, effective_prompt


def recovered_review_result(
    stream: RecoveryStream,
    run_id: str,
) -> tuple[ReviewResult, str | None, str]:
    parsed = stream.parsed
    if parsed is None or not stream.terminal_complete:
        raise ValueError(
            f"terminal event stream is unavailable for {stream.reviewer.display_name}"
        )
    _, _, markdown_path = reviewer_artifact_paths(
        stream.events_path.parent,
        run_id,
        stream.reviewer,
    )
    stderr_path = stream.stderr_path
    try:
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr_text = ""
    terminal = stream.terminal or {}
    errors = list(stream.validation_errors)
    verified = not errors and catalog_model_verified(stream.reviewer)
    result = ReviewResult(
        reviewer=stream.reviewer.display_name,
        requested_model=stream.reviewer.model_id,
        requested_variant=stream.reviewer.variant,
        reported_model=None,
        model_verified=verified,
        model_verification_source="launch_catalog_and_preserved_terminal_marker",
        session_id=parsed.session_id,
        status="success" if not errors else "failed",
        exit_code=terminal.get("exit_code"),
        timed_out=terminal.get("timed_out"),
        started_at=terminal.get("started_at"),
        completed_at=terminal.get("completed_at"),
        duration_seconds=terminal.get("duration_seconds"),
        markdown_file=markdown_path.name,
        events_file=stream.events_path.name,
        stderr_file=stderr_path.name,
        command=None,
        error="; ".join(errors) if errors else None,
        warnings=[
            *parsed.warnings,
            (
                "Recovered from a preserved OpenCode event stream and verified "
                "worker terminal marker after supervisor loss."
            ),
        ],
        recovered=True,
        timing_source={
            "started_at": "worker_terminal_marker",
            "completed_at": "worker_terminal_marker",
            "duration_seconds": "worker_terminal_marker",
        },
        recovery_validation={
            "event_count": parsed.event_count,
            "text_event_count": parsed.text_event_count,
            "terminal_marker": stream.terminal_path.name,
            "malformed_event_lines": list(parsed.malformed_lines),
            "model_verified": verified,
            "errors": errors,
        },
    )
    return result, parsed.final_text, stderr_text


def report_recovery_refusal(
    run_dir: Path,
    status: str,
    recovery: str,
    staging_dir: Path | None = None,
    detail: str | None = None,
) -> int:
    print(f"RUN_DIR={run_dir}")
    print(f"STATUS={status}")
    print(f"RECOVERY={recovery}")
    if detail:
        print(f"RECOVERY_REASON={detail}")
    if staging_dir is not None:
        print(f"STAGING_PRESERVED={staging_dir}")
    return 75 if status == "recovery_not_ready" else 65


def recover_review(args: argparse.Namespace) -> int:
    if args.run_dir is None:
        raise SystemExit("--recover requires --run-dir")
    run_id, run_dir = validate_run_dir(args.run_dir)
    try:
        payload = read_manifest_for_recovery(run_dir, run_id)
    except ValueError as exc:
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
            detail=str(exc),
        )

    current_status = str(payload.get("status") or "unknown")
    if current_status in PUBLISHED_STATUSES:
        return read_status(args)
    if (
        current_status != "running"
        and current_status not in RECOVERABLE_FAILURE_STATUSES
    ):
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
            detail=f"manifest has unsupported status: {current_status}",
        )
    try:
        manifest_reviewers = reviewers_from_manifest(payload)
        configure_reviewers(manifest_reviewers, str(payload["reviewer_config_sha256"]))
        catalog = payload.get("catalog_verification")
        if not isinstance(catalog, dict):
            raise ValueError("running manifest has no catalog verification")
        global CATALOG_VERIFICATION
        CATALOG_VERIFICATION = catalog
    except (KeyError, ValueError) as exc:
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
            detail=str(exc),
        )
    if args.notify in {"auto", "webhook"}:
        raise SystemExit(
            "--recover never uses the network; use --notify none or --notify desktop"
        )

    tmux_supervisor_absent = False
    if payload.get("execution_mode") == TMUX_EXECUTION_MODE:
        tmux_session = payload.get("tmux_session")
        if not isinstance(tmux_session, str) or tmux_session != run_id:
            return report_recovery_refusal(
                run_dir,
                "recovery_invalid",
                "invalid",
                detail="running manifest has no valid tmux session",
            )
        try:
            tmux_bin = resolve_tmux_executable(
                str(getattr(args, "tmux_bin", DEFAULT_TMUX_BIN))
            )
            if tmux_session_is_running(tmux_bin, tmux_session):
                return report_recovery_refusal(
                    run_dir,
                    "recovery_refused",
                    "refused",
                    detail="recorded tmux supervisor session is still running",
                )
            tmux_supervisor_absent = True
        except (FileNotFoundError, RuntimeError) as exc:
            return report_recovery_refusal(
                run_dir,
                "recovery_invalid",
                "invalid",
                detail=f"could not verify tmux supervisor state: {exc}",
            )

    try:
        original_supervisor_pid = recorded_supervisor_pid(
            payload,
            run_dir,
            run_id,
        )
    except ValueError as exc:
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
            detail=str(exc),
        )
    if not tmux_supervisor_absent and process_is_running(original_supervisor_pid):
        return report_recovery_refusal(
            run_dir,
            "recovery_refused",
            "refused",
            detail="recorded supervisor PID is still running",
        )

    assessment = assess_recovery(run_dir, run_id)
    if not assessment.can_publish:
        state = (
            "recovery_not_ready"
            if assessment.state == "not_ready"
            else "recovery_invalid"
        )
        if assessment.staging_dir is None and assessment.errors:
            detail = assessment.errors[0]
        elif assessment.state == "not_ready":
            detail = (
                "not all reviewers have a verified terminal marker "
                f"({assessment.terminal_results}/{len(REVIEWERS)})"
            )
        else:
            detail = (
                "one or more preserved event streams are malformed or "
                "unverifiable"
            )
        return report_recovery_refusal(
            run_dir,
            state,
            assessment.state,
            assessment.staging_dir,
            detail,
        )
    staging_dir = assessment.staging_dir
    if staging_dir is None:
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
        )

    try:
        prompt_file, _review_brief, effective_prompt = validate_recovery_prompt(
            payload,
            run_dir,
            run_id,
            staging_dir,
        )
    except (OSError, SystemExit, ValueError) as exc:
        detail = str(exc)
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
            staging_dir,
            detail,
        )

    workspace_value = payload.get("workspace")
    if not isinstance(workspace_value, str) or not workspace_value:
        return report_recovery_refusal(
            run_dir,
            "recovery_invalid",
            "invalid",
            staging_dir,
            "running manifest has no workspace",
        )
    workspace = Path(workspace_value).expanduser().resolve()
    recovered_at = iso_now()
    results: list[ReviewResult] = []
    try:
        for stream in assessment.streams:
            result, final_text, stderr_text = recovered_review_result(
                stream,
                run_id,
            )
            results.append(result)
            markdown_path = staging_dir / result.markdown_file
            atomic_write_text(
                markdown_path,
                markdown_report(
                    stream.reviewer,
                    result,
                    final_text,
                    stderr_text,
                ),
            )
        status = write_final_manifest(
            staging_dir,
            run_id,
            workspace,
            prompt_file,
            effective_prompt,
            results,
            payload.get("started_at")
            if isinstance(payload.get("started_at"), str)
            else None,
            None,
            args.notify,
            execution_mode=str(payload.get("execution_mode") or EXECUTION_MODE),
            tmux_session=(
                str(payload["tmux_session"])
                if isinstance(payload.get("tmux_session"), str)
                else None
            ),
            supervisor_pid=original_supervisor_pid,
            completed_at=recovered_at,
            extra_fields={
                "recovered": True,
                "recovered_at": recovered_at,
                "recovery_source": RECOVERY_SOURCE,
                "original_supervisor_pid": original_supervisor_pid,
                "recovery_pid": os.getpid(),
                "completed_at_source": "recovery_publication_time",
                "review_completed_at": None,
                "recovery_assessment": assessment.state,
                "recovery_previous_status": current_status,
            },
        )
    except Exception as exc:  # noqa: BLE001 - raw staging must survive recovery.
        print(f"RUN_DIR={run_dir}")
        print("STATUS=recovery_failure")
        print(f"STAGING_PRESERVED={staging_dir}")
        print(f"ERROR={type(exc).__name__}: {exc}", file=sys.stderr)
        return 74

    try:
        publish_staging(staging_dir, run_dir)
    except Exception as exc:  # noqa: BLE001 - raw staging must survive recovery.
        write_artifact_failure_manifest(run_dir, run_id, staging_dir, exc)
        notify_terminal_status(
            args.notify,
            run_dir,
            run_id,
            "artifact_failure",
        )
        print(f"RUN_DIR={run_dir}")
        print("STATUS=artifact_failure")
        print(f"STAGING_PRESERVED={staging_dir}")
        print(f"ERROR={type(exc).__name__}: {exc}", file=sys.stderr)
        return 74

    cleanup_published_staging(
        staging_dir.parent,
        run_dir / f".{run_id}-staging",
    )
    notify_terminal_status(args.notify, run_dir, run_id, status)
    print(f"RUN_DIR={run_dir}")
    print(f"STATUS={status}")
    print("RECOVERED=true")
    for result in results:
        print(
            f"RESULT {result.reviewer}: {result.status} "
            f"{run_dir / result.markdown_file}"
        )
    return terminal_exit_code(status)


def dry_run(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    run_id: str | None = None
    run_dir: Path | None = None
    if args.run_dir is not None:
        run_id, run_dir = validate_run_dir(args.run_dir)
    prompt_file = resolve_prompt_file(args, run_dir, run_id)
    _, _effective_prompt = read_review_brief(prompt_file)
    opencode_bin = resolve_executable(args.opencode_bin)
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
        "execution_mode": EXECUTION_MODE,
        "notification": {
            "mode": args.notify,
            "webhook_env": NOTIFICATION_WEBHOOK_ENV,
        },
        "commands": [
            {
                "reviewer": reviewer.display_name,
                "model_id": reviewer.model_id,
                "variant": reviewer.variant,
                "argv": build_command(opencode_bin, reviewer, workspace),
                "shell_display": shlex.join(build_command(opencode_bin, reviewer, workspace)),
                "prompt_transport": "stdin",
            }
            for reviewer in REVIEWERS
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def announce_supervisor_run(
    run_dir: Path,
    run_id: str,
    execution_mode: str,
    tmux_session: str | None,
) -> None:
    print(f"RUN_DIR={run_dir}")
    print("STATUS=running")
    print(f"EXECUTION_MODE={execution_mode}")
    if tmux_session is not None:
        print(f"TMUX_SESSION={tmux_session}")
    print(f"RUNNER_PID={os.getpid()}")
    print(f"MANIFEST={manifest_path(run_dir, run_id)}")
    sys.stdout.flush()


def run_requested_review(
    args: argparse.Namespace,
    *,
    execution_mode: str = EXECUTION_MODE,
    tmux_session: str | None = None,
) -> int:
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
    review_brief, effective_prompt = read_review_brief(prompt_file)
    opencode_bin = resolve_executable(args.opencode_bin)
    run_preflight_checks(workspace, opencode_bin)

    run_started_at = iso_now()
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
        run_started_at,
        args.notify,
        execution_mode,
        tmux_session,
    )
    atomic_write_text(
        run_dir / f"{run_id}-runner.pid",
        f"{os.getpid()}\n",
    )
    announce_supervisor_run(
        run_dir,
        run_id,
        execution_mode,
        tmux_session,
    )

    return execute_reviews(
        workspace=workspace,
        prompt_file=canonical_prompt,
        run_dir=run_dir,
        run_id=run_id,
        opencode_bin=opencode_bin,
        timeout_seconds=args.timeout_seconds,
        review_brief=review_brief,
        effective_prompt=effective_prompt,
        run_started_at=run_started_at,
        notification_mode=args.notify,
        execution_mode=execution_mode,
        tmux_session=tmux_session,
    )


def supervisor_log_path(run_dir: Path, run_id: str, stream: str) -> Path:
    return run_dir / f"{run_id}-runner.{stream}.log"


def open_private_append(path: Path) -> Any:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_APPEND | os.O_WRONLY,
        PRIVATE_FILE_MODE,
    )
    return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)


def run_tmux_supervisor(args: argparse.Namespace) -> int:
    if args.run_dir is None:
        raise SystemExit("--tmux-supervisor requires --run-dir")
    run_id, run_dir = validate_run_dir(args.run_dir)
    if args.tmux_session != run_id:
        raise SystemExit("tmux session must exactly match the validated run ID")
    if not os.environ.get("TMUX"):
        raise SystemExit("--tmux-supervisor must run inside tmux")

    stdout_path = supervisor_log_path(run_dir, run_id, "stdout")
    stderr_path = supervisor_log_path(run_dir, run_id, "stderr")
    with (
        open_private_append(stdout_path) as stdout_stream,
        open_private_append(stderr_path) as stderr_stream,
        contextlib.redirect_stdout(stdout_stream),
        contextlib.redirect_stderr(stderr_stream),
    ):
        try:
            os.environ.pop(NOTIFICATION_WEBHOOK_ENV, None)
            consume_tmux_webhook_handoff(
                args.tmux_webhook_file,
                run_dir,
                run_id,
            )
            return run_requested_review(
                args,
                execution_mode=TMUX_EXECUTION_MODE,
                tmux_session=run_id,
            )
        except SystemExit as exc:
            if exc.code is not None and exc.code != 0:
                print(str(exc.code), file=sys.stderr, flush=True)
            return exc.code if isinstance(exc.code, int) else 2
        except Exception as exc:  # noqa: BLE001 - preserve supervisor diagnostics.
            write_supervisor_failure_manifest(run_dir, run_id, exc)
            notify_terminal_status(
                args.notify,
                run_dir,
                run_id,
                "supervisor_failure",
            )
            print(
                f"Monju supervisor failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 70


def tmux_supervisor_command(
    args: argparse.Namespace,
    workspace: Path,
    run_dir: Path,
    prompt_file: Path,
    opencode_bin: str,
    tmux_session: str,
    webhook_handoff: Path | None,
) -> list[str]:
    reviewers_file = args.reviewers_file.expanduser().resolve()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--tmux-supervisor",
        "--tmux-session",
        tmux_session,
        "--workspace",
        str(workspace),
        "--run-dir",
        str(run_dir),
        "--prompt-file",
        str(prompt_file),
        "--opencode-bin",
        opencode_bin,
        "--reviewers-file",
        str(reviewers_file),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--notify",
        args.notify,
    ]
    if webhook_handoff is not None:
        command.extend(["--tmux-webhook-file", str(webhook_handoff)])
    return command


def read_supervisor_log_excerpt(run_dir: Path, run_id: str) -> str:
    path = supervisor_log_path(run_dir, run_id, "stderr")
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return content[-4000:]


def announce_tmux_launch(
    run_dir: Path,
    run_id: str,
    payload: dict[str, Any],
    tmux_server: str,
    tmux_alive: bool,
) -> None:
    print(f"RUN_DIR={run_dir}")
    print(f"STATUS={payload.get('status', 'unknown')}")
    print(f"EXECUTION_MODE={TMUX_EXECUTION_MODE}")
    print(f"TMUX_SESSION={run_id}")
    print(f"TMUX_SERVER={tmux_server}")
    print(f"TMUX_SESSION_ALIVE={'yes' if tmux_alive else 'no'}")
    supervisor_pid = payload.get("supervisor_pid")
    if isinstance(supervisor_pid, int):
        print(f"RUNNER_PID={supervisor_pid}")
    print(f"MANIFEST={manifest_path(run_dir, run_id)}")


def launch_tmux_review(args: argparse.Namespace) -> int:
    if args.run_dir is None:
        raise SystemExit("--tmux requires a prepared --run-dir")
    workspace = resolve_workspace(args)
    run_id, run_dir = validate_run_dir(args.run_dir)
    prompt_file = resolve_prompt_file(args, run_dir, run_id)
    read_review_brief(prompt_file)
    opencode_bin = resolve_executable(args.opencode_bin)
    tmux_bin = resolve_tmux_executable(args.tmux_bin)
    tmux_server = inspect_tmux_server(tmux_bin)

    if (run_dir / ".monju-started").exists():
        raise SystemExit(f"run directory was already started: {run_dir}")
    if tmux_session_is_running(tmux_bin, run_id):
        raise SystemExit(f"tmux session already exists for run: {run_id}")

    launch_marker = tmux_launch_marker_path(run_dir, run_id)
    atomic_write_text(launch_marker, f"{run_id}\n")
    webhook_handoff = prepare_tmux_webhook_handoff(
        run_dir,
        run_id,
        args.notify,
    )

    supervisor_command = tmux_supervisor_command(
        args,
        workspace,
        run_dir,
        prompt_file,
        opencode_bin,
        run_id,
        webhook_handoff,
    )
    try:
        result = run_tmux_capture(
            tmux_bin,
            [
                "new-session",
                "-d",
                "-s",
                run_id,
                "-c",
                str(workspace),
                *supervisor_command,
            ],
        )
    except Exception:
        for path_to_remove in (launch_marker, webhook_handoff):
            if path_to_remove is None:
                continue
            try:
                path_to_remove.unlink()
            except OSError:
                pass
        raise
    if result.returncode != 0:
        try:
            launch_marker.unlink()
        except OSError:
            pass
        if webhook_handoff is not None:
            try:
                webhook_handoff.unlink()
            except OSError:
                pass
        detail = strip_ansi(result.stdout).strip()
        raise SystemExit(
            "TMUX_LAUNCH=failed\n"
            f"{detail or f'tmux exited with status {result.returncode}'}"
        )

    path = manifest_path(run_dir, run_id)
    deadline = time.monotonic() + TMUX_STARTUP_TIMEOUT_SECONDS
    last_tmux_alive = True
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("run_id") == run_id:
                status = str(payload.get("status") or "unknown")
                if status != "unknown":
                    try:
                        last_tmux_alive = tmux_session_is_running(
                            tmux_bin,
                            run_id,
                        )
                    except RuntimeError:
                        last_tmux_alive = status == "running"
                    announce_tmux_launch(
                        run_dir,
                        run_id,
                        payload,
                        tmux_server,
                        last_tmux_alive,
                    )
                    return 0 if status == "running" else terminal_exit_code(status)
        try:
            last_tmux_alive = tmux_session_is_running(tmux_bin, run_id)
        except RuntimeError:
            last_tmux_alive = True
        if not last_tmux_alive:
            break
        time.sleep(0.05)

    if last_tmux_alive:
        print(f"RUN_DIR={run_dir}")
        print("STATUS=tmux_starting")
        print(f"EXECUTION_MODE={TMUX_EXECUTION_MODE}")
        print(f"TMUX_SESSION={run_id}")
        print(f"TMUX_SERVER={tmux_server}")
        print("TMUX_SESSION_ALIVE=yes")
        return 0

    print(f"RUN_DIR={run_dir}")
    print("STATUS=tmux_launch_failed")
    print(f"TMUX_SESSION={run_id}")
    print("TMUX_SESSION_ALIVE=no")
    if webhook_handoff is not None:
        try:
            webhook_handoff.unlink()
        except OSError:
            pass
    excerpt = read_supervisor_log_excerpt(run_dir, run_id)
    if excerpt:
        print(f"ERROR={excerpt}", file=sys.stderr)
    return 70


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    if args.reviewer_worker is not None:
        return run_reviewer_worker(args.reviewer_worker)

    if args.preflight or args.dry_run or args.tmux or args.tmux_supervisor or not any(
        (
            args.prepare,
            args.status,
            args.recover,
            args.background,
            args.tmux,
            args.tmux_supervisor,
        )
    ):
        reviewers, config_hash = load_reviewer_configuration(args.reviewers_file)
        configure_reviewers(reviewers, config_hash)

    if args.background:
        raise SystemExit(
            "--background is unsafe under Codex process cleanup and is no longer "
            "supported. Use --tmux for a tracked persistent supervisor or omit it "
            "for a direct foreground run."
        )
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than zero")
    if args.preflight:
        return preflight_run(args)
    if args.tmux:
        return launch_tmux_review(args)
    if args.tmux_supervisor:
        return run_tmux_supervisor(args)
    if args.prepare:
        return prepare_run(args)
    if args.status:
        return read_status(args)
    if args.recover:
        try:
            return recover_review(args)
        except Exception as exc:  # noqa: BLE001 - recovery must stay non-destructive.
            print(
                f"Monju recovery failed without publishing: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 70
    if args.dry_run:
        return dry_run(args)
    try:
        return run_requested_review(args)
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
