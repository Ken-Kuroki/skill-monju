from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
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
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "run_monju.py"
SPEC = importlib.util.spec_from_file_location("run_monju", SCRIPT)
assert SPEC and SPEC.loader
run_monju = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run_monju
SPEC.loader.exec_module(run_monju)

REVIEW_TEXT = "# Verdict\nLooks sound.\n\n# Findings\n## Act now\nNone.\n\n## Usually defer (YAGNI)\nNone.\n\n# Gaps and uncertainties\nNone.\n\n# Proposed experiments\nNo additional experiment is warranted.\n\n# Recommended next actions\nNone."


class FlushRecorder(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        run_monju.configure_backend("opencode")
        reviewers, config_hash = run_monju.load_reviewer_configuration(
            REPOSITORY / "reviewers.json"
        )
        run_monju.configure_reviewers(reviewers, config_hash)
        run_monju.CATALOG_VERIFICATION = self.catalog_for(reviewers)

    @staticmethod
    def catalog_for(reviewers: tuple[run_monju.Reviewer, ...]) -> dict[str, object]:
        return {
            "verified_at": "2026-08-03T00:00:00.000+00:00",
            "provider": "opencode-go",
            "models": {
                reviewer.model_id: {
                    "status": "active",
                    "variants": [reviewer.variant] if reviewer.variant else [],
                }
                for reviewer in reviewers
            },
        }

    @staticmethod
    def write_config(path: Path, count: int) -> Path:
        reviewers = []
        for index in range(1, count + 1):
            reviewers.append(
                {
                    "key": f"reviewer-{index}",
                    "display_name": f"Reviewer {index}",
                    "model_id": f"opencode-go/model-{index}",
                    "variant": "max",
                }
            )
        path.write_text(
            json.dumps(
                {"schema_version": 1, "provider": "opencode-go", "reviewers": reviewers}
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def make_fake_opencode(
        root: Path,
        embedded_config: Path | None = None,
    ) -> Path:
        fake = root / "opencode"
        embedded_reviewers = (
            embedded_config.read_text(encoding="utf-8")
            if embedded_config is not None
            else json.dumps({"reviewers": []})
        )
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import sys
                import time

                args = sys.argv[1:]
                if args == ["auth", "list"]:
                    if os.environ.get("FAKE_OPENCODE_NO_AUTH"):
                        print("0 credentials")
                    else:
                        print("OpenCode Go api\\n1 credentials")
                    raise SystemExit(0)
                if len(args) >= 2 and args[:2] == ["models", "opencode-go"]:
                    if os.environ.get("FAKE_OPENCODE_MODELS_FAIL"):
                        print("Provider not found: opencode-go")
                        raise SystemExit(1)
                    config = json.loads(os.environ.get("FAKE_REVIEWERS_JSON", {embedded_reviewers!r}))
                    for item in config["reviewers"]:
                        if os.environ.get("FAKE_OPENCODE_DROP_MODEL") == item["model_id"]:
                            continue
                        variants = {{item.get("variant") or "default": {{}}}}
                        if os.environ.get("FAKE_OPENCODE_DROP_VARIANT") == item["model_id"]:
                            variants = {{}}
                        print(item["model_id"])
                        print(json.dumps({{
                            "id": item["model_id"].split("/", 1)[1],
                            "providerID": "opencode-go",
                            "status": "active",
                            "variants": variants,
                        }}))
                    raise SystemExit(0)
                if "run" not in args:
                    print("unsupported fake command", file=sys.stderr)
                    raise SystemExit(64)
                if "--auto" in args or "--pure" not in args or "--format" not in args:
                    print("unsafe or incomplete command", file=sys.stderr)
                    raise SystemExit(65)
                if "--agent" not in args or args[args.index("--agent") + 1] != "monju-review":
                    print("missing monju agent", file=sys.stderr)
                    raise SystemExit(66)
                if os.environ.get("MONJU_NOTIFY_WEBHOOK_URL"):
                    print("webhook secret leaked", file=sys.stderr)
                    raise SystemExit(67)
                agent = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])["agent"]["monju-review"]
                permission = agent["permission"]
                if permission["*"] != "deny" or permission["read"]["*.env"] != "deny":
                    print("permissions are not read-only", file=sys.stderr)
                    raise SystemExit(68)
                prompt = sys.stdin.read()
                if "<BEGIN_MONJU_REVIEW_BRIEF>" not in prompt:
                    print("prompt was not supplied on stdin", file=sys.stderr)
                    raise SystemExit(69)
                if any("BEGIN_MONJU" in arg for arg in args):
                    print("prompt leaked to argv", file=sys.stderr)
                    raise SystemExit(70)
                model = args[args.index("--model") + 1]
                time.sleep(float(os.environ.get("FAKE_OPENCODE_SLEEP", "0")))
                session = "session-" + model.rsplit("/", 1)[-1]
                if os.environ.get("FAKE_OPENCODE_REGION_ERROR_MODEL") == model:
                    error = {{
                        "name": "APIError",
                        "data": {{
                            "message": "The latest version of this model is only available hosted in China and requires explicit opt in",
                            "statusCode": 403,
                            "isRetryable": False,
                            "responseBody": json.dumps({{"type": "error", "error": {{"type": "RegionError"}}}}),
                        }},
                    }}
                    print(json.dumps({{"type": "error", "sessionID": session, "error": error}}), flush=True)
                    raise SystemExit(1)
                if os.environ.get("FAKE_OPENCODE_ERROR_MODEL") == model:
                    print(json.dumps({{"type": "error", "sessionID": session, "error": {{"name": "ProviderError", "data": {{"message": "simulated OpenCode error"}}}}}}), flush=True)
                    raise SystemExit(1)
                print(json.dumps({{"type": "text", "sessionID": session, "part": {{"type": "text", "text": {REVIEW_TEXT!r}}}}}), flush=True)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    @staticmethod
    def make_fake_cursor(root: Path) -> Path:
        fake = root / "agent"
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import sys

                args = sys.argv[1:]
                if args == ["status"]:
                    print("Logged in")
                    raise SystemExit(0)
                if args == ["models"]:
                    print("kimi-k3-max")
                    print("cursor-grok-4.5-high")
                    print("claude-fable-5-thinking-max")
                    raise SystemExit(0)
                if "--mode=ask" not in args or "--output-format" not in args:
                    print("unsafe or incomplete Cursor command", file=sys.stderr)
                    raise SystemExit(65)
                if os.environ.get("MONJU_NOTIFY_WEBHOOK_URL"):
                    print("webhook secret leaked", file=sys.stderr)
                    raise SystemExit(67)
                model = args[args.index("--model") + 1]
                prompt = args[-1]
                if "<BEGIN_MONJU_REVIEW_BRIEF>" not in prompt:
                    print("prompt missing from Cursor argv", file=sys.stderr)
                    raise SystemExit(69)
                reported = {{
                    "kimi-k3-max": "Kimi K3 Max",
                    "cursor-grok-4.5-high": "Cursor Grok 4.5 High",
                    "claude-fable-5-thinking-max": "Fable 5 300K Max",
                }}[model]
                print(json.dumps({{
                    "type": "system",
                    "subtype": "init",
                    "session_id": "cursor-session-" + model,
                    "model": reported,
                }}), flush=True)
                print(json.dumps({{
                    "type": "result",
                    "subtype": "success",
                    "session_id": "cursor-session-" + model,
                    "result": {REVIEW_TEXT!r},
                }}), flush=True)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    @staticmethod
    def make_fake_tmux(root: Path) -> Path:
        fake = root / "tmux"
        pid_file = root / "fake-tmux-session.pid"
        argv_file = root / "fake-tmux-new-session.json"
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                pid_file = Path({str(pid_file)!r})
                argv_file = Path({str(argv_file)!r})
                args = sys.argv[1:]
                if args and args[0] == "list-sessions":
                    print("persistent-test-session")
                    raise SystemExit(0)
                if args and args[0] == "has-session":
                    try:
                        pid = int(pid_file.read_text())
                        os.kill(pid, 0)
                    except (OSError, ValueError):
                        print("can't find session", file=sys.stderr)
                        raise SystemExit(1)
                    raise SystemExit(0)
                if args and args[0] == "new-session":
                    argv_file.write_text(json.dumps(args))
                    command = args[args.index("-c") + 2:]
                    environment = os.environ.copy()
                    environment["TMUX"] = "/private/tmp/fake-tmux,1,0"
                    process = subprocess.Popen(
                        command,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    pid_file.write_text(str(process.pid))
                    raise SystemExit(0)
                print("unsupported fake tmux command", file=sys.stderr)
                raise SystemExit(64)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def command_env(self, home: Path, config: Path, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["FAKE_REVIEWERS_JSON"] = config.read_text(encoding="utf-8")
        env.update(extra)
        return env

    def run_command(
        self,
        home: Path,
        config: Path,
        *args: str,
        env: dict[str, str] | None = None,
        timeout: float = 20,
    ) -> subprocess.CompletedProcess[str]:
        command_env = self.command_env(home, config)
        if env:
            command_env.update(env)
        command = [sys.executable, str(SCRIPT), "--reviewers-file", str(config), *args]
        inactive_modes = {
            "--prepare",
            "--status",
            "--recover",
            "--manual-handoff",
            "--background",
        }
        needs_reviewers = not inactive_modes.intersection(args)
        if needs_reviewers:
            command.extend(["--backend", "opencode"])
            if "--reviewer" not in args:
                payload = json.loads(config.read_text(encoding="utf-8"))
                for reviewer in payload["reviewers"]:
                    command.extend(["--reviewer", reviewer["key"]])
        return subprocess.run(
            command,
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
                return line[len(prefix):]
        raise AssertionError(f"missing {key} in output:\n{output}")

    def prepare(self, root: Path, workspace: Path, config: Path) -> tuple[Path, Path]:
        result = self.run_command(
            root / "home",
            config,
            "--prepare",
            "--workspace",
            str(workspace),
            "--output-root",
            str(root / "reviews"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(self.parse_key(result.stdout, "RUN_DIR")), Path(
            self.parse_key(result.stdout, "PROMPT_FILE")
        )

    def test_default_configuration_uses_highest_available_variants(self) -> None:
        reviewers, _ = run_monju.load_reviewer_configuration(REPOSITORY / "reviewers.json")
        self.assertEqual(
            [(item.model_id, item.variant) for item in reviewers],
            [
                ("opencode-go/kimi-k3", "max"),
                ("opencode-go/grok-4.5", "high"),
                ("opencode-go/deepseek-v4-flash", "max"),
                ("opencode-go/qwen3.8-max", "max"),
            ],
        )

    def test_default_reviewer_timeout_is_two_hours(self) -> None:
        with mock.patch.object(sys, "argv", [str(SCRIPT)]):
            args = run_monju.parse_args()
        self.assertEqual(args.timeout_seconds, 7200)

    def test_backend_and_exact_reviewer_are_required(self) -> None:
        missing_backend = subprocess.run(
            [sys.executable, str(SCRIPT), "--preflight"],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(missing_backend.returncode, 0)
        self.assertIn("--backend cursor or --backend opencode", missing_backend.stderr)

        missing_reviewer = subprocess.run(
            [sys.executable, str(SCRIPT), "--backend", "opencode", "--preflight"],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(missing_reviewer.returncode, 0)
        self.assertIn("select each exact model explicitly", missing_reviewer.stderr)
        self.assertIn("kimi-k3=opencode-go/kimi-k3/max", missing_reviewer.stderr)

    def test_cursor_preflight_and_review_use_exact_reported_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            projects = home / ".cursor" / "projects" / "trusted-project"
            projects.mkdir(parents=True)
            (projects / ".workspace-trusted").write_text(
                json.dumps({"workspacePath": str(workspace)}),
                encoding="utf-8",
            )
            cursor_config = REPOSITORY / "cursor_reviewers.json"
            fake_cursor = self.make_fake_cursor(root)
            fake_tmux = self.make_fake_tmux(root)
            env = os.environ.copy()
            env["HOME"] = str(home)

            preflight = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--backend",
                    "cursor",
                    "--reviewer",
                    "kimi-k3",
                    "--reviewers-file",
                    str(cursor_config),
                    "--preflight",
                    "--workspace",
                    str(workspace),
                    "--agent-bin",
                    str(fake_cursor),
                    "--tmux-bin",
                    str(fake_tmux),
                ],
                cwd=REPOSITORY,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertIn("BACKEND=cursor", preflight.stdout)
            self.assertIn("REVIEWER_KEYS=kimi-k3", preflight.stdout)

            prepare = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--prepare",
                    "--workspace",
                    str(workspace),
                    "--output-root",
                    str(root / "reviews"),
                ],
                cwd=REPOSITORY,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(prepare.returncode, 0, prepare.stderr)
            run_dir = Path(self.parse_key(prepare.stdout, "RUN_DIR"))
            prompt = Path(self.parse_key(prepare.stdout, "PROMPT_FILE"))
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            review = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--backend",
                    "cursor",
                    "--reviewer",
                    "kimi-k3",
                    "--reviewers-file",
                    str(cursor_config),
                    "--workspace",
                    str(workspace),
                    "--run-dir",
                    str(run_dir),
                    "--prompt-file",
                    str(prompt),
                    "--agent-bin",
                    str(fake_cursor),
                ],
                cwd=REPOSITORY,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(review.returncode, 0, review.stderr)
            manifest = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["backend"], "cursor")
            self.assertEqual(manifest["parallel_reviewer_count"], 1)
            result = manifest["results"][0]
            self.assertEqual(result["reported_model"], "Kimi K3 Max")
            self.assertTrue(result["model_verified"])
            self.assertEqual(result["command"][-1], "<effective-prompt>")

    def test_cursor_configuration_rejects_fast_or_unverifiable_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "cursor-reviewers.json"
            run_monju.configure_backend("cursor")
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "cursor",
                        "reviewers": [
                            {
                                "key": "unsafe",
                                "display_name": "Unsafe",
                                "model_id": "some-fast-model",
                                "allowed_reported_models": ["some-fast-model"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                run_monju.load_reviewer_configuration(config)

            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["reviewers"][0]["model_id"] = "safe-thinking-max"
            payload["reviewers"][0]["allowed_reported_models"] = ["different-model"]
            config.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SystemExit):
                run_monju.load_reviewer_configuration(config)

    def test_cursor_tmux_command_preserves_backend_and_model_selection(self) -> None:
        run_monju.configure_backend("cursor")
        configured, _ = run_monju.load_reviewer_configuration(
            REPOSITORY / "cursor_reviewers.json"
        )
        reviewers, config_hash = run_monju.select_configured_reviewers(
            configured,
            ["grok-4-5"],
        )
        run_monju.configure_reviewers(reviewers, config_hash)
        run_dir = Path("/tmp/monju-cursor-tmux-test")
        args = argparse.Namespace(
            reviewers_file=REPOSITORY / "cursor_reviewers.json",
            timeout_seconds=run_monju.DEFAULT_REVIEWER_TIMEOUT_SECONDS,
            notify="none",
            reviewer_keys=["grok-4-5"],
        )
        command = run_monju.tmux_supervisor_command(
            args,
            Path("/workspace"),
            run_dir,
            run_dir / "monju-cursor-tmux-test-00-review-brief.md",
            "/bin/agent",
            run_dir.name,
            None,
        )
        self.assertIn("cursor", command)
        self.assertIn("--agent-bin", command)
        self.assertNotIn("--opencode-bin", command)
        self.assertEqual(command[-2:], ["--reviewer", "grok-4-5"])

    def test_configuration_supports_one_and_four_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for count in (1, 4):
                config = self.write_config(root / f"reviewers-{count}.json", count)
                reviewers, digest = run_monju.load_reviewer_configuration(config)
                self.assertEqual(len(reviewers), count)
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_reviewer_selection_uses_requested_order_and_rejects_bad_keys(self) -> None:
        configured, full_hash = run_monju.load_reviewer_configuration(
            REPOSITORY / "reviewers.json"
        )
        selected, selected_hash = run_monju.select_configured_reviewers(
            configured,
            ["qwen3-8-max", "kimi-k3"],
        )
        self.assertEqual([item.key for item in selected], ["qwen3-8-max", "kimi-k3"])
        self.assertEqual([item.ordinal for item in selected], [1, 2])
        self.assertNotEqual(selected_hash, full_hash)
        with self.assertRaises(SystemExit):
            run_monju.select_configured_reviewers(configured, ["missing"])
        with self.assertRaises(SystemExit):
            run_monju.select_configured_reviewers(
                configured,
                ["kimi-k3", "kimi-k3"],
            )

    def test_configuration_rejects_provider_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root / "reviewers.json", 2)
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["provider"] = "other"
            config.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SystemExit):
                run_monju.load_reviewer_configuration(config)
            payload["provider"] = "opencode-go"
            payload["reviewers"][1]["key"] = payload["reviewers"][0]["key"]
            config.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SystemExit):
                run_monju.load_reviewer_configuration(config)

    def test_configuration_rejects_empty_reviewer_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "reviewers.json"
            config.write_text(
                json.dumps({"schema_version": 1, "provider": "opencode-go", "reviewers": []}),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                run_monju.load_reviewer_configuration(config)

    def test_command_and_permissions_are_read_only(self) -> None:
        reviewer = run_monju.REVIEWERS[0]
        command = run_monju.build_command("/bin/opencode", reviewer, Path("/workspace"))
        self.assertEqual(command[:3], ["/bin/opencode", "--pure", "run"])
        self.assertNotIn("--auto", command)
        self.assertEqual(command[command.index("--variant") + 1], "max")
        permission = run_monju.opencode_agent_config()["agent"]["monju-review"]["permission"]
        self.assertEqual(permission["*"], "deny")
        self.assertEqual(permission["read"]["*.env"], "deny")
        self.assertEqual(permission["read"]["*.env.*"], "deny")
        self.assertEqual(permission["read"]["*.env.example"], "allow")

    def test_tmux_webhook_handoff_is_private_consumed_and_not_in_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "monju-webhook-test"
            run_dir.mkdir()
            secret = "https://secret.invalid/token"
            with mock.patch.dict(
                os.environ,
                {run_monju.NOTIFICATION_WEBHOOK_ENV: secret},
            ):
                handoff = run_monju.prepare_tmux_webhook_handoff(
                    run_dir,
                    run_dir.name,
                    "auto",
                )
                self.assertIsNotNone(handoff)
                assert handoff is not None
                self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
                args = argparse.Namespace(
                    reviewers_file=REPOSITORY / "reviewers.json",
                    timeout_seconds=run_monju.DEFAULT_REVIEWER_TIMEOUT_SECONDS,
                    notify="auto",
                    reviewer_keys=[],
                )
                command = run_monju.tmux_supervisor_command(
                    args,
                    Path("/workspace"),
                    run_dir,
                    run_dir / f"{run_dir.name}-00-review-brief.md",
                    "/bin/opencode",
                    run_dir.name,
                    handoff,
                )
                self.assertNotIn(secret, command)
                run_monju.consume_tmux_webhook_handoff(
                    handoff,
                    run_dir,
                    run_dir.name,
                )
                self.assertFalse(handoff.exists())
                self.assertEqual(
                    os.environ[run_monju.NOTIFICATION_WEBHOOK_ENV],
                    secret,
                )

    def test_event_parser_accepts_text_and_rejects_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                json.dumps({"type": "text", "sessionID": "s1", "part": {"text": "ok"}})
                + "\nnot-json\n",
                encoding="utf-8",
            )
            parsed = run_monju.inspect_event_stream(path)
            self.assertEqual(parsed.final_text, "ok")
            self.assertEqual(parsed.session_id, "s1")
            self.assertEqual(parsed.malformed_lines, (2,))

    def test_preflight_validates_auth_models_and_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 3)
            fake = self.make_fake_opencode(root)
            fake_tmux = self.make_fake_tmux(root)
            result = self.run_command(
                home,
                config,
                "--preflight",
                "--workspace",
                str(workspace),
                "--opencode-bin",
                str(fake),
                "--tmux-bin",
                str(fake_tmux),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PREFLIGHT=ok", result.stdout)
            self.assertIn("REVIEWERS=3", result.stdout)

    def test_preflight_validates_only_selected_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 2)
            fake = self.make_fake_opencode(root)
            fake_tmux = self.make_fake_tmux(root)
            result = self.run_command(
                home,
                config,
                "--preflight",
                "--reviewer",
                "reviewer-1",
                "--workspace",
                str(workspace),
                "--opencode-bin",
                str(fake),
                "--tmux-bin",
                str(fake_tmux),
                env={"FAKE_OPENCODE_DROP_MODEL": "opencode-go/model-2"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("REVIEWERS=1", result.stdout)
            self.assertIn("REVIEWER_KEYS=reviewer-1", result.stdout)

    def test_dry_run_uses_only_selected_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 3)
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            result = self.run_command(
                home,
                config,
                "--dry-run",
                "--reviewer",
                "reviewer-3",
                "--reviewer",
                "reviewer-1",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(
                [item["reviewer"] for item in payload["commands"]],
                ["Reviewer 3", "Reviewer 1"],
            )

    def test_manual_handoff_is_private_non_destructive_and_visible_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect the supplied files.", encoding="utf-8")
            result = self.run_command(
                home,
                config,
                "--manual-handoff",
                "claude-opus",
                "--manual-display-name",
                "Claude Opus",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(root / "must-not-run-opencode"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("STATUS=manual_handoff_ready", result.stdout)
            manual_prompt = Path(self.parse_key(result.stdout, "MANUAL_PROMPT_FILE"))
            response = Path(self.parse_key(result.stdout, "MANUAL_RESPONSE_FILE"))
            metadata_path = Path(self.parse_key(result.stdout, "MANUAL_HANDOFF_FILE"))
            for path in (manual_prompt, response, metadata_path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("<BEGIN_MONJU_REVIEW_BRIEF>", manual_prompt.read_text())
            self.assertEqual(response.read_text(), "")
            metadata = json.loads(metadata_path.read_text())
            self.assertEqual(metadata["display_name"], "Claude Opus")
            self.assertFalse(metadata["model_verified"])

            response.write_text(REVIEW_TEXT, encoding="utf-8")
            status = self.run_command(
                home,
                config,
                "--status",
                "--run-dir",
                str(run_dir),
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("MANUAL_HANDOFFS=1", status.stdout)
            self.assertIn("MANUAL_RESPONSES_READY=1", status.stdout)
            self.assertIn(f"MANUAL_RESULT claude-opus: {response}", status.stdout)

            repeated = self.run_command(
                home,
                config,
                "--manual-handoff",
                "claude-opus",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
            )
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(response.read_text(), REVIEW_TEXT)

    def test_preflight_rejects_missing_auth_model_and_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 2)
            fake = self.make_fake_opencode(root)
            fake_tmux = self.make_fake_tmux(root)
            cases = (
                ({"FAKE_OPENCODE_NO_AUTH": "1"}, "PREFLIGHT=opencode_auth_required"),
                ({"FAKE_OPENCODE_DROP_MODEL": "opencode-go/model-1"}, "PREFLIGHT=reviewer_model_invalid"),
                ({"FAKE_OPENCODE_DROP_VARIANT": "opencode-go/model-1"}, "PREFLIGHT=reviewer_model_invalid"),
            )
            for environment, expected in cases:
                with self.subTest(expected=expected):
                    result = self.run_command(
                        home,
                        config,
                        "--preflight",
                        "--workspace",
                        str(workspace),
                        "--opencode-bin",
                        str(fake),
                        "--tmux-bin",
                        str(fake_tmux),
                        env=environment,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)

    def test_foreground_run_uses_stdin_and_publishes_dynamic_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 4)
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect the workspace.", encoding="utf-8")
            result = self.run_command(
                home,
                config,
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake),
                env={"MONJU_NOTIFY_WEBHOOK_URL": "https://secret.invalid/token"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((run_dir / f"{run_dir.name}-manifest.json").read_text())
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["backend"], "opencode")
            self.assertEqual(len(manifest["results"]), 4)
            self.assertTrue(all(item["model_verified"] for item in manifest["results"]))
            self.assertEqual(len(list(run_dir.glob("*.terminal.json"))), 4)

    def test_tmux_launch_tracks_session_and_publishes_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 2)
            fake_opencode = self.make_fake_opencode(root)
            fake_tmux = self.make_fake_tmux(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect through tmux.", encoding="utf-8")

            result = self.run_command(
                home,
                config,
                "--tmux",
                "--reviewer",
                "reviewer-2",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake_opencode),
                "--tmux-bin",
                str(fake_tmux),
                env={
                    "FAKE_OPENCODE_SLEEP": "0.5",
                    "MONJU_NOTIFY_WEBHOOK_URL": "https://secret.invalid/token",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("EXECUTION_MODE=tmux_supervisor", result.stdout)
            self.assertIn(f"TMUX_SESSION={run_dir.name}", result.stdout)

            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            deadline = time.monotonic() + 10
            payload: dict[str, object] | None = None
            while time.monotonic() < deadline:
                if manifest_path.is_file():
                    payload = json.loads(manifest_path.read_text())
                    if payload.get("status") != "running":
                        break
                time.sleep(0.05)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["execution_mode"], "tmux_supervisor")
            self.assertEqual(payload["tmux_session"], run_dir.name)
            self.assertEqual(payload["parallel_reviewer_count"], 1)
            self.assertEqual(payload["reviewers"][0]["key"], "reviewer-2")
            self.assertTrue(
                (run_dir / f"{run_dir.name}-runner.stdout.log").is_file()
            )
            tmux_argv = (root / "fake-tmux-new-session.json").read_text()
            self.assertIn('"--reviewer", "reviewer-2"', tmux_argv)
            self.assertNotIn("https://secret.invalid/token", tmux_argv)
            self.assertNotIn("Inspect through tmux", tmux_argv)

    def test_tmux_launch_refuses_an_existing_exact_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            fake_opencode = self.make_fake_opencode(root)
            fake_tmux = self.make_fake_tmux(root)
            (root / "fake-tmux-session.pid").write_text(str(os.getpid()))
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")

            result = self.run_command(
                home,
                config,
                "--tmux",
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake_opencode),
                "--tmux-bin",
                str(fake_tmux),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tmux session already exists", result.stderr)
            self.assertFalse((run_dir / ".monju-started").exists())

    @unittest.skipUnless(
        os.environ.get("MONJU_TEST_REAL_TMUX") == "1",
        "set MONJU_TEST_REAL_TMUX=1 for the local tmux integration smoke",
    )
    def test_real_tmux_smoke_uses_no_external_model(self) -> None:
        tmux_bin = shutil.which("tmux")
        self.assertIsNotNone(tmux_bin)
        assert tmux_bin is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            fake_opencode = self.make_fake_opencode(root, config)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nSynthetic tmux smoke.", encoding="utf-8")
            try:
                result = self.run_command(
                    home,
                    config,
                    "--tmux",
                    "--workspace",
                    str(workspace),
                    "--run-dir",
                    str(run_dir),
                    "--prompt-file",
                    str(prompt),
                    "--opencode-bin",
                    str(fake_opencode),
                    "--tmux-bin",
                    tmux_bin,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest_path = run_dir / f"{run_dir.name}-manifest.json"
                deadline = time.monotonic() + 15
                payload: dict[str, object] | None = None
                while time.monotonic() < deadline:
                    if manifest_path.is_file():
                        payload = json.loads(manifest_path.read_text())
                        if payload.get("status") != "running":
                            break
                    time.sleep(0.05)
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["status"], "success")
                self.assertEqual(payload["execution_mode"], "tmux_supervisor")
                self.assertEqual(payload["tmux_session"], run_dir.name)
            finally:
                subprocess.run(
                    [tmux_bin, "kill-session", "-t", f"={run_dir.name}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    def test_one_opencode_error_produces_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 3)
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            result = self.run_command(
                home,
                config,
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake),
                env={"FAKE_OPENCODE_ERROR_MODEL": "opencode-go/model-2"},
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            manifest = json.loads((run_dir / f"{run_dir.name}-manifest.json").read_text())
            self.assertEqual(manifest["status"], "partial_failure")

    def test_deepseek_region_error_is_an_expected_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = REPOSITORY / "reviewers.json"
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            result = self.run_command(
                home,
                config,
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake),
                env={
                    "FAKE_OPENCODE_REGION_ERROR_MODEL":
                        "opencode-go/deepseek-v4-flash"
                },
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            manifest = json.loads((run_dir / f"{run_dir.name}-manifest.json").read_text())
            self.assertEqual(manifest["status"], "partial_failure")
            deepseek = next(
                item
                for item in manifest["results"]
                if item["requested_model"] == "opencode-go/deepseek-v4-flash"
            )
            self.assertEqual(deepseek["status"], "failed")
            self.assertIn("only available hosted in China", deepseek["error"])
            self.assertEqual(
                sum(item["status"] == "success" for item in manifest["results"]),
                3,
            )

    def test_reviewer_timeout_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            result = self.run_command(
                home,
                config,
                "--workspace",
                str(workspace),
                "--run-dir",
                str(run_dir),
                "--prompt-file",
                str(prompt),
                "--opencode-bin",
                str(fake),
                "--timeout-seconds",
                "1",
                env={"FAKE_OPENCODE_SLEEP": "5"},
            )
            self.assertEqual(result.returncode, 4, result.stderr)
            manifest = json.loads((run_dir / f"{run_dir.name}-manifest.json").read_text())
            self.assertTrue(manifest["results"][0]["timed_out"])

    def make_recovery_run(
        self,
        root: Path,
        *,
        states: dict[str, str] | None = None,
        supervisor_pid: int = 2_147_483_647,
    ) -> tuple[Path, Path]:
        reviewers = run_monju.REVIEWERS
        run_id = f"monju-recovery-{time.time_ns()}"
        run_dir = root / run_id
        workspace = root / "workspace"
        staging_dir = root / f"{run_id}-staging-test" / run_id
        run_dir.mkdir()
        workspace.mkdir(exist_ok=True)
        staging_dir.mkdir(parents=True)
        prompt = run_dir / f"{run_id}-00-review-brief.md"
        prompt.write_text("# Review\nInspect preserved results.", encoding="utf-8")
        review_brief, effective_prompt = run_monju.read_review_brief(prompt)
        run_monju.copy_prompt_artifacts(staging_dir, run_id, review_brief, effective_prompt)
        manifest = {
            "schema_version": 3,
            "backend": "opencode",
            "provider": "opencode-go",
            "run_id": run_id,
            "status": "running",
            "started_at": "2026-08-03T00:00:00.000+00:00",
            "completed_at": None,
            "workspace": str(workspace),
            "source_prompt_file": str(prompt),
            "effective_prompt_sha256": hashlib.sha256(effective_prompt.encode()).hexdigest(),
            "reviewers": run_monju.reviewers_payload(reviewers),
            "reviewer_config_sha256": run_monju.REVIEWER_CONFIG_HASH,
            "catalog_verification": run_monju.CATALOG_VERIFICATION,
            "parallel_reviewer_count": len(reviewers),
            "execution_mode": run_monju.EXECUTION_MODE,
            "supervisor_pid": supervisor_pid,
            "notification": run_monju.notification_pending("none"),
            "results": [],
        }
        run_monju.write_json_atomic(run_dir / f"{run_id}-manifest.json", manifest)
        run_monju.atomic_write_text(run_dir / f"{run_id}-runner.pid", f"{supervisor_pid}\n")
        run_monju.atomic_write_text(run_dir / f".{run_id}-staging", f"{staging_dir}\n")
        for reviewer in reviewers:
            state = (states or {}).get(reviewer.key, "success")
            events, stderr, _ = run_monju.reviewer_artifact_paths(staging_dir, run_id, reviewer)
            if state == "error":
                event = {"type": "error", "sessionID": f"s-{reviewer.key}", "error": {"data": {"message": "simulated error"}}}
                exit_code = 1
            elif state == "progress":
                event = {
                    "type": "text",
                    "sessionID": f"s-{reviewer.key}",
                    "part": {"text": "I will inspect the remaining files next."},
                }
                exit_code = 0
            elif state == "malformed":
                events.write_text("not-json\n", encoding="utf-8")
                event = None
                exit_code = 0
            else:
                event = {"type": "text", "sessionID": f"s-{reviewer.key}", "part": {"text": REVIEW_TEXT}}
                exit_code = 0
            if event is not None:
                events.write_text(json.dumps(event) + "\n", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            if state != "incomplete":
                marker = {
                    "schema_version": 1,
                    "completed": True,
                    "run_id": run_id,
                    "ordinal": reviewer.ordinal,
                    "reviewer_key": reviewer.key,
                    "model_id": reviewer.model_id,
                    "variant": reviewer.variant,
                    "reviewer_config_sha256": run_monju.REVIEWER_CONFIG_HASH,
                    "started_at": "2026-08-03T00:00:00.000+00:00",
                    "completed_at": "2026-08-03T00:01:00.000+00:00",
                    "duration_seconds": 60.0,
                    "exit_code": exit_code,
                    "timed_out": False,
                    "interrupted_signal": None,
                    "launch_error": None,
                    "events_size": events.stat().st_size,
                    "events_sha256": run_monju.file_sha256(events),
                    "stderr_size": stderr.stat().st_size,
                    "stderr_sha256": run_monju.file_sha256(stderr),
                }
                if state == "bad_hash":
                    marker["events_sha256"] = "0" * 64
                run_monju.write_json_atomic(
                    run_monju.terminal_marker_path(staging_dir, run_id, reviewer), marker
                )
        return run_dir, staging_dir

    @staticmethod
    def recovery_args(run_dir: Path, notify: str = "none") -> argparse.Namespace:
        return argparse.Namespace(run_dir=run_dir, notify=notify)

    def test_recover_publishes_success_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(Path(temporary))
            with contextlib.redirect_stdout(io.StringIO()):
                first = run_monju.recover_review(self.recovery_args(run_dir))
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            before = manifest_path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                second = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual((first, second), (0, 0))
            self.assertEqual(manifest_path.read_bytes(), before)
            payload = json.loads(before)
            self.assertTrue(payload["recovered"])
            self.assertEqual(payload["status"], "success")

    def test_recover_missing_marker_is_not_ready_and_hash_mismatch_is_invalid(self) -> None:
        for state, expected in (("incomplete", "recovery_not_ready"), ("bad_hash", "recovery_invalid")):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                key = run_monju.REVIEWERS[-1].key
                run_dir, staging = self.make_recovery_run(Path(temporary), states={key: state})
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = run_monju.recover_review(self.recovery_args(run_dir))
                self.assertNotEqual(code, 0)
                self.assertIn(f"STATUS={expected}", stdout.getvalue())
                self.assertTrue(staging.is_dir())

    def test_recover_error_stream_is_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key = run_monju.REVIEWERS[0].key
            run_dir, _ = self.make_recovery_run(Path(temporary), states={key: "error"})
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(code, 3)
            payload = json.loads((run_dir / f"{run_dir.name}-manifest.json").read_text())
            self.assertEqual(payload["status"], "partial_failure")

    def test_progress_only_text_is_not_a_successful_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key = run_monju.REVIEWERS[0].key
            run_dir, _ = self.make_recovery_run(
                Path(temporary), states={key: "progress"}
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(code, 3)
            payload = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text()
            )
            result = payload["results"][0]
            self.assertEqual(result["status"], "failed")
            self.assertIn("omitted required section", result["error"])

    def test_recovery_uses_frozen_manifest_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_reviewers = run_monju.REVIEWERS
            original_hash = run_monju.REVIEWER_CONFIG_HASH
            original_catalog = run_monju.CATALOG_VERIFICATION
            run_dir, _ = self.make_recovery_run(Path(temporary))
            replacement = (
                run_monju.Reviewer(
                    ordinal=1,
                    key="replacement",
                    display_name="Replacement",
                    model_id="opencode-go/replacement",
                    variant="max",
                ),
            )
            run_monju.configure_reviewers(
                replacement,
                run_monju.reviewer_config_hash(replacement),
            )
            run_monju.CATALOG_VERIFICATION = self.catalog_for(replacement)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = run_monju.recover_review(self.recovery_args(run_dir))
                payload = json.loads(
                    (run_dir / f"{run_dir.name}-manifest.json").read_text()
                )
            finally:
                run_monju.configure_reviewers(original_reviewers, original_hash)
                run_monju.CATALOG_VERIFICATION = original_catalog

            self.assertEqual(code, 0)
            self.assertEqual(
                [item["key"] for item in payload["reviewers"]],
                [item.key for item in original_reviewers],
            )

    def test_recover_refuses_live_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(
                Path(temporary), supervisor_pid=os.getpid()
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(code, 65)
            self.assertIn("STATUS=recovery_refused", stdout.getvalue())
            self.assertTrue(staging.is_dir())

    def test_recover_refuses_live_tmux_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(Path(temporary))
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            payload = json.loads(manifest_path.read_text())
            payload["execution_mode"] = run_monju.TMUX_EXECUTION_MODE
            payload["tmux_session"] = run_dir.name
            run_monju.write_json_atomic(manifest_path, payload)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    run_monju,
                    "resolve_tmux_executable",
                    return_value="/bin/tmux",
                ),
                mock.patch.object(
                    run_monju,
                    "tmux_session_is_running",
                    return_value=True,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(code, 65)
            self.assertIn("STATUS=recovery_refused", stdout.getvalue())
            self.assertIn("tmux supervisor session is still running", stdout.getvalue())
            self.assertTrue(staging.is_dir())

    def test_recover_tmux_uses_session_identity_not_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(
                Path(temporary),
                supervisor_pid=os.getpid(),
            )
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            payload = json.loads(manifest_path.read_text())
            payload["execution_mode"] = run_monju.TMUX_EXECUTION_MODE
            payload["tmux_session"] = run_dir.name
            run_monju.write_json_atomic(manifest_path, payload)
            with (
                mock.patch.object(
                    run_monju,
                    "resolve_tmux_executable",
                    return_value="/bin/tmux",
                ),
                mock.patch.object(
                    run_monju,
                    "tmux_session_is_running",
                    return_value=False,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(code, 0)
            terminal = json.loads(manifest_path.read_text())
            self.assertEqual(terminal["status"], "success")

    def test_status_uses_tmux_session_liveness_for_running_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = self.make_recovery_run(Path(temporary))
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            payload = json.loads(manifest_path.read_text())
            payload["execution_mode"] = run_monju.TMUX_EXECUTION_MODE
            payload["tmux_session"] = run_dir.name
            run_monju.write_json_atomic(manifest_path, payload)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    run_monju,
                    "resolve_tmux_executable",
                    return_value="/bin/tmux",
                ),
                mock.patch.object(
                    run_monju,
                    "tmux_session_is_running",
                    return_value=True,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = run_monju.read_status(self.recovery_args(run_dir))
            self.assertEqual(code, 0)
            self.assertIn("STATUS=running", stdout.getvalue())
            self.assertIn("TMUX_SESSION_ALIVE=yes", stdout.getvalue())
            self.assertNotIn("STATUS=stale_running", stdout.getvalue())

    def test_status_reports_tmux_starting_before_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            home = root / "home"
            workspace.mkdir()
            home.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            run_dir, _ = self.prepare(root, workspace, config)
            run_monju.atomic_write_text(
                run_monju.tmux_launch_marker_path(run_dir, run_dir.name),
                f"{run_dir.name}\n",
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    run_monju,
                    "resolve_tmux_executable",
                    return_value="/bin/tmux",
                ),
                mock.patch.object(
                    run_monju,
                    "tmux_session_is_running",
                    return_value=True,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = run_monju.read_status(self.recovery_args(run_dir))
            self.assertEqual(code, 0)
            self.assertIn("STATUS=tmux_starting", stdout.getvalue())
            self.assertIn("TMUX_SESSION_ALIVE=yes", stdout.getvalue())

    def test_status_reports_dynamic_recovery_readiness(self) -> None:
        for state, expected in ((None, "ready"), ("incomplete", "not_ready"), ("bad_hash", "invalid")):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                states = {} if state is None else {run_monju.REVIEWERS[-1].key: state}
                run_dir, _ = self.make_recovery_run(Path(temporary), states=states)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = run_monju.read_status(self.recovery_args(run_dir))
                self.assertEqual(code, 0)
                self.assertIn("STATUS=stale_running", stdout.getvalue())
                self.assertIn(f"RECOVERY={expected}", stdout.getvalue())

    def test_publish_failure_preserves_recovery_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(Path(temporary))
            with (
                mock.patch.object(run_monju, "publish_staging", side_effect=PermissionError("no")),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(code, 74)
            self.assertTrue(staging.is_dir())
            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                self.assertEqual(
                    run_monju.read_status(self.recovery_args(run_dir)),
                    0,
                )
            self.assertIn("STATUS=artifact_failure", status_output.getvalue())
            self.assertIn("RECOVERY=ready", status_output.getvalue())
            self.assertIn("RECOVERY_CAN_PUBLISH=yes", status_output.getvalue())
            self.assertNotIn("RECOVERY=published", status_output.getvalue())
            self.assertNotIn("RESULT ", status_output.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                recovered = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(recovered, 0)
            payload = json.loads(
                (run_dir / f"{run_dir.name}-manifest.json").read_text()
            )
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["recovery_previous_status"], "artifact_failure")
            self.assertFalse(staging.parent.exists())

    def test_supervisor_failure_with_complete_staging_can_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, staging = self.make_recovery_run(Path(temporary))
            manifest_path = run_dir / f"{run_dir.name}-manifest.json"
            payload = json.loads(manifest_path.read_text())
            payload.update(
                {
                    "status": "supervisor_failure",
                    "completed_at": "2026-08-03T00:02:00.000+00:00",
                    "staging_preserved": str(staging),
                    "supervisor_error": "RuntimeError: simulated",
                }
            )
            run_monju.write_json_atomic(manifest_path, payload)

            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                self.assertEqual(
                    run_monju.read_status(self.recovery_args(run_dir)),
                    0,
                )
            self.assertIn("RECOVERY=ready", status_output.getvalue())
            self.assertNotIn("RECOVERY=published", status_output.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                recovered = run_monju.recover_review(self.recovery_args(run_dir))
            self.assertEqual(recovered, 0)
            terminal = json.loads(manifest_path.read_text())
            self.assertEqual(terminal["status"], "success")
            self.assertEqual(
                terminal["recovery_previous_status"],
                "supervisor_failure",
            )

    def test_legacy_terminal_manifest_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "monju-legacy-terminal"
            run_dir = Path(temporary) / run_id
            run_dir.mkdir()
            run_monju.write_json_atomic(
                run_dir / f"{run_id}-manifest.json",
                {
                    "schema_version": 2,
                    "run_id": run_id,
                    "status": "success",
                    "results": [],
                    "notification": run_monju.notification_pending("none"),
                },
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = run_monju.read_status(self.recovery_args(run_dir))
            self.assertEqual(code, 0)
            self.assertIn("STATUS=success", stdout.getvalue())
            self.assertIn("RECOVERY=published", stdout.getvalue())

    def test_notification_failure_does_not_change_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "monju-notification"
            run_dir = Path(temporary) / run_id
            run_dir.mkdir()
            run_monju.write_json_atomic(
                run_dir / f"{run_id}-manifest.json",
                {
                    "schema_version": 3,
                    "run_id": run_id,
                    "status": "success",
                    "notification": run_monju.notification_pending("desktop"),
                },
            )
            with (
                mock.patch.object(
                    run_monju,
                    "run_desktop_notification",
                    side_effect=RuntimeError("unavailable"),
                ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                run_monju.notify_terminal_status("desktop", run_dir, run_id, "success")
            payload = json.loads((run_dir / f"{run_id}-manifest.json").read_text())
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["notification"]["status"], "failed")

    def test_heartbeat_flushes_and_stops_without_secrets(self) -> None:
        output = FlushRecorder()
        heartbeat = run_monju.SupervisorHeartbeat(
            "monju-safe", interval_seconds=0.01, output=output
        ).start()
        time.sleep(0.04)
        heartbeat.stop()
        content = output.getvalue()
        self.assertFalse(heartbeat.is_alive)
        self.assertGreater(output.flush_count, 0)
        self.assertIn("MONJU_HEARTBEAT run_id=monju-safe", content)
        self.assertNotIn("secret", content)

    def test_background_remains_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            result = self.run_command(home, config, "--background")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Use --tmux", result.stderr)

    @unittest.skipIf(os.name != "posix", "POSIX signal behavior")
    def test_interrupt_stops_workers_and_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            env = self.command_env(home, config, FAKE_OPENCODE_SLEEP="20")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--reviewers-file",
                    str(config),
                    "--backend",
                    "opencode",
                    "--reviewer",
                    "reviewer-1",
                    "--workspace",
                    str(workspace),
                    "--run-dir",
                    str(run_dir),
                    "--prompt-file",
                    str(prompt),
                    "--opencode-bin",
                    str(fake),
                ],
                cwd=REPOSITORY,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            manifest = run_dir / f"{run_dir.name}-manifest.json"
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if manifest.is_file() and json.loads(manifest.read_text()).get("status") == "running":
                    break
                time.sleep(0.05)
            else:
                proc.kill()
                self.fail("runner did not enter running state")
            staging_pointer = run_dir / f".{run_dir.name}-staging"
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if staging_pointer.is_file():
                    staging = Path(staging_pointer.read_text().strip())
                    if list(staging.glob("*.worker-task.json")):
                        break
                time.sleep(0.05)
            else:
                proc.kill()
                self.fail("reviewer worker did not start")
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=12)
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertIn("STATUS=interrupted", stdout)
            self.assertFalse(any(t.name.startswith("monju-heartbeat-") for t in threading.enumerate()))

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "SIGHUP"),
        "POSIX SIGHUP behavior",
    )
    def test_sighup_stops_workers_for_tmux_session_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = self.write_config(root / "reviewers.json", 1)
            fake = self.make_fake_opencode(root)
            run_dir, prompt = self.prepare(root, workspace, config)
            prompt.write_text("# Review\nInspect.", encoding="utf-8")
            env = self.command_env(home, config, FAKE_OPENCODE_SLEEP="20")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--reviewers-file",
                    str(config),
                    "--backend",
                    "opencode",
                    "--reviewer",
                    "reviewer-1",
                    "--workspace",
                    str(workspace),
                    "--run-dir",
                    str(run_dir),
                    "--prompt-file",
                    str(prompt),
                    "--opencode-bin",
                    str(fake),
                ],
                cwd=REPOSITORY,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            manifest = run_dir / f"{run_dir.name}-manifest.json"
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if manifest.is_file() and json.loads(manifest.read_text()).get("status") == "running":
                    break
                time.sleep(0.05)
            else:
                proc.kill()
                self.fail("runner did not enter running state")
            staging_pointer = run_dir / f".{run_dir.name}-staging"
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if staging_pointer.is_file():
                    staging = Path(staging_pointer.read_text().strip())
                    if list(staging.glob("*.worker-task.json")):
                        break
                time.sleep(0.05)
            else:
                proc.kill()
                self.fail("reviewer worker did not start")
            proc.send_signal(signal.SIGHUP)
            stdout, stderr = proc.communicate(timeout=12)
            self.assertEqual(proc.returncode, 128 + signal.SIGHUP, stderr)
            self.assertIn("STATUS=interrupted", stdout)
            payload = json.loads(manifest.read_text())
            self.assertEqual(payload["interrupted_signal"], signal.SIGHUP)


if __name__ == "__main__":
    unittest.main()
