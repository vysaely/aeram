"""
Windows Roblox Account Manager (RAM) and cookie importer engine.
Supports:
1. Windows RAM AccountData.json (unencrypted JSON).
2. Windows RAM password-encrypted AccountData.json (RAMHeader + Argon2 + libsodium SecretBox).
3. Line-delimited .ROBLOSECURITY cookie lists.
4. CSV formatted account dumps.
"""

import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from .vault import (
    derive_key_argon2,
    secretbox_decrypt,
    crypto_secretbox_NONCEBYTES,
    crypto_pwhash_SALTBYTES,
    crypto_pwhash_ALG_ARGON2ID13,
    crypto_pwhash_ALG_ARGON2I13,
    WrongPasswordError,
    VaultCorruptError
)
from .storage import clean_cookie

# Original 64-byte RAM magic header bytes
RAM_HEADER = bytes([
    82, 111, 98, 108, 111, 120, 32, 65, 99, 99, 111, 117, 110, 116, 32, 77,
    97, 110, 97, 103, 101, 114, 32, 99, 114, 101, 97, 116, 101, 100, 32, 98,
    121, 32, 105, 99, 51, 119, 48, 108, 102, 50, 50, 32, 64, 32, 103, 105,
    116, 104, 117, 98, 46, 99, 111, 109, 32, 46, 46, 46, 46, 46, 46, 46
])

COOKIE_PATTERN = re.compile(r'_\|WARNING:-DO-NOT-SHARE-THIS[^\s"\';,]+')


def is_ram_encrypted(data: bytes) -> bool:
    """Returns True if the byte payload starts with the RAM encrypted header."""
    return data.startswith(RAM_HEADER)


def decrypt_ram_file(data: bytes, password: str) -> str:
    """
    Decrypts an encrypted RAM AccountData file using the master password.
    Tries Argon2id first, then falls back to Argon2i.
    """
    if not is_ram_encrypted(data):
        raise ValueError("File does not start with RAMHeader")

    header_len = len(RAM_HEADER)
    salt_end = header_len + crypto_pwhash_SALTBYTES
    nonce_end = salt_end + crypto_secretbox_NONCEBYTES

    if len(data) <= nonce_end:
        raise VaultCorruptError("Encrypted RAM file is truncated")

    salt = data[header_len:salt_end]
    nonce = data[salt_end:nonce_end]
    ciphertext = data[nonce_end:]

    # Construct standard SecretBox blob: nonce + ciphertext
    blob = nonce + ciphertext

    # Try both Argon2 algorithms (Argon2id and Argon2i) with moderate strength parameters
    exceptions = []
    for alg in (crypto_pwhash_ALG_ARGON2ID13, crypto_pwhash_ALG_ARGON2I13):
        for mem in (67108864, 268435456):
            try:
                derived_key = derive_key_argon2(password, salt, opslimit=3, memlimit=mem, alg=alg)
                plaintext = secretbox_decrypt(derived_key, blob)
                return plaintext.decode("utf-8")
            except (WrongPasswordError, VaultCorruptError) as e:
                exceptions.append(e)
            except Exception as e:
                exceptions.append(e)

    raise WrongPasswordError("Incorrect master password or unable to decrypt RAM archive")


def parse_account_data_json(json_str: str) -> List[Dict[str, Any]]:
    """Parses JSON text conforming to Windows RAM AccountData.json schema."""
    data = json.loads(json_str)
    if not isinstance(data, list):
        raise ValueError("AccountData.json must contain a list of accounts")

    accounts = []
    for item in data:
        if not isinstance(item, dict):
            continue

        # In original RAM, the token field is "SecurityToken"
        raw_cookie = item.get("SecurityToken") or item.get("cookie") or item.get("Cookie") or ""
        cookie = clean_cookie(raw_cookie)
        if not cookie:
            continue

        username = item.get("Username") or item.get("username") or item.get("Alias") or "Unknown"
        alias = item.get("Alias") or item.get("display_name") or username
        user_id = item.get("UserID") or item.get("user_id") or 0
        password = item.get("Password") or item.get("password") or item.get("_Password") or ""
        group = item.get("Group") or item.get("group") or "Default"
        notes = item.get("Description") or item.get("notes") or ""
        totp_secret = item.get("totp_secret") or item.get("two_step_secret") or ""

        accounts.append({
            "username": username,
            "display_name": alias,
            "user_id": int(user_id) if isinstance(user_id, (int, str)) and str(user_id).isdigit() else 0,
            "cookie": cookie,
            "password": str(password).strip(),
            "group": group,
            "notes": notes,
            "totp_secret": str(totp_secret).strip(),
        })

    return accounts


def parse_cookie_list(text: str) -> List[Dict[str, Any]]:
    """
    Parses arbitrary text with lines containing cookies, username:cookie, or CSV.
    """
    accounts = []
    lines = text.strip().splitlines()

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Check CSV format first if commas are present
        if "," in line:
            parts = [p.strip() for p in line.split(",")]
            # Format: Username, Cookie, [Group], [Notes]
            if len(parts) >= 2 and COOKIE_PATTERN.search(parts[1]):
                cookie = clean_cookie(parts[1])
                accounts.append({
                    "username": parts[0] or "ImportedAccount",
                    "display_name": parts[0] or "ImportedAccount",
                    "user_id": 0,
                    "cookie": cookie,
                    "group": parts[2] if len(parts) > 2 and parts[2] else "Imported",
                    "notes": parts[3] if len(parts) > 3 else "",
                })
                continue

        # Look for .ROBLOSECURITY pattern in non-CSV lines
        match = COOKIE_PATTERN.search(line)
        if match:
            cookie = match.group(0)
            prefix = line[:match.start()].strip(": ,;=")
            username = prefix.split(":")[-1].strip() if prefix else ""
            accounts.append({
                "username": username or "ImportedAccount",
                "display_name": username or "ImportedAccount",
                "user_id": 0,
                "cookie": cookie,
                "group": "Imported",
                "notes": "",
            })
            continue

    return accounts


def import_from_file_or_text(
    source: str,
    password: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Auto-detects format from filepath or raw string and returns parsed accounts.
    Returns: (list_of_account_dicts, is_password_needed_and_missing)
    """
    path = None
    if len(source) < 500 and "\n" not in source:
        try:
            p = Path(source)
            if p.exists() and p.is_file():
                path = p
        except (OSError, ValueError):
            path = None

    if path:
        raw_bytes = path.read_bytes()
        from .migration import is_aeram_export, decrypt_aeram_export
        if is_aeram_export(raw_bytes):
            if not password:
                return [], True
            return decrypt_aeram_export(raw_bytes, password), False
        elif is_ram_encrypted(raw_bytes):
            if not password:
                return [], True
            json_text = decrypt_ram_file(raw_bytes, password)
            return parse_account_data_json(json_text), False
        else:
            text = raw_bytes.decode("utf-8", errors="replace")
    else:
        text = source

    # Try JSON first
    text_stripped = text.strip()
    if text_stripped.startswith("[") or text_stripped.startswith("{"):
        try:
            return parse_account_data_json(text_stripped), False
        except Exception:
            pass

    # Fallback to regex cookie parsing
    return parse_cookie_list(text), False
