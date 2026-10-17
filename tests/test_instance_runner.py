"""
Unit tests for InstanceRunner process supervisor logic.
"""

import json
import shutil
import tempfile
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.instance_runner import extract_private_server_link_code, InstanceRunner
from core.models import RobloxAccount, RunningInstance


class TestInstanceRunner(unittest.TestCase):
    def test_extract_private_server_link_code(self):
        # Direct code
        code = "1234567890abcdef12345678"
        self.assertEqual(extract_private_server_link_code(code), code)

        # URL with privateServerLinkCode
        url1 = "https://www.roblox.com/games/123456/game?privateServerLinkCode=abcdef1234567890"
        self.assertEqual(extract_private_server_link_code(url1), "abcdef1234567890")

        # URL with linkCode
        url2 = "https://www.roblox.com/games/123456/game?linkCode=xyz987654321"
        self.assertEqual(extract_private_server_link_code(url2), "xyz987654321")

        # Invalid
        self.assertIsNone(extract_private_server_link_code(""))
        self.assertIsNone(extract_private_server_link_code("invalid"))

    def test_instance_tracking_and_liveness(self):
        mock_storage = MagicMock()
        runner = InstanceRunner(mock_storage)

        # Register self PID (always alive)
        inst = RunningInstance(
            account_id="acc_live",
            username="LivePlayer",
            pid=os.getpid(),
            start_time=100.0,
        )
        runner.active_instances["acc_live"] = inst
        self.assertTrue(runner.is_instance_running("acc_live"))
        self.assertEqual(runner.get_running_instance("acc_live"), inst)

        # Register an inactive PID (e.g. 999999999)
        inst_dead = RunningInstance(
            account_id="acc_dead",
            username="DeadPlayer",
            pid=999999999,
            start_time=100.0,
        )
        runner.active_instances["acc_dead"] = inst_dead
        self.assertFalse(runner.is_instance_running("acc_dead"))
        # Should be removed from dict
        self.assertNotIn("acc_dead", runner.active_instances)

    def test_launch_instance_sets_isolated_xdg_runtime_dir(self):
        from unittest.mock import patch
        mock_storage = MagicMock()
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir)
            (profile_dir / "run").mkdir(parents=True)
            mock_bin = profile_dir / "bin" / "mocktail"
            mock_storage.init_profile_sandbox.return_value = profile_dir
            mock_storage.get_runtime_dir.return_value = profile_dir / "run"
            mock_storage.get_instance_binary.return_value = mock_bin

            runner = InstanceRunner(mock_storage)
            acc = RobloxAccount(
                account_id="acc_test_runner",
                username="RunnerPlayer",
                display_name="Runner Player",
                user_id=12345,
                cookie="tok123",
            )

            mock_proc = MagicMock()
            mock_proc.pid = os.getpid()
            mock_proc.poll.return_value = None

            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                inst = runner.launch_instance(acc)
                self.assertEqual(inst.pid, os.getpid())
                self.assertTrue(runner.is_instance_running("acc_test_runner"))

                # Check Popen was called with appropriate Linux arguments
                call_args = mock_popen.call_args[0][0]
                self.assertIn(str(mock_bin), call_args)
                self.assertIn("--windowed", call_args)
                self.assertIn("--graphics", call_args)

                call_kwargs = mock_popen.call_args.kwargs
                env = call_kwargs["env"]
                self.assertEqual(env["MOCKTAIL_DATA_ROOT"], str(profile_dir / "data"))
                self.assertEqual(env["MOCKTAIL_COOKIE_FILE"], str(profile_dir / "roblox.cookie"))
                self.assertEqual(env["MOCKTAIL_GRAPHICS_BACKEND"], "opengl")

    def _launch_with_backend_setting(self, api_setting, is_main):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        storage = MagicMock()
        storage.is_main_account.return_value = is_main
        storage.get_setting.side_effect = lambda key, default=None: api_setting if key == "fflag_graphics_api" else default
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir)
            (profile_dir / "run").mkdir()
            storage.init_profile_sandbox.return_value = profile_dir
            storage.get_runtime_dir.return_value = profile_dir / "run"
            storage.get_instance_binary.return_value = profile_dir / "mocktail"
            runner = InstanceRunner(storage)
            acc = RobloxAccount(account_id="acc_gfx", username="Gfx", display_name="Gfx", user_id=1, cookie="tok")
            proc = MagicMock(pid=os.getpid())
            proc.poll.return_value = None
            with patch("subprocess.Popen", return_value=proc) as popen, patch("shutil.which", return_value=None):
                runner.launch_instance(acc)
            argv = popen.call_args[0][0]
            env = popen.call_args.kwargs["env"]
            self._last_fflags = json.loads(env["MOCKTAIL_FAST_FLAGS_JSON"])
            return argv[argv.index("--graphics") + 1], env["MOCKTAIL_GRAPHICS_BACKEND"]

    def _launch_env(self, settings, is_main):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        storage = MagicMock()
        storage.is_main_account.return_value = is_main
        storage.get_setting.side_effect = lambda key, default=None: settings.get(key, default)
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir)
            (profile_dir / "run").mkdir()
            storage.init_profile_sandbox.return_value = profile_dir
            storage.get_runtime_dir.return_value = profile_dir / "run"
            storage.get_instance_binary.return_value = profile_dir / "mocktail"
            runner = InstanceRunner(storage)
            acc = RobloxAccount(account_id="acc_env", username="Env", display_name="Env", user_id=1, cookie="tok")
            proc = MagicMock(pid=os.getpid())
            proc.poll.return_value = None
            with patch("subprocess.Popen", return_value=proc) as popen, patch("shutil.which", return_value=None):
                runner.launch_instance(acc)
            return popen.call_args.kwargs["env"]

    def test_host_cursor_is_main_only_and_can_be_disabled(self):
        self.assertEqual(self._launch_env({}, is_main=True).get("MOCKTAIL_HOST_CURSOR"), "1")
        self.assertNotIn("MOCKTAIL_HOST_CURSOR", self._launch_env({}, is_main=False))
        self.assertNotIn("MOCKTAIL_HOST_CURSOR", self._launch_env({"main_host_cursor": "false"}, is_main=True))

    def test_shiftlock_cursor_passed_only_when_file_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            env = self._launch_env({"shiftlock_cursor_path": image.name}, is_main=False)
            self.assertEqual(env.get("MOCKTAIL_SHIFTLOCK_CURSOR"), image.name)
        self.assertNotIn("MOCKTAIL_SHIFTLOCK_CURSOR",
                         self._launch_env({"shiftlock_cursor_path": "/missing/cursor.png"}, is_main=True))
        self.assertNotIn("MOCKTAIL_SHIFTLOCK_CURSOR", self._launch_env({}, is_main=True))

    def test_alts_rest_between_engine_polls(self):
        self.assertEqual(self._launch_env({}, is_main=False).get("MOCKTAIL_ENGINE_PUMP_REST"), "sleep")
        self.assertNotIn("MOCKTAIL_ENGINE_PUMP_REST", self._launch_env({}, is_main=True))

    def test_helper_fps_scan_skipped_only_for_patched_mocktail(self):
        from unittest.mock import patch
        from core.mocktail_manager import MocktailInstallationInfo
        helper = Path(tempfile.mkdtemp()) / "libmocktail_mouse_helper.so"
        helper.write_bytes(b"")
        try:
            for patched in (False, True):
                info = MocktailInstallationInfo(is_installed=True, helper_path=helper, latency_patched=patched)
                with patch("core.mocktail_manager.MocktailManager.detect", return_value=info):
                    env = self._launch_env({}, is_main=True)
                self.assertEqual(env.get("AERAM_SKIP_FPS_UNCLAMP") == "1", patched)
        finally:
            shutil.rmtree(helper.parent, ignore_errors=True)

    def test_recovery_adopts_game_not_its_session_log_writer(self):
        import shutil
        import subprocess
        tmp = Path(tempfile.mkdtemp())
        fake = tmp / "mocktail_acc_test"
        shutil.copy("/usr/bin/sleep", fake)
        # The writer is forked first in reality, but /proc order is not guaranteed: start the
        # stand-in for it first so it has the lower PID and would win a first-match scan.
        writer = subprocess.Popen([str(fake), "30"])
        game = subprocess.Popen([str(fake), "30"], start_new_session=True)
        try:
            storage = MagicMock()
            storage.list_accounts.return_value = [RobloxAccount(
                account_id="acc_test", username="Rec", display_name="Rec", user_id=1, cookie="c")]
            storage.get_profile_dir.return_value = tmp
            runner = InstanceRunner.__new__(InstanceRunner)
            runner.storage = storage
            runner.active_instances = {}
            runner.scan_running_instances()
            self.assertEqual(runner.active_instances["acc_test"].pid, game.pid)
        finally:
            for proc in (writer, game):
                proc.kill()
                proc.wait()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_main_account_uses_vulkan_when_selected(self):
        self.assertEqual(self._launch_with_backend_setting("Vulkan", is_main=True), ("direct-vulkan", "direct-vulkan"))

    def test_vulkan_main_gets_clean_fastflags(self):
        self._launch_with_backend_setting("Vulkan", is_main=True)
        flags = self._last_fflags
        self.assertTrue(flags["FFlagDebugGraphicsPreferVulkan"])
        self.assertTrue(flags["FFlagEnableGenericChallengeResponseHandling"])  # login challenges still work
        for quality_override in ("DFIntDebugFRMQualityLevelOverride", "DFIntTextureQualityOverride",
                                 "DFFlagDebugPauseVoxelizer", "FFlagDisablePostFx", "FFlagDebugGraphicsPreferOpenGL"):
            self.assertNotIn(quality_override, flags)

        self._launch_with_backend_setting("OpenGL", is_main=True)
        self.assertIn("DFIntDebugFRMQualityLevelOverride", self._last_fflags)

    def test_alts_and_default_settings_stay_on_opengl(self):
        self.assertEqual(self._launch_with_backend_setting("Vulkan", is_main=False), ("opengl", "opengl"))
        self.assertEqual(self._launch_with_backend_setting("Auto", is_main=True), ("opengl", "opengl"))
        self.assertEqual(self._launch_with_backend_setting("OpenGL", is_main=True), ("opengl", "opengl"))

    def test_detects_http_401_on_early_exit(self):
        import tempfile
        from pathlib import Path
        mock_storage = MagicMock()
        runner = InstanceRunner(mock_storage)

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "instance.log"
            log_path.write_text(
                "[mocktail] started\n"
                "[FATAL] Typed Roblox authentication preflight failed: authentication rejected (HTTP 401)\n"
            )

            inst = RunningInstance(
                account_id="acc_401",
                username="ExpiredPlayer",
                pid=999999998,
                start_time=100.0,
                log_path=str(log_path),
            )
            runner.active_instances["acc_401"] = inst

            self.assertFalse(runner.is_instance_running("acc_401"))
            err = runner.get_instance_error("acc_401")
            self.assertIsNotNone(err)
            self.assertIn("HTTP 401", err)

    def test_launch_instance_with_follow_user_id(self):
        from unittest.mock import patch
        mock_storage = MagicMock()
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir)
            (profile_dir / "run").mkdir(parents=True)
            mock_bin = profile_dir / "bin" / "mocktail"
            mock_storage.init_profile_sandbox.return_value = profile_dir
            mock_storage.get_runtime_dir.return_value = profile_dir / "run"
            mock_storage.get_instance_binary.return_value = mock_bin

            runner = InstanceRunner(mock_storage)
            acc = RobloxAccount(
                account_id="acc_follow",
                username="Follower",
                display_name="Follower",
                user_id=11111,
                cookie="tok123",
            )

            mock_proc = MagicMock()
            mock_proc.pid = os.getpid()
            mock_proc.poll.return_value = None

            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                runner.launch_instance(acc, follow_user_id=12345678)
                call_args = mock_popen.call_args[0][0]
                self.assertIn("--launch-uri", call_args)
                idx = call_args.index("--launch-uri")
                self.assertEqual(call_args[idx + 1], "roblox://experiences/start?userId=12345678")

    def test_get_memory_stats(self):
        stats = InstanceRunner.get_memory_stats()
        self.assertIn("mem_avail_mb", stats)
        self.assertIn("swap_free_mb", stats)
        self.assertGreater(stats["mem_avail_mb"], 0)

    def test_alt_instance_virtual_memory_scoping(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        mock_storage = MagicMock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir)
            (profile_dir / "run").mkdir(parents=True)
            mock_bin = profile_dir / "bin" / "mocktail"
            mock_storage.init_profile_sandbox.return_value = profile_dir
            mock_storage.get_runtime_dir.return_value = profile_dir / "run"
            mock_storage.get_instance_binary.return_value = mock_bin
            mock_storage.is_main_account.return_value = False
            mock_storage.get_setting.side_effect = lambda k, d=None: "350" if k == "alt_memory_high_mb" else d

            runner = InstanceRunner(mock_storage)
            acc = RobloxAccount(
                account_id="acc_alt_vm",
                username="AltAccount1",
                display_name="AltAccount1",
                user_id=22222,
                cookie="tok456",
            )

            mock_proc = MagicMock()
            mock_proc.pid = os.getpid()
            mock_proc.poll.return_value = None

            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
                 patch("shutil.which", return_value="/usr/bin/systemd-run"):
                runner.launch_instance(acc)
                call_args = mock_popen.call_args[0][0]
                self.assertEqual(call_args[0], "systemd-run")
                self.assertIn("-pMemoryHigh=350M", call_args)
                self.assertIn("-pCPUWeight=1", call_args)
                self.assertIn("-pIOWeight=1", call_args)

    def test_dynamic_font_resolution_from_payload(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        mock_storage = MagicMock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir) / "profile"
            mocktail_data = Path(tmp_dir) / "mocktail"
            profile_dir.mkdir(parents=True)
            mock_bin = profile_dir / "bin" / "mocktail"

            # Create mock payload with font
            font_dir = mocktail_data / "payloads" / "payload-xyz-123" / "assets" / "fonts"
            font_dir.mkdir(parents=True)
            mock_font = font_dir / "SourceSansPro-Regular.ttf"
            mock_font.write_text("dummy font")

            mock_storage.init_profile_sandbox.return_value = profile_dir
            mock_storage.get_runtime_dir.return_value = profile_dir / "run"
            mock_storage.get_instance_binary.return_value = mock_bin
            mock_storage.is_main_account.return_value = True

            runner = InstanceRunner(mock_storage)
            acc = RobloxAccount(
                account_id="acc_font_test",
                username="FontPlayer",
                display_name="Font Player",
                user_id=33333,
                cookie="tokfont",
            )

            mock_info = MagicMock()
            mock_info.data_dir = mocktail_data
            mock_info.payload_id = "payload-xyz-123"
            mock_info.helper_path = None
            mock_info.lib_dir = None

            mock_proc = MagicMock()
            mock_proc.pid = os.getpid()
            mock_proc.poll.return_value = None

            with patch("core.mocktail_manager.MocktailManager.detect", return_value=mock_info), \
                 patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                runner.launch_instance(acc)
                env = mock_popen.call_args.kwargs["env"]
                self.assertIn("MOCKTAIL_TEXT_FONT", env)
                self.assertEqual(env["MOCKTAIL_TEXT_FONT"], str(mock_font))

    def test_concurrent_multi_account_isolation_and_termination(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path
        mock_storage = MagicMock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            dirA = Path(tmp_dir) / "profileA"
            dirB = Path(tmp_dir) / "profileB"
            dirA.mkdir(parents=True)
            dirB.mkdir(parents=True)
            binA = Path(tmp_dir) / "mocktail_accA"
            binB = Path(tmp_dir) / "mocktail_accB"

            def get_prof(acc):
                return dirA if acc.account_id == "accA" else dirB
            def get_run(acc):
                return (dirA / "run") if acc.account_id == "accA" else (dirB / "run")
            def get_bin(acc):
                return binA if acc.account_id == "accA" else binB

            mock_storage.init_profile_sandbox.side_effect = get_prof
            mock_storage.get_runtime_dir.side_effect = get_run
            mock_storage.get_instance_binary.side_effect = get_bin
            mock_storage.is_main_account.return_value = False

            runner = InstanceRunner(mock_storage)
            accA = RobloxAccount(account_id="accA", username="UserA", display_name="A", user_id=111, cookie="c1")
            accB = RobloxAccount(account_id="accB", username="UserB", display_name="B", user_id=222, cookie="c2")

            procA = MagicMock(pid=1001)
            procB = MagicMock(pid=1002)
            procA.poll.return_value = None
            procB.poll.return_value = None

            popen_calls = []
            def fake_popen(cmd, **kwargs):
                popen_calls.append((cmd, kwargs))
                return procA if len(popen_calls) == 1 else procB

            # Test launching without systemd-run (clean graceful degradation)
            with patch("subprocess.Popen", side_effect=fake_popen), \
                 patch("shutil.which", return_value=None):
                instA = runner.launch_instance(accA)
                instB = runner.launch_instance(accB)

                self.assertEqual(instA.pid, 1001)
                self.assertEqual(instB.pid, 1002)

                envA = popen_calls[0][1]["env"]
                envB = popen_calls[1][1]["env"]

                # Strict isolation: distinct cookie files and XDG runtime directories
                self.assertNotEqual(envA["MOCKTAIL_COOKIE_FILE"], envB["MOCKTAIL_COOKIE_FILE"])
                self.assertNotEqual(envA["XDG_RUNTIME_DIR"], envB["XDG_RUNTIME_DIR"])
                self.assertIn("profileA", envA["MOCKTAIL_COOKIE_FILE"])
                self.assertIn("profileB", envB["MOCKTAIL_COOKIE_FILE"])

                # Check both are registered
                self.assertIn("accA", runner.active_instances)
                self.assertIn("accB", runner.active_instances)

                # Terminate A, verify B remains active
                with patch("os.kill") as mock_kill:
                    runner.terminate_instance("accA")
                    self.assertNotIn("accA", runner.active_instances)
                    self.assertIn("accB", runner.active_instances)


class TestTerminateSafety(unittest.TestCase):
    """terminate_instance must never signal a process that is not the tracked Mocktail client."""

    def _spawn(self, argv0):
        import subprocess
        proc = subprocess.Popen(
            ["bash", "-c", f'exec -a "{argv0}" sleep 30'],
            start_new_session=True,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        # Wait for exec so /proc/<pid>/cmdline reflects argv0
        for _ in range(100):
            try:
                with open(f"/proc/{proc.pid}/cmdline", "rb") as f:
                    if f.read().startswith(argv0.encode()):
                        break
            except OSError:
                pass
            import time
            time.sleep(0.01)
        return proc

    def _runner_tracking(self, pid):
        runner = InstanceRunner(MagicMock())
        runner.active_instances["acc_safe"] = RunningInstance(
            account_id="acc_safe", username="Safe", pid=pid, start_time=0.0
        )
        return runner

    def test_recycled_pid_is_not_signalled(self):
        from unittest.mock import patch
        unrelated = self._spawn("unrelated-editor")
        runner = self._runner_tracking(unrelated.pid)

        with patch("os.kill", wraps=os.kill) as kill, patch("os.killpg") as killpg:
            self.assertTrue(runner.terminate_instance("acc_safe"))
            sent = [c.args[1] for c in kill.call_args_list if c.args[0] == unrelated.pid]
            self.assertFalse([s for s in sent if s != 0], "unrelated PID must not receive a signal")
            killpg.assert_not_called()

        self.assertIsNone(unrelated.poll(), "unrelated process must still be running")
        self.assertNotIn("acc_safe", runner.active_instances)

    def test_mocktail_instance_is_terminated(self):
        client = self._spawn("/opt/mocktail/mocktail_acc_safe")
        runner = self._runner_tracking(client.pid)

        self.assertTrue(runner.terminate_instance("acc_safe"))
        self.assertIsNotNone(client.wait(timeout=5))

    def test_scan_attributes_process_to_exact_account(self):
        client = self._spawn("/opt/mocktail/mocktail_acc_10")
        storage = MagicMock()
        storage.list_accounts.return_value = [
            RobloxAccount(account_id="acc_1", username="One", display_name="One", user_id=1, cookie=""),
            RobloxAccount(account_id="acc_10", username="Ten", display_name="Ten", user_id=10, cookie=""),
        ]
        runner = InstanceRunner(storage)
        runner.scan_running_instances()

        self.assertNotIn("acc_1", runner.active_instances)
        self.assertEqual(runner.active_instances["acc_10"].pid, client.pid)

    def test_own_process_group_is_never_killed(self):
        from unittest.mock import patch
        client = self._spawn("/opt/mocktail/mocktail_acc_safe")
        runner = self._runner_tracking(client.pid)

        with patch("os.getpgid", return_value=os.getpgrp()), patch("os.killpg") as killpg:
            runner.terminate_instance("acc_safe")
            killpg.assert_not_called()

    def test_client_ignoring_sigterm_is_killed_after_grace(self):
        from unittest.mock import patch
        import subprocess
        import time
        proc = subprocess.Popen(
            ["bash", "-c", 'trap "" TERM; exec -a /opt/mocktail/mocktail_acc_safe sleep 30'],
            start_new_session=True,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        for _ in range(100):
            if InstanceRunner._is_mocktail_process(proc.pid):
                break
            time.sleep(0.01)
        runner = self._runner_tracking(proc.pid)

        # Shared process group: only the PID itself may be signalled, so the SIGKILL has to
        # come from the grace-period fallback rather than the group kill.
        with patch("os.getpgid", return_value=os.getpgrp()):
            started = time.monotonic()
            self.assertTrue(runner.terminate_instance("acc_safe", grace=0.3))
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertFalse(os.path.exists(f"/proc/{proc.pid}"), "client must be dead and reaped")

    def test_terminate_all_stops_instances_in_parallel(self):
        import time
        clients = [self._spawn(f"/opt/mocktail/mocktail_acc_{i}") for i in range(3)]
        runner = InstanceRunner(MagicMock())
        for i, client in enumerate(clients):
            runner.active_instances[f"acc_{i}"] = RunningInstance(
                account_id=f"acc_{i}", username=f"U{i}", pid=client.pid, start_time=0.0
            )

        started = time.monotonic()
        self.assertEqual(runner.terminate_all_instances(), 3)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(runner.active_instances, {})
        for client in clients:
            self.assertFalse(os.path.exists(f"/proc/{client.pid}"))


class TestMemoryLaunchCheck(unittest.TestCase):
    def _runner(self, avail, swap_free):
        runner = InstanceRunner(MagicMock())
        runner.get_memory_stats = lambda: {
            "mem_avail_mb": avail, "mem_total_mb": 32768, "swap_free_mb": swap_free, "swap_total_mb": 32768,
        }
        return runner

    def test_comfortable_memory(self):
        self.assertEqual(self._runner(8000, 8000).memory_launch_check("U"), (True, None))

    def test_low_ram_with_swap_warns_but_launches(self):
        may_launch, warning = self._runner(1000, 4000).memory_launch_check("U")
        self.assertTrue(may_launch)
        self.assertIn("RAM low", warning)

    def test_critical_memory_blocks_launch(self):
        may_launch, warning = self._runner(200, 100).memory_launch_check("U")
        self.assertFalse(may_launch)
        self.assertIn("U", warning)


if __name__ == "__main__":
    unittest.main()


