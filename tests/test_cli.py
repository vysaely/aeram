"""
Automated unit and integration tests for CLI functions.
"""

import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.models import RobloxAccount
from core.storage import StorageManager
from core.roblox_api import RobloxAPI
from core.instance_runner import InstanceRunner
from cli import run_cli


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name) / "ram-test"
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
        self.api = MagicMock(spec=RobloxAPI)
        self.runner = InstanceRunner(self.storage)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cli_list_empty(self):
        args = MagicMock()
        args.install_desktop = False
        args.list = True
        args.status = False
        args.stop = None
        args.stop_all = False
        args.add = None
        args.import_file = None
        args.launch = None
        args.multi = None

        with patch("sys.stdout", new=StringIO()) as fake_out:
            run_cli(args, self.storage, self.api, self.runner)
            output = fake_out.getvalue()
            self.assertIn("ROBLOX ACCOUNTS (0)", output)

    def test_cli_import_and_list(self):
        # Create a test account file
        acc_file = Path(self.tmp_dir.name) / "test_accounts.txt"
        acc_file.write_text("AltUser1,_|WARNING:-DO-NOT-SHARE-THIS...tokA,Trader,NotesA\n")

        self.api.enrich_account.return_value = {
            "username": "AltUser1",
            "display_name": "Alt Display",
            "user_id": 55555,
            "avatar_url": "",
            "robux_balance": 150,
        }

        import_args = MagicMock()
        import_args.install_desktop = False
        import_args.list = False
        import_args.status = False
        import_args.stop = None
        import_args.stop_all = False
        import_args.add = None
        import_args.import_file = str(acc_file)
        import_args.password = None
        import_args.launch = None
        import_args.multi = None

        with patch("sys.stdout", new=StringIO()) as fake_out:
            run_cli(import_args, self.storage, self.api, self.runner)
            output = fake_out.getvalue()
            self.assertIn("Successfully imported 1 accounts", output)

        # Now list accounts
        list_args = MagicMock()
        list_args.install_desktop = False
        list_args.list = True
        list_args.status = False
        list_args.stop = None
        list_args.stop_all = False
        list_args.add = None
        list_args.import_file = None
        list_args.launch = None
        list_args.multi = None

        with patch("sys.stdout", new=StringIO()) as fake_out:
            run_cli(list_args, self.storage, self.api, self.runner)
            output = fake_out.getvalue()
            self.assertIn("@AltUser1", output)
            self.assertIn("Low (6 FPS)", output)
            self.assertIn("Trader", output)
            # Secrets must NOT be printed!
            self.assertNotIn("WARNING:-DO-NOT-SHARE-THIS", output)

    def test_entrypoint_orphan_flags_exit_code_2(self):
        from core.entrypoint import main
        test_cases = [
            ["aeram", "--place", "189707"],
            ["aeram", "--job", "job-1234"],
            ["aeram", "--server", "https://roblox.com/games/1?privateServerLinkCode=abc"],
            ["aeram", "--group", "MyGroup"],
            ["aeram", "--password", "secret"],
        ]
        for argv in test_cases:
            with self.subTest(argv=argv):
                with patch("sys.argv", argv), patch("sys.stderr", new=StringIO()) as fake_err, \
                        patch("core.entrypoint.StorageManager") as storage_cls:
                    with self.assertRaises(SystemExit) as cm:
                        main()
                    self.assertEqual(cm.exception.code, 2)
                    self.assertIn("aeram: error:", fake_err.getvalue())
                    # Invalid arguments must be rejected before the vault is opened
                    storage_cls.assert_not_called()

    def test_cli_add_via_stdin(self):
        self.api.enrich_account.return_value = {
            "username": "StdinUser",
            "display_name": "Stdin Display",
            "user_id": 112233,
            "avatar_url": "",
            "robux_balance": 50,
        }
        args = MagicMock()
        args.install_desktop = False
        args.list = False
        args.status = False
        args.stop = None
        args.stop_all = False
        args.add = "-"
        args.import_file = None
        args.launch = None
        args.multi = None
        args.group = "Default"
        args.notes = ""

        with patch("sys.stdin", StringIO("_|WARNING:-DO-NOT-SHARE-THIS...stdin_tok")), \
             patch("sys.stdout", new=StringIO()) as fake_out:
            run_cli(args, self.storage, self.api, self.runner)
            self.assertIn("Added account @StdinUser", fake_out.getvalue())
            acc = self.storage.find_account_by_username("StdinUser")
            self.assertIsNotNone(acc)
            self.assertEqual(acc.username, "StdinUser")

    def test_cli_add_via_getpass_prompt(self):
        self.api.enrich_account.return_value = {
            "username": "PromptUser",
            "display_name": "Prompt Display",
            "user_id": 445566,
            "avatar_url": "",
            "robux_balance": 100,
        }
        args = MagicMock()
        args.install_desktop = False
        args.list = False
        args.status = False
        args.stop = None
        args.stop_all = False
        args.add = "-"
        args.import_file = None
        args.launch = None
        args.multi = None
        args.group = "Default"
        args.notes = ""

        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = True

        with patch("sys.stdin", fake_stdin), \
             patch("getpass.getpass", return_value="_|WARNING:-DO-NOT-SHARE-THIS...prompt_tok"), \
             patch("sys.stdout", new=StringIO()) as fake_out:
            run_cli(args, self.storage, self.api, self.runner)
            self.assertIn("Added account @PromptUser", fake_out.getvalue())
            acc = self.storage.find_account_by_username("PromptUser")
            self.assertIsNotNone(acc)
            self.assertEqual(acc.username, "PromptUser")

    def test_cli_import_password_stdin(self):
        acc_file = Path(self.tmp_dir.name) / "enc_accounts.txt"
        acc_file.write_text("DummyEncryptedPayload")

        args = MagicMock()
        args.install_desktop = False
        args.list = False
        args.status = False
        args.stop = None
        args.stop_all = False
        args.add = None
        args.import_file = str(acc_file)
        args.password = "-"
        args.launch = None
        args.multi = None

        with patch("sys.stdin", StringIO("my_secret_pwd\n")), \
             patch("cli.import_from_file_or_text", return_value=([], False)) as mock_import, \
             patch("sys.stdout", new=StringIO()):
            run_cli(args, self.storage, self.api, self.runner)
            mock_import.assert_called_once()
            self.assertEqual(mock_import.call_args[0][1], "my_secret_pwd")

    def test_cli_import_password_prompt(self):
        acc_file = Path(self.tmp_dir.name) / "enc_accounts2.txt"
        acc_file.write_text("DummyEncryptedPayload2")

        args = MagicMock()
        args.install_desktop = False
        args.list = False
        args.status = False
        args.stop = None
        args.stop_all = False
        args.add = None
        args.import_file = str(acc_file)
        args.password = "-"
        args.launch = None
        args.multi = None

        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = True

        with patch("sys.stdin", fake_stdin), \
             patch("getpass.getpass", return_value="prompted_pwd"), \
             patch("cli.import_from_file_or_text", return_value=([], False)) as mock_import, \
             patch("sys.stdout", new=StringIO()):
            run_cli(args, self.storage, self.api, self.runner)
            mock_import.assert_called_once()
            self.assertEqual(mock_import.call_args[0][1], "prompted_pwd")


if __name__ == "__main__":
    unittest.main()

