import unittest
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.anti_afk import AntiAfkEngine, PhysicalIdleMonitor


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
            self.assertEqual(self.engine.get_roblox_windows_count(), 0)

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


if __name__ == "__main__":
    unittest.main()
