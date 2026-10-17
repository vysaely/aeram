"""
Unit tests for Vault and libsodium cryptographic functions.
"""

import os
import tempfile
import unittest
from pathlib import Path

from core.vault import (
    Vault,
    VaultCorruptError,
    WrongPasswordError,
    derive_key_argon2,
    secretbox_encrypt,
    secretbox_decrypt,
    crypto_secretbox_KEYBYTES,
    crypto_pwhash_SALTBYTES,
)


class TestVaultCrypto(unittest.TestCase):
    def test_secretbox_roundtrip(self):
        key = os.urandom(crypto_secretbox_KEYBYTES)
        data = b"Testing sensitive session cookie: _|WARNING:-DO-NOT-SHARE-THIS..."
        encrypted = secretbox_encrypt(key, data)
        self.assertNotEqual(encrypted, data)
        decrypted = secretbox_decrypt(key, encrypted)
        self.assertEqual(decrypted, data)

    def test_secretbox_tampering_fails(self):
        key = os.urandom(crypto_secretbox_KEYBYTES)
        data = b"Secret payload"
        encrypted = bytearray(secretbox_encrypt(key, data))
        # Tamper with the last byte
        encrypted[-1] ^= 0x55
        with self.assertRaises(WrongPasswordError):
            secretbox_decrypt(key, bytes(encrypted))

    def test_secretbox_corrupted_short_payload(self):
        key = os.urandom(crypto_secretbox_KEYBYTES)
        with self.assertRaises(VaultCorruptError):
            secretbox_decrypt(key, b"short")

    def test_argon2_deterministic_derivation(self):
        salt = os.urandom(crypto_pwhash_SALTBYTES)
        pwd = "ComplexMasterPassword!@#123"
        k1 = derive_key_argon2(pwd, salt, opslimit=1, memlimit=16777216)
        k2 = derive_key_argon2(pwd, salt, opslimit=1, memlimit=16777216)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 32)

    def test_vault_key_lifecycle_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "vault.key"
            vault = Vault(key_path)

            # Check file permissions on Linux
            mode = key_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

            # Test string encrypt and decrypt
            msg = "MySecretRobloxCookieToken_12345"
            enc = vault.encrypt_string(msg)
            dec = vault.decrypt_string(enc)
            self.assertEqual(dec, msg)

            # Re-open vault with existing key file
            vault2 = Vault(key_path)
            dec2 = vault2.decrypt_string(enc)
            self.assertEqual(dec2, msg)

    def test_corrupt_keyfile_raises_vault_corrupt_error_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "vault.key"
            # Write an invalid 10-byte key
            key_path.write_bytes(b"1234567890")

            with self.assertRaises(VaultCorruptError) as cm:
                Vault(key_path)

            self.assertIn("Key overwrite aborted", str(cm.exception))
            # Verify the corrupted file was NOT silently overwritten
            self.assertEqual(key_path.read_bytes(), b"1234567890")


    def test_concurrent_key_creation_keeps_first_key(self):
        """If another process publishes vault.key first, adopt it instead of overwriting it."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "vault.key"
            winner_key = os.urandom(32)
            real_link = os.link

            def competitor_wins(src, dst):
                Path(dst).write_bytes(winner_key)  # other process got there first
                return real_link(src, dst)

            with patch("core.vault.os.link", side_effect=competitor_wins):
                vault = Vault(key_path)

            self.assertEqual(key_path.read_bytes(), winner_key)
            self.assertEqual(vault._key, winner_key)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["vault.key"])

if __name__ == "__main__":
    unittest.main()
