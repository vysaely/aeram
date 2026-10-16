"""
Unit tests for AERAM RFC 6238 TOTP generation, Google Authenticator migration decoding,
and automated Two-Step Verification workflows.
"""

import base64
import unittest
from unittest.mock import MagicMock, patch
from core.models import RobloxAccount
from core.storage import StorageManager
from core.totp import (
    generate_totp,
    get_totp_remaining_seconds,
    parse_otpauth_migration,
    parse_otpauth_uri,
    parse_universal_2fa_input,
)
from ui.dialogs import GoogleAuthImportDialog, BatchSignOutWorker


class TestTOTPEngine(unittest.TestCase):
    def test_rfc6238_test_vectors(self):
        """Verify standard RFC 6238 HMAC-SHA1 vectors."""
        # Secret "12345678901234567890" -> Base32 "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        # At Unix epoch 59 seconds: counter = 1, expected code = 287082
        self.assertEqual(generate_totp(secret, for_time=59), "287082")
        # At Unix epoch 1111111109 seconds: counter = 37037036, expected code = 081804
        self.assertEqual(generate_totp(secret, for_time=1111111109), "081804")

    def test_remaining_seconds(self):
        rem = get_totp_remaining_seconds(30)
        self.assertGreaterEqual(rem, 1)
        self.assertLessEqual(rem, 30)

    def test_parse_otpauth_migration_payload(self):
        """Verify Protobuf wire decoding of Google Authenticator export format."""
        # Construct Protobuf payload with two accounts:
        # 1. Roblox:MainUser, secret="12345678901234567890" (GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ)
        # 2. Roblox:AltFarmer, secret="abcdefghijabcdefghij" (MFRGGZDFMYZXAYTVPFRGGZDFMY======)
        sec1 = b"12345678901234567890"
        name1 = b"Roblox:MainUser"
        p1 = bytes([1 << 3 | 2, len(sec1)]) + sec1 + bytes([2 << 3 | 2, len(name1)]) + name1
        top1 = bytes([1 << 3 | 2, len(p1)]) + p1

        b64_payload = base64.b64encode(top1).decode("utf-8")
        uri = f"otpauth-migration://offline?data={b64_payload}"

        parsed = parse_otpauth_migration(uri)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["clean_account_name"], "MainUser")
        self.assertEqual(parsed[0]["secret_b32"], "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    def test_parse_otpauth_standard_uri(self):
        uri = "otpauth://totp/Roblox:Farmer99?secret=JBSWY3DPEHPK3PXP&issuer=Roblox"
        res = parse_otpauth_uri(uri)
        self.assertIsNotNone(res)
        self.assertEqual(res["clean_account_name"], "Farmer99")
        self.assertEqual(res["secret_b32"], "JBSWY3DPEHPK3PXP")

    def test_parse_universal_input_raw_base32(self):
        res = parse_universal_2fa_input("JBSWY3DPEHPK3PXP")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["secret_b32"], "JBSWY3DPEHPK3PXP")


