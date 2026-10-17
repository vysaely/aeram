"""
Keeps Mocktail's default auth cookie (~/.local/share/mocktail/auth/roblox.cookie) on the
selected account, so launches started from roblox.com use that account.
"""

import os
import sys
import logging

log = logging.getLogger("BrowserHelper")


def sync_mocktail_auth_cookie(cookie_value: str) -> bool:
    """
    Synchronizes the given .ROBLOSECURITY cookie to the default Mocktail auth cookie path
    (~/.local/share/mocktail/auth/roblox.cookie) so that browser-initiated launches
    (clicking Play on roblox.com) authenticate as the active account.
    """
    if not cookie_value or len(cookie_value) < 50:
        return False
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False
    try:
        from core.storage import clean_cookie
        cleaned = clean_cookie(cookie_value)
        if not cleaned:
            return False
        cookie_line = cleaned if cleaned.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={cleaned}"
        target_dir = os.path.expanduser("~/.local/share/mocktail/auth")
        os.makedirs(target_dir, exist_ok=True)
        target_file = os.path.join(target_dir, "roblox.cookie")
        tmp_file = target_file + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp_file, flags, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(cookie_line + "\n")
        os.replace(tmp_file, target_file)
        return True
    except Exception as ex:
        log.debug(f"Failed to sync mocktail auth cookie: {ex}")
        return False
