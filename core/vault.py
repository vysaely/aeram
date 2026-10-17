"""
Secure credential vault using native libsodium authenticated encryption.
Zero plaintext cookie storage; Argon2id KDF and SecretBox (XSalsa20-Poly1305).
Strict 0600 file permissions and atomic writes.
"""

import os
import ctypes
from pathlib import Path
from typing import Optional


# Cryptographic constants (matching libsodium)
crypto_secretbox_KEYBYTES = 32
crypto_secretbox_NONCEBYTES = 24
crypto_secretbox_MACBYTES = 16
crypto_pwhash_SALTBYTES = 16
crypto_pwhash_ALG_ARGON2ID13 = 2
crypto_pwhash_ALG_ARGON2I13 = 1

_sodium = None
_nb = None

# 1. Try loading system libsodium via ctypes (standard on Linux)
for lib_name in ("libsodium.so.26", "libsodium.so", "libsodium.dll"):
    try:
        candidate = ctypes.CDLL(lib_name)
        if candidate.sodium_init() >= 0:
            _sodium = candidate
            break
    except (OSError, AttributeError):
        continue

# 2. If ctypes libsodium is not available (e.g. standard Windows), fallback to PyNaCl bindings
if _sodium is None:
    try:
        import nacl.bindings as _nb
    except ImportError:
        raise RuntimeError(
            "Neither system libsodium nor pynacl is installed. "
            "Please install pynacl (`pip install pynacl`) or libsodium."
        )

if _sodium is not None:
    _sodium.crypto_secretbox_easy.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulonglong, ctypes.c_char_p, ctypes.c_char_p
    ]
    _sodium.crypto_secretbox_easy.restype = ctypes.c_int

    _sodium.crypto_secretbox_open_easy.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulonglong, ctypes.c_char_p, ctypes.c_char_p
    ]
    _sodium.crypto_secretbox_open_easy.restype = ctypes.c_int

    _sodium.crypto_pwhash.argtypes = [
        ctypes.c_char_p, ctypes.c_ulonglong,
        ctypes.c_char_p, ctypes.c_ulonglong,
        ctypes.c_char_p,
        ctypes.c_ulonglong, ctypes.c_size_t,
        ctypes.c_int
    ]
    _sodium.crypto_pwhash.restype = ctypes.c_int




class VaultError(Exception):
    """Base exception for Vault errors."""
    pass


class WrongPasswordError(VaultError):
    """Raised when an incorrect master password is supplied."""
    pass


class VaultCorruptError(VaultError):
    """Raised when vault data is corrupt or cannot be authenticated."""
    pass


def derive_key_argon2(
    password: str,
    salt: bytes,
    opslimit: int = 3,
    memlimit: int = 67108864,
    alg: int = crypto_pwhash_ALG_ARGON2ID13
) -> bytes:
    """Derives a 32-byte encryption key using Argon2 via libsodium or PyNaCl."""
    pw_bytes = password.encode("utf-8")
    if _sodium is not None:
        key = ctypes.create_string_buffer(crypto_secretbox_KEYBYTES)
        ret = _sodium.crypto_pwhash(
            key, crypto_secretbox_KEYBYTES,
            pw_bytes, len(pw_bytes),
            salt, opslimit, memlimit, alg
        )
        if ret != 0:
            raise VaultError("Argon2 key derivation failed")
        return key.raw
    else:
        try:
            return _nb.crypto_pwhash_alg(
                crypto_secretbox_KEYBYTES, pw_bytes, salt, opslimit, memlimit, alg
            )
        except Exception as e:
            raise VaultError(f"Argon2 key derivation failed: {e}")


