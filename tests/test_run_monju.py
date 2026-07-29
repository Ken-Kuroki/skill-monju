from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import run_monju

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "run_monju.py"


REVIEW_TEXT = """\
# Verdict
ok
# Findings
## Act now
none
## Usually defer (YAGNI)
none
# Gaps and uncertainties
none
# Proposed experiments
No additional experiment is warranted.
# Recommended next actions
none
"""


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._test_home_context = tempfile.TemporaryDirectory()
        self.test_home = Path(self._test_home_context.name)

    def tearDown(self) -> None:
        self._test_home_context.cleanup()

    @staticmethod
    def trust_workspace(workspace: Path, home: Path) -> Path:
        project = home / ".cursor" / "projects" / (
            f"workspace-{abs(hash(str(workspace.resolve())))}"
        )
        project.mkdir(parents=True, exist_ok=True)
        marker = project / ".workspace-trusted"
        marker.write_text(
            json.dumps(
                {
                    "trustedAt": "2026-01-01T00:00:00.000Z",
                    "workspacePath": str(workspace.resolve()),
                }
            ),
            encoding="utf-8",
        )
        return marker

    def make_fake_agent(self, directory: Path) -> Path:
        fake = directory / "fake-agent"
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import sys
                import time

                if len(sys.argv) > 1 and sys.argv[1] == "status":
                    print(os.environ.get("FAKE_AGENT_STATUS_OUTPUT", "Logged in"))
                    raise SystemExit(
                        int(os.environ.get("FAKE_AGENT_STATUS_CODE", "0"))
                    )

                if (
                    os.environ.get("FAIL_IF_WEBHOOK_VISIBLE")
                    and os.environ.get("MONJU_NOTIFY_WEBHOOK_URL")
                ):
                    print("webhook secret leaked to reviewer", file=sys.stderr)
                    raise SystemExit(86)

                if os.environ.get("FAKE_AGENT_STARTUP_FAILURE"):
                    print(
                        os.environ["FAKE_AGENT_STARTUP_FAILURE"],
                        file=sys.stderr,
                    )
                    raise SystemExit(76)

                model = sys.argv[sys.argv.index("--model") + 1]
                reported = {{
                    "kimi-k3-max": "Kimi K3 Max",
                    "cursor-grok-4.5-high": "Cursor Grok 4.5 High",
                    "claude-fable-5-thinking-max": "Fable 5 300K Max",
                }}[model]
                if os.environ.get("FAKE_AGENT_BAD_MODEL") == model:
                    reported = os.environ.get(
                        "FAKE_AGENT_REPORTED_MODEL",
                        "Grok 5.4 High",
                    )
                print(json.dumps({{
                    "type": "system",
                    "subtype": "init",
                    "model": reported,
                    "session_id": "session-" + model,
                }}), flush=True)
                time.sleep(float(os.environ.get("FAKE_AGENT_SLEEP", "0")))
                print(json.dumps({{
                    "type": "result",
                    "subtype": "success",
                    "result": {REVIEW_TEXT!r},
                }}), flush=True)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def run_command(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
        timeout: float = 10,
        trusted_workspace: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env["AGENT_CLI_CREDENTIAL_STORE"] = "file"
        command_env["HOME"] = str(self.test_home)
        if env:
            command_env.update(env)
        if trusted_workspace and "--workspace" in arguments:
            workspace = Path(arguments[arguments.index("--workspace") + 1])
            self.trust_workspace(workspace, Path(command_env["HOME"]))
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=REPOSITORY,
            env=command_env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def parse_key(output: str, key: str) -> str:
        prefix = f"{key}="
        for line in output.splitlines():
            if line.startswith(prefix):
                return line.removeprefix(prefix)
        raise AssertionError(f"{key} not found in output:\n{output}")

    def prepare(self, workspace: Path, output_root: Path) -> tuple[Path, Path]:
        result = self.run_command(
            "--prepare",
            "--workspace",
            str(workspace),
            "--output-root",
            str(output_root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return (
            Path(self.parse_key(result.stdout, "RUN_DIR")),
            Path(self.parse_key(result.stdout, "PROMPT_FILE")),
        )

    def make_recovery_run(
        self,
        root: Path,
        *,
        terminal_states: dict[str, str] | None = None,
        reported_models: dict[str, str] | None = None,
        supervisor_pid: int = 2_147_483_647,
    ) -> tuple[Path, Path]:
        run_id = f"monju-recovery-{time.time_ns()}"
        run_dir = root / run_id
        workspace = root / f"{run_id}-workspace"
        staging_parent = root / f"{run_id}-staging-test"
        staging_dir = staging_parent / run_id
        run_dir.mkdir()
        workspace.mkdir()
        staging_dir.mkdir(parents=True)
        prompt = run_dir / f"{run_id}-00-review-brief.md"
        prompt.write_text("# Review\nInspect preserved results.", encoding="utf-8")
        review_brief, effective_prompt = run_monju.read_review_brief(prompt)
        run_monju.copy_prompt_artifacts(
            staging_dir,
            run_id,
            review_brief,
            effective_prompt,
        )
        prompt_hash = hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest()
        manifest = {
            "schema_version": 2,
            "run_id": run_id,
            "status": "running",
            "started_at": "2026-07-29T13:14:53.695+00:00",
            "completed_at": None,
            "workspace": str(workspace),
            "source_prompt_file": str(prompt),
            "effective_prompt_sha256": prompt_hash,
            "reasoning_policy": run_monju.REASONING_POLICY,
            "parallel_reviewer_count": len(run_monju.REVIEWERS),
            "execution_mode": run_monju.EXECUTION_MODE,
            "supervisor_pid": supervisor_pid,
            "notification": run_monju.notification_pending("none"),
            "results": [],
        }
        run_monju.write_json_atomic(
            run_dir / f"{run_id}-manifest.json",
            manifest,
        )
        run_monju.atomic_write_text(
            run_dir / f"{run_id}-runner.pid",
            f"{supervisor_pid}\n",
        )
        run_monju.atomic_write_text(
            run_dir / f".{run_id}-staging",
            f"{staging_dir}\n",
        )

        states = terminal_states or {}
        model_overrides = reported_models or {}
        for reviewer in run_monju.REVIEWERS:
            state = states.get(reviewer.key, "success")
            events, stderr, _ = run_monju.reviewer_artifact_paths(
                staging_dir,
                run_id,
                reviewer,
            )
            lines = [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "model": model_overrides.get(
                            reviewer.key,
                            reviewer.allowed_reported_models[-1],
                        ),
                        "session_id": f"session-{reviewer.key}",
                    }
                )
            ]
            if state == "success":
                lines.append(
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "is_error": False,
                            "duration_ms": 1234,
                            "result": REVIEW_TEXT,
                        }
                    )
                )
            elif state == "cursor_error":
                lines.append(
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "error",
                            "is_error": True,
                            "duration_ms": 1234,
                            "result": "simulated Cursor error",
                        }
                    )
                )
            elif state == "malformed":
                lines.extend(
                    [
                        "{malformed",
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "success",
                                "is_error": False,
                                "result": REVIEW_TEXT,
                            }
                        ),
                    ]
                )
            elif state != "incomplete":
                raise AssertionError(f"unsupported terminal state: {state}")
            events.write_text("\n".join(lines) + "\n", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
        return run_dir, staging_dir

    @staticmethod
    def recovery_args(
        run_dir: Path,
        *,
        notify: str = "none",
    ) -> SimpleNamespace:
        return SimpleNamespace(run_dir=run_dir, notify=notify)

    def wait_for_terminal_manifest(self, run_dir: Path, timeout: float = 8) -> dict:
        manifest = run_dir / f"{run_dir.name}-manifest.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if manifest.is_file():
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                notification = payload.get("notification", {})
                if (
                    payload.get("status") != "running"
                    and notification.get("status") != "pending"
                ):
                    return payload
            time.sleep(0.05)
        self.fail(f"run did not finish: {run_dir}")

    def test_model_verification_requires_quality_markers(self) -> None:
        kimi, grok, fable = run_monju.REVIEWERS
        self.assertTrue(run_monju.model_matches(kimi, "Kimi K3 Max"))
        self.assertTrue(run_monju.model_matches(kimi, "kimi-k3-max"))
        self.assertTrue(run_monju.model_matches(grok, "Cursor Grok 4.5 High"))
        self.assertTrue(run_monju.model_matches(fable, "Fable 5 300K Max"))
        self.assertFalse(run_monju.model_matches(kimi, "Kimi K3"))
        self.assertFalse(run_monju.model_matches(kimi, "Kimi K30 Max"))
        self.assertFalse(run_monju.model_matches(kimi, "Kimi K3.5 Max"))
        self.assertFalse(run_monju.model_matches(grok, "Grok 4.5 High"))
        self.assertFalse(run_monju.model_matches(grok, "Grok 5.4 High"))
        self.assertFalse(run_monju.model_matches(grok, "Grok 4.5 Mini"))
        self.assertFalse(run_monju.model_matches(grok, "Grok 4.50 High"))
        self.assertFalse(run_monju.model_matches(grok, "Grok 4.5 SuperFast High"))
        self.assertFalse(run_monju.model_matches(fable, "Fable 5 Max"))
        self.assertFalse(run_monju.model_matches(fable, "Fable 5.5 Max"))
        self.assertFalse(run_monju.model_matches(fable, "Fable 6 Max"))
        self.assertFalse(run_monju.model_matches(fable, "Fable 50 Max"))
        self.assertFalse(run_monju.has_forbidden_variant("fast=false"))

    def test_prompt_envelope_requires_yagni_disposition(self) -> None:
        prompt = run_monju.PROMPT_ENVELOPE.format(review_brief="Review this change.")
        self.assertIn("## Act now", prompt)
        self.assertIn("## Usually defer (YAGNI)", prompt)
        self.assertIn("include only Act-now work by default", prompt)
        self.assertIn("not spacecraft code", prompt)
        self.assertIn("Do not use YAGNI to downgrade", prompt)
        self.assertIn("small, localized correction", prompt)
        self.assertIn("purely stylistic preferences", prompt)
        self.assertIn("expected decision value", prompt)

    def test_parser_retains_diagnostics_but_rejects_non_json_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        "wrapper warning",
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "init",
                                "model": "Kimi K3 Max",
                                "session_id": "abc",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "result",
                                "subtype": "success",
                                "result": REVIEW_TEXT,
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            model, session, final, error, warnings = run_monju.parse_event_stream(
                events
            )
            self.assertEqual(model, "Kimi K3 Max")
            self.assertEqual(session, "abc")
            self.assertEqual(final, REVIEW_TEXT)
            self.assertIsNone(error)
            self.assertEqual(len(warnings), 1)
            parsed = run_monju.inspect_event_stream(events)
            self.assertTrue(parsed.malformed_lines)
            self.assertTrue(
                any(
                    "malformed Cursor event stream" in item
                    for item in run_monju.parsed_review_errors(
                        run_monju.REVIEWERS[0],
                        parsed,
                    )
                )
            )

    def test_heartbeat_flushes_safe_liveness_output_at_interval(self) -> None:
        class FlushRecorder(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.flush_count = 0

            def flush(self) -> None:
                self.flush_count += 1
                super().flush()

        output = FlushRecorder()
        heartbeat = run_monju.SupervisorHeartbeat(
            "monju-heartbeat-test",
            interval_seconds=0.01,
            output=output,
        )
        with heartbeat:
            time.sleep(0.035)

        lines = output.getvalue().splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertGreaterEqual(output.flush_count, len(lines))
        self.assertTrue(
            all(
                line.startswith(
                    "MONJU_HEARTBEAT run_id=monju-heartbeat-test "
                    "elapsed_seconds="
                )
                for line in lines
            )
        )
        self.assertNotIn("private prompt", output.getvalue())
        self.assertNotIn("/secret/workspace", output.getvalue())
        self.assertNotIn("webhook-token", output.getvalue())

    def test_heartbeat_thread_stops_after_normal_completion(self) -> None:
        heartbeat = run_monju.SupervisorHeartbeat(
            "monju-normal-stop",
            interval_seconds=0.01,
            output=io.StringIO(),
        )
        self.assertFalse(heartbeat.daemon)
        with heartbeat:
            self.assertTrue(heartbeat.is_alive)
        self.assertFalse(heartbeat.is_alive)

    def test_foreground_supervisor_uses_and_stops_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "monju-heartbeat-integration"
            workspace.mkdir()
            run_dir.mkdir()
            fake = self.make_fake_agent(root)
            prompt = run_dir / f"{run_dir.name}-00-review-brief.md"
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            review_brief, effective_prompt = run_monju.read_review_brief(prompt)
            stdout = io.StringIO()

            with (
                mock.patch.object(
                    run_monju,
                    "HEARTBEAT_INTERVAL_SECONDS",
                    0.01,
                ),
                mock.patch.dict(
                    os.environ,
                    {"FAKE_AGENT_SLEEP": "0.05"},
                    clear=False,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = run_monju.execute_reviews(
                    workspace=workspace,
                    prompt_file=prompt,
                    run_dir=run_dir,
                    run_id=run_dir.name,
                    agent_bin=str(fake),
                    timeout_seconds=5,
                    review_brief=review_brief,
                    effective_prompt=effective_prompt,
                    run_started_at=run_monju.iso_now(),
                    notification_mode="none",
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(
                "MONJU_HEARTBEAT "
                "run_id=monju-heartbeat-integration elapsed_seconds=",
                stdout.getvalue(),
            )
            self.assertFalse(
                any(
                    thread.name == "monju-heartbeat-monju-heartbeat-integration"
                    for thread in threading.enumerate()
                )
            )

    def test_heartbeat_thread_stops_after_sigterm_equivalent_exception(self) -> None:
        heartbeat = run_monju.SupervisorHeartbeat(
            "monju-signal-stop",
            interval_seconds=0.01,
            output=io.StringIO(),
        )
        with self.assertRaises(run_monju.RunInterrupted):
            with heartbeat:
                raise run_monju.RunInterrupted(signal.SIGTERM)
        self.assertFalse(heartbeat.is_alive)

    def test_foreground_run_publishes_three_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            brief = root / "brief.md"
            brief.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--prompt-file",
                str(brief),
                "--output-root",
                str(output),
                "--agent-bin",
                str(fake),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = Path(self.parse_key(result.stdout, "RUN_DIR"))
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["notification"]["status"], "disabled")
            self.assertEqual(len(manifest["results"]), 3)
            self.assertEqual(len(list(run_dir.glob(f"{run_dir.name}-0[1-3]-*.md"))), 3)

    def test_model_mismatch_produces_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            brief = root / "brief.md"
            brief.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--prompt-file",
                str(brief),
                "--output-root",
                str(output),
                "--agent-bin",
                str(fake),
                env={
                    "FAKE_AGENT_BAD_MODEL": "cursor-grok-4.5-high",
                    "FAKE_AGENT_REPORTED_MODEL": "Grok 5.4 High",
                },
            )

            self.assertEqual(result.returncode, 3, result.stderr)
            run_dir = Path(self.parse_key(result.stdout, "RUN_DIR"))
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "partial_failure")
            results = {
                item["reviewer"]: item for item in manifest["results"]
            }
            self.assertEqual(results["Grok 4.5"]["status"], "failed")
            self.assertFalse(results["Grok 4.5"]["model_verified"])

    def test_reviewer_timeout_is_a_visible_total_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            brief = root / "brief.md"
            brief.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--prompt-file",
                str(brief),
                "--output-root",
                str(output),
                "--agent-bin",
                str(fake),
                "--timeout-seconds",
                "1",
                env={"FAKE_AGENT_SLEEP": "5"},
            )

            self.assertEqual(result.returncode, 4, result.stderr)
            run_dir = Path(self.parse_key(result.stdout, "RUN_DIR"))
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failure")
            self.assertTrue(all(item["timed_out"] for item in manifest["results"]))

    def test_authentication_failure_stops_before_reviewers_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            brief = root / "brief.md"
            brief.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--prompt-file",
                str(brief),
                "--output-root",
                str(output),
                "--agent-bin",
                str(fake),
                env={"FAKE_AGENT_STATUS_OUTPUT": "Not logged in"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("file credential store is not authenticated", result.stderr)
            self.assertEqual(list(output.glob("*/*.events.jsonl")), [])

    def test_preflight_checks_state_trust_and_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            fake = self.make_fake_agent(root)

            result = self.run_command(
                "--preflight",
                "--workspace",
                str(workspace),
                "--agent-bin",
                str(fake),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PREFLIGHT=ok", result.stdout)
            self.assertIn("WORKSPACE_TRUST=", result.stdout)

    def test_preflight_reports_unwritable_cursor_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            blocked_home = root / "blocked-home"
            blocked_home.write_text("not a directory", encoding="utf-8")

            result = self.run_command(
                "--preflight",
                "--workspace",
                str(workspace),
                "--agent-bin",
                str(fake),
                env={"HOME": str(blocked_home)},
                trusted_workspace=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PREFLIGHT=cursor_state_unwritable", result.stderr)
            self.assertIn("outside the filesystem sandbox", result.stderr)

    def test_preflight_requires_explicit_workspace_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            fake = self.make_fake_agent(root)

            result = self.run_command(
                "--preflight",
                "--workspace",
                str(workspace),
                "--agent-bin",
                str(fake),
                trusted_workspace=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PREFLIGHT=workspace_trust_required", result.stderr)
            self.assertIn("agent --workspace", result.stderr)
            self.assertIn("--trust", result.stderr)
            self.assertIn("calling coding agent", result.stderr)
            self.assertNotIn("Run this yourself", result.stderr)
            self.assertIn("Do not use --yolo or --force", result.stderr)

    def test_auto_notification_prefers_configured_webhook(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                os.environ,
                {run_monju.NOTIFICATION_WEBHOOK_ENV: "https://example.invalid/hook"},
            ),
            mock.patch.object(
                run_monju,
                "run_webhook_notification",
                return_value="webhook",
            ) as webhook,
            mock.patch.object(run_monju, "run_desktop_notification") as desktop,
        ):
            run_dir = Path(temporary) / "monju-test"
            result = run_monju.send_completion_notification(
                "auto",
                "monju-test",
                "success",
                run_dir,
            )

        self.assertEqual(result.status, "sent")
        self.assertEqual(result.backend, "webhook")
        webhook.assert_called_once()
        desktop.assert_not_called()

    def test_macos_notification_passes_arguments_after_separator(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(run_monju.sys, "platform", "darwin"),
            mock.patch.object(run_monju.shutil, "which", return_value="osascript"),
            mock.patch.object(
                run_monju.subprocess,
                "run",
                return_value=completed,
            ) as execute,
        ):
            backend = run_monju.run_desktop_notification("Monju", "done")

        self.assertEqual(backend, "macos")
        command = execute.call_args.args[0]
        self.assertEqual(command[-3:], ["--", "Monju", "done"])
        self.assertEqual(command[1:3], ["-l", "JavaScript"])
        self.assertIn(
            "app.displayNotification(argv[1], {withTitle: argv[0]})",
            command[4],
        )

    @unittest.skipUnless(sys.platform == "darwin", "macOS AppleScript compiler")
    def test_macos_notification_jxa_compiles(self) -> None:
        executable = shutil.which("osacompile")
        if executable is None:
            self.skipTest("osacompile is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "notification.scpt"
            result = subprocess.run(
                [
                    executable,
                    "-l",
                    "JavaScript",
                    "-e",
                    run_monju.macos_notification_script(),
                    "-o",
                    str(output),
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_desktop_notification_targets_current_user(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(run_monju.sys, "platform", "win32"),
            mock.patch.object(run_monju.os, "name", "nt"),
            mock.patch.dict(run_monju.os.environ, {"USERNAME": "ken"}, clear=False),
            mock.patch.object(run_monju.shutil, "which", return_value="msg.exe"),
            mock.patch.object(
                run_monju.subprocess,
                "run",
                return_value=completed,
            ) as execute,
        ):
            backend = run_monju.run_desktop_notification("Monju", "done")

        self.assertEqual(backend, "windows")
        command = execute.call_args.args[0]
        self.assertEqual(command[:2], ["msg.exe", "ken"])
        self.assertNotIn("*", command)

    def test_windows_liveness_probe_never_uses_os_kill(self) -> None:
        with (
            mock.patch.object(run_monju.os, "name", "nt"),
            mock.patch.object(
                run_monju,
                "windows_process_is_running",
                return_value=True,
            ) as windows_probe,
            mock.patch.object(run_monju.os, "kill") as kill,
        ):
            self.assertTrue(run_monju.process_is_running(1234))

        windows_probe.assert_called_once_with(1234)
        kill.assert_not_called()

    def test_posix_permission_denied_liveness_probe_is_conservative(self) -> None:
        with (
            mock.patch.object(run_monju.os, "name", "posix"),
            mock.patch.object(
                run_monju.os,
                "kill",
                side_effect=PermissionError("sandbox denied process inspection"),
            ),
        ):
            self.assertTrue(run_monju.process_is_running(1234))

    def test_notification_failure_does_not_change_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            brief = root / "brief.md"
            brief.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--prompt-file",
                str(brief),
                "--output-root",
                str(output),
                "--agent-bin",
                str(fake),
                "--notify",
                "webhook",
                env={run_monju.NOTIFICATION_WEBHOOK_ENV: ""},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("notification failed", result.stderr)
            run_dir = Path(self.parse_key(result.stdout, "RUN_DIR"))
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["notification"]["requested_mode"], "webhook")
            self.assertEqual(manifest["notification"]["backend"], "webhook")
            self.assertEqual(manifest["notification"]["status"], "failed")
            self.assertIn(
                run_monju.NOTIFICATION_WEBHOOK_ENV,
                manifest["notification"]["error"],
            )

    def test_webhook_secret_is_not_inherited_by_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            brief = root / "brief.md"
            brief.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--prompt-file",
                str(brief),
                "--output-root",
                str(output),
                "--agent-bin",
                str(fake),
                "--notify",
                "none",
                env={
                    run_monju.NOTIFICATION_WEBHOOK_ENV: (
                        "https://example.invalid/secret-token"
                    ),
                    "FAIL_IF_WEBHOOK_VISIBLE": "1",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = Path(self.parse_key(result.stdout, "RUN_DIR"))
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")

    def test_foreground_supervisor_announces_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "AGENT_CLI_CREDENTIAL_STORE": "file",
                    "FAKE_AGENT_SLEEP": "0.7",
                    "HOME": str(self.test_home),
                }
            )
            self.trust_workspace(workspace, self.test_home)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace",
                    str(workspace),
                    "--run-dir",
                    str(run_dir),
                    "--prompt-file",
                    str(prompt),
                    "--agent-bin",
                    str(fake),
                ],
                cwd=REPOSITORY,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                manifest_path = run_dir / f"{run_dir.name}-manifest.json"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if manifest_path.is_file():
                        payload = json.loads(
                            manifest_path.read_text(encoding="utf-8")
                        )
                        if payload.get("status") == "running":
                            break
                    time.sleep(0.02)
                else:
                    self.fail("foreground supervisor never announced running")

                self.assertIsNone(proc.poll())
                self.assertEqual(
                    (run_dir / f"{run_dir.name}-runner.pid")
                    .read_text(encoding="utf-8")
                    .strip(),
                    str(proc.pid),
                )
                stdout, stderr = proc.communicate(timeout=8)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("STATUS=running", stdout)
            self.assertIn("EXECUTION_MODE=foreground_supervisor", stdout)
            self.assertIn("STATUS=success", stdout)

    def test_background_flag_is_rejected_without_claiming_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--background",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--agent-bin",
                str(fake),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--background is unsafe under Codex", result.stderr)
            self.assertFalse((run_dir / ".monju-started").exists())
            self.assertFalse(
                (run_dir / f"{run_dir.name}-manifest.json").exists()
            )

    def test_foreground_reports_immediate_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--agent-bin",
                str(fake),
                env={"FAKE_AGENT_STARTUP_FAILURE": "Workspace Trust Required"},
            )

            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn("STATUS=failure", result.stdout)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "failure")
            self.assertTrue(
                all(
                    "Workspace Trust Required"
                    in (run_dir / item["stderr_file"]).read_text(encoding="utf-8")
                    for item in manifest["results"]
                )
            )

    def test_foreground_does_not_claim_run_before_trust_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            for marker in self.test_home.glob(
                ".cursor/projects/*/.workspace-trusted"
            ):
                marker.unlink()

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--agent-bin",
                str(fake),
                trusted_workspace=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PREFLIGHT=workspace_trust_required", result.stderr)
            self.assertFalse((run_dir / ".monju-started").exists())
            self.assertFalse(
                (run_dir / f"{run_dir.name}-manifest.json").exists()
            )

    def test_second_launch_cannot_replace_claimed_review_brief(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            original = "# Review\nOriginal scope."
            prompt.write_text(original, encoding="utf-8")

            first = self.run_command(
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--agent-bin",
                str(fake),
                env={"FAKE_AGENT_SLEEP": "0.3"},
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            replacement = root / "replacement.md"
            replacement.write_text("# Review\nReplacement scope.", encoding="utf-8")
            second = self.run_command(
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(replacement),
                "--agent-bin",
                str(fake),
            )

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already started", second.stderr)
            self.assertEqual(prompt.read_text(encoding="utf-8").strip(), original)
            self.assertEqual(self.wait_for_terminal_manifest(run_dir)["status"], "success")

    def test_status_reports_dead_runner_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            run_dir, _ = self.prepare(workspace, output)
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            manifest_path.write_text(
                json.dumps({"status": "running", "results": []}),
                encoding="utf-8",
            )
            (run_dir / f"{run_dir.name}-runner.pid").write_text(
                "2147483647\n",
                encoding="utf-8",
            )

            status = self.run_command("--status", "--run-dir", str(run_dir))

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("STATUS=stale_running", status.stdout)
            self.assertIn(
                "STALE_REASON=external_termination_before_startup",
                status.stdout,
            )

    def test_status_identifies_external_termination_after_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            staging = root / "staging"
            workspace.mkdir()
            staging.mkdir()
            run_dir, _ = self.prepare(workspace, output)
            (run_dir / f"{run_dir.name}-manifest.json").write_text(
                json.dumps({"status": "running", "results": []}),
                encoding="utf-8",
            )
            (run_dir / f"{run_dir.name}-runner.pid").write_text(
                "2147483647\n",
                encoding="utf-8",
            )
            (run_dir / f".{run_dir.name}-staging").write_text(
                str(staging),
                encoding="utf-8",
            )
            for reviewer in run_monju.REVIEWERS:
                stem = (
                    f"{run_dir.name}-{reviewer.ordinal:02d}-{reviewer.key}"
                )
                (staging / f"{stem}.events.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "init",
                            "model": reviewer.allowed_reported_models[-1],
                            "session_id": f"session-{reviewer.key}",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (staging / f"{stem}.stderr.log").write_text(
                    "bash: warning: setlocale: LC_ALL: cannot change locale "
                    "(C.UTF-8): Bad file descriptor\n",
                    encoding="utf-8",
                )

            status = self.run_command("--status", "--run-dir", str(run_dir))

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("STATUS=stale_running", status.stdout)
            self.assertIn(
                "STALE_REASON=external_termination_after_startup",
                status.stdout,
            )
            self.assertIn("initialized:3/3", status.stdout)
            self.assertIn("startup_errors:0/3", status.stdout)
            self.assertIn(f"STAGING_PRESERVED={staging}", status.stdout)

    def test_status_identifies_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            staging = root / "staging"
            workspace.mkdir()
            staging.mkdir()
            run_dir, _ = self.prepare(workspace, output)
            (run_dir / f"{run_dir.name}-manifest.json").write_text(
                json.dumps({"status": "running", "results": []}),
                encoding="utf-8",
            )
            (run_dir / f"{run_dir.name}-runner.pid").write_text(
                "2147483647\n",
                encoding="utf-8",
            )
            (run_dir / f".{run_dir.name}-staging").write_text(
                str(staging),
                encoding="utf-8",
            )
            for reviewer in run_monju.REVIEWERS:
                stem = (
                    f"{run_dir.name}-{reviewer.ordinal:02d}-{reviewer.key}"
                )
                (staging / f"{stem}.events.jsonl").touch()
                (staging / f"{stem}.stderr.log").write_text(
                    "Workspace Trust Required\n",
                    encoding="utf-8",
                )

            status = self.run_command("--status", "--run-dir", str(run_dir))

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("STATUS=stale_running", status.stdout)
            self.assertIn("STALE_REASON=startup_failure", status.stdout)
            self.assertIn("initialized:0/3", status.stdout)
            self.assertIn("startup_errors:3/3", status.stdout)

    def test_recover_refuses_a_live_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(
                Path(temporary),
                supervisor_pid=os.getpid(),
            )
            before = (
                run_dir / f"{run_dir.name}-manifest.json"
            ).read_bytes()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir)
                )

            self.assertEqual(exit_code, 65)
            self.assertIn("STATUS=recovery_refused", stdout.getvalue())
            self.assertIn(
                "RECOVERY_REASON=recorded supervisor PID is still running",
                stdout.getvalue(),
            )
            self.assertEqual(
                (run_dir / f"{run_dir.name}-manifest.json").read_bytes(),
                before,
            )
            self.assertTrue(staging.is_dir())

    def test_recover_requires_preserved_staging_pointer_and_directory(self) -> None:
        for missing in ("pointer", "directory"):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as temporary:
                    run_dir, staging = self.make_recovery_run(Path(temporary))
                    manifest = run_dir / f"{run_dir.name}-manifest.json"
                    before = manifest.read_bytes()
                    if missing == "pointer":
                        (run_dir / f".{run_dir.name}-staging").unlink()
                    else:
                        shutil.rmtree(staging.parent)
                    stdout = io.StringIO()

                    with contextlib.redirect_stdout(stdout):
                        exit_code = run_monju.recover_review(
                            self.recovery_args(run_dir)
                        )

                    self.assertEqual(exit_code, 65)
                    self.assertIn("STATUS=recovery_invalid", stdout.getvalue())
                    self.assertIn("RECOVERY=invalid", stdout.getvalue())
                    self.assertEqual(manifest.read_bytes(), before)

    def test_recover_is_non_destructive_when_terminal_event_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(
                Path(temporary),
                terminal_states={"fable-5": "incomplete"},
            )
            manifest = run_dir / f"{run_dir.name}-manifest.json"
            before_manifest = manifest.read_bytes()
            before_run_files = sorted(path.name for path in run_dir.iterdir())
            before_staging = {
                path.name: path.read_bytes()
                for path in staging.iterdir()
                if path.is_file()
            }
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir)
                )

            self.assertEqual(exit_code, 75)
            self.assertIn("STATUS=recovery_not_ready", stdout.getvalue())
            self.assertIn("RECOVERY=not_ready", stdout.getvalue())
            self.assertEqual(manifest.read_bytes(), before_manifest)
            self.assertEqual(
                sorted(path.name for path in run_dir.iterdir()),
                before_run_files,
            )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in staging.iterdir()
                    if path.is_file()
                },
                before_staging,
            )

    def test_recover_publishes_three_verified_successes_without_external_calls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(Path(temporary))
            stdout = io.StringIO()
            with (
                mock.patch.object(run_monju.subprocess, "Popen") as popen,
                mock.patch.object(
                    run_monju.urllib.request,
                    "urlopen",
                ) as urlopen,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir)
                )

            self.assertEqual(exit_code, 0)
            popen.assert_not_called()
            urlopen.assert_not_called()
            self.assertFalse(staging.parent.exists())
            self.assertFalse(
                (run_dir / f".{run_dir.name}-staging").exists()
            )
            markdown = list(run_dir.glob(f"{run_dir.name}-0[1-3]-*.md"))
            self.assertEqual(len(markdown), 3)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "success")
            self.assertTrue(manifest["recovered"])
            self.assertEqual(
                manifest["recovery_source"],
                run_monju.RECOVERY_SOURCE,
            )
            self.assertEqual(manifest["review_completed_at"], None)
            self.assertEqual(manifest["notification"]["status"], "disabled")
            self.assertTrue(
                all(
                    result["recovered"]
                    and result["model_verified"]
                    and result["status"] == "success"
                    and result["recovery_validation"]["errors"] == []
                    for result in manifest["results"]
                )
            )

    def test_recover_publishes_cursor_error_as_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(
                Path(temporary),
                terminal_states={"grok-4-5": "cursor_error"},
            )
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir)
                )

            self.assertEqual(exit_code, 3)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "partial_failure")
            grok = next(
                item
                for item in manifest["results"]
                if item["reviewer"] == "Grok 4.5"
            )
            self.assertEqual(grok["status"], "failed")
            self.assertIn("simulated Cursor error", grok["error"])

    def test_recover_never_accepts_unknown_or_forbidden_models(self) -> None:
        cases = ("Unknown Reviewer 9 Max", "Kimi K3 Fast Max")
        for reported_model in cases:
            with self.subTest(reported_model=reported_model):
                with tempfile.TemporaryDirectory() as temporary:
                    run_dir, _ = self.make_recovery_run(
                        Path(temporary),
                        reported_models={"kimi-k3": reported_model},
                    )
                    with contextlib.redirect_stdout(io.StringIO()):
                        exit_code = run_monju.recover_review(
                            self.recovery_args(run_dir)
                        )
                    manifest = json.loads(
                        (run_dir / f"{run_dir.name}-manifest.json").read_text(
                            encoding="utf-8"
                        )
                    )

                self.assertEqual(exit_code, 3)
                self.assertEqual(manifest["status"], "partial_failure")
                kimi = next(
                    item
                    for item in manifest["results"]
                    if item["reviewer"] == "Kimi K3"
                )
                self.assertEqual(kimi["status"], "failed")
                self.assertFalse(kimi["model_verified"])

    def test_recover_never_accepts_a_malformed_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(
                Path(temporary),
                terminal_states={"kimi-k3": "malformed"},
            )
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir)
                )
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(exit_code, 3)
            self.assertEqual(manifest["status"], "partial_failure")
            kimi = next(
                item
                for item in manifest["results"]
                if item["reviewer"] == "Kimi K3"
            )
            self.assertEqual(kimi["status"], "failed")
            self.assertIn("malformed Cursor event stream", kimi["error"])

    def test_recover_is_idempotent_after_terminal_manifest_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(Path(temporary))
            args = self.recovery_args(run_dir)
            with contextlib.redirect_stdout(io.StringIO()):
                first_exit = run_monju.recover_review(args)
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            first_manifest = manifest_path.read_bytes()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                second_exit = run_monju.recover_review(args)

            self.assertEqual(first_exit, 0)
            self.assertEqual(second_exit, 0)
            self.assertEqual(manifest_path.read_bytes(), first_manifest)
            self.assertIn("STATUS=success", stdout.getvalue())
            self.assertIn("RECOVERY=published", stdout.getvalue())

    def test_recover_publish_failure_preserves_raw_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(Path(temporary))
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    run_monju,
                    "publish_staging",
                    side_effect=PermissionError("simulated recovery publish failure"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir)
                )

            self.assertEqual(exit_code, 74)
            self.assertTrue(staging.is_dir())
            self.assertTrue(
                (run_dir / f".{run_dir.name}-staging").is_file()
            )
            self.assertIn(
                f"STAGING_PRESERVED={staging.resolve()}",
                stdout.getvalue(),
            )
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "artifact_failure")
            self.assertTrue(manifest["recovered"])

    def test_recover_notification_failure_does_not_change_review_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(Path(temporary))
            with (
                mock.patch.object(
                    run_monju,
                    "run_desktop_notification",
                    side_effect=RuntimeError("desktop unavailable"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = run_monju.recover_review(
                    self.recovery_args(run_dir, notify="desktop")
                )

            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["notification"]["status"], "failed")
            self.assertEqual(manifest["notification"]["backend"], "desktop")

    def test_recover_rejects_network_notification_modes_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(Path(temporary))
            manifest = run_dir / f"{run_dir.name}-manifest.json"
            before = manifest.read_bytes()

            with self.assertRaises(SystemExit):
                run_monju.recover_review(
                    self.recovery_args(run_dir, notify="webhook")
                )

            self.assertEqual(manifest.read_bytes(), before)
            self.assertTrue(staging.is_dir())

    def test_status_reports_recovery_ready_not_ready_and_invalid(self) -> None:
        cases = (
            ({}, "ready", "3/3", "yes"),
            ({"fable-5": "incomplete"}, "not_ready", "2/3", "no"),
            ({"kimi-k3": "malformed"}, "invalid", "3/3", "yes"),
        )
        for states, expected, terminal_count, can_publish in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as temporary:
                    run_dir, _ = self.make_recovery_run(
                        Path(temporary),
                        terminal_states=states,
                    )
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        exit_code = run_monju.read_status(
                            self.recovery_args(run_dir)
                        )
                    output = stdout.getvalue()

                self.assertEqual(exit_code, 0)
                self.assertIn("STATUS=stale_running", output)
                self.assertIn(f"RECOVERY={expected}", output)
                self.assertIn(
                    f"RECOVERY_TERMINAL_RESULTS={terminal_count}",
                    output,
                )
                self.assertIn(
                    f"RECOVERY_CAN_PUBLISH={can_publish}",
                    output,
                )

    def test_bad_agent_path_records_supervisor_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            result = self.run_command(
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--agent-bin",
                str(root / "missing-agent"),
            )

            self.assertEqual(result.returncode, 70, result.stderr)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "supervisor_failure")

    def test_publication_failure_preserves_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "monju-test-publication-failure"
            workspace.mkdir()
            run_dir.mkdir()
            fake = self.make_fake_agent(root)
            prompt = run_dir / f"{run_dir.name}-00-review-brief.md"
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            review_brief, effective_prompt = run_monju.read_review_brief(prompt)

            with (
                mock.patch.object(
                    run_monju,
                    "publish_staging",
                    side_effect=PermissionError("simulated publication failure"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = run_monju.execute_reviews(
                    workspace=workspace,
                    prompt_file=prompt,
                    run_dir=run_dir,
                    run_id=run_dir.name,
                    agent_bin=str(fake),
                    timeout_seconds=5,
                    review_brief=review_brief,
                    effective_prompt=effective_prompt,
                    run_started_at=run_monju.iso_now(),
                    notification_mode="none",
                )

            self.assertEqual(exit_code, 74)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "artifact_failure")
            self.assertEqual(len(manifest["results"]), 3)
            self.assertTrue(
                all(item["status"] == "success" for item in manifest["results"])
            )
            staging = Path(manifest["staging_preserved"])
            self.assertTrue(staging.is_dir())
            self.assertEqual(len(list(staging.glob("*.md"))), 5)
            shutil.rmtree(staging.parent)

    def test_post_publication_cleanup_failure_keeps_success_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "monju-test-cleanup-failure"
            workspace.mkdir()
            run_dir.mkdir()
            fake = self.make_fake_agent(root)
            prompt = run_dir / f"{run_dir.name}-00-review-brief.md"
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            review_brief, effective_prompt = run_monju.read_review_brief(prompt)
            stderr = io.StringIO()

            with (
                mock.patch.object(
                    run_monju.shutil,
                    "rmtree",
                    side_effect=RuntimeError("simulated cleanup failure"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = run_monju.execute_reviews(
                    workspace=workspace,
                    prompt_file=prompt,
                    run_dir=run_dir,
                    run_id=run_dir.name,
                    agent_bin=str(fake),
                    timeout_seconds=5,
                    review_brief=review_brief,
                    effective_prompt=effective_prompt,
                    run_started_at=run_monju.iso_now(),
                    notification_mode="none",
                )

            self.assertEqual(exit_code, 0)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")
            self.assertIn("could not remove staging", stderr.getvalue())
            pointer = run_dir / f".{run_dir.name}-staging"
            staging = Path(pointer.read_text(encoding="utf-8").strip())
            shutil.rmtree(staging.parent)
            pointer.unlink()

    @unittest.skipIf(os.name != "posix", "POSIX signal behavior")
    def test_interrupt_terminates_children_and_publishes_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "AGENT_CLI_CREDENTIAL_STORE": "file",
                    "FAKE_AGENT_SLEEP": "20",
                    "HOME": str(self.test_home),
                }
            )
            self.trust_workspace(workspace, self.test_home)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--workspace",
                    str(workspace),
                    "--run-dir",
                    str(run_dir),
                    "--prompt-file",
                    str(prompt),
                    "--agent-bin",
                    str(fake),
                ],
                cwd=REPOSITORY,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if manifest_path.is_file():
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if payload.get("status") == "running":
                        break
                time.sleep(0.05)
            else:
                proc.kill()
                self.fail("runner never entered running state")

            time.sleep(0.2)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertIn("STATUS=interrupted", stdout)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "interrupted")
            self.assertEqual(len(payload["results"]), 3)

if __name__ == "__main__":
    unittest.main()
