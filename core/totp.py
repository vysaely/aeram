"""
RFC 6238 TOTP (Time-Based One-Time Password) Generator and Google Authenticator
migration QR code / URI parser for AERAM.
Pure Python standard library implementation with zero external dependencies.
"""

import time
import hmac
import hashlib
import struct
import base64
import urllib.parse
import subprocess
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Tuple

log = logging.getLogger("AERAM.TOTP")


def generate_totp(secret: str, interval: int = 30, digits: int = 6, for_time: Optional[float] = None) -> str:
    """
    Generates a standard RFC 6238 TOTP 6-digit code from a Base32 secret key.
    Compatible with Google Authenticator, Microsoft Authenticator, and Roblox 2SV.
    """
    if not secret:
        raise ValueError("TOTP secret key cannot be empty")

    secret_clean = secret.replace(" ", "").replace("-", "").upper()
    # Normalize base32 padding
    missing_padding = len(secret_clean) % 8
    if missing_padding:
        secret_clean += "=" * (8 - missing_padding)

    try:
        key = base64.b32decode(secret_clean, casefold=True)
    except Exception as e:
        raise ValueError(f"Invalid Base32 secret key: {e}")

    t = int(time.time() if for_time is None else for_time)
    counter = t // interval
    msg = struct.pack(">Q", counter)

    # Standard HMAC-SHA1
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[19] & 0x0F
    code_int = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)


def get_totp_remaining_seconds(interval: int = 30) -> int:
    """Returns the remaining seconds before the current TOTP time-step expires."""
    return interval - int(time.time()) % interval


def _parse_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Decodes a Protobuf variable-length integer (varint) starting at pos."""
    res = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, pos


def _parse_protobuf_fields(data: bytes) -> List[Tuple[int, int, Any]]:
    """
    Lightweight, robust Protobuf wire format decoder without external dependencies.
    Returns list of (field_number, wire_type, value).
    """
    fields = []
    pos = 0
    total = len(data)
    while pos < total:
        tag, pos = _parse_varint(data, pos)
        wire_type = tag & 0x7
        field_num = tag >> 3

        if wire_type == 0:  # Varint
            val, pos = _parse_varint(data, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:  # Length-delimited (string, bytes, embedded msg)
            length, pos = _parse_varint(data, pos)
            if pos + length > total:
                break
            val = data[pos:pos + length]
            pos += length
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:  # 64-bit
            val = data[pos:pos + 8]
            pos += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 5:  # 32-bit
            val = data[pos:pos + 4]
            pos += 4
            fields.append((field_num, wire_type, val))
        else:
            # Unknown wire type, cannot proceed safely
            break
    return fields


def parse_otpauth_migration(uri_or_payload: str) -> List[Dict[str, Any]]:
    """
    Parses Google Authenticator's 'otpauth-migration://offline?data=...' export payload.
    Extracts all accounts, issuers, and Base32 secrets.
    """
    raw = uri_or_payload.strip()
    data_b64 = ""

    if raw.startswith("otpauth-migration:"):
        parsed_url = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed_url.query)
        data_list = query.get("data", [])
        if data_list:
            data_b64 = data_list[0]
    else:
        data_b64 = raw

    if not data_b64:
        return []

    # Handle URL unquoting and base64 padding
    data_b64 = urllib.parse.unquote(data_b64)
    missing_pad = len(data_b64) % 4
    if missing_pad:
        data_b64 += "=" * (4 - missing_pad)

    try:
        raw_bytes = base64.b64decode(data_b64)
    except Exception as e:
        log.warning(f"Base64 decode failed for migration data: {e}")
        return []

    results = []
    top_fields = _parse_protobuf_fields(raw_bytes)

    for fnum, wtype, fval in top_fields:
        if fnum == 1 and wtype == 2:  # OtpParameters repeated message
            param_fields = _parse_protobuf_fields(fval)
            entry: Dict[str, Any] = {
                "name": "",
                "issuer": "",
                "secret_b32": "",
                "type": "TOTP",
                "digits": 6,
            }
            for pfnum, pwtype, pfval in param_fields:
                if pfnum == 1:  # secret bytes
                    entry["secret_b32"] = base64.b32encode(pfval).decode("utf-8").rstrip("=")
                elif pfnum == 2:  # name (e.g. Roblox:Username)
                    entry["name"] = pfval.decode("utf-8", errors="replace")
                elif pfnum == 3:  # issuer (e.g. Roblox)
                    entry["issuer"] = pfval.decode("utf-8", errors="replace")
                elif pfnum == 5:  # digits
                    entry["digits"] = 6 if pfval == 1 else (8 if pfval == 2 else 6)
                elif pfnum == 6:  # type
                    entry["type"] = "TOTP" if pfval == 2 else "HOTP"

            if entry.get("secret_b32"):
                # Clean up display name
                clean_name = entry["name"]
                if ":" in clean_name:
                    clean_name = clean_name.split(":", 1)[1]
                entry["clean_account_name"] = clean_name.strip()
                results.append(entry)

    return results


def parse_otpauth_uri(uri: str) -> Optional[Dict[str, Any]]:
    """Parses standard otpauth://totp/Label?secret=... URI."""
    if not uri.startswith("otpauth://"):
        return None

    try:
        parsed = urllib.parse.urlparse(uri)
        query = urllib.parse.parse_qs(parsed.query)
        secret_list = query.get("secret", [])
        if not secret_list:
            return None

        secret = secret_list[0].strip()
        issuer = query.get("issuer", [""])[0]
        label = urllib.parse.unquote(parsed.path.lstrip("/"))
        clean_name = label
        if ":" in label:
            parts = label.split(":", 1)
            if not issuer:
                issuer = parts[0]
            clean_name = parts[1]

        digits = int(query.get("digits", [6])[0])

        return {
            "name": label,
            "issuer": issuer,
            "secret_b32": secret.replace(" ", "").upper(),
            "clean_account_name": clean_name.strip(),
            "digits": digits,
            "type": "TOTP",
        }
    except Exception as e:
        log.warning(f"Failed to parse otpauth URI: {e}")
        return None


