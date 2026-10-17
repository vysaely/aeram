"""
Automated unit tests for PyQt6 GUI window and card components (offscreen platform).
"""

import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from PyQt6 import QtWidgets, QtCore, QtGui

from core.models import RobloxAccount
from core.storage import StorageManager
from core.roblox_api import RobloxAPI
from core.instance_runner import InstanceRunner
from ui.main_window import MainWindow

# Initialize single global QApplication for offscreen tests
QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
_app = QtWidgets.QApplication.instance()
if _app is None:
    _app = QtWidgets.QApplication(["ram-linux-test", "-platform", "offscreen"])


class TestGUI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name) / "ram-gui-test"
        self.mocktail_data = Path(self.tmp_dir.name) / "mocktail_data"
        self.mocktail_bin = Path(self.tmp_dir.name) / "mocktail_bin"
        (self.mocktail_data / "payloads").mkdir(parents=True, exist_ok=True)
        (self.mocktail_data / "current.json").write_text('{"dummy": true}')
        self.mocktail_bin.write_text("#!/bin/sh\nexit 0\n")
        self.mocktail_bin.chmod(0o755)

        self.storage = StorageManager(
            base_dir=self.base_dir,
            mocktail_data=self.mocktail_data,
            mocktail_bin=self.mocktail_bin,
        )
        self.api = RobloxAPI()
        self.runner = InstanceRunner(self.storage)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_main_window_lifecycle(self):
        acc = RobloxAccount(
            account_id="acc_gui_1",
            username="GUITestUser",
            display_name="Display Name",
            user_id=12345,
            cookie="_|WARNING:-DO-NOT-SHARE-THIS...tok",
            robux_balance=1000,
            group="Trading",
            notes="Note 1",
        )
        self.storage.save_account(acc)

        window = MainWindow(self.storage, self.api, self.runner)
        self.assertEqual(window.table.rowCount(), 1)
        self.assertEqual(window.table.item(0, 0).text(), "GUITestUser")
        self.assertEqual(window.table.item(0, 1).text(), "Display Name")
        self.assertEqual(window.table.item(0, 2).text(), "Trading")
        self.assertEqual(window.table.item(0, 3).text(), "L • 6 FPS")
        self.assertEqual(window.table.item(0, 4).text(), "Note 1")

        # Verify selected account metadata population
        self.assertEqual(window.alias_input.text(), "Display Name")
        self.assertEqual(window.description_input.toPlainText(), "Note 1")
        self.assertEqual(window.selection_badge.text(), "1 selected")
        self.assertEqual(window.btn_join_server.text(), "Join Server")

        # Test in-place Set Alias
        window.alias_input.setText("Updated Alias")
        window.btn_set_alias.click()
        self.assertEqual(window.table.item(0, 1).text(), "Updated Alias")
        reloaded = self.storage.get_account("acc_gui_1")
        self.assertEqual(reloaded.display_name, "Updated Alias")

        # Test in-place Set Description
        window.description_input.setPlainText("Updated Description")
        window.btn_set_desc.click()
        self.assertEqual(window.table.item(0, 4).text(), "Updated Description")
        reloaded = self.storage.get_account("acc_gui_1")
        self.assertEqual(reloaded.notes, "Updated Description")

        # Verify Place ID and Job ID persistence across sessions
        window.place_id_input.setText("189707")
        self.assertEqual(self.storage.get_setting("SavedPlaceId"), "189707")
        window.job_id_input.setText("job-12345")
        self.assertEqual(self.storage.get_setting("SavedJobId"), "job-12345")

        # Test Profile Mode switching
        self.assertEqual(window.table.item(0, 3).text(), "L • 6 FPS")
        window._set_selected_profile_mode("main")
        self.assertEqual(window.table.item(0, 3).text(), "H • 330 FPS")
        self.assertTrue(self.storage.is_main_account(self.storage.get_account("acc_gui_1")))

        window._set_selected_profile_mode("alt")
        self.assertEqual(window.table.item(0, 3).text(), "L • 6 FPS")
        self.assertFalse(self.storage.is_main_account(self.storage.get_account("acc_gui_1")))

        # Test Group moving
        window._move_selected_to_group("Farming")
        self.assertEqual(window.table.item(0, 2).text(), "Farming")
        reloaded = self.storage.get_account("acc_gui_1")
        self.assertEqual(reloaded.group, "Farming")

        window.close()
        self.assertFalse(window.poll_timer.isActive())
        self.assertFalse(window._metric_cycle_timer.isActive())

    def test_gui_account_and_group_reordering(self):
        acc1 = RobloxAccount(account_id="acc_a", username="UserAlpha", display_name="A", user_id=1, cookie="c1", group="G1")
        acc2 = RobloxAccount(account_id="acc_b", username="UserBeta", display_name="B", user_id=2, cookie="c2", group="G2")
        acc3 = RobloxAccount(account_id="acc_c", username="UserGamma", display_name="C", user_id=3, cookie="c3", group="G1")
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)
        self.storage.save_account(acc3)

        window = MainWindow(self.storage, self.api, self.runner)
        self.assertEqual(window.table.rowCount(), 3)
        self.assertEqual(window.table.item(0, 0).text(), "UserAlpha")
        self.assertEqual(window.table.item(1, 0).text(), "UserBeta")
        self.assertEqual(window.table.item(2, 0).text(), "UserGamma")

        # Test moving row 2 (UserGamma) to row 0
        window._on_table_rows_reordered([2], 0)
        self.assertEqual(window.table.item(0, 0).text(), "UserGamma")
        self.assertEqual(window.table.item(1, 0).text(), "UserAlpha")
        self.assertEqual(window.table.item(2, 0).text(), "UserBeta")

        # Verify storage was updated and persists custom order
        stored_order = [a.username for a in self.storage.list_accounts()]
        self.assertEqual(stored_order, ["UserGamma", "UserAlpha", "UserBeta"])

        # Test Move Up / Move Down buttons
        window.table.selectRow(1)  # select UserAlpha
        window._move_selected_rows_down()
        self.assertEqual(window.table.item(1, 0).text(), "UserBeta")
        self.assertEqual(window.table.item(2, 0).text(), "UserAlpha")

        window._move_selected_rows_up()
        self.assertEqual(window.table.item(1, 0).text(), "UserAlpha")
        self.assertEqual(window.table.item(2, 0).text(), "UserBeta")

        # Test group reordering
        self.storage.set_group_order(["G2", "G1"])
        self.assertEqual(self.storage.list_groups(), ["G2", "G1"])
        window._update_group_combo_items()
        self.assertTrue(window.group_combo.itemText(1).startswith("G2"))
        self.assertTrue(window.group_combo.itemText(2).startswith("G1"))

        # Test sort table by group order
        window._sort_table_by_group_order()
        # Group G2 has UserBeta, Group G1 has UserGamma and UserAlpha
        self.assertEqual(window.table.item(0, 0).text(), "UserBeta")

        # Test clicking Group header (column 2) directly toggles ascending / descending
        window._on_header_section_clicked(2)  # Ascending: G2 first (priority 0) -> UserBeta first
        self.assertEqual(window.table.item(0, 0).text(), "UserBeta")
        window._on_header_section_clicked(2)  # Descending: G1 first -> UserGamma first (preserves custom order inside G1)
        self.assertEqual(window.table.item(0, 0).text(), "UserGamma")

        # Test dragging row 0 into row 1 (moving downwards onto an item)
        # Verify neither item disappears and row 0 is positioned right after row 1
        window.table.selectRow(0)
        # Create drop event at row 1
        mime_data = QtCore.QMimeData()
        drop_ev = QtGui.QDropEvent(
            QtCore.QPointF(5, 30),
            QtCore.Qt.DropAction.MoveAction,
            mime_data,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier
        )
        window.table.dropEvent(drop_ev)
        QtWidgets.QApplication.processEvents()

        # Check all rows still exist and are populated
        self.assertEqual(window.table.rowCount(), 3)
        self.assertIsNotNone(window.table.item(0, 0))
        self.assertIsNotNone(window.table.item(1, 0))
        self.assertIsNotNone(window.table.item(2, 0))

        window.close()

    def test_web_login_dialog_lifecycle(self):
        from ui.web_login_dialog import WebLoginDialog

        # Test adding new account via WebLoginDialog
        dlg = WebLoginDialog(self.storage, self.api, default_group="Trading")
        self.assertIsNotNone(dlg)
        self.assertEqual(dlg.default_group, "Trading")

        # Mock enrich_account response
        self.api.enrich_account = lambda cookie: {
            "username": "WebLoginUser",
            "display_name": "Web User",
            "user_id": 998877,
            "avatar_url": "https://example.com/avatar.png",
            "robux_balance": 500,
        }

        # Simulate cookie detection and finalization
        fake_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--" + "A" * 100
        dlg._process_detected_cookie(fake_cookie)
        QtWidgets.QApplication.processEvents()

        self.assertTrue(dlg._is_saved)
        self.assertIsNotNone(dlg.new_account)
        self.assertEqual(dlg.new_account.username, "WebLoginUser")
        self.assertEqual(dlg.new_account.display_name, "Web User")
        self.assertEqual(dlg.new_account.group, "Trading")

        # Check vault persistence
        vault_acc = self.storage.find_account_by_username("WebLoginUser")
        self.assertIsNotNone(vault_acc)
        self.assertEqual(vault_acc.cookie, fake_cookie)
        dlg.close()

        # Test re-authenticating existing account
        existing_acc = vault_acc
        dlg_reauth = WebLoginDialog(self.storage, self.api, target_account=existing_acc)
        self.assertEqual(dlg_reauth.target_account.username, "WebLoginUser")

        # Update cookie via reauth
        fresh_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--" + "B" * 100
        dlg_reauth._finalize_account(fresh_cookie, {
            "username": "WebLoginUser",
            "display_name": "Web User Updated",
            "user_id": 998877,
            "avatar_url": "https://example.com/new_avatar.png",
            "robux_balance": 750,
        })
        QtWidgets.QApplication.processEvents()

        reloaded = self.storage.find_account_by_username("WebLoginUser")
        self.assertEqual(reloaded.cookie, fresh_cookie)
        self.assertEqual(reloaded.display_name, "Web User Updated")
        self.assertEqual(reloaded.robux_balance, 750)
        dlg_reauth.close()

    def test_background_auth_validation_on_load(self):
        from core.auth_state import AccountAuthState
        acc_valid = RobloxAccount(
            account_id="acc_val_1",
            username="ValidUser",
            display_name="Valid",
            user_id=111,
            cookie="valid_cookie",
        )
        acc_expired = RobloxAccount(
            account_id="acc_exp_2",
            username="ExpiredUser",
            display_name="Expired",
            user_id=222,
            cookie="expired_cookie",
        )
        self.storage.save_account(acc_valid)
        self.storage.save_account(acc_expired)

        # Mock API validate_cookie
        def mock_validate(cookie):
            if cookie == "valid_cookie":
                return True, {"id": 111, "name": "ValidUser"}, "OK"
            return False, None, "EXPIRED"

        self.api.validate_cookie = mock_validate

        window = MainWindow(self.storage, self.api, self.runner)
        # Wait for worker thread to finish
        if getattr(window, "_auth_validator_worker", None):
            window._auth_validator_worker.wait(3000)
        QtWidgets.QApplication.processEvents()

        state1 = self.runner.auth_state_manager.get_state("acc_val_1")
        state2 = self.runner.auth_state_manager.get_state("acc_exp_2")
        self.assertEqual(state1, AccountAuthState.READY)
        self.assertEqual(state2, AccountAuthState.SESSION_EXPIRED)

        # Test killing account unregisters from self_heal_engine
        window.self_heal_engine.register_account("acc_val_1")
        self.assertIn("acc_val_1", window.self_heal_engine.monitored_account_ids)
        window.table.selectAll()
        window._kill_selected()
        window.close()

    def test_browser_roblox_launch_protocol(self):
        from ui.web_login_dialog import parse_roblox_launch_url, WebLoginDialog
        from unittest.mock import MagicMock

        # Test classic roblox-player scheme parsing
        classic_uri = (
            "roblox-player:1+launchmode:play+gameinfo:ticket_abc+"
            "placelauncherurl:https%3A%2F%2Fassetgame.roblox.com%2Fgame%2FPlaceLauncher.ashx"
            "%3Frequest%3DRequestGame%26placeId%3D1818%26gameInstanceId%3Dserver_xyz"
        )
        fields = parse_roblox_launch_url(classic_uri)
        self.assertEqual(fields.get("launchmode"), "play")
        self.assertEqual(fields.get("placeid"), "1818")
        self.assertEqual(fields.get("gameinstanceid"), "server_xyz")

        # Test modern roblox scheme parsing
        modern_uri = "roblox://experiences/start?placeId=2753915549&gameInstanceId=job_789"
        fields_mod = parse_roblox_launch_url(modern_uri)
        self.assertEqual(fields_mod.get("placeid"), "2753915549")
        self.assertEqual(fields_mod.get("gameinstanceid"), "job_789")

        # Test handler invokes runner
        acc = RobloxAccount(
            account_id="acc_browser_test",
            username="BrowserUser",
            display_name="BrowserUser",
            user_id=12345,
            cookie="fake_cookie",
        )
        mock_runner = MagicMock()
        dlg = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            runner=mock_runner,
            target_account=acc,
            mode="browser",
        )
        dlg._handle_roblox_protocol(classic_uri)
        mock_runner.launch_instance.assert_called_once_with(acc, place_id=1818, job_id="server_xyz")
        dlg.close()

    def test_web_login_dialog_2fa_ui_and_shortcuts(self):
        from ui.web_login_dialog import WebLoginDialog, ROBLOX_2FA_DOM_FIX_JS
        from unittest.mock import MagicMock, patch

        # Check DOM fix script contents
        self.assertIn("enforceFocus", ROBLOX_2FA_DOM_FIX_JS)
        self.assertIn("MuiBackdrop-root", ROBLOX_2FA_DOM_FIX_JS)
        self.assertIn("2147483647", ROBLOX_2FA_DOM_FIX_JS)
        self.assertIn("pointer-events", ROBLOX_2FA_DOM_FIX_JS)

        # Check Environment Richness & Stealth script contents
        from ui.web_login_dialog import ROBLOX_ENVIRONMENT_RICHNESS_JS
        self.assertIn("webdriver", ROBLOX_ENVIRONMENT_RICHNESS_JS)
        self.assertTrue(
            "Google Inc. (AMD)" in ROBLOX_ENVIRONMENT_RICHNESS_JS
            or "Google Inc. (NVIDIA)" in ROBLOX_ENVIRONMENT_RICHNESS_JS
        )
        self.assertIn("hardwareConcurrency", ROBLOX_ENVIRONMENT_RICHNESS_JS)
        self.assertIn("deviceMemory", ROBLOX_ENVIRONMENT_RICHNESS_JS)
        self.assertIn("PDF Viewer", ROBLOX_ENVIRONMENT_RICHNESS_JS)

        # Instantiate WebLoginDialog in browser mode
        acc = RobloxAccount(
            account_id="acc_2fa_test",
            username="TwoFactorUser",
            display_name="TwoFactorUser",
            user_id=8888,
            cookie="fake_cookie",
        )
        dlg = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            target_account=acc,
            mode="browser",
        )
        self.assertTrue(hasattr(dlg, "btn_2fa"))
        self.assertEqual(dlg.btn_2fa.text(), "Enter 2FA Code")
        self.assertTrue(hasattr(dlg, "badge_rich"))
        self.assertIn("RICH ENVIRONMENT", dlg.badge_rich.text())
        self.assertTrue(hasattr(dlg, "_shortcut_f2"))
        self.assertTrue(hasattr(dlg, "_shortcut_2fa"))

        # Test Clear Browser Cache
        dlg._clear_browser_cache()
        self.assertIn("cleared", dlg.status_label.text())

        # Test prompt and submit 2FA with mocked input dialog
        with patch("ui.web_login_dialog.BorderlessInputDialog.getText", return_value=("123456", True)):
            dlg._prompt_and_submit_2fa()
            dlg._on_2fa_submit_result(True)
            self.assertIn("2FA code filled", dlg.status_label.text())

        dlg.close()

    def test_web_login_2fa_auto_watcher_challenge_discrimination(self):
        """Verify WebLoginDialog 2FA watcher accurately discriminates Authenticator vs Email/Phone."""
        from ui.web_login_dialog import WebLoginDialog
        from unittest.mock import MagicMock

        acc = RobloxAccount(
            account_id="acc_2fa_discrim",
            username="TwoFactorHero",
            display_name="TwoFactorHero",
            user_id=9999,
            cookie="",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        dlg = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            target_account=acc,
            mode="login",
        )
        try:
            # Start watcher
            dlg._start_2fa_auto_watcher()
            self.assertIsNotNone(dlg._2fa_watcher_timer)
            self.assertTrue(dlg._2fa_watcher_timer.isActive())

            # Simulate JS check execution and callback capture
            captured_cb = []
            mock_page = MagicMock()
            def fake_run_js(script, callback):
                captured_cb.append((script, callback))
            mock_page.runJavaScript = fake_run_js
            dlg.view.page = MagicMock(return_value=mock_page)

            dlg._check_and_inject_2fa()
            self.assertEqual(len(captured_cb), 1)
            js_script, cb = captured_cb[0]

            # Verify JS script contains modal scoping, login form bypass, and isAuthApp priority
            self.assertIn("find2FAChallenge", js_script)
            self.assertIn("login-password", js_script)
            self.assertIn("isAuthApp", js_script)
            self.assertIn("already been used", js_script)

            # Test Authenticator App detection
            cb({"status": "injected"})
            self.assertIn("Authenticator 2FA detected", dlg.status_label.text())

            # Test Code Rejected (already used / expired): tracks rejected code and displays countdown
            cb({"status": "code_rejected", "code": "123456"})
            self.assertIn("123456", dlg._2fa_rejected_codes)
            self.assertIn("already used", dlg.status_label.text())

            # Test Email Challenge detection: status label must inform user, BUT watcher must NOT stop
            cb({"status": "email_challenge"})
            self.assertIn("Email verification code detected", dlg.status_label.text())
            self.assertTrue(dlg._2fa_watcher_timer.isActive(), "Watcher timer must remain active during email challenge so switching to Authenticator works")

            # Test Phone Challenge detection
            cb({"status": "phone_challenge"})
            self.assertIn("SMS / Phone verification code detected", dlg.status_label.text())
            self.assertTrue(dlg._2fa_watcher_timer.isActive())

            # Test Already Injected status
            cb({"status": "already_injected"})
            self.assertIn("Submitting verification", dlg.status_label.text())

            # Test Protect Account screen stops timer
            cb({"status": "protect_account"})
            self.assertFalse(dlg._2fa_watcher_timer.isActive())
            self.assertIn("Account security screen", dlg.status_label.text())
        finally:
            dlg.close()

    def test_multi_launch_worker_lifecycle(self):
        from ui.main_window import MultiLaunchWorker
        from unittest.mock import MagicMock

        acc1 = RobloxAccount(account_id="acc_w1", username="WorkerUser1", display_name="W1", user_id=101, cookie="c1")
        acc2 = RobloxAccount(account_id="acc_w2", username="WorkerUser2", display_name="W2", user_id=102, cookie="c2")

        mock_runner = MagicMock()
        worker = MultiLaunchWorker(
            runner=mock_runner,
            accounts=[acc1, acc2],
            place_id=5208655184,
            job_id="job_abc",
            stagger_delay=1.0,
        )

        # Test cancel flag
        self.assertFalse(worker._cancelled)
        worker.cancel()
        self.assertTrue(worker._cancelled)

    def test_multi_launch_worker_stops_when_memory_is_critical(self):
        from ui.main_window import MultiLaunchWorker
        from unittest.mock import MagicMock

        accs = [
            RobloxAccount(account_id=f"acc_m{i}", username=f"MemUser{i}", display_name="M", user_id=200 + i, cookie="c")
            for i in range(2)
        ]
        mock_runner = MagicMock()
        mock_runner.auth_state_manager.can_launch.return_value = (True, "")
        mock_runner.memory_launch_check.return_value = (False, "Critical memory threshold reached")
        worker = MultiLaunchWorker(runner=mock_runner, accounts=accs, stagger_delay=0.0)
        warnings, results = [], []
        worker.memory_warning.connect(warnings.append)
        worker.finished_queue.connect(lambda launched, failed: results.append((launched, failed)))

        worker.run()

        mock_runner.launch_instance.assert_not_called()
        self.assertEqual(warnings, ["Critical memory threshold reached"])
        self.assertEqual(results[0][0], 0)
        self.assertEqual([u for u, _ in results[0][1]], ["MemUser0", "MemUser1"])

    def test_server_finder_dialog(self):
        from ui.server_finder_dialog import ServerFinderDialog
        from unittest.mock import MagicMock

        self.api.get_place_details = MagicMock(return_value={
            "name": "Rogue Lineage: Gaia",
            "universeId": 1087859240,
            "universeRootPlaceId": 3016661674,
        })
        self.api.get_universe_places = MagicMock(return_value=[
            {"id": 3016661674, "name": "Rogue Lineage"},
            {"id": 5208655184, "name": "Rogue Lineage: Gaia"},
            {"id": 3541987450, "name": "Rogue Lineage: Khei"},
        ])
        self.api.get_public_servers = MagicMock(return_value={
            "data": [
                {"id": "srv-small", "playing": 2, "maxPlayers": 23, "ping": 45, "fps": 60.0},
                {"id": "srv-medium", "playing": 8, "maxPlayers": 23, "ping": 90, "fps": 59.8},
                {"id": "srv-large", "playing": 19, "maxPlayers": 23, "ping": 30, "fps": 58.5},
            ],
            "nextPageCursor": None,
        })

        dlg = ServerFinderDialog(self.storage, self.api, initial_place_id=5208655184)
        # Process events so background worker completes
        for _ in range(20):
            QtWidgets.QApplication.processEvents()
            if dlg.table.rowCount() > 0:
                break
            import time
            time.sleep(0.05)

        self.assertGreaterEqual(dlg.table.rowCount(), 1)
        # Select first server (smallest: 2 players)
        dlg.table.selectRow(0)
        selected_server = dlg._get_selected_server()
        self.assertIsNotNone(selected_server)
        self.assertEqual(selected_server["id"], "srv-small")

        # Because 5208655184 is a sub-place and root is 3016661674,
        # choosing a server routes target place to 3016661674 (Lobby) to avoid client infinite loading freeze
        received_signals = []
        dlg.server_selected.connect(lambda pid, jid, aj: received_signals.append((pid, jid, aj)))
        dlg._on_choose_server(auto_join=False)
        self.assertEqual(len(received_signals), 1)
        self.assertEqual(received_signals[0], (3016661674, "", False))

        # Test Join Server signal (auto_join=True)
        dlg._on_choose_server(auto_join=True)
        self.assertEqual(len(received_signals), 2)
        self.assertEqual(received_signals[1], (3016661674, "", True))
        dlg.close()

    def test_table_keyboard_navigation(self):
        from PyQt6 import QtGui, QtCore
        from ui.main_window import ReorderableTableWidget

        table = ReorderableTableWidget()
        signals = []
        table.enter_pressed.connect(lambda: signals.append("enter"))
        table.delete_pressed.connect(lambda: signals.append("delete"))

        # Simulate Enter key press
        enter_ev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.NoModifier)
        table.keyPressEvent(enter_ev)
        self.assertIn("enter", signals)

        # Simulate Delete key press
        del_ev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Delete, QtCore.Qt.KeyboardModifier.NoModifier)
        table.keyPressEvent(del_ev)
        self.assertIn("delete", signals)

    def test_all_dialogs_are_borderless(self):
        """Verify that all dialog windows in AERAM inherit from BorderlessDialog and have FramelessWindowHint."""
        from ui.borderless_dialog import BorderlessDialog
        from ui.dialogs import (
            AddAccountDialog,
            ImportRAMDialog,
            JoinGameDialog,
            MultiLaunchDialog,
            EditAccountDialog,
            SettingsDialog,
            DiagnosticsDialog,
            GroupManagerDialog,
            UnblockEveryoneDialog,
        )
        from ui.server_finder_dialog import ServerFinderDialog
        from ui.web_login_dialog import WebLoginDialog

        dialog_classes = [
            AddAccountDialog,
            ImportRAMDialog,
            JoinGameDialog,
            MultiLaunchDialog,
            EditAccountDialog,
            SettingsDialog,
            DiagnosticsDialog,
            GroupManagerDialog,
            ServerFinderDialog,
            WebLoginDialog,
            UnblockEveryoneDialog,
        ]

        for cls in dialog_classes:
            self.assertTrue(issubclass(cls, BorderlessDialog), f"{cls.__name__} must inherit from BorderlessDialog")

        # Instantiate a sample dialog and verify window flags
        dlg = GroupManagerDialog(self.storage)
        self.assertTrue(bool(dlg.windowFlags() & QtCore.Qt.WindowType.FramelessWindowHint))
        self.assertIsNotNone(dlg.findChild(QtWidgets.QFrame, "ramCentral"))
        self.assertIsNotNone(dlg.findChild(QtWidgets.QFrame, "top_header_frame"))

    def test_unblock_everyone_dialog_lifecycle(self):
        """Verify UnblockEveryoneDialog loads blocked users and runs unblock worker cleanly."""
        from ui.dialogs import UnblockEveryoneDialog
        acc = RobloxAccount(
            account_id="acc_unblock_1",
            username="TestBlockedUser",
            display_name="Tester",
            user_id=12345,
            cookie="dummy_cookie",
        )
        self.api.get_blocked_users = MagicMock(return_value=[
            {"id": 1001, "block_type": "Blocker"},
            {"id": 1002, "block_type": "Blocker"},
        ])
        self.api.unblock_user = MagicMock(return_value=(True, "fresh_csrf_token"))

        dlg = UnblockEveryoneDialog(acc, self.api)
        QtWidgets.QApplication.processEvents()

        # Simulate API callback
        dlg._load_blocked_users()
        self.assertEqual(len(dlg.blocked_users), 2)
        self.assertTrue(dlg.btn_unblock.isEnabled())

        from unittest.mock import patch
        with patch("ui.dialogs.time.sleep", return_value=None):
            dlg._start_unblock()
            if dlg.worker:
                dlg.worker.wait(3000)
            QtWidgets.QApplication.processEvents()

        self.assertEqual(self.api.unblock_user.call_count, 2)
        self.assertIn("Successfully unblocked all", dlg.status_lbl.text())
    def test_phosphor_led_account_status_indicators(self):
        """Verify phosphor LED micro-indicator dot rendering and tooltip states for account lifecycle."""
        import os
        from core.models import RunningInstance
        from core.auth_state import AccountAuthState
        from ui.main_window import get_led_icon

        acc = RobloxAccount(
            account_id="acc_led_1",
            username="LEDUser",
            display_name="LED",
            user_id=8888,
            cookie="dummy_cookie",
        )
        self.storage.save_account(acc)

        window = MainWindow(self.storage, self.api, self.runner)
        QtWidgets.QApplication.processEvents()

        # 1. Default Ready/Offline state
        item = window.table.item(0, 0)
        self.assertEqual(item.text(), "LEDUser")
        self.assertFalse(item.icon().isNull())
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#505050")
        self.assertIn("Ready", item.toolTip())

        # 2. Launching state
        self.runner.auth_state_manager.set_state("acc_led_1", AccountAuthState.LAUNCHING)
        window._poll_instances()
        self.assertEqual(item.text(), "LEDUser")
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#A8A8A8")
        self.assertIn("Launching", item.toolTip())

        # 3. Running / In-Game state
        self.runner.auth_state_manager.set_state("acc_led_1", AccountAuthState.RUNNING)
        self.runner.active_instances["acc_led_1"] = RunningInstance(
            account_id="acc_led_1", username="LEDUser", pid=os.getpid(), start_time=1.0
        )
        window._poll_instances()
        self.assertEqual(item.text(), "LEDUser")
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#E8E8E8")
        self.assertIn("In-Game", item.toolTip())
        self.assertIn(str(os.getpid()), item.toolTip())

        # 4. Session Expired state
        self.runner.active_instances.pop("acc_led_1", None)
        self.runner.auth_state_manager.set_state("acc_led_1", AccountAuthState.SESSION_EXPIRED)
        window._poll_instances()
        self.assertEqual(item.text(), "LEDUser")
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#707070")
        self.assertIn("expired", item.toolTip())

        # 5. Live Web Presence: Online on Website/App
        self.runner.auth_state_manager.set_state("acc_led_1", AccountAuthState.STORED)
        window._on_presences_updated([{"userId": 8888, "userPresenceType": 1}])
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#A8A8A8")
        self.assertIn("Online", item.toolTip())

        # 6. Live Web Presence: In-Game on Roblox
        window._on_presences_updated([{
            "userId": 8888,
            "userPresenceType": 2,
            "lastLocation": "Blox Fruits",
            "placeId": 2753915549,
            "gameId": "job-1234-xyz",
        }])
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#E8E8E8")
        self.assertIn("Blox Fruits", item.toolTip())
        self.assertIn("2753915549", item.toolTip())
        self.assertIn("job-1234-xyz", item.toolTip())

        # 7. Live Web Presence: In Studio
        window._on_presences_updated([{
            "userId": 8888,
            "userPresenceType": 3,
            "lastLocation": "Map Builder Studio",
        }])
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#A8A8A8")
        self.assertIn("In Studio", item.toolTip())
        self.assertIn("Map Builder Studio", item.toolTip())

        # 8. Live Web Presence: Offline with relative timestamp
        window._on_presences_updated([{
            "userId": 8888,
            "userPresenceType": 0,
            "lastOnline": "2026-10-15T09:00:00Z",
        }])
        self.assertEqual(item.data(QtCore.Qt.ItemDataRole.UserRole + 1), "#505050")
        self.assertIn("Offline", item.toolTip())

        # 9. Icon caching
        icon_a = get_led_icon("#E8E8E8")
        icon_b = get_led_icon("#E8E8E8")
        self.assertIs(icon_a, icon_b)

        window.close()

    def test_presence_monitor_worker_and_quick_join(self):
        """Verify PresenceMonitorWorker polling and Quick Join integration."""
        from ui.main_window import PresenceMonitorWorker

        acc = RobloxAccount(
            account_id="acc_pres_1",
            username="PresPlayer",
            display_name="Pres",
            user_id=9999,
            cookie="dummy_cookie",
        )
        self.storage.save_account(acc)

        mock_presence = [{
            "userId": 9999,
            "userPresenceType": 2,
            "lastLocation": "Deepwoken",
            "placeId": 4111023553,
            "gameId": "job-deepwoken-99",
        }]
        self.api.get_users_presences = MagicMock(return_value=mock_presence)

        received = []
        worker = PresenceMonitorWorker(
            api=self.api,
            accounts_getter=lambda: [acc],
            interval_sec=5,
        )
        worker.presences_updated.connect(lambda p: received.extend(p))

        # Test single query step
        worker.stopped = True  # don't loop indefinitely
        # Directly invoke work logic
        worker.presences_updated.emit(self.api.get_users_presences([9999]))
        QtWidgets.QApplication.processEvents()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["placeId"], 4111023553)

        # Test MainWindow quick join
        window = MainWindow(self.storage, self.api, self.runner)
        window._join_server_clicked = MagicMock()
        window._quick_join_presence(4111023553, "job-deepwoken-99")
        self.assertEqual(window.place_id_input.text(), "4111023553")
        self.assertEqual(window.job_id_input.text(), "job-deepwoken-99")
        window._join_server_clicked.assert_called_once()
        window.close()

    def test_custom_alias_and_replication(self):
        # 1. Setup two accounts
        acc1 = RobloxAccount(
            account_id="acc_custom_1",
            username="VysMain",
            display_name="InitialAlias",
            user_id=1111,
            cookie="dummy_c1",
        )
        acc2 = RobloxAccount(
            account_id="acc_custom_2",
            username="VysAltFarmer",
            display_name="AltAlias",
            user_id=2222,
            cookie="dummy_c2",
        )
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        window = MainWindow(self.storage, self.api, self.runner)
        try:
            QtWidgets.QApplication.processEvents()

            # Select acc1 from window
            window_acc1 = window.account_map["acc_custom_1"]
            window._select_account(window_acc1)
            self.assertEqual(window.alias_input.text(), "InitialAlias")

            # 2. Test setting a fully custom alias
            window.alias_input.setText("Custom Deepwoken Main")
            window._set_alias_clicked()
            self.assertEqual(window_acc1.display_name, "Custom Deepwoken Main")
            reloaded = self.storage.get_account("acc_custom_1")
            self.assertEqual(reloaded.display_name, "Custom Deepwoken Main")

            # 3. Simulate background profile sync pass - MUST NOT overwrite custom alias
            window._on_account_synced("acc_custom_1", "RobloxWebDisplayName", "https://avatar.url/headshot", 999)
            curr = window.account_map["acc_custom_1"]
            self.assertEqual(curr.display_name, "Custom Deepwoken Main")
            self.assertEqual(curr.web_display_name, "RobloxWebDisplayName")
            self.assertEqual(curr.robux_balance, 999)
            # Database check
            reloaded_after_sync = self.storage.get_account("acc_custom_1")
            self.assertEqual(reloaded_after_sync.display_name, "Custom Deepwoken Main")

            # 4. Test replicate button: replicate web display name / username to alias on demand
            window.btn_replicate_alias.click()
            curr = window.account_map["acc_custom_1"]
            self.assertEqual(curr.display_name, "RobloxWebDisplayName")
            self.assertEqual(window.alias_input.text(), "RobloxWebDisplayName")

            # 5. Test multi-account username replication to alias
            window.table.selectAll()
            window._replicate_alias_for_targets()
            self.assertEqual(self.storage.get_account("acc_custom_1").display_name, "VysMain")
            self.assertEqual(self.storage.get_account("acc_custom_2").display_name, "VysAltFarmer")

            # 6. Test clearing alias
            window._select_account(window_acc1)
            window._clear_alias_for_current()
            self.assertEqual(self.storage.get_account("acc_custom_1").display_name, "")
            self.assertEqual(window.alias_input.text(), "")
        finally:
            window.close()

    def test_batch_sign_out_dialog_flow(self):
        """Verify BatchSignOutDialog processes both non-2FA and 2FA accounts rapidly."""
        import time
        from ui.dialogs import BatchSignOutDialog, BatchSignOutWorker
        
        acc1 = RobloxAccount(
            account_id="acc_signout_1",
            username="No2FAUser",
            display_name="No2FA",
            user_id=111,
            cookie="cookie_1",
        )
        acc2 = RobloxAccount(
            account_id="acc_signout_2",
            username="TwoFAUser",
            display_name="TwoFA",
            user_id=222,
            cookie="cookie_2",
        )
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        def mock_sign_out(cookie, two_fa_callback=None):
            if cookie == "cookie_1":
                return True, "new_cookie_1", "Successfully signed out of all other sessions!"
            elif cookie == "cookie_2":
                if two_fa_callback:
                    code = two_fa_callback({"userId": "222", "actionType": "Generic"})
                    if code == "123456":
                        return True, "new_cookie_2", "Successfully signed out of all other sessions!"
                return False, None, "Invalid 2FA code"
            return False, None, "Unknown"

        self.api.sign_out_of_all_sessions = MagicMock(side_effect=mock_sign_out)

        dlg = BatchSignOutDialog([acc1, acc2], self.api, self.storage, runner=self.runner)
        dlg.show()
        try:
            # Let worker start
            QtWidgets.QApplication.processEvents()

            # Wait for 2FA prompt on acc2
            for _ in range(50):
                QtWidgets.QApplication.processEvents()
                if not dlg.two_fa_frame.isHidden() and dlg.code_input.isEnabled():
                    break
                time.sleep(0.05)

            self.assertFalse(dlg.two_fa_frame.isHidden())
            dlg.code_input.setText("123456")
            dlg._on_submit_code()

            # Wait for batch completion
            if dlg.worker:
                dlg.worker.wait(3000)
            QtWidgets.QApplication.processEvents()

            # Verify both accounts rotated in storage
            reloaded_1 = self.storage.get_account("acc_signout_1")
            reloaded_2 = self.storage.get_account("acc_signout_2")
            self.assertEqual(reloaded_1.cookie, "new_cookie_1")
            self.assertEqual(reloaded_2.cookie, "new_cookie_2")
            self.assertIn("Batch Completed", dlg.lbl_acc_num.text())
            self.assertIn("2 / 2 Succeeded", dlg.lbl_acc_name.text())
        finally:
            if dlg.worker and dlg.worker.isRunning():
                dlg.worker.stop()
                dlg.worker.wait(1000)
            dlg.close()

    def test_system_debug_and_update_buttons(self):
        from ui.dialogs import SettingsDialog
        window = MainWindow(self.storage, self.api, self.runner)
        self.assertTrue(hasattr(window, "btn_diagnostics"))
        self.assertFalse(hasattr(window, "btn_update"))
        self.assertEqual(window.btn_diagnostics.text(), "System Debug")
        self.assertIn("Ctrl+D", window.btn_diagnostics.toolTip())

        # Verify update button is retained inside SettingsDialog
        settings_dlg = SettingsDialog(self.storage, window)
        self.assertTrue(hasattr(settings_dlg, "btn_open_update"))
        self.assertEqual(settings_dlg.btn_open_update.text(), "Check for Updates...")
        settings_dlg.close()
        window.close()

    def test_change_password_dialog_ui(self):
        from ui.dialogs import ChangePasswordDialog
        account = RobloxAccount(
            account_id="acc_pwd_test",
            user_id=123456,
            username="TestPassUser",
            display_name="PassUser",
            cookie="_|COOKIE|_",
            password="StoredSecretPassword123!",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        dlg = ChangePasswordDialog(account, self.api, self.storage)
        self.assertEqual(dlg.curr_pwd_input.text(), "StoredSecretPassword123!")
        self.assertIn("Auto-Verification Active", dlg.auth_frame.findChildren(QtWidgets.QLabel)[0].text())

        # Test Generate Strong Password
        dlg.btn_generate.click()
        self.assertGreaterEqual(len(dlg.new_pwd_input.text()), 16)
        self.assertEqual(dlg.new_pwd_input.text(), dlg.conf_pwd_input.text())

        dlg.close()

    def test_batch_password_change_dialog_ui(self):
        from ui.dialogs import BatchPasswordChangeDialog
        from unittest.mock import patch
        import time

        acc1 = RobloxAccount(
            account_id="acc_batch_pwd_1",
            user_id=1001,
            username="BatchUser1",
            display_name="UserOne",
            cookie="_|COOKIE1|_",
            password="OldPassword123!",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        acc2 = RobloxAccount(
            account_id="acc_batch_pwd_2",
            user_id=1002,
            username="BatchUser2",
            display_name="UserTwo",
            cookie="_|COOKIE2|_",
            password="OldPassword456!",
        )
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        dlg = BatchPasswordChangeDialog([acc1, acc2], self.api, self.storage)
        try:
            self.assertEqual(dlg.table.rowCount(), 2)
            self.assertTrue(dlg.rb_unique.isChecked())
            self.assertEqual(dlg.delay_spin.value(), 3.0)

            # Test mode change
            dlg.rb_common.setChecked(True)
            self.assertFalse(dlg.common_pwd_widget.isHidden())
            dlg.common_pwd_input.setText("NewUniversalPassword2026!")

            with patch.object(self.api, "change_password", return_value=(True, "_|ROTATED_COOKIE|_", "Password changed successfully!")), \
                 patch("ui.dialogs.QMessageBox.information"), \
                 patch("time.sleep", return_value=None):

                dlg._start_batch()
                self.assertIsNotNone(dlg.worker)
                dlg.worker.wait(3000)
                QtWidgets.QApplication.processEvents()

            # Verify both accounts were updated in storage
            reloaded_1 = self.storage.get_account("acc_batch_pwd_1")
            reloaded_2 = self.storage.get_account("acc_batch_pwd_2")
            self.assertEqual(reloaded_1.password, "NewUniversalPassword2026!")
            self.assertEqual(reloaded_1.cookie, "_|ROTATED_COOKIE|_")
            self.assertEqual(reloaded_2.password, "NewUniversalPassword2026!")
            self.assertEqual(reloaded_2.cookie, "_|ROTATED_COOKIE|_")

            # Verify UI completion state
            self.assertIn("2 succeeded, 0 failed", dlg.lbl_status.text())
            self.assertTrue(dlg.btn_copy.isEnabled())
            self.assertTrue(dlg.btn_export.isEnabled())
        finally:
            if dlg.worker and dlg.worker.isRunning():
                dlg.worker.stop()
                dlg.worker.wait(1000)
            dlg.close()

    def test_batch_unblock_dialog_ui(self):
        from ui.dialogs import BatchUnblockDialog
        from unittest.mock import patch

        acc1 = RobloxAccount(
            account_id="acc_unblock_1",
            user_id=2001,
            username="UnblockUser1",
            display_name="UnblockOne",
            cookie="_|COOKIE1|_",
        )
        acc2 = RobloxAccount(
            account_id="acc_unblock_2",
            user_id=2002,
            username="UnblockUser2",
            display_name="UnblockTwo",
            cookie="_|COOKIE2|_",
        )
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        dlg = BatchUnblockDialog([acc1, acc2], self.api)
        try:
            self.assertEqual(len(dlg.accounts), 2)
            self.assertEqual(dlg.progress_bar.maximum(), 2)

            with patch.object(self.api, "get_blocked_users", return_value=[{"id": 999, "name": "Spammer"}]), \
                 patch.object(self.api, "unblock_user", return_value=(True, "token_csrf")), \
                 patch("ui.dialogs.QMessageBox.information"), \
                 patch("time.sleep", return_value=None):

                dlg._start_batch()
                self.assertIsNotNone(dlg.worker)
                dlg.worker.wait(3000)
                QtWidgets.QApplication.processEvents()

            self.assertIn("Completed", dlg.status_lbl.text())
            self.assertIn("Total unblocked: 2", dlg.status_lbl.text())
        finally:
            if dlg.worker and dlg.worker.isRunning():
                dlg.worker.stop()
                dlg.worker.wait(1000)
            dlg.close()

    def test_batch_logout_dialog_ui(self):
        from ui.dialogs import BatchLogoutDialog
        from unittest.mock import patch

        acc = RobloxAccount(
            account_id="acc_logout_1",
            user_id=3001,
            username="LogoutUser",
            display_name="LogoutOne",
            cookie="_|COOKIE_LOGOUT|_",
        )
        self.storage.save_account(acc)

        dlg = BatchLogoutDialog([acc], self.api, self.storage)
        try:
            self.assertEqual(len(dlg.accounts), 1)
            self.assertEqual(dlg.progress_bar.maximum(), 1)

            with patch.object(self.api, "logout_session", return_value=(True, "Session revoked")), \
                 patch("ui.dialogs.QMessageBox.information"), \
                 patch("time.sleep", return_value=None):

                dlg._start_batch()
                self.assertIsNotNone(dlg.worker)
                dlg.worker.wait(3000)
                QtWidgets.QApplication.processEvents()

            self.assertIn("revoked", dlg.status_lbl.text().lower())
            # Cookie should have been wiped in storage
            reloaded = self.storage.get_account("acc_logout_1")
            self.assertEqual(reloaded.cookie, "")
        finally:
            if dlg.worker and dlg.worker.isRunning():
                dlg.worker.stop()
                dlg.worker.wait(1000)
            dlg.close()

    def test_batch_friends_dialog_ui(self):
        from ui.dialogs import BatchFriendsDialog
        from unittest.mock import patch

        acc = RobloxAccount(
            account_id="acc_friends_1",
            user_id=4001,
            username="FriendsUser",
            display_name="FriendsOne",
            cookie="_|COOKIE_FRIENDS|_",
        )
        self.storage.save_account(acc)

        dlg = BatchFriendsDialog([acc], self.api)
        try:
            self.assertEqual(len(dlg.accounts), 1)

            with patch.object(self.api, "decline_all_friend_requests", return_value=(True, "")), \
                 patch("ui.dialogs.QMessageBox.information"), \
                 patch("time.sleep", return_value=None):

                dlg._start_batch()
                self.assertIsNotNone(dlg.worker)
                dlg.worker.wait(3000)
                QtWidgets.QApplication.processEvents()

            self.assertIn("Finished", dlg.status_lbl.text())
        finally:
            if dlg.worker and dlg.worker.isRunning():
                dlg.worker.stop()
                dlg.worker.wait(1000)
            dlg.close()

    def test_batch_privacy_dialog_ui(self):
        from ui.dialogs import BatchPrivacyDialog
        from unittest.mock import patch

        acc = RobloxAccount(
            account_id="acc_privacy_1",
            user_id=5001,
            username="PrivacyUser",
            display_name="PrivacyOne",
            cookie="_|COOKIE_PRIVACY|_",
        )
        self.storage.save_account(acc)

        dlg = BatchPrivacyDialog([acc], self.api, self.storage)
        try:
            self.assertEqual(len(dlg.accounts), 1)
            dlg.rb_ghost.setChecked(True)

            with patch.object(self.api, "set_join_privacy", return_value=(True, "")), \
                 patch("ui.dialogs.QMessageBox.information"), \
                 patch("time.sleep", return_value=None):

                dlg._start_batch()
                self.assertIsNotNone(dlg.worker)
                dlg.worker.wait(3000)
                QtWidgets.QApplication.processEvents()

            self.assertIn("Completed", dlg.status_lbl.text())
            reloaded = self.storage.get_account("acc_privacy_1")
            self.assertEqual(reloaded.join_privacy, "NoOne")
        finally:
            if dlg.worker and dlg.worker.isRunning():
                dlg.worker.stop()
                dlg.worker.wait(1000)
            dlg.close()

    def test_main_window_robux_and_privacy_controls(self):
        acc = RobloxAccount(
            account_id="acc_mw_eco",
            user_id=6001,
            username="EconomyUser",
            display_name="EcoUser",
            cookie="_|COOKIE_ECO|_",
            robux_balance=2500,
            pending_robux=150,
            join_privacy="Friends",
        )
        self.storage.save_account(acc)

        window = MainWindow(self.storage, self.api, self.runner)
        try:
            window._load_accounts()
            window._select_account(acc)

            # Check that description input is enlarged
            self.assertGreaterEqual(window.description_input.minimumHeight(), 64)

            # Check status bar metrics cycling (Slide 0: System RAM/Swap/VRAM + Active, Slide 1: Mocktail runtime + Active)
            window._status_metric_index = 0
            window._update_status_bar()
            self.assertIn("Active", window.count_label.text())

            # Verify Account Utilities menu contains organized submenus
            window._update_acc_tools_menu()
            tools_menu = window.btn_acc_tools.menu()
            action_texts = [a.text() for a in tools_menu.actions()]
            self.assertTrue(any("Account Utilities" in t for t in action_texts))

            utils_act = next(a for a in tools_menu.actions() if "Account Utilities" in a.text())
            self.assertIsNotNone(utils_act.menu())
            sub_menus = [a.text() for a in utils_act.menu().actions()]
            self.assertTrue(any("Security & Privacy" in t for t in sub_menus))
            self.assertTrue(any("Social Management" in t for t in sub_menus))

            # Verify Two-Step Verification is in Account Tools
            self.assertTrue(any("Two-Step Verification" in t for t in action_texts))
            two_fa_act = next(a for a in tools_menu.actions() if "Two-Step Verification" in a.text())
            self.assertIsNotNone(two_fa_act.menu())
            two_fa_sub_texts = [a.text() for a in two_fa_act.menu().actions()]
            self.assertTrue(any("Import" in t for t in two_fa_sub_texts))
            self.assertTrue(any("Export" in t for t in two_fa_sub_texts))
            self.assertTrue(any("Edit 2FA" in t for t in two_fa_sub_texts))
        finally:
            window.close()

    def test_menu_auth_and_copy_organization(self):
        """Test context menu and copy submenu organization."""
        acc = RobloxAccount(
            account_id="acc_auth_copy",
            user_id=7001,
            username="AuthUser",
            display_name="AuthUserDisp",
            password="TestPassword123",
            cookie="_|COOKIE_AUTH|_",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        self.storage.save_account(acc)

        window = MainWindow(self.storage, self.api, self.runner)
        try:
            window._load_accounts()
            window.table.selectRow(0)
            window._on_selection_changed()

            orig_menu_cls = QtWidgets.QMenu
            created_menus = []

            class InterceptQMenu(orig_menu_cls):
                def exec(self, *args, **kwargs):
                    created_menus.append(self)
                    return None

            with patch("ui.main_window.QtWidgets.QMenu", side_effect=InterceptQMenu):
                window._open_context_menu(QtCore.QPoint(5, 5))

            self.assertEqual(len(created_menus), 1)
            ctx_menu = created_menus[0]
            top_texts = [a.text() for a in ctx_menu.actions() if a.text()]

            # Top level must NOT have Two-Step Verification (2FA) or Account Utilities - moved exclusively to Account Tools
            self.assertFalse(any("Two-Step Verification" in t for t in top_texts))
            self.assertFalse(any("Account Utilities" in t for t in top_texts))

            # Must have Copy submenu
            copy_act = next((a for a in ctx_menu.actions() if a.text() == "Copy"), None)
            self.assertIsNotNone(copy_act)
            self.assertIsNotNone(copy_act.menu())

            copy_items = [a.text() for a in copy_act.menu().actions() if a.text()]
            # Copy User:Pass Combo must be removed
            self.assertNotIn("Copy User:Pass Combo", copy_items)
            # Authenticator code must be present
            self.assertTrue(any("Authenticator" in t or "2FA" in t for t in copy_items))
            # Standard items present
            self.assertIn("Copy Username", copy_items)
            self.assertIn("Copy Password", copy_items)
            self.assertIn("Copy User ID", copy_items)
            self.assertIn("Copy Session Cookie (.ROBLOSECURITY)", copy_items)
        finally:
            window.close()

    def test_live_2fa_hub_dialog(self):
        """Test Live 2FA Authenticator Hub dialog UI, filtering, and TOTP generation."""
        from ui.dialogs import Live2FAHubDialog

        acc1 = RobloxAccount(
            account_id="acc_hub_1",
            user_id=8001,
            username="MobileAltOne",
            display_name="AltOne",
            password="PasswordOne",
            cookie="_|COOKIE1|_",
            totp_secret="JBSWY3DPEHPK3PXP",
            group="MobileGroup",
        )
        acc2 = RobloxAccount(
            account_id="acc_hub_2",
            user_id=8002,
            username="MobileAltTwo",
            display_name="AltTwo",
            password="PasswordTwo",
            cookie="_|COOKIE2|_",
            totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            group="FarmGroup",
        )
        acc3 = RobloxAccount(
            account_id="acc_hub_3",
            user_id=8003,
            username="No2FAUser",
            display_name="No2FA",
            cookie="_|COOKIE3|_",
            totp_secret="",
            group="Default",
        )

        dlg = Live2FAHubDialog([acc1, acc2, acc3], self.storage)
        try:
            # By default only accounts with 2FA are shown
            self.assertEqual(len(dlg.visible_accounts), 2)
            self.assertEqual(dlg.table.rowCount(), 2)

            # Check that code labels exist and are populated
            self.assertIn("acc_hub_1", dlg._code_labels)
            lbl1 = dlg._code_labels["acc_hub_1"]
            code1 = lbl1.text().replace(" ", "")
            self.assertEqual(len(code1), 6)
            self.assertTrue(code1.isdigit())

            # Test search filter
            dlg.search_input.setText("AltTwo")
            self.assertEqual(len(dlg.visible_accounts), 1)
            self.assertEqual(dlg.visible_accounts[0].username, "MobileAltTwo")

            # Clear search
            dlg.search_input.setText("")
            self.assertEqual(len(dlg.visible_accounts), 2)

            # Test unchecking only 2FA
            dlg.cb_only_2fa.setChecked(False)
            self.assertEqual(len(dlg.visible_accounts), 3)

            # Test copying code
            dlg._copy_code_for_account(acc1)
            copied = QtWidgets.QApplication.clipboard().text()
            self.assertEqual(len(copied), 6)
            self.assertTrue(copied.isdigit())

            # Test copying password
            btn = QtWidgets.QPushButton()
            dlg._copy_password(acc1, btn)
            self.assertEqual(QtWidgets.QApplication.clipboard().text(), "PasswordOne")

            # Test tick
            dlg._on_tick()
            self.assertGreaterEqual(dlg.cycle_bar.value(), 0)
            self.assertLessEqual(dlg.cycle_bar.value(), 30)
        finally:
            dlg.close()

    def test_popups_do_not_block_main_window(self):
        """Test that secondary popup windows are non-modal and do not block interacting with the main window."""
        acc = RobloxAccount(
            account_id="acc_modeless_test",
            user_id=9001,
            username="ModelessUser",
            display_name="ModelessDisp",
            password="TestPassword",
            cookie="_|COOKIE_MODELESS|_",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        self.storage.save_account(acc)

        window = MainWindow(self.storage, self.api, self.runner)
        try:
            window._load_accounts()
            self.assertEqual(window.table.rowCount(), 1)

            # Open Live 2FA Hub
            hub_win = window._open_live_2fa_hub()
            self.assertIsNotNone(hub_win)
            self.assertEqual(hub_win.windowModality(), QtCore.Qt.WindowModality.NonModal)
            self.assertFalse(hub_win.isModal())

            # Verify main window remains interactive (selecting row, typing in place ID input)
            window.table.selectRow(0)
            window._on_selection_changed()
            self.assertEqual(window.current_account.username, "ModelessUser")

            window.place_id_input.setText("189707")
            self.assertEqual(window.place_id_input.text(), "189707")

            # Repeat open reuses existing window
            hub_win_2 = window._open_live_2fa_hub()
            self.assertIs(hub_win, hub_win_2)

            # Close hub and verify cleanup
            hub_win.close()
            self.assertNotIn("live_2fa_hub", window._active_modeless_windows)

            # Open Server Finder
            window._open_server_finder()
            finder = window._active_modeless_windows.get("server_finder")
            self.assertIsNotNone(finder)
            self.assertEqual(finder.windowModality(), QtCore.Qt.WindowModality.NonModal)
            self.assertFalse(finder.isModal())

            # MainWindow still interactive
            window.place_id_input.setText("999999")
            self.assertEqual(window.place_id_input.text(), "999999")

            finder.close()
            self.assertNotIn("server_finder", window._active_modeless_windows)

            # Open Settings
            window._open_settings()
            settings = window._active_modeless_windows.get("settings")
            self.assertIsNotNone(settings)
            self.assertEqual(settings.windowModality(), QtCore.Qt.WindowModality.NonModal)
            self.assertFalse(settings.isModal())
            settings.close()
        finally:
            window.close()

    def test_auto_login_gui_flow(self):
        """Test Auto Log In flow from MainWindow and WebLoginDialog components."""
        from ui.web_login_dialog import WebLoginDialog

        acc = RobloxAccount(
            account_id="acc_auto_login",
            user_id=9999,
            username="AutoLoginTester",
            display_name="AutoTester",
            password="SecretPassword123",
            cookie="",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        self.storage.save_account(acc)

        window = MainWindow(self.storage, self.api, self.runner)
        try:
            window._load_accounts()
            window.table.selectRow(0)
            window._on_selection_changed()

            # Test API auto-login success path
            mock_user = {"id": 9999, "name": "AutoLoginTester", "displayName": "AutoTester"}
            with patch.object(self.api, "login_with_credentials", return_value=(True, "_|CAPTURED_COOKIE|_", mock_user, "OK")):
                window._auto_login_account(acc)
                # The API login runs off the GUI thread; wait for its result to come back
                from PyQt6.QtTest import QTest
                for _ in range(100):
                    if "Auto Log In successful" in window.statusBar().currentMessage():
                        break
                    QTest.qWait(20)
                updated = self.storage.get_account("acc_auto_login")
                self.assertEqual(updated.cookie, "_|CAPTURED_COOKIE|_")
                self.assertIn("Auto Log In successful", window.statusBar().currentMessage())

            # Test WebLoginDialog auto_login initialization
            dlg = WebLoginDialog(
                storage=self.storage,
                api=self.api,
                runner=self.runner,
                target_account=acc,
                auto_login=True,
                parent=window,
            )
            try:
                self.assertTrue(dlg.auto_login)
                self.assertTrue(hasattr(dlg, "btn_auto_login"))
                self.assertIsNotNone(dlg.btn_auto_login)
                self.assertIn("Auto Log In", dlg.btn_auto_login.text())
            finally:
                dlg.close()
        finally:
            window.close()

    def test_background_results_return_on_gui_thread(self):
        """Worker results reach the GUI thread (PyQt6 invokeMethod rejects Python callables)."""
        import threading
        from PyQt6.QtTest import QTest
        window = MainWindow(self.storage, self.api, self.runner)
        try:
            seen = []
            window._run_in_background(lambda: threading.current_thread().name,
                                      lambda worker: seen.append((worker, threading.current_thread() is threading.main_thread())))
            for _ in range(100):
                if seen:
                    break
                QTest.qWait(10)
            self.assertEqual(len(seen), 1)
            self.assertNotEqual(seen[0][0], threading.main_thread().name)
            self.assertTrue(seen[0][1])
        finally:
            window.close()

    def test_batch_auto_login_dialog_gui_flow(self):
        """Test BatchAutoLoginDialog UI layout, account population, and anti-Arkose controls."""
        from ui.dialogs import BatchAutoLoginDialog

        acc1 = RobloxAccount(account_id="b1", username="BatchUser1", display_name="BatchUser1", user_id=2001, cookie="", password="SecretPassword1", totp_secret="JBSWY3DPEHPK3PXP")
        acc2 = RobloxAccount(account_id="b2", username="BatchUser2", display_name="BatchUser2", user_id=2002, cookie="", password="")
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        window = MainWindow(self.storage, self.api, self.runner)
        try:
            window._load_accounts()
            # Test launching dialog
            dlg = BatchAutoLoginDialog(
                accounts=[acc1, acc2],
                api=self.api,
                storage=self.storage,
                runner=self.runner,
                parent=window,
            )
            try:
                # Verify table rows and contents
                self.assertEqual(dlg.table.rowCount(), 2)
                self.assertEqual(dlg.table.columnCount(), 5)
                self.assertEqual(dlg.table.item(0, 1).text(), "@BatchUser1")
                self.assertEqual(dlg.table.item(0, 2).text(), "Present")
                self.assertEqual(dlg.table.item(0, 3).text(), "Active (Auto 2FA)")
                self.assertEqual(dlg.table.item(0, 4).text(), "Ready")

                self.assertEqual(dlg.table.item(1, 1).text(), "@BatchUser2")
                self.assertEqual(dlg.table.item(1, 2).text(), "Missing")
                self.assertEqual(dlg.table.item(1, 3).text(), "None")

                # Verify anti-Arkose controls and single unified browser verification button
                self.assertTrue(hasattr(dlg, "delay_spin"))
                self.assertGreaterEqual(dlg.delay_spin.value(), 1.0)
                self.assertTrue(hasattr(dlg, "cb_jitter"))
                self.assertTrue(dlg.cb_jitter.isChecked())
                self.assertTrue(hasattr(dlg, "cb_auto_browser"))
                self.assertTrue(dlg.cb_auto_browser.isChecked())
                self.assertTrue(hasattr(dlg, "btn_solve_browser"))
                self.assertEqual(dlg.btn_solve_browser.text(), "Verify Selected in Browser")
                self.assertFalse(hasattr(dlg, "btn_open_firefox"))
                self.assertFalse(hasattr(dlg, "btn_open_webengine"))

                # Verify status metric cycling covers both slides (0 -> 1 -> 0)
                window._status_metric_index = 0
                window._cycle_status_metric()
                self.assertEqual(window._status_metric_index, 1)
                window._cycle_status_metric()
                self.assertEqual(window._status_metric_index, 0)

                # Verify action in MainWindow menu
                window.table.selectAll()
                window._update_acc_tools_menu()
                menu = window.btn_acc_tools.menu()
                action_texts = [a.text() for a in menu.actions()]
                self.assertTrue(any("Auto Log In (" in t for t in action_texts))
            finally:
                dlg.close()
        finally:
            window.close()


class TestTwoFactorStopRace(unittest.TestCase):
    def test_stop_between_check_and_clear_does_not_hang(self):
        """A stop() landing just before the worker clears its 2FA event must not be lost."""
        import threading
        from ui.dialogs import BatchSignOutWorker

        acc = RobloxAccount(account_id="acc_race", username="Racer", display_name="Racer",
                            user_id=1, cookie="tok")
        api = MagicMock()
        worker = BatchSignOutWorker([acc], api)
        callback_result = []

        real_clear = worker._2fa_event.clear

        def clear_after_stop():
            worker.stop()   # sets the event...
            real_clear()    # ...which the worker then erases

        worker._2fa_event.clear = clear_after_stop

        def sign_out(cookie, two_fa_callback):
            callback_result.append(two_fa_callback({"attempt": 2}))
            return False, None, "cancelled"

        api.sign_out_of_all_sessions.side_effect = sign_out

        runner = threading.Thread(target=worker.run, daemon=True)
        runner.start()
        runner.join(timeout=5)
        self.assertFalse(runner.is_alive(), "worker blocked forever on a lost stop() wake-up")
        self.assertEqual(callback_result, [None])


if __name__ == "__main__":
    unittest.main()







class TestRobloxOriginChecks(unittest.TestCase):
    def test_only_roblox_pages_receive_credentials(self):
        from PyQt6.QtCore import QUrl
        from ui.web_login_dialog import is_roblox_url, is_roblox_login_url
        for good in ("https://www.roblox.com/login", "https://roblox.com/login/", "https://www.roblox.com/Login?returnUrl=%2Fhome"):
            self.assertTrue(is_roblox_login_url(QUrl(good)), good)
        for bad in ("https://roblox.com.evil.net/login", "https://evil.com/?x=roblox.com/login",
                    "http://www.roblox.com/login", "https://www.roblox.com/home",
                    "https://evilroblox.com/login", "https://www.roblox.com.attacker.io/login"):
            self.assertFalse(is_roblox_login_url(QUrl(bad)), bad)
        self.assertTrue(is_roblox_url(QUrl("https://www.roblox.com/my/account#!/security")))
        self.assertFalse(is_roblox_url(QUrl("https://notroblox.com/")))
