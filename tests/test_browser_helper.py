"""
Tests for syncing the selected account into Mocktail's default auth cookie.
"""

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.browser_helper import sync_mocktail_auth_cookie


class TestBrowserHelper(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="aeram_test_bh_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sync_mocktail_auth_cookie_atomic_permissions(self):
        # We test with a simulated target directory
        dummy_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--" + "A" * 80
        with tempfile.TemporaryDirectory() as tmp_home:
            with patch("os.path.expanduser", return_value=os.path.join(tmp_home, ".local/share/mocktail/auth")), \
                 patch("sys.modules", {}):
                ok = sync_mocktail_auth_cookie(dummy_cookie)
                self.assertTrue(ok)
                target_file = os.path.join(tmp_home, ".local/share/mocktail/auth", "roblox.cookie")
                self.assertTrue(os.path.exists(target_file))
                mode = stat.S_IMODE(os.stat(target_file).st_mode)
                self.assertEqual(mode, 0o600)
                content = Path(target_file).read_text(encoding="utf-8")
                self.assertIn(dummy_cookie, content)


if __name__ == "__main__":
    unittest.main()
