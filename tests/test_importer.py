"""
Unit tests for Windows RAM importer and cookie parsing engine.
"""

import json
import os
import unittest

from core.ram_importer import (
    parse_account_data_json,
    parse_cookie_list,
    decrypt_ram_file,
    is_ram_encrypted,
    import_from_file_or_text,
    RAM_HEADER,
)
from core.vault import (
    derive_key_argon2,
    secretbox_encrypt,
    crypto_pwhash_SALTBYTES,
    crypto_secretbox_NONCEBYTES,
    WrongPasswordError,
)


class TestRAMImporter(unittest.TestCase):
    def test_parse_windows_ram_json(self):
        sample_json = json.dumps([
            {
                "SecurityToken": "_|WARNING:-DO-NOT-SHARE-THIS...tok1",
                "Username": "WindowsAlt1",
                "Alias": "MainAlt",
                "Group": "Trading",
                "UserID": 10001,
                "Description": "Original description notes",
            },
            {
                "SecurityToken": "_|WARNING:-DO-NOT-SHARE-THIS...tok2",
                "Username": "WindowsAlt2",
                "Alias": "PVPAlt",
                "Group": "Combat",
                "UserID": 10002,
                "Description": "PVP account",
            }
        ])

        accounts = parse_account_data_json(sample_json)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["username"], "WindowsAlt1")
        self.assertEqual(accounts[0]["display_name"], "MainAlt")
        self.assertEqual(accounts[0]["cookie"], "_|WARNING:-DO-NOT-SHARE-THIS...tok1")
        self.assertEqual(accounts[0]["group"], "Trading")
        self.assertEqual(accounts[0]["user_id"], 10001)

    def test_parse_cookie_list_and_csv(self):
        text = """
# Comments should be ignored
_|WARNING:-DO-NOT-SHARE-THIS...rawcookie1
AltAccount2:_|WARNING:-DO-NOT-SHARE-THIS...rawcookie2
UserThree,_|WARNING:-DO-NOT-SHARE-THIS...rawcookie3,VIPGroup,SampleNotes
"""
        accounts = parse_cookie_list(text)
        self.assertEqual(len(accounts), 3)
        self.assertEqual(accounts[0]["cookie"], "_|WARNING:-DO-NOT-SHARE-THIS...rawcookie1")
        self.assertEqual(accounts[1]["username"], "AltAccount2")
        self.assertEqual(accounts[1]["cookie"], "_|WARNING:-DO-NOT-SHARE-THIS...rawcookie2")
        self.assertEqual(accounts[2]["username"], "UserThree")
        self.assertEqual(accounts[2]["group"], "VIPGroup")
        self.assertEqual(accounts[2]["notes"], "SampleNotes")

    def test_encrypted_ram_roundtrip(self):
        # Construct synthetic RAM encrypted file conforming to original RAM format:
        # RAMHeader + 16-byte salt + 24-byte nonce + SecretBox ciphertext
        pwd = "TestMasterPassword123!"
        salt = os.urandom(crypto_pwhash_SALTBYTES)
        derived_key = derive_key_argon2(pwd, salt, opslimit=3, memlimit=67108864)

        payload_json = json.dumps([
            {
                "SecurityToken": "_|WARNING:-DO-NOT-SHARE-THIS...enc_cookie",
                "Username": "EncryptedAlt",
                "Alias": "EncryptedAlias",
                "Group": "Secret",
                "UserID": 99999,
                "Description": "Secure account",
            }
        ])

        # secretbox_encrypt returns nonce (24 bytes) + ciphertext (includes 16-byte MAC)
        encrypted_blob = secretbox_encrypt(derived_key, payload_json.encode("utf-8"))

        full_file_bytes = RAM_HEADER + salt + encrypted_blob
        self.assertTrue(is_ram_encrypted(full_file_bytes))

        # Decrypt with correct password
        decrypted_text = decrypt_ram_file(full_file_bytes, pwd)
        accounts = parse_account_data_json(decrypted_text)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["username"], "EncryptedAlt")
        self.assertEqual(accounts[0]["cookie"], "_|WARNING:-DO-NOT-SHARE-THIS...enc_cookie")

        # Decrypt with wrong password fails
        with self.assertRaises(WrongPasswordError):
            decrypt_ram_file(full_file_bytes, "WrongPassword!")

    def test_long_raw_cookie_string_without_newlines(self):
        # 400+ char single line string without newlines
        long_cookie = "_|WARNING:-DO-NOT-SHARE-THIS--" + "A" * 380
        accounts, needs_pw = import_from_file_or_text(long_cookie)
        self.assertFalse(needs_pw)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["cookie"], long_cookie)


if __name__ == "__main__":
    unittest.main()