class TestTOTPStorageAndGUI(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.storage = StorageManager(base_dir=self.base_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_totp_secret_vault_encryption(self):
        """Verify totp_secret is encrypted at rest in SQLite and decrypted correctly."""
        acc = RobloxAccount(
            account_id="acc_totp_vault",
            username="VaultUser",
            display_name="VaultAlias",
            user_id=999,
            cookie="dummy_cookie",
            totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        )
        self.storage.save_account(acc)

        # Raw DB check: ensure secret is encrypted BLOB, not plain text
        with self.storage._db() as cursor:
            cursor.execute("SELECT encrypted_totp_secret FROM accounts WHERE account_id = ?", ("acc_totp_vault",))
            row = cursor.fetchone()
            self.assertIsNotNone(row[0])
            self.assertNotIn(b"GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", row[0])

        # Decrypted retrieval
        reloaded = self.storage.get_account("acc_totp_vault")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.totp_secret, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        self.assertEqual(reloaded.two_step_secret, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    def test_batch_sign_out_worker_auto_generates_totp(self):
        """Verify BatchSignOutWorker automatically submits TOTP code without prompting user."""
        acc = RobloxAccount(
            account_id="acc_auto_totp",
            username="AutoTotpPlayer",
            display_name="AutoTotp",
            user_id=123,
            cookie="cookie_auto_1",
            totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        )
        api = MagicMock()

        submitted_codes = []
        def mock_sign_out(cookie, two_fa_callback=None):
            if two_fa_callback:
                code = two_fa_callback({"attempt": 1, "userId": "123"})
                submitted_codes.append(code)
                return True, "new_cookie_rotated", "Success"
            return False, None, "No callback"

        api.sign_out_of_all_sessions.side_effect = mock_sign_out

        worker = BatchSignOutWorker([acc], api)
        request_2fa_calls = []
        worker.request_2fa.connect(lambda data: request_2fa_calls.append(data))

        # Run worker synchronously for test
        worker.run()

        # Must have submitted code automatically without emitting request_2fa
        self.assertEqual(len(request_2fa_calls), 0)
        self.assertEqual(len(submitted_codes), 1)
        self.assertEqual(len(submitted_codes[0]), 6)
        self.assertTrue(submitted_codes[0].isdigit())

    def test_google_auth_import_dialog_matching(self):
        """Verify GoogleAuthImportDialog correlates accounts from migration payload."""
        from PyQt6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        acc1 = RobloxAccount(
            account_id="acc_correlate_1",
            username="Player1",
            display_name="OIIO",
            user_id=101,
            cookie="c1",
        )
        self.storage.save_account(acc1)

        dlg = GoogleAuthImportDialog([acc1], self.storage)
        try:
            # Construct a mock migration record
            sec = b"12345678901234567890"
            name = b"Roblox:Player1"
            p = bytes([1 << 3 | 2, len(sec)]) + sec + bytes([2 << 3 | 2, len(name)]) + name
            top = bytes([1 << 3 | 2, len(p)]) + p
            b64_payload = base64.b64encode(top).decode("utf-8")

            dlg.txt_manual.setText(f"otpauth-migration://offline?data={b64_payload}")
            dlg._on_parse_text()

            self.assertEqual(dlg.table.rowCount(), 1)
            combo = dlg.table.cellWidget(0, 1)
            # Should have auto-selected acc1
            self.assertEqual(combo.currentData().account_id, "acc_correlate_1")

            # Save to vault
            with patch("ui.dialogs.QMessageBox.information", return_value=None):
                dlg._on_save_to_vault()

            reloaded = self.storage.get_account("acc_correlate_1")
            self.assertEqual(reloaded.totp_secret, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        finally:
            dlg.close()

    def test_two_factor_import_dialog_multi_png_accumulation(self):
        """Verify TwoFactorImportDialog accumulates multiple QR screenshots without losing matches."""
        from PyQt6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        acc1 = RobloxAccount(account_id="acc_1", username="UserOne", display_name="One", user_id=1, cookie="c1")
        acc2 = RobloxAccount(account_id="acc_2", username="UserTwo", display_name="Two", user_id=2, cookie="c2")
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        dlg = GoogleAuthImportDialog([acc1, acc2], self.storage)
        try:
            # QR 1 has UserOne
            payload_1 = "otpauth://totp/Roblox:UserOne?secret=JBSWY3DPEHPK3PXP&issuer=Roblox"
            # QR 2 has UserTwo
            payload_2 = "otpauth://totp/Roblox:UserTwo?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Roblox"

            dlg._process_multiple_payloads([payload_1, payload_2], source_label="2 QR images")

            self.assertEqual(dlg.table.rowCount(), 2)
            combo1 = dlg.table.cellWidget(0, 1)
            combo2 = dlg.table.cellWidget(1, 1)

            self.assertEqual(combo1.currentData().username, "UserOne")
            self.assertEqual(combo2.currentData().username, "UserTwo")

            # Verify clearing works cleanly
            dlg._on_clear_records()
            self.assertEqual(dlg.table.rowCount(), 0)
            self.assertEqual(len(dlg.parsed_records), 0)
            self.assertFalse(dlg.btn_save.isEnabled())
        finally:
            dlg.close()

    def test_two_factor_export_dialog(self):
        """Verify TwoFactorExportDialog properly formats otpauth URIs and secret masking."""
        from PyQt6 import QtWidgets
        from ui.dialogs import TwoFactorExportDialog
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        acc1 = RobloxAccount(
            account_id="acc_exp_1",
            username="ExportUser1",
            display_name="EU1",
            user_id=101,
            cookie="c1",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        acc2 = RobloxAccount(
            account_id="acc_no_2fa",
            username="No2FAUser",
            display_name="No2FA",
            user_id=102,
            cookie="c2",
            totp_secret=None,
        )

        dlg = TwoFactorExportDialog([acc1, acc2])
        try:
            # Only acc1 has 2FA
            self.assertEqual(len(dlg.accounts_with_2fa), 1)
            self.assertEqual(dlg.table.rowCount(), 1)

            # Test masking
            it_sec = dlg.table.item(0, 2)
            self.assertIn("••••••••", it_sec.text())
            dlg.chk_hide_keys.setChecked(False)
            self.assertEqual(dlg.table.item(0, 2).text(), "JBSWY3DPEHPK3PXP")

            # Test URI generation
            uri = dlg._generate_otpauth_uri(acc1)
            self.assertEqual(uri, "otpauth://totp/Roblox:ExportUser1?secret=JBSWY3DPEHPK3PXP&issuer=Roblox")

            # Test clipboard copy
            dlg._copy_all_uris()
            self.assertEqual(QtWidgets.QApplication.clipboard().text(), uri)
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
