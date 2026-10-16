"""
Tests for browser helper profile registration, stale profile sweeping, and cleanup watchers.
"""

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.browser_helper import (
    _register_temp_profile,
    _unregister_temp_profile,
    _cleanup_all_temp_profiles,
    _ACTIVE_TEMP_PROFILES,
    cleanup_stale_browser_profiles,
    sync_mocktail_auth_cookie,
)


class TestBrowserHelper(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="aeram_test_bh_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_temp_profile_registry_and_cleanup(self):
        profile_path = os.path.join(self.test_dir, "test_profile_1")
        os.makedirs(profile_path, exist_ok=True)
        (Path(profile_path) / "dummy.txt").write_text("data")

        _register_temp_profile(profile_path)
        self.assertIn(profile_path, _ACTIVE_TEMP_PROFILES)

        _cleanup_all_temp_profiles()
        self.assertFalse(os.path.exists(profile_path))
        self.assertNotIn(profile_path, _ACTIVE_TEMP_PROFILES)

    def test_cleanup_stale_browser_profiles(self):
        tmp_base = tempfile.gettempdir()
        stale_ff = os.path.join(tmp_base, "aeram_firefox_test_stale_123")
        stale_ch = os.path.join(tmp_base, "aeram_chrome_test_stale_456")

        os.makedirs(stale_ff, exist_ok=True)
        os.makedirs(stale_ch, exist_ok=True)

        self.assertTrue(os.path.exists(stale_ff))
        self.assertTrue(os.path.exists(stale_ch))

        cleaned = cleanup_stale_browser_profiles()
        self.assertGreaterEqual(cleaned, 2)
        self.assertFalse(os.path.exists(stale_ff))
        self.assertFalse(os.path.exists(stale_ch))

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
