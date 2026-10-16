"""
Encrypted account archive migration engine for AERAM.
Allows secure, password-protected export and import of AERAM vaults and Windows RAM archives.
Uses Argon2id KDF and XSalsa20-Poly1305 authenticated encryption (libsodium / PyNaCl).
Zero plaintext token leakage; master export passwords are never persisted.
"""

import os
import json
from pathlib import Path
from typing import List, Dict, Any

from .models import RobloxAccount
from .vault import (
    derive_key_argon2,
    secretbox_encrypt,
    secretbox_decrypt,
    crypto_secretbox_NONCEBYTES,
    crypto_pwhash_SALTBYTES,
    crypto_pwhash_ALG_ARGON2ID13,
    WrongPasswordError,
    VaultCorruptError,
)

AERAM_EXPORT_MAGIC = b"AERAM_VAULT_EXPORT_V1\n"


def is_aeram_export(data: bytes) -> bool:
    """Returns True if the byte payload begins with the AERAM export magic header."""
    return data.startswith(AERAM_EXPORT_MAGIC)


def export_accounts_encrypted(
    accounts: List[RobloxAccount],
    password: str,
    output_path: Path,
) -> Path:
    """
    Exports a list of RobloxAccount objects into an authenticated, encrypted archive.
    """
    if not password:
        raise ValueError("A secure master password is required to encrypt exported accounts.")

    payload = []
    for acc in accounts:
        payload.append({
            "account_id": acc.account_id,
            "username": acc.username,
            "display_name": acc.display_name,
            "user_id": acc.user_id,
            "cookie": acc.cookie,
            "password": acc.password or "",
            "avatar_url": acc.avatar_url,
            "robux_balance": acc.robux_balance,
            "pending_robux": acc.pending_robux,
            "join_privacy": acc.join_privacy or "NoOne",
            "web_display_name": acc.web_display_name or "",
            "group": acc.group,
            "notes": acc.notes,
            "tags": acc.tags,
            "sort_order": acc.sort_order,
            "totp_secret": acc.totp_secret or "",
        })

    raw_json = json.dumps(payload, indent=2).encode("utf-8")
    salt = os.urandom(crypto_pwhash_SALTBYTES)

    derived_key = derive_key_argon2(
        password=password,
        salt=salt,
        opslimit=3,
        memlimit=67108864,
        alg=crypto_pwhash_ALG_ARGON2ID13,
    )

    encrypted_blob = secretbox_encrypt(derived_key, raw_json)
    # File layout: MAGIC + SALT (16 bytes) + ENCRYPTED_BLOB (nonce 24 bytes + ciphertext)
    archive_bytes = AERAM_EXPORT_MAGIC + salt + encrypted_blob

    output_path = Path(output_path)
    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_bytes(archive_bytes)
    try:
        os.chmod(tmp_path, 0o600)
    except Exception:
        pass
    tmp_path.replace(output_path)
    return output_path


def decrypt_aeram_export(data: bytes, password: str) -> List[Dict[str, Any]]:
    """
    Decrypts an AERAM encrypted export archive using the master password.
    Returns the parsed list of account attribute dictionaries.
    """
    if not is_aeram_export(data):
        raise ValueError("File is not a valid AERAM encrypted export archive.")

    header_len = len(AERAM_EXPORT_MAGIC)
    salt_end = header_len + crypto_pwhash_SALTBYTES
    if len(data) <= salt_end + crypto_secretbox_NONCEBYTES:
        raise VaultCorruptError("Encrypted export archive is truncated or corrupt.")

    salt = data[header_len:salt_end]
    blob = data[salt_end:]

    derived_key = derive_key_argon2(
        password=password,
        salt=salt,
        opslimit=3,
        memlimit=67108864,
        alg=crypto_pwhash_ALG_ARGON2ID13,
    )

    try:
        decrypted_json = secretbox_decrypt(derived_key, blob)
        accounts_data = json.loads(decrypted_json.decode("utf-8"))
        if not isinstance(accounts_data, list):
            raise VaultCorruptError("Invalid account format in decrypted archive.")
        return accounts_data
    except (WrongPasswordError, VaultCorruptError):
        raise
    except Exception as e:
        raise WrongPasswordError(f"Authentication failed or incorrect password: {e}")