def secretbox_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Encrypts plaintext with key using SecretBox with a random 24-byte nonce."""
    if len(key) != crypto_secretbox_KEYBYTES:
        raise ValueError("Invalid key length")
    nonce = os.urandom(crypto_secretbox_NONCEBYTES)
    if _sodium is not None:
        ciphertext = ctypes.create_string_buffer(len(plaintext) + crypto_secretbox_MACBYTES)
        ret = _sodium.crypto_secretbox_easy(
            ciphertext, plaintext, len(plaintext), nonce, key
        )
        if ret != 0:
            raise VaultError("Encryption failed")
        return nonce + ciphertext.raw
    else:
        try:
            ciphertext = _nb.crypto_secretbox_easy(plaintext, nonce, key)
            return nonce + ciphertext
        except Exception as e:
            raise VaultError(f"Encryption failed: {e}")


def secretbox_decrypt(key: bytes, blob: bytes) -> bytes:
    """Decrypts ciphertext blob with key using SecretBox. Validates auth tag."""
    if len(key) != crypto_secretbox_KEYBYTES:
        raise ValueError("Invalid key length")
    min_len = crypto_secretbox_NONCEBYTES + crypto_secretbox_MACBYTES
    if len(blob) < min_len:
        raise VaultCorruptError("Ciphertext payload too short")

    nonce = blob[:crypto_secretbox_NONCEBYTES]
    ciphertext = blob[crypto_secretbox_NONCEBYTES:]

    if _sodium is not None:
        plaintext_len = len(ciphertext) - crypto_secretbox_MACBYTES
        plaintext = ctypes.create_string_buffer(plaintext_len)
        ret = _sodium.crypto_secretbox_open_easy(
            plaintext, ciphertext, len(ciphertext), nonce, key
        )
        if ret != 0:
            raise WrongPasswordError("Decryption failed / authentication tag mismatch")
        return plaintext.raw
    else:
        try:
            return _nb.crypto_secretbox_open_easy(ciphertext, nonce, key)
        except Exception:
            raise WrongPasswordError("Decryption failed / authentication tag mismatch")


def _fsync_dir(path: Path) -> None:
    """Persists a directory entry change (best effort; unsupported on some platforms)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Vault:
    """
    Manages encrypted credential persistence using libsodium SecretBox (XSalsa20-Poly1305).
    Credentials are encrypted at rest with a dedicated 256-bit key stored in a protected
    local keyfile with strict POSIX DAC permissions (0700 directory, 0600 file).
    """

    def __init__(self, key_file_path: Path):
        self.key_file_path = Path(key_file_path)
        try:
            self.key_file_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            self.key_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._key: Optional[bytes] = None
        self._init_or_load_key()

    def _init_or_load_key(self):
        """Loads or creates the local default encryption key."""
        if self.key_file_path.exists():
            try:
                try:
                    os.chmod(self.key_file_path, 0o600)
                except OSError:
                    pass
                data = self.key_file_path.read_bytes()
                if len(data) == crypto_secretbox_KEYBYTES:
                    self._key = data
                    return
                raise VaultCorruptError(
                    f"Vault key file at {self.key_file_path} is corrupt (invalid size: {len(data)} bytes; expected {crypto_secretbox_KEYBYTES}). "
                    "Key overwrite aborted to protect existing vault credentials."
                )
            except VaultCorruptError:
                raise
            except Exception as e:
                raise VaultError(f"Failed to read vault key: {e}")

        # Generate new random 256-bit key with strict 0600 permissions. It is published with
        # link(), which fails if another process (e.g. GUI and CLI starting together) created the
        # key first; replacing it would orphan everything already encrypted under that key.
        new_key = os.urandom(crypto_secretbox_KEYBYTES)
        tmp_path = self.key_file_path.with_name(f"{self.key_file_path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new_key)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp_path, self.key_file_path)
            except FileExistsError:
                return self._init_or_load_key()
            except OSError:
                # Filesystem without hard links: fall back to an atomic rename
                os.replace(tmp_path, self.key_file_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        _fsync_dir(self.key_file_path.parent)
        self._key = new_key

    def encrypt_string(self, text: str) -> bytes:
        """Encrypts a UTF-8 string into an authenticated ciphertext blob."""
        if self._key is None:
            raise VaultError("Vault is locked or key not available")
        return secretbox_encrypt(self._key, text.encode("utf-8"))

    def decrypt_string(self, blob: bytes) -> str:
        """Decrypts an authenticated ciphertext blob into a UTF-8 string."""
        if self._key is None:
            raise VaultError("Vault is locked or key not available")
        raw = secretbox_decrypt(self._key, blob)
        return raw.decode("utf-8")
