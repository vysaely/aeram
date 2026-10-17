import unittest
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.anti_afk import AntiAfkEngine, PhysicalIdleMonitor, write_pulse_file


class TestAntiAfkEngine(unittest.TestCase):
    def setUp(self):
        self.engine = AntiAfkEngine(interval_seconds=60, idle_threshold_seconds=0)

    def tearDown(self):
        self.engine.stop()

    def test_get_mocktail_pids_empty(self):
        """When no mocktail processes run, returns empty list without error."""
        with patch("os.scandir") as mock_scandir:
            mock_scandir.return_value.__enter__.return_value = []
            pids = self.engine.get_mocktail_pids()
            self.assertEqual(pids, [])

    def test_get_mocktail_pids_detection(self):
        """Detects mocktail processes by cmdline and filters out unrelated processes."""
        mock_entry1 = MagicMock()
        mock_entry1.name = "12345"
        mock_entry1.is_dir.return_value = True
        mock_entry1.stat.return_value.st_uid = os.getuid()
        mock_entry1.path = "/proc/12345"

        mock_entry2 = MagicMock()
        mock_entry2.name = "67890"
        mock_entry2.is_dir.return_value = True
        mock_entry2.stat.return_value.st_uid = os.getuid()
        mock_entry2.path = "/proc/67890"

        with patch("os.scandir") as mock_scandir:
            mock_scandir.return_value.__enter__.return_value = [mock_entry1, mock_entry2]
            with patch("builtins.open", unittest.mock.mock_open()) as mock_file:
                # First file has mocktail_acc_, second file is firefox
                mock_file.side_effect = [
                    unittest.mock.mock_open(read_data=b"/usr/bin/mocktail_acc_1\x00").return_value,
                    unittest.mock.mock_open(read_data=b"/usr/bin/firefox\x00").return_value,
                ]
                pids = self.engine.get_mocktail_pids()
                self.assertEqual(pids, [12345])

    def test_only_game_processes_are_signalled(self):
        from core.anti_afk import is_mocktail_game_cmdline
        for game in (b"/home/u/.local/lib/mocktail/mocktail_acc_50e3\x00--windowed\x00",
                     b"/usr/bin/mocktail\x00", b"mocktail\x00roblox://x\x00"):
            self.assertTrue(is_mocktail_game_cmdline(game), game)
        for other in (b"/home/u/.local/lib/mocktail/mocktail_webview_helper\x00",
                      b"/home/u/.local/lib/mocktail/mocktail_updater\x00",
                      b"tail\x00-f\x00/home/u/.local/share/mocktail/log\x00",
                      b"cmake\x00--build\x00/src/mocktail/build\x00",
                      b"/bin/sh\x00/home/u/.local/bin/mocktail\x00",
                      b"code\x00/home/u/mocktail_acc_notes.txt\x00"):
            self.assertFalse(is_mocktail_game_cmdline(other), other)

    def test_trigger_ipc_pulse(self):
        """Triggers pulse file in /dev/shm and sends signal to target PIDs."""
        pulse_path = Path("/dev/shm/aeram_afk_pulse")
        initial_mtime = pulse_path.stat().st_mtime if pulse_path.exists() else 0

        with patch("os.kill") as mock_kill:
            signaled = self.engine.trigger_ipc_pulse([99999])
            self.assertEqual(signaled, 1)
            mock_kill.assert_called_once_with(99999, unittest.mock.ANY)

        self.assertTrue(pulse_path.exists())
        self.assertGreaterEqual(pulse_path.stat().st_mtime, initial_mtime)

    def test_pulse_all_instances_skips_when_user_busy(self):
        """When user is actively using mouse/keyboard, desktop focus cycling is completely skipped."""
        with patch.object(self.engine, "get_mocktail_pids", return_value=[11111]):
            with patch.object(self.engine.monitor, "is_user_busy", return_value=True):
                with patch.object(self.engine.compositor, "cycle_mocktail_windows") as mock_cycle:
                    pulsed = self.engine.pulse_all_instances(force=False)
                    self.assertEqual(pulsed, 1)
                    # Window focus cycling must NOT be called when user is busy
                    mock_cycle.assert_not_called()

    def test_pulse_all_instances_zero_instances(self):
        """When zero instances are active, returns 0 cleanly."""
        with patch.object(self.engine, "get_mocktail_pids", return_value=[]):
            pulsed = self.engine.pulse_all_instances(force=False)
            self.assertEqual(pulsed, 0)


class TestPulseFile(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "aeram_afk_pulse"

    def tearDown(self):
        self.tmp.cleanup()

    def test_pulse_file_is_owner_only(self):
        write_pulse_file(123.0, self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.read_text(), "123.0")

    def test_world_writable_legacy_file_is_tightened(self):
        self.path.write_text("old")
        os.chmod(self.path, 0o666)
        write_pulse_file(5.0, self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_planted_symlink_is_not_followed(self):
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("keep")
        self.path.symlink_to(victim)
        with self.assertRaises(OSError):
            write_pulse_file(1.0, self.path)
        self.assertEqual(victim.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
