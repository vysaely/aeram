"""
Regression and lifecycle tests for Roblox runtime authentication cookies (DEF-01).
Verifies that plaintext .ROBLOSECURITY cookies:
1. Are NOT created or left at rest upon account creation, save, or import.
2. Are written strictly upon instance launch with 0600 permissions inside the isolated sandbox.
3. Are safely unlinked upon normal process exit, explicit termination, preflight crash, and launch error.
4. Are recovered and purged on startup if left behind by an ungraceful crash (SIGKILL/power loss).
5. Are strictly contained and path-validated to prevent traversal outside instances directory.
6. Support concurrent instances without cross-account cookie clobbering or premature deletion.
"""

import os
import time
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.models import RobloxAccount, RunningInstance
from core.storage import StorageManager
from core.instance_runner import InstanceRunner
from core.auth_state import AccountAuthState


class TestCookieLifecycleRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.storage = StorageManager(base_dir=self.base_dir)

        # Create mock mocktail binary in the temp environment
        self.mock_bin = self.base_dir / "bin" / "mocktail"
        self.mock_bin.parent.mkdir(parents=True, exist_ok=True)
        self.mock_bin.write_bytes(b"\x7fELFfakebinary")
        os.chmod(self.mock_bin, 0o755)
        self.storage.mocktail_bin = self.mock_bin

        self.account_a = RobloxAccount(
            account_id="acc_alpha_001",
            username="AlphaPlayer",
            display_name="Alpha Player",
            user_id=100001,
            cookie="_|WARNING:-DO-NOT-SHARE-THIS...ALPHA_SYNTHETIC_TOKEN",
        )
        self.account_b = RobloxAccount(
            account_id="acc_beta_002",
            username="BetaPlayer",
            display_name="Beta Player",
            user_id=100002,
            cookie="_|WARNING:-DO-NOT-SHARE-THIS...BETA_SYNTHETIC_TOKEN",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_account_does_not_create_plaintext_cookie_at_rest(self):
        """Saving or batch-saving accounts must encrypt secrets in DB and never write plaintext cookies to disk."""
        self.storage.save_account(self.account_a)
        self.storage.save_accounts_batch([self.account_b])

        profile_a = self.storage.get_profile_dir(self.account_a)
        profile_b = self.storage.get_profile_dir(self.account_b)

        cookie_a = profile_a / "roblox.cookie"
        cookie_b = profile_b / "roblox.cookie"
        mocktail_cookie_a = profile_a / "data" / "mocktail" / "roblox.cookie"
        mocktail_cookie_b = profile_b / "data" / "mocktail" / "roblox.cookie"

        self.assertFalse(cookie_a.exists(), "Account A roblox.cookie must not exist at rest")
        self.assertFalse(cookie_b.exists(), "Account B roblox.cookie must not exist at rest")
        self.assertFalse(mocktail_cookie_a.exists(), "Account A data/mocktail cookie must not exist at rest")
        self.assertFalse(mocktail_cookie_b.exists(), "Account B data/mocktail cookie must not exist at rest")

    def test_successful_launch_creates_cookie_and_normal_exit_unlinks_it(self):
        """Launching creates the cookie with mode 0600; detecting normal process exit immediately unlinks it."""
        self.storage.save_account(self.account_a)
        runner = InstanceRunner(self.storage)

        mock_proc = MagicMock()
        mock_proc.pid = os.getpid()
        mock_proc.poll.return_value = None

        profile_dir = self.storage.get_profile_dir(self.account_a)
        cookie_file = profile_dir / "roblox.cookie"
        mocktail_cookie = profile_dir / "data" / "mocktail" / "roblox.cookie"

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("os.waitpid", return_value=(0, 0)):
                inst = runner.launch_instance(self.account_a)
                self.assertEqual(inst.pid, os.getpid())
                self.assertTrue(runner.is_instance_running(self.account_a.account_id))

            # Cookie must exist during active runtime with permissions 0600
            self.assertTrue(cookie_file.exists())
            self.assertTrue(mocktail_cookie.exists())
            self.assertEqual(cookie_file.stat().st_mode & 0o777, 0o600)
            self.assertIn("ALPHA_SYNTHETIC_TOKEN", cookie_file.read_text())

            # Simulate normal exit: child process has ended
            mock_proc.poll.return_value = 0
            with patch("os.waitpid", return_value=(os.getpid(), 0)):
                is_running = runner.is_instance_running(self.account_a.account_id)
                self.assertFalse(is_running)

            # Cookie must be unlinked immediately upon detected exit
            self.assertFalse(cookie_file.exists(), "roblox.cookie must be unlinked on normal exit")
            self.assertFalse(mocktail_cookie.exists(), "data/mocktail/roblox.cookie must be unlinked on normal exit")

    def test_explicit_instance_termination_unlinks_cookie(self):
        """Calling terminate_instance terminates process and cleans up cookie files."""
        self.storage.save_account(self.account_a)
        runner = InstanceRunner(self.storage)

        mock_proc = MagicMock()
        mock_proc.pid = os.getpid()
        mock_proc.poll.return_value = None

        profile_dir = self.storage.get_profile_dir(self.account_a)
        cookie_file = profile_dir / "roblox.cookie"

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("os.waitpid", return_value=(0, 0)):
                runner.launch_instance(self.account_a)
                self.assertTrue(cookie_file.exists())

            with patch("os.killpg"), patch("os.kill"), patch("os.getpgid", return_value=os.getpid()):
                with patch("os.waitpid", return_value=(0, 0)):
                    stopped = runner.terminate_instance(self.account_a.account_id)
                    self.assertTrue(stopped)

            self.assertFalse(cookie_file.exists(), "roblox.cookie must be unlinked after terminate_instance")

    def test_initialization_and_launch_failures_clean_up_cookie(self):
        """If subprocess.Popen fails (e.g. binary exec failure), any written cookie must be cleaned up."""
        self.storage.save_account(self.account_a)
        runner = InstanceRunner(self.storage)

        profile_dir = self.storage.get_profile_dir(self.account_a)
        cookie_file = profile_dir / "roblox.cookie"

        with patch("subprocess.Popen", side_effect=OSError("Exec format error")):
            with self.assertRaises(RuntimeError):
                runner.launch_instance(self.account_a)

        self.assertFalse(cookie_file.exists(), "roblox.cookie must be cleaned up after launch failure")

    def test_immediate_preflight_crash_cleans_up_cookie(self):
        """If Mocktail exits immediately on launch (e.g. missing dynamic libraries), cookie is unlinked."""
        self.storage.save_account(self.account_a)
        runner = InstanceRunner(self.storage)

        mock_proc = MagicMock()
        mock_proc.pid = os.getpid()
        # Exits immediately with error code 127
        mock_proc.poll.return_value = 127

        profile_dir = self.storage.get_profile_dir(self.account_a)
        cookie_file = profile_dir / "roblox.cookie"

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.launch_instance(self.account_a)

        self.assertFalse(cookie_file.exists(), "roblox.cookie must be unlinked after immediate preflight crash")

    def test_unexpected_crash_recovery_on_startup(self):
        """Simulated crash recovery: Stale cookies from ungraceful shutdown (SIGKILL/power loss) are purged on startup."""
        self.storage.save_account(self.account_a)
        self.storage.save_account(self.account_b)

        profile_a = self.storage.get_profile_dir(self.account_a)
        profile_b = self.storage.get_profile_dir(self.account_b)

        # Write synthetic stale cookies directly into profiles as if AERAM was SIGKILLed
        self.storage.write_runtime_cookie(self.account_a)
        self.storage.write_runtime_cookie(self.account_b)

        cookie_a = profile_a / "roblox.cookie"
        cookie_b = profile_b / "roblox.cookie"
        self.assertTrue(cookie_a.exists())
        self.assertTrue(cookie_b.exists())

        # Test recovery via cleanup_stale_runtime_cookies: Account B is active, Account A is stale
        cleaned = self.storage.cleanup_stale_runtime_cookies(running_account_ids={self.account_b.account_id})
        self.assertGreaterEqual(cleaned, 1)
        self.assertFalse(cookie_a.exists(), "Stale Account A cookie must be recovered and unlinked")
        self.assertTrue(cookie_b.exists(), "Active Account B cookie must NOT be touched by recovery")

        # Now test that when an InstanceRunner starts fresh and account_b is not in /proc, it purges Account B too
        runner = InstanceRunner(self.storage)
        self.assertFalse(cookie_b.exists(), "Inactive Account B cookie must be purged during InstanceRunner startup recovery")

    def test_concurrent_instances_independent_cleanup(self):
        """Concurrent running instances have independent cookies; stopping one does NOT remove the other."""
        self.storage.save_account(self.account_a)
        self.storage.save_account(self.account_b)
        runner = InstanceRunner(self.storage)

        proc_a = MagicMock(pid=999201)
        proc_a.poll.return_value = None
        proc_b = MagicMock(pid=999202)
        proc_b.poll.return_value = None

        profile_a = self.storage.get_profile_dir(self.account_a)
        profile_b = self.storage.get_profile_dir(self.account_b)
        cookie_a = profile_a / "roblox.cookie"
        cookie_b = profile_b / "roblox.cookie"

        with patch("subprocess.Popen", side_effect=[proc_a, proc_b]):
            runner.launch_instance(self.account_a)
            runner.launch_instance(self.account_b)

            self.assertTrue(cookie_a.exists())
            self.assertTrue(cookie_b.exists())

            # Terminate account A only
            with patch("os.killpg"), patch("os.kill"), patch("os.getpgid", return_value=999201):
                runner.terminate_instance(self.account_a.account_id)

            self.assertFalse(cookie_a.exists(), "Account A cookie must be unlinked")
            self.assertTrue(cookie_b.exists(), "Account B cookie must remain active")

            # Now terminate account B
            with patch("os.killpg"), patch("os.kill"), patch("os.getpgid", return_value=999202):
                runner.terminate_instance(self.account_b.account_id)

            self.assertFalse(cookie_b.exists(), "Account B cookie must be unlinked")

    def test_path_validation_prevents_cleanup_outside_sandbox(self):
        """Malicious or manipulated account identifiers cannot cause deletion outside the instances directory."""
        # Attempt directory traversal attacks
        traversal_attempts = [
            "../../etc",
            "../system_file",
            "/etc/passwd",
            "....//....//shadow",
        ]

        # Create a canary file outside instances directory
        canary = self.base_dir / "critical_vault_file.txt"
        canary.write_text("DO NOT DELETE")

        for bad_id in traversal_attempts:
            cleaned = self.storage.cleanup_runtime_cookie(bad_id)
            self.assertEqual(cleaned, 0, f"Traversal attempt '{bad_id}' must return 0 deletions")

        self.assertTrue(canary.exists())
        self.assertEqual(canary.read_text(), "DO NOT DELETE")

    def test_cleanup_idempotent_when_already_absent(self):
        """Calling cleanup_runtime_cookie repeatedly when file does not exist must be safe and return 0."""
        self.storage.save_account(self.account_a)
        # First call on empty
        cleaned1 = self.storage.cleanup_runtime_cookie(self.account_a)
        self.assertEqual(cleaned1, 0)

        # Write once, clean twice
        self.storage.write_runtime_cookie(self.account_a)
        cleaned2 = self.storage.cleanup_runtime_cookie(self.account_a)
        self.assertGreaterEqual(cleaned2, 1)

        cleaned3 = self.storage.cleanup_runtime_cookie(self.account_a)
        self.assertEqual(cleaned3, 0)

    def test_application_shutdown_atexit_cleanup(self):
        """The emergency atexit cleanup hook safely cleans all active instances' cookies."""
        self.storage.save_account(self.account_a)
        self.storage.save_account(self.account_b)
        runner = InstanceRunner(self.storage)

        proc_a = MagicMock(pid=999301)
        proc_a.poll.return_value = None
        proc_b = MagicMock(pid=999302)
        proc_b.poll.return_value = None

        profile_a = self.storage.get_profile_dir(self.account_a)
        profile_b = self.storage.get_profile_dir(self.account_b)
        cookie_a = profile_a / "roblox.cookie"
        cookie_b = profile_b / "roblox.cookie"

        with patch("subprocess.Popen", side_effect=[proc_a, proc_b]):
            runner.launch_instance(self.account_a)
            runner.launch_instance(self.account_b)

            self.assertTrue(cookie_a.exists())
            self.assertTrue(cookie_b.exists())

            # Trigger atexit handler
            runner._atexit_cleanup()

            self.assertFalse(cookie_a.exists(), "Account A cookie must be cleaned up by atexit")
            self.assertFalse(cookie_b.exists(), "Account B cookie must be cleaned up by atexit")


if __name__ == "__main__":
    unittest.main()
