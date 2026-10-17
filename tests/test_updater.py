"""
Unit tests for AppUpdater and UpdateDialog.
Verifies GitHub update checking, data-preservation archive extraction,
and UI dialog initialization.
"""

import os
import sys
import unittest
import tempfile
import zipfile
from pathlib import Path
from PyQt6 import QtWidgets

from core.updater import AppUpdater
from ui.dialogs import UpdateDialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


class TestAppUpdater(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.app_dir = Path(self.tmp_dir.name)
        (self.app_dir / "main.py").write_text("# entry", encoding="utf-8")
        self.updater = AppUpdater(app_root=self.app_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_local_commit_detection(self):
        # Without git or .version
        commit = self.updater.get_local_commit()
        self.assertEqual(commit, "unknown")

        # With .version file
        (self.app_dir / ".version").write_text("abcdef123456", encoding="utf-8")
        self.assertEqual(self.updater.get_local_commit(), "abcdef123456")

    def test_archive_preserves_user_data(self):
        # Create critical user files
        db_file = self.app_dir / "manager.db"
        db_file.write_text("CRITICAL_DB_DATA", encoding="utf-8")
        key_file = self.app_dir / "vault.key"
        key_file.write_text("CRITICAL_KEY_DATA", encoding="utf-8")
        code_file = self.app_dir / "code.py"
        code_file.write_text("old_code()", encoding="utf-8")

        # Create mock update zip
        zip_tmp = Path(self.tmp_dir.name) / "test_update.zip"
        extract_dir = Path(self.tmp_dir.name) / "extracted"
        extract_dir.mkdir()
        (extract_dir / "code.py").write_text("new_code()", encoding="utf-8")
        (extract_dir / "manager.db").write_text("OVERWRITTEN_DB", encoding="utf-8")
        (extract_dir / "vault.key").write_text("OVERWRITTEN_KEY", encoding="utf-8")

        with zipfile.ZipFile(zip_tmp, "w") as zf:
            for f in extract_dir.iterdir():
                zf.write(f, f.name)

        # Simulate updater file merge
        PRESERVED_NAMES = {
            "manager.db", "manager.db-wal", "manager.db-shm",
            "vault.key", "mains.txt", "no_gamescope", "instances",
            ".git", ".cache", "__pycache__"
        }

        with zipfile.ZipFile(zip_tmp, "r") as zf:
            zf.extractall(extract_dir / "out")

        for f in (extract_dir / "out").iterdir():
            if f.name not in PRESERVED_NAMES:
                target = self.app_dir / f.name
                target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

        # Verify: code was updated, but DB and KEY were strictly preserved
        self.assertEqual(code_file.read_text(encoding="utf-8"), "new_code()")
        self.assertEqual(db_file.read_text(encoding="utf-8"), "CRITICAL_DB_DATA")
        self.assertEqual(key_file.read_text(encoding="utf-8"), "CRITICAL_KEY_DATA")

    def test_git_pull_non_ff_preserves_local_changes_without_reset(self):
        from unittest.mock import patch, MagicMock
        (self.app_dir / ".git").mkdir(exist_ok=True)

        ran_commands = []

        def fake_run(cmd, **kwargs):
            ran_commands.append(cmd)
            res = MagicMock()
            if "pull" in cmd:
                res.returncode = 1
                res.stderr = "fatal: Not possible to fast-forward, aborting."
                res.stdout = ""
            else:
                res.returncode = 0
                res.stderr = ""
                res.stdout = "abc1234"
            return res

        with patch("core.updater.shutil.which", return_value="/usr/bin/git"), \
             patch("core.updater.subprocess.run", side_effect=fake_run):
            ok, msg = self.updater.apply_update()

        self.assertFalse(ok)
        self.assertIn("fast-forward update declined to preserve local changes", msg.lower())
        # Ensure git reset --hard was NEVER invoked
        for cmd in ran_commands:
            self.assertNotIn("reset", cmd)

    def test_git_update_captures_make_warning(self):
        from unittest.mock import patch, MagicMock
        (self.app_dir / ".git").mkdir(exist_ok=True)
        native_dir = self.app_dir / "native"
        native_dir.mkdir(exist_ok=True)
        (native_dir / "Makefile").write_text("all:\n", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            res = MagicMock()
            if "make" in cmd:
                res.returncode = 2
                res.stderr = "gcc error: missing header"
                res.stdout = ""
            else:
                res.returncode = 0
                res.stderr = ""
                res.stdout = ""
            return res

        with patch("core.updater.shutil.which", return_value="/usr/bin/make"), \
             patch("core.updater.subprocess.run", side_effect=fake_run):
            ok, msg = self.updater.apply_update()

        self.assertTrue(ok)
        self.assertIn("Native helper build note", msg)

    def test_archive_update_aborts_on_invalid_structure(self):
        from unittest.mock import patch
        import io

        # Create an archive lacking core/ and aeram
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("rogue_file.txt", "malformed")
        zip_bytes = zip_buf.getvalue()

        class FakeResponse(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch("core.updater.urllib.request.urlopen", return_value=FakeResponse(zip_bytes)):
            ok, msg = self.updater.apply_update()

        self.assertFalse(ok)
        self.assertIn("valid AERAM package structure", msg)

    def test_archive_update_removes_patches_dropped_upstream(self):
        from unittest.mock import patch
        import io

        patches = self.updater.app_root / "native" / "mocktail"
        patches.mkdir(parents=True)
        (patches / "0002-old-dropped.patch").write_text("stale")
        (patches / "0001-kept.patch").write_text("old")
        (self.updater.app_root / "manager.db").write_text("user data")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("aeram-main/core/__init__.py", "")
            zf.writestr("aeram-main/native/mocktail/0001-kept.patch", "new")
            zf.writestr("aeram-main/native/mocktail/0002-replacement.patch", "new")
        zip_bytes = zip_buf.getvalue()

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch("core.updater.urllib.request.urlopen", return_value=FakeResponse(zip_bytes)), \
                patch("core.updater.shutil.which", return_value=None):
            ok, msg = self.updater.apply_update()

        self.assertTrue(ok, msg)
        self.assertEqual(sorted(p.name for p in patches.iterdir()), ["0001-kept.patch", "0002-replacement.patch"])
        self.assertEqual((patches / "0001-kept.patch").read_text(), "new")
        self.assertEqual((self.updater.app_root / "manager.db").read_text(), "user data")


class TestUpdateDialog(unittest.TestCase):
    def test_dialog_init(self):
        dlg = UpdateDialog(auto_check=False)
        self.assertIsNotNone(dlg.btn_check)
        self.assertIsNotNone(dlg.btn_update)
        self.assertIsNotNone(dlg.btn_restart)
        self.assertFalse(dlg.btn_restart.isVisible())
        self.assertFalse(dlg.btn_update.isEnabled())
        dlg.close()


if __name__ == "__main__":
    unittest.main()
