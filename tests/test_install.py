from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "install.sh"


class InstallerTestCase(unittest.TestCase):
    def run_installer(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(INSTALLER), *arguments],
            cwd=REPOSITORY,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_interactive(
        self,
        choice: str,
        *arguments: str,
        timeout: float = 5,
    ) -> tuple[int, str]:
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(REPOSITORY)
            os.execv(
                "/bin/bash",
                ["/bin/bash", str(INSTALLER), *arguments],
            )

        output = bytearray()
        os.write(descriptor, f"{choice}\n".encode())
        deadline = time.monotonic() + timeout
        wait_status: int | None = None
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([descriptor], [], [], 0.1)
                if readable:
                    try:
                        output.extend(os.read(descriptor, 8192))
                    except OSError:
                        pass
                finished, status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    wait_status = status
                    break
            if wait_status is None:
                os.kill(pid, signal.SIGKILL)
                _, wait_status = os.waitpid(pid, 0)
                self.fail("interactive installer timed out")
        finally:
            os.close(descriptor)
        return os.waitstatus_to_exitcode(wait_status), output.decode(
            "utf-8", errors="replace"
        )

    def test_force_never_removes_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "monju"
            destination.mkdir()
            canary = destination / "keep.txt"
            canary.write_text("keep", encoding="utf-8")

            result = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
                "--force",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(canary.is_file())
            self.assertIn("refusing to remove or replace", result.stderr)

    def test_force_uninstall_never_removes_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "monju"
            destination.mkdir()
            canary = destination / "keep.txt"
            canary.write_text("keep", encoding="utf-8")

            result = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
                "--uninstall",
                "--force",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(canary.is_file())

    def test_dest_must_name_monju_or_skills_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "unrelated"
            destination.mkdir()

            result = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
                "--force",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(destination.is_dir())
            self.assertIn("--dest must end with /monju", result.stderr)

    def test_skills_parent_trailing_slash_appends_monju(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills"
            skills.mkdir()
            result = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                f"{skills}/",
                "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(skills / "monju"), result.stdout)

    def test_foreign_symlink_requires_force_to_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = root / "foreign"
            foreign.mkdir()
            destination = root / "monju"
            destination.symlink_to(foreign)

            refused = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
                "--uninstall",
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(destination.is_symlink())

            removed = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
                "--uninstall",
                "--force",
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.is_symlink())
            self.assertTrue(foreign.is_dir())

    def test_real_install_is_idempotent_and_uninstalls_own_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "monju"

            installed = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), REPOSITORY)

            repeated = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("already linked", repeated.stdout)

            removed = self.run_installer(
                "--agent",
                "codex",
                "--dest",
                str(destination),
                "--uninstall",
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.is_symlink())

    def test_noninteractive_all_targets_all_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "monju"
            result = self.run_installer(
                "--agent",
                "all",
                "--dest",
                str(destination),
                "--dry-run",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("install Codex", result.stdout)
            self.assertIn("install Cursor", result.stdout)
            self.assertIn("install Claude Code", result.stdout)

    @unittest.skipIf(os.name != "posix", "PTY behavior")
    def test_interactive_all_targets_all_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "monju"
            exit_code, output = self.run_interactive(
                "4",
                "--dest",
                str(destination),
                "--dry-run",
            )
            self.assertEqual(exit_code, 0, output)
            self.assertIn("install Codex", output)
            self.assertIn("install Cursor", output)
            self.assertIn("install Claude Code", output)

    @unittest.skipIf(os.name != "posix", "PTY behavior")
    def test_interactive_cancel_exits_successfully(self) -> None:
        exit_code, output = self.run_interactive("5", "--dry-run")
        self.assertEqual(exit_code, 0, output)
        self.assertNotIn("error: no agent selected", output)


if __name__ == "__main__":
    unittest.main()
