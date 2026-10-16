"""
Unit tests for AERAM cross-platform encrypted migration engine.
"""

import tempfile
import unittest
from pathlib import Path

from core.models import RobloxAccount
from core.migration import (
    export_accounts_encrypted,
    decrypt_aeram_export,
    is_aeram_export,
    WrongPasswordError,
)
from core.ram_importer import import_from_file_or_text


class TestEncryptedMigration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.export_file = Path(self.tmp_dir.name) / "accounts_backup.aeram"
        self.accounts = [
            RobloxAccount(
                account_id="acc_synth_1",
                username="SyntheticUser1",
                display_name="First Synthetic",
                user_id=111111,
                cookie="_|WARNING:-DO-NOT-SHARE-THIS...synthetic1",
                avatar_url="https://example.com/avatar1.png",
                robux_balance=250,
                group="MainGroup",
                notes="Synthetic test account",
                tags=["main", "fps:165"],
                sort_order=1,
            ),
            RobloxAccount(
                account_id="acc_synth_2",
                username="SyntheticUser2",
                display_name="Second Synthetic",
                user_id=222222,
                cookie="_|WARNING:-DO-NOT-SHARE-THIS...synthetic2",
                avatar_url="https://example.com/avatar2.png",
                robux_balance=0,
                group="Alts",
                notes="Alt test account",
                tags=["alt", "fps:24"],
                sort_order=2,
            ),
        ]

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_encrypted_export_and_import_roundtrip(self):
        password = "VerySecureCrossPlatformPassword123!"

        # 1. Export encrypted archive
        out_path = export_accounts_encrypted(self.accounts, password, self.export_file)
        self.assertTrue(out_path.exists())

        raw_bytes = out_path.read_bytes()
        self.assertTrue(is_aeram_export(raw_bytes))
        # Ensure plaintext cookies are NOT in the raw file
        self.assertNotIn(b"synthetic1", raw_bytes)
        self.assertNotIn(b"synthetic2", raw_bytes)

        # 2. Decrypt with correct password
        decrypted = decrypt_aeram_export(raw_bytes, password)
        self.assertEqual(len(decrypted), 2)
        self.assertEqual(decrypted[0]["username"], "SyntheticUser1")
        self.assertEqual(decrypted[0]["cookie"], "_|WARNING:-DO-NOT-SHARE-THIS...synthetic1")
        self.assertEqual(decrypted[0]["group"], "MainGroup")
        self.assertEqual(decrypted[1]["username"], "SyntheticUser2")
        self.assertEqual(decrypted[1]["cookie"], "_|WARNING:-DO-NOT-SHARE-THIS...synthetic2")

        # 3. Import via import_from_file_or_text auto-detector
        imported, needs_pw = import_from_file_or_text(str(self.export_file), password=password)
        self.assertFalse(needs_pw)
        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0]["username"], "SyntheticUser1")

    def test_wrong_password_fails_cleanly(self):
        password = "CorrectMasterPassword!@#$"
        export_accounts_encrypted(self.accounts, password, self.export_file)

        raw_bytes = self.export_file.read_bytes()
        with self.assertRaises(WrongPasswordError):
            decrypt_aeram_export(raw_bytes, "WrongPassword456!")

    def test_password_required_flag(self):
        password = "SomePassword123"
        export_accounts_encrypted(self.accounts, password, self.export_file)

        # Calling without password should indicate password is required
        imported, needs_pw = import_from_file_or_text(str(self.export_file), password=None)
        self.assertTrue(needs_pw)
        self.assertEqual(len(imported), 0)

    def test_totp_secret_and_password_preservation_roundtrip(self):
        acc = RobloxAccount(
            account_id="acc_with_2fa",
            username="TwoFactorUser",
            display_name="2FA User",
            user_id=777888,
            cookie="_|WARNING:-DO-NOT-SHARE-THIS...tok_2fa",
            password="MySecretPassword#999",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        export_accounts_encrypted([acc], "MasterKey123", self.export_file)

        raw_bytes = self.export_file.read_bytes()
        decrypted = decrypt_aeram_export(raw_bytes, "MasterKey123")
        self.assertEqual(len(decrypted), 1)
        self.assertEqual(decrypted[0]["username"], "TwoFactorUser")
        self.assertEqual(decrypted[0]["password"], "MySecretPassword#999")
        self.assertEqual(decrypted[0]["totp_secret"], "JBSWY3DPEHPK3PXP")

        # Check file mode is strictly 0o600
        import stat
        file_mode = self.export_file.stat().st_mode
        self.assertEqual(file_mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH), 0)


if __name__ == "__main__":
    unittest.main()
