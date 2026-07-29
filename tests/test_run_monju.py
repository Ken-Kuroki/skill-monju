from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
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
        command_env.pop(run_monju.INTERNAL_WORKER_ENV, None)
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

    def test_valid_result_survives_non_json_noise(self) -> None:
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

    def test_windows_background_options_detach_process(self) -> None:
        with mock.patch.object(run_monju.os, "name", "nt"):
            options = run_monju.detached_process_options()

        self.assertNotIn("start_new_session", options)
        self.assertTrue(options["creationflags"] & 0x00000008)
        self.assertTrue(options["creationflags"] & 0x00000200)

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

    def test_background_run_returns_then_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            started = time.monotonic()
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
                "--notify",
                "webhook",
                env={
                    "FAKE_AGENT_SLEEP": "0.3",
                    run_monju.NOTIFICATION_WEBHOOK_ENV: "",
                },
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 2)
            self.assertIn("STATUS=running", result.stdout)
            self.assertIn("STARTUP=confirmed", result.stdout)
            manifest = self.wait_for_terminal_manifest(run_dir)
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["notification"]["status"], "failed")

            status = self.run_command("--status", "--run-dir", str(run_dir))
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("STATUS=success", status.stdout)
            self.assertIn("NOTIFICATION=failed BACKEND=webhook", status.stdout)

    def test_background_reports_immediate_startup_failure(self) -> None:
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
                env={"FAKE_AGENT_STARTUP_FAILURE": "Workspace Trust Required"},
            )

            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn("STATUS=failure", result.stdout)
            self.assertIn("STARTUP=failed", result.stdout)
            self.assertNotIn("STATUS=running", result.stdout)
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

    def test_background_startup_grace_can_return_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            proc = mock.Mock()
            proc.poll.return_value = None
            with (
                mock.patch.object(
                    run_monju,
                    "BACKGROUND_STARTUP_GRACE_SECONDS",
                    0.01,
                ),
                mock.patch.object(
                    run_monju,
                    "BACKGROUND_STARTUP_POLL_SECONDS",
                    0.001,
                ),
            ):
                startup, status = run_monju.wait_for_background_startup(
                    proc,
                    run_dir,
                    "monju-test",
                )

            self.assertEqual(startup, "pending")
            self.assertIsNone(status)

    def test_background_does_not_claim_run_before_trust_preflight(self) -> None:
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
                "--background",
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
                "--background",
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
                "--background",
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

    def test_internal_worker_flag_cannot_be_invoked_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            original = "# Review\nOriginal scope."
            prompt.write_text(original, encoding="utf-8")
            (run_dir / ".monju-started").write_text(
                run_monju.iso_now() + "\n",
                encoding="utf-8",
            )

            replacement = root / "replacement.md"
            replacement.write_text("# Review\nReplacement scope.", encoding="utf-8")
            result = self.run_command(
                "--_worker",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(replacement),
                "--agent-bin",
                str(fake),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reserved for the detached Monju supervisor", result.stderr)
            self.assertEqual(prompt.read_text(encoding="utf-8"), original)
            self.assertFalse(
                (run_dir / f"{run_dir.name}-manifest.json").exists()
            )

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

    def test_bad_agent_path_records_supervisor_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
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

    @unittest.skipIf(os.name != "posix", "POSIX signal behavior")
    def test_background_sigterm_publishes_interrupted_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            output = root / "reviews"
            workspace.mkdir()
            fake = self.make_fake_agent(root)
            run_dir, prompt = self.prepare(workspace, output)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")

            started = self.run_command(
                "--background",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--agent-bin",
                str(fake),
                env={"FAKE_AGENT_SLEEP": "20"},
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            runner_pid = int(self.parse_key(started.stdout, "RUNNER_PID"))

            pointer = run_dir / f".{run_dir.name}-staging"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if pointer.is_file():
                    staging = Path(pointer.read_text(encoding="utf-8").strip())
                    if any(staging.glob("*.events.jsonl")):
                        break
                time.sleep(0.05)
            else:
                os.kill(runner_pid, signal.SIGKILL)
                self.fail("background worker never started reviewer processes")

            os.kill(runner_pid, signal.SIGTERM)
            manifest = self.wait_for_terminal_manifest(run_dir)

            self.assertEqual(manifest["status"], "interrupted")
            self.assertEqual(manifest["interrupted_signal"], signal.SIGTERM)
            self.assertEqual(len(manifest["results"]), 3)


if __name__ == "__main__":
    unittest.main()
