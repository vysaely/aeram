"""
Unit tests for StorageManager and isolated container sandbox filesystem.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from core.models import RobloxAccount, FavoriteGame
from core.storage import StorageManager, clean_cookie


class TestStorageManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name) / "ram-linux"
        self.mocktail_data = Path(self.tmp_dir.name) / "mocktail_data"
        self.mocktail_bin = Path(self.tmp_dir.name) / "mocktail_bin"

        # Create dummy mocktail shared assets
        (self.mocktail_data / "payloads").mkdir(parents=True, exist_ok=True)
        (self.mocktail_data / "current.json").write_text('{"dummy": true}')
        self.mocktail_bin.write_text("#!/bin/sh\nexit 0\n")
        self.mocktail_bin.chmod(0o755)

        self.storage = StorageManager(
            base_dir=self.base_dir,
            mocktail_data=self.mocktail_data,
            mocktail_bin=self.mocktail_bin,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clean_cookie(self):
        c1 = "_|WARNING:-DO-NOT-SHARE-THIS...abc"
        self.assertEqual(clean_cookie(c1), c1)

        c2 = '.ROBLOSECURITY="_|WARNING:-DO-NOT-SHARE-THIS...abc"; Path=/'
        self.assertEqual(clean_cookie(c2), c1)

    def test_account_crud_and_encryption(self):
        acc = RobloxAccount(
            account_id="acc_123456",
            username="TestPlayer1",
            display_name="Player One",
            user_id=1234567,
            cookie="_|WARNING:-DO-NOT-SHARE-THIS...12345",
            robux_balance=500,
            group="VIP",
            notes="Main trading account",
            tags=["trader", "alt"],
        )

        # Save
        self.storage.save_account(acc)

        # List
        accounts = self.storage.list_accounts()
        self.assertEqual(len(accounts), 1)
        loaded = accounts[0]
        self.assertEqual(loaded.account_id, "acc_123456")
        self.assertEqual(loaded.username, "TestPlayer1")
        self.assertEqual(loaded.cookie, "_|WARNING:-DO-NOT-SHARE-THIS...12345")
        self.assertEqual(loaded.robux_balance, 500)
        self.assertEqual(loaded.group, "VIP")
        self.assertEqual(loaded.tags, ["trader", "alt"])

        # Check isolated sandbox directory structure was created without leaving plaintext cookie at rest
        profile_dir = self.storage.get_profile_dir(acc)
        self.assertTrue(profile_dir.exists())

        cookie_file = profile_dir / "roblox.cookie"
        self.assertFalse(cookie_file.exists(), "Plaintext cookie must not remain at rest upon save")

        # Verify write_runtime_cookie provisions the cookie file with mode 0600
        written = self.storage.write_runtime_cookie(acc)
        self.assertTrue(cookie_file.exists())
        self.assertEqual(cookie_file.read_text().strip(), ".ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS...12345")
        mode = cookie_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

        # Verify cleanup_runtime_cookie safely unlinks the runtime cookie
        cleaned = self.storage.cleanup_runtime_cookie(acc)
        self.assertGreaterEqual(cleaned, 1)
        self.assertFalse(cookie_file.exists(), "Runtime cookie must be unlinked after cleanup")

        # Verify Mocktail directories and payloads
        self.assertTrue((profile_dir / "data" / "mocktail" / "payloads").exists())
        self.assertTrue((profile_dir / "data" / "mocktail" / "current.json").exists())
        self.assertTrue((profile_dir / "config" / "mocktail" / "config.yaml").exists())
        self.assertTrue((self.mocktail_bin.parent / f"mocktail_{acc.account_id}").exists())

        # Update metadata
        self.storage.update_metadata("acc_123456", "Super Player", "https://avatar.url", 1000)
        updated = self.storage.get_account("acc_123456")
        self.assertEqual(updated.display_name, "Super Player")
        self.assertEqual(updated.robux_balance, 1000)

        # Delete
        self.storage.delete_account("acc_123456")
        self.assertIsNone(self.storage.get_account("acc_123456"))
        self.assertFalse(profile_dir.exists())
        self.assertFalse((self.mocktail_bin.parent / f"mocktail_{acc.account_id}").exists())

    def test_update_metadata_preserve_alias(self):
        acc = RobloxAccount(
            account_id="acc_alias_test",
            username="TestUser",
            display_name="CustomNickname",
            user_id=5555,
            cookie="dummy",
        )
        self.storage.save_account(acc)

        # Sync metadata should preserve custom nickname
        self.storage.update_metadata_preserve_alias(
            "acc_alias_test", "RobloxWebName", "https://avatar.url/1", 250
        )
        loaded = self.storage.get_account("acc_alias_test")
        self.assertEqual(loaded.display_name, "CustomNickname")
        self.assertEqual(loaded.robux_balance, 250)
        self.assertEqual(loaded.avatar_url, "https://avatar.url/1")

        # If alias is empty, preserve_alias populates it
        self.storage.update_alias("acc_alias_test", "")
        self.storage.update_metadata_preserve_alias(
            "acc_alias_test", "RobloxWebName", "https://avatar.url/2", 300
        )
        loaded_empty = self.storage.get_account("acc_alias_test")
        self.assertEqual(loaded_empty.display_name, "RobloxWebName")
        self.assertEqual(loaded_empty.robux_balance, 300)

    def test_favorites_crud(self):
        fav = FavoriteGame(
            place_id=987654,
            name="Natural Disaster Survival",
            universe_id=112233,
            notes="Fun classic game",
        )
        self.storage.save_favorite(fav)
        favs = self.storage.list_favorites()
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0].place_id, 987654)
        self.assertEqual(favs[0].name, "Natural Disaster Survival")

        self.storage.delete_favorite(987654)
        self.assertEqual(len(self.storage.list_favorites()), 0)

    def test_isolated_runtime_dir_provisioning(self):
        acc = RobloxAccount(
            account_id="acc_runtime_test",
            username="RuntimeTestUser",
            display_name="Runtime Tester",
            user_id=999000,
            cookie="_|WARNING:-DO-NOT-SHARE-THIS...tok",
        )
        profile_dir = self.storage.init_profile_sandbox(acc)
        run_dir = self.storage.get_runtime_dir(acc)
        self.assertTrue(run_dir.exists())
        self.assertTrue(run_dir.is_dir())

        # Verify permissions: 0700 on Linux
        mode = run_dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

        # Verify that .flatpak was NOT symlinked from host
        self.assertFalse((run_dir / ".flatpak").is_symlink())

    def test_account_custom_reorder(self):
        acc1 = RobloxAccount(account_id="acc_1", username="UserA", display_name="A", user_id=1, cookie="c1")
        acc2 = RobloxAccount(account_id="acc_2", username="UserB", display_name="B", user_id=2, cookie="c2")
        acc3 = RobloxAccount(account_id="acc_3", username="UserC", display_name="C", user_id=3, cookie="c3")

        self.storage.save_account(acc1)
        self.storage.save_account(acc2)
        self.storage.save_account(acc3)

        # Initial list
        accounts = self.storage.list_accounts()
        self.assertEqual([a.account_id for a in accounts], ["acc_1", "acc_2", "acc_3"])

        # Reorder: acc_3, acc_1, acc_2
        self.storage.reorder_accounts(["acc_3", "acc_1", "acc_2"])
        reordered = self.storage.list_accounts()
        self.assertEqual([a.account_id for a in reordered], ["acc_3", "acc_1", "acc_2"])

    def test_group_custom_reorder_and_crud(self):
        # Create accounts in different groups
        acc1 = RobloxAccount(account_id="acc_g1", username="U1", display_name="1", user_id=1, cookie="c1", group="Mains")
        acc2 = RobloxAccount(account_id="acc_g2", username="U2", display_name="2", user_id=2, cookie="c2", group="Alts")
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        # Set custom group order: Alts before Mains
        self.storage.set_group_order(["Alts", "Mains"])
        groups = self.storage.list_groups()
        self.assertEqual(groups, ["Alts", "Mains"])

        # Add custom empty group
        self.storage.add_custom_group("Storage")
        groups = self.storage.list_groups()
        self.assertIn("Storage", groups)

        # Reorder groups: Storage first
        self.storage.set_group_order(["Storage", "Alts", "Mains"])
        self.assertEqual(self.storage.list_groups(), ["Storage", "Alts", "Mains"])

        # Rename group
        self.storage.rename_group("Storage", "Bank")
        groups = self.storage.list_groups()
        self.assertIn("Bank", groups)
        self.assertNotIn("Storage", groups)

        # Delete group
        self.storage.delete_group("Bank")
        self.assertNotIn("Bank", self.storage.list_groups())

    def test_shift_lock_and_graphics_provisioning(self):
        # Create a main account and an alt account
        main_acc = RobloxAccount(
            account_id="acc_main_test",
            username="MainUser",
            display_name="Main",
            user_id=101,
            cookie="c_main",
            group="Main",
            tags=["main"],
        )
        alt_acc = RobloxAccount(
            account_id="acc_alt_test",
            username="AltUser",
            display_name="Alt",
            user_id=102,
            cookie="c_alt",
            group="Alts",
            tags=["alt"],
        )
        self.storage.save_account(main_acc)
        self.storage.save_account(alt_acc)

        # Provision sandboxes
        main_run = self.storage.init_profile_sandbox(main_acc)
        alt_run = self.storage.init_profile_sandbox(alt_acc)

        main_xml = main_run / "data" / "files" / "appData" / "GlobalBasicSettings_13.xml"
        alt_xml = alt_run / "data" / "files" / "appData" / "GlobalBasicSettings_13.xml"

        self.assertTrue(main_xml.exists())
        self.assertTrue(alt_xml.exists())

        main_content = main_xml.read_text(encoding="utf-8")
        alt_content = alt_xml.read_text(encoding="utf-8")

        # Both must have Shift Lock enabled (<token name="ControlMode">1</token>)
        self.assertIn('<token name="ControlMode">1</token>', main_content)
        self.assertIn('<token name="ControlMode">1</token>', alt_content)

        # Main must have Graphics Quality 7, Alt must have Graphics Quality 1
        self.assertIn('<int name="GraphicsQualityLevel">7</int>', main_content)
        self.assertIn('<token name="SavedQualityLevel">7</token>', main_content)
        self.assertIn('<int name="GraphicsQualityLevel">1</int>', alt_content)
        self.assertIn('<token name="SavedQualityLevel">1</token>', alt_content)

        # Now test apply_roblox_engine_preferences
        self.storage.apply_roblox_engine_preferences(graphics_level=7)

        main_content_after = main_xml.read_text(encoding="utf-8")
        alt_content_after = alt_xml.read_text(encoding="utf-8")
        self.assertIn('<token name="ControlMode">1</token>', main_content_after)
        self.assertIn('<token name="ControlMode">1</token>', alt_content_after)
        self.assertIn('<int name="GraphicsQualityLevel">7</int>', main_content_after)
        self.assertIn('<int name="GraphicsQualityLevel">1</int>', alt_content_after)

    def test_get_effective_fflags(self):
        # Default flags for alt and main
        alt_ff = self.storage.get_effective_fflags(is_alt=True)
        self.assertTrue(alt_ff["FFlagDebugDisableDRS"])
        self.assertEqual(alt_ff["DFIntTextureQualityOverride"], 1)

        main_ff = self.storage.get_effective_fflags(is_alt=False, texture_quality=3, frm_quality=21)
        self.assertEqual(main_ff["DFIntTextureQualityOverride"], 3)
        self.assertEqual(main_ff["DFIntDebugFRMQualityLevelOverride"], 21)

        # Test Vulkan setting
        self.storage.set_setting("fflag_graphics_api", "Vulkan")
        vul_ff = self.storage.get_effective_fflags(is_alt=False)
        self.assertTrue(vul_ff.get("FFlagDebugGraphicsPreferVulkan"))
        self.assertNotIn("FFlagDebugGraphicsPreferOpenGL", vul_ff)

        # Test custom JSON override
        self.storage.set_setting("custom_fastflags_json", '{"DFIntTaskSchedulerTargetFps": 165}')
        custom_ff = self.storage.get_effective_fflags(is_alt=False)
        self.assertEqual(custom_ff.get("DFIntTaskSchedulerTargetFps"), 165)

    def test_save_accounts_batch(self):
        batch = [
            RobloxAccount(
                account_id=f"batch_{i}",
                username=f"BatchUser{i}",
                display_name=f"Batch Alias {i}",
                user_id=888000 + i,
                cookie=f"_|WARNING:-DO-NOT-SHARE-THIS...tok_{i}",
                totp_secret=f"JBSWY3DPEHPK3P{i:02d}",
                group="BatchGrp",
            )
            for i in range(10)
        ]
        self.storage.save_accounts_batch(batch)
        all_accounts = self.storage.list_accounts()
        saved_map = {a.account_id: a for a in all_accounts}
        self.assertEqual(len(saved_map), 10)
        self.assertEqual(saved_map["batch_0"].username, "BatchUser0")
        self.assertEqual(saved_map["batch_0"].totp_secret, "JBSWY3DPEHPK3P00")
        self.assertEqual(saved_map["batch_9"].username, "BatchUser9")
        self.assertEqual(saved_map["batch_9"].totp_secret, "JBSWY3DPEHPK3P09")

    def test_mains_set_debounce(self):
        # Initial check
        mains1 = self.storage._get_mains_set()
        self.assertIsInstance(mains1, set)
        last_check = getattr(self.storage, "_mains_last_checked", 0.0)
        self.assertGreater(last_check, 0.0)

        # Call again immediately; should return cached without stat
        mains2 = self.storage._get_mains_set()
        self.assertEqual(mains1, mains2)

    def test_get_instance_binary_mirrors_companion_so_files(self):
        # Create a mock system lib dir containing mocktail and companion .so files
        sys_lib_dir = Path(self.tmp_dir.name) / "mock_system_lib"
        sys_lib_dir.mkdir(parents=True, exist_ok=True)
        sys_mocktail = sys_lib_dir / "mocktail"
        sys_mocktail.write_text("#!/bin/sh\nexit 0\n")
        sys_mocktail.chmod(0o755)

        # Create companion shared objects
        (sys_lib_dir / "libmocktail_audio_sdl.so").write_bytes(b"\x7fELFfakeaudio")
        (sys_lib_dir / "libmocktail_mouse_helper.so").write_bytes(b"\x7fELFfakemouse")
        (sys_lib_dir / "libvulkan.so").write_bytes(b"\x7fELFfakevulkan")

        # Simulate read-only directory
        storage_ro = StorageManager(
            base_dir=self.base_dir / "storage_ro",
            mocktail_data=self.mocktail_data,
            mocktail_bin=sys_mocktail,
        )

        acc = RobloxAccount(
            account_id="acc_readonly_test",
            username="ROPlayer",
            display_name="RO Player",
            user_id=999888,
            cookie="dummy",
        )

        # Force target_dir to fall back to base_dir / "bin" by pointing to non-writable target or testing ensure_mocktail_companion_libraries
        fallback_bin_dir = storage_ro.base_dir / "bin"
        storage_ro.ensure_mocktail_companion_libraries(fallback_bin_dir)

        # Verify companion shared libraries were symlinked into fallback_bin_dir
        self.assertTrue((fallback_bin_dir / "libmocktail_audio_sdl.so").exists())
        self.assertTrue((fallback_bin_dir / "libmocktail_mouse_helper.so").exists())
        self.assertTrue((fallback_bin_dir / "libvulkan.so").exists())

        # Test get_instance_binary returns an executable instance
        inst_bin = storage_ro.get_instance_binary(acc)
        self.assertTrue(inst_bin.exists())
        self.assertTrue(os.access(inst_bin, os.X_OK))

        # Test deletion cleans up
        storage_ro.delete_account(acc.account_id)
        self.assertFalse((fallback_bin_dir / f"mocktail_{acc.account_id}").exists())

    def test_get_instance_binary_follows_replaced_mocktail_binary(self):
        lib_dir = Path(self.tmp_dir.name) / "mocktail_lib"
        lib_dir.mkdir()
        (lib_dir / "libmocktail_audio_sdl.so").write_bytes(b"\x7fELFaudio")
        source = lib_dir / "mocktail"
        source.write_bytes(b"\x7fELF-client-v1")
        source.chmod(0o755)

        storage = StorageManager(
            base_dir=self.base_dir / "upgrade",
            mocktail_data=self.mocktail_data,
            mocktail_bin=source,
        )
        acc = RobloxAccount(account_id="acc_upgrade_1", username="Upgrader", display_name="Upgrader", user_id=1, cookie="dummy")

        first = storage.get_instance_binary(acc)
        self.assertEqual(first, lib_dir / "mocktail_acc_upgrade_1")
        self.assertTrue(os.path.samefile(first, source))

        # A Mocktail upgrade writes the new client to a temp file and renames it into place,
        # giving it a new inode; the old hardlink would otherwise keep the v1 client alive.
        staged = lib_dir / "mocktail.new"
        staged.write_bytes(b"\x7fELF-client-v2-larger")
        staged.chmod(0o755)
        os.replace(staged, source)

        second = storage.get_instance_binary(acc)
        self.assertEqual(second, first)
        self.assertTrue(os.path.samefile(second, source))
        self.assertEqual(second.read_bytes(), b"\x7fELF-client-v2-larger")
        self.assertFalse((lib_dir / "mocktail_acc_upgrade_1.provision").exists())

    def test_clear_password_and_purge_all_passwords(self):
        acc1 = RobloxAccount(
            account_id="acc_pwd_1",
            username="UserPwd1",
            display_name="User Pwd 1",
            user_id=10101,
            cookie="tok1",
            password="SecretPassword123!",
        )
        acc2 = RobloxAccount(
            account_id="acc_pwd_2",
            username="UserPwd2",
            display_name="User Pwd 2",
            user_id=20202,
            cookie="tok2",
            password="AnotherSecret456!",
        )
        self.storage.save_account(acc1)
        self.storage.save_account(acc2)

        loaded1 = self.storage.get_account("acc_pwd_1")
        loaded2 = self.storage.get_account("acc_pwd_2")
        self.assertEqual(loaded1.password, "SecretPassword123!")
        self.assertEqual(loaded2.password, "AnotherSecret456!")

        # 1. Clear single account password
        cleared = self.storage.clear_account_password("acc_pwd_1")
        self.assertTrue(cleared)
        loaded1_after = self.storage.get_account("acc_pwd_1")
        self.assertEqual(loaded1_after.password, "")
        # Cookies & user_id must be completely intact!
        self.assertEqual(loaded1_after.cookie, "tok1")
        self.assertEqual(loaded1_after.user_id, 10101)

        # acc2 password still intact
        self.assertEqual(self.storage.get_account("acc_pwd_2").password, "AnotherSecret456!")

        # 2. Purge all passwords
        purged = self.storage.purge_all_passwords()
        self.assertEqual(purged, 1)  # Only acc2 had a password left
        self.assertEqual(self.storage.get_account("acc_pwd_2").password, "")
        self.assertEqual(self.storage.get_account("acc_pwd_2").cookie, "tok2")


if __name__ == "__main__":
    unittest.main()
