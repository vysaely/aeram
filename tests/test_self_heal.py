"""
Unit tests for AERAM Self-Healing Watchdog Engine.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.models import RobloxAccount
from core.self_heal import SelfHealEngine


class TestSelfHeal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.runner = MagicMock()
        self.storage = MagicMock()
        self.engine = SelfHealEngine(self.runner, self.storage, check_interval=5)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enable_disable(self):
        self.assertFalse(self.engine.running)
        self.engine.enable()
        self.assertTrue(self.engine.running)
        self.engine.disable()
        self.assertFalse(self.engine.running)

    def test_disconnect_log_detection(self):
        log_file = self.base_dir / "instance.log"
        log_file.write_text("Normal game session...\nframe=100\n", encoding="utf-8")
        self.assertFalse(self.engine.is_disconnected_log(log_file))

        log_file.write_text("Normal game session...\nError: Lost connection to the server\n", encoding="utf-8")
        self.assertTrue(self.engine.is_disconnected_log(log_file))

    def test_check_and_heal_triggers_reconnect(self):
        self.engine.enable()
        acc = RobloxAccount(
            account_id="acc_alt_test",
            username="AltTestUser",
            display_name="Alt Test",
            user_id=9999,
            cookie="dummy_cookie",
        )
        self.storage.load_accounts.return_value = [acc]
        self.storage.is_main_account.return_value = False
        self.storage.get_setting.side_effect = lambda k, d="": "123456" if k == "SavedPlaceId" else ""

        prof_dir = self.base_dir / "prof_alt"
        prof_dir.mkdir(parents=True, exist_ok=True)
        log_path = prof_dir / "instance.log"
        log_path.write_text("Error Code: 277 Disconnect event received\n")
        self.storage.get_profile_dir.return_value = prof_dir

        self.runner.get_running_account_ids.return_value = ["acc_alt_test"]
        self.runner.is_account_running.return_value = True

        healed = self.engine.check_and_heal()
        self.assertEqual(healed, 1)
        self.runner.stop_instance.assert_called_with("acc_alt_test")
        self.runner.launch_instance.assert_called_with(acc, place_id="123456", job_id="", follow_user_id=None)

    def test_check_and_heal_reconnects_directly_without_chaining(self):
        """Verify that when no SavedFollowTarget is set, alts do not hijack each other's joins."""
        self.engine.enable()
        acc1 = RobloxAccount(
            account_id="acc_alt_dropped",
            username="DroppedAlt",
            display_name="Dropped",
            user_id=1111,
            cookie="dummy_cookie",
        )
        acc2 = RobloxAccount(
            account_id="acc_alt_survivor",
            username="SurvivorAlt",
            display_name="Survivor",
            user_id=2222,
            cookie="dummy_cookie",
        )
        self.storage.load_accounts.return_value = [acc1, acc2]
        self.storage.is_main_account.return_value = False
        self.storage.get_setting.side_effect = lambda k, d="": "123456" if k == "SavedPlaceId" else ""

        prof_dir1 = self.base_dir / "prof_alt1"
        prof_dir1.mkdir(parents=True, exist_ok=True)
        (prof_dir1 / "instance.log").write_text("Error Code: 277 Disconnect event received\n")

        prof_dir2 = self.base_dir / "prof_alt2"
        prof_dir2.mkdir(parents=True, exist_ok=True)
        (prof_dir2 / "instance.log").write_text("Normal active game session frame=100\n")

        self.storage.get_profile_dir.side_effect = lambda a: prof_dir1 if a.account_id == "acc_alt_dropped" else prof_dir2
        self.runner.get_running_account_ids.return_value = ["acc_alt_dropped", "acc_alt_survivor"]
        self.runner.is_account_running.side_effect = lambda aid: True if aid == "acc_alt_survivor" else False

        healed = self.engine.check_and_heal()
        self.assertEqual(healed, 1)
        # Should reconnect cleanly to SavedPlaceId without forcing follow_user_id
        self.runner.launch_instance.assert_called_with(
            acc1,
            place_id="123456",
            job_id="",
            follow_user_id=None,
        )

    def test_check_and_heal_follows_configured_target(self):
        """Verify that when SavedFollowTarget is explicitly configured, it follows that target."""
        self.engine.enable()
        api = MagicMock()
        self.engine.api = api

        acc1 = RobloxAccount(
            account_id="acc_alt_lone",
            username="LoneAlt",
            display_name="Lone",
            user_id=5555,
            cookie="dummy_cookie",
        )
        self.storage.load_accounts.return_value = [acc1]
        self.storage.is_main_account.return_value = False
        self.storage.get_setting.side_effect = lambda k, d="": {
            "SavedPlaceId": "3016661674",
            "SavedFollowTarget": "TestTargetUser",
        }.get(k, "")

        prof_dir = self.base_dir / "prof_lone"
        prof_dir.mkdir(parents=True, exist_ok=True)
        (prof_dir / "instance.log").write_text("Error Code: 277 Disconnect event received\n")
        self.storage.get_profile_dir.return_value = prof_dir

        self.runner.get_running_account_ids.return_value = ["acc_alt_lone"]
        self.runner.is_account_running.return_value = False

        # Target TestTargetUser is InGame (userPresenceType=2) in subplace 5208655184
        api.resolve_username_to_user_id.return_value = 67949029
        api.get_user_presence.return_value = {"userPresenceType": 2, "placeId": 5208655184}

        healed = self.engine.check_and_heal()
        self.assertEqual(healed, 1)
        self.runner.launch_instance.assert_called_with(
            acc1,
            place_id="5208655184",
            job_id="",
            follow_user_id=67949029,
        )

    def test_detailed_error_code_detection(self):
        log_file = self.base_dir / "instance.log"

        # Code 277
        log_file.write_text("Connection dropped. Error Code: 277\n", encoding="utf-8")
        self.assertEqual(self.engine.get_disconnect_reason(log_file), "Error 277 (Lost connection to the server)")

        # Code 273
        log_file.write_text("Disconnected: Error 273 (Launched from another device)\n", encoding="utf-8")
        self.assertEqual(self.engine.get_disconnect_reason(log_file), "Error 273 (Disconnected: Launched from another device)")

        # Code 267
        log_file.write_text("Error 267: You were kicked by server\n", encoding="utf-8")
        self.assertEqual(self.engine.get_disconnect_reason(log_file), "Error 267 (Kicked by game script or anti-cheat)")

        # Normal log
        log_file.write_text("Normal heartbeat frame=120\n", encoding="utf-8")
        self.assertIsNone(self.engine.get_disconnect_reason(log_file))

    def test_simulate_disconnect(self):
        acc = RobloxAccount(
            account_id="acc_sim_test",
            username="SimAlt",
            display_name="Sim Alt",
            user_id=5555,
            cookie="dummy_cookie",
        )
        self.storage.load_accounts.return_value = [acc]
        prof_dir = self.base_dir / "prof_sim"
        self.storage.get_profile_dir.return_value = prof_dir

        ok = self.engine.simulate_disconnect("acc_sim_test", error_code=277)
        self.assertTrue(ok)

        log_file = prof_dir / "instance.log"
        self.assertTrue(log_file.exists())
        self.assertIn("Error Code: 277", log_file.read_text())
        self.assertTrue(self.engine.is_disconnected_log(log_file))


if __name__ == "__main__":
    unittest.main()