def parse_universal_2fa_input(text: str) -> List[Dict[str, Any]]:
    """
    Accepts any 2FA input:
    1. otpauth-migration:// (Google Authenticator multi-account export)
    2. otpauth://totp/... (Single authenticator URI)
    3. Raw Base32 secret string (e.g. JBSWY3DPEHPK3PXP)
    Returns list of parsed account dictionary records.
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    # 1. Google Authenticator Migration
    if "otpauth-migration:" in cleaned or cleaned.startswith("data="):
        results = parse_otpauth_migration(cleaned)
        if results:
            return results

    # 2. Standard otpauth:// URI
    if cleaned.startswith("otpauth://"):
        single = parse_otpauth_uri(cleaned)
        if single:
            return [single]

    # 3. Check for multiple lines of otpauth:// or secrets
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    if len(lines) > 1:
        aggregated = []
        for line in lines:
            sub = parse_universal_2fa_input(line)
            if sub:
                aggregated.extend(sub)
        if aggregated:
            return aggregated

    # 4. Raw Base32 secret
    # Valid Base32 characters: A-Z, 2-7
    base32_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=")
    candidate = cleaned.replace(" ", "").replace("-", "").upper()
    if len(candidate) >= 8 and set(candidate).issubset(base32_chars):
        try:
            # Test if generate_totp succeeds
            generate_totp(candidate)
            return [{
                "name": "Manual Key",
                "issuer": "Roblox",
                "secret_b32": candidate,
                "clean_account_name": "",
                "digits": 6,
                "type": "TOTP",
            }]
        except Exception:
            pass

    return []


def extract_qr_payloads(raw_text: str) -> List[str]:
    """
    Extracts all individual OTP URIs or tokens from raw decoded QR text.
    Handles multiple QR codes concatenated by scanner output.
    """
    if not raw_text:
        return []

    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    payloads = []
    current = ""
    for line in lines:
        if line.startswith("otpauth://") or line.startswith("otpauth-migration://"):
            if current:
                payloads.append(current)
            current = line
        else:
            if current:
                current += line
            else:
                current = line
    if current:
        payloads.append(current)
    return payloads if payloads else [raw_text.strip()]


def decode_qr_image_files(image_paths: List[Union[str, Path]]) -> List[str]:
    """
    Decodes QR code(s) from one or more image files using zbarimg.
    Returns a list of all decoded string payloads found across all images.
    """
    all_payloads: List[str] = []
    for image_path in image_paths:
        path = Path(image_path)
        if not path.exists():
            continue
        try:
            res = subprocess.run(
                ["zbarimg", "-q", "--raw", str(path.resolve())],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0 and res.stdout.strip():
                extracted = extract_qr_payloads(res.stdout)
                all_payloads.extend(extracted)
        except Exception as e:
            log.debug(f"zbarimg execution error for {path}: {e}")
    return all_payloads


def decode_qr_image_file(image_path: Union[str, Path]) -> Optional[str]:
    """
    Decodes QR code from a single image file using zbarimg.
    Returns the first decoded string payload or None if not found.
    """
    payloads = decode_qr_image_files([image_path])
    return payloads[0] if payloads else None
