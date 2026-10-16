"""
Linux Browser Helper for AERAM.
Provides launch capabilities for external web browsers (Firefox, Chromium, Chrome, Brave)
with injected Roblox credentials and fallback support for systems without WebEngine.
"""

import os
import sys
import time
import shutil
import sqlite3
import tempfile
import logging
import threading
import subprocess
import atexit
import glob
from typing import Optional, Callable, Set

log = logging.getLogger("BrowserHelper")

_ACTIVE_TEMP_PROFILES: Set[str] = set()
_PROFILE_LOCK = threading.Lock()


def _register_temp_profile(path: str) -> None:
    with _PROFILE_LOCK:
        _ACTIVE_TEMP_PROFILES.add(path)


def _unregister_temp_profile(path: str) -> None:
    with _PROFILE_LOCK:
        _ACTIVE_TEMP_PROFILES.discard(path)


def _cleanup_all_temp_profiles() -> None:
    with _PROFILE_LOCK:
        to_clean = list(_ACTIVE_TEMP_PROFILES)
        _ACTIVE_TEMP_PROFILES.clear()
    for p in to_clean:
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


atexit.register(_cleanup_all_temp_profiles)


def cleanup_stale_browser_profiles() -> int:
    """Purges leftover aeram_firefox_* and aeram_chrome_* directories in temp location."""
    cleaned = 0
    tmp_base = tempfile.gettempdir()
    for pattern in ("aeram_firefox_*", "aeram_chrome_*"):
        for p in glob.glob(os.path.join(tmp_base, pattern)):
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass
    return cleaned


# Run initial sweep of stale profiles on module load
try:
    cleanup_stale_browser_profiles()
except Exception:
    pass


def get_default_browser_command() -> Optional[str]:
    """Finds the user's preferred or installed web browser binary on Linux."""
    # Check common binaries
    for cmd in ["google-chrome-stable", "google-chrome", "chromium", "brave-browser", "brave", "firefox"]:
        path = shutil.which(cmd)
        if path:
            return path
    return shutil.which("xdg-open")


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


def launch_external_browser(
    cookie_value: str,
    target_url: str = "https://www.roblox.com/home",
    browser_binary: Optional[str] = None,
    on_cookie_changed: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Launches an external Linux browser with an isolated session profile containing
    the account's .ROBLOSECURITY cookie.

    Supports:
    - Firefox: Sets up an ephemeral profile directory with cookies.sqlite.
    - Chromium/Chrome/Brave: Launches with --user-data-dir and temp profile or opens target_url.
    - Fallback: Uses xdg-open if no specific browser profile injector matches.
    """
    if cookie_value:
        sync_mocktail_auth_cookie(cookie_value)
    if not browser_binary:
        browser_binary = get_default_browser_command()

    if not browser_binary:
        log.error("No web browser found on system.")
        return False

    binary_name = os.path.basename(browser_binary).lower()

    # 1. Firefox profile injection
    if "firefox" in binary_name:
        profile_dir = None
        try:
            profile_dir = tempfile.mkdtemp(prefix="aeram_firefox_")
            try:
                os.chmod(profile_dir, 0o700)
            except OSError:
                pass
            _register_temp_profile(profile_dir)
            db_path = os.path.join(profile_dir, "cookies.sqlite")
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("PRAGMA user_version = 17;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS moz_cookies (
                    id INTEGER PRIMARY KEY,
                    originAttributes TEXT NOT NULL DEFAULT '',
                    name TEXT,
                    value TEXT,
                    host TEXT,
                    path TEXT,
                    expiry INTEGER,
                    lastAccessed INTEGER,
                    creationTime INTEGER,
                    isSecure INTEGER,
                    isHttpOnly INTEGER,
                    inBrowserElement INTEGER DEFAULT 0,
                    sameSite INTEGER DEFAULT 0,
                    schemeMap INTEGER DEFAULT 0,
                    isPartitionedAttributeSet INTEGER DEFAULT 0,
                    updateTime INTEGER,
                    CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes)
                )
                """
            )
            now_us = int(time.time() * 1000000)
            # Modern Firefox moz_cookies schema (PRAGMA user_version 17) stores expiry in milliseconds
            expiry_ms = int((time.time() + 86400 * 365) * 1000)
            from core.storage import clean_cookie
            clean_cookie_val = clean_cookie(cookie_value) if cookie_value else ""

            if clean_cookie_val:
                cur.execute(
                    """
                    INSERT INTO moz_cookies (
                        originAttributes, name, value, host, path, expiry, lastAccessed, creationTime,
                        isSecure, isHttpOnly, inBrowserElement, sameSite, schemeMap, isPartitionedAttributeSet, updateTime
                    ) VALUES ('', ?, ?, ?, ?, ?, ?, ?, 1, 1, 0, 0, 2, 0, ?)
                    """,
                    (".ROBLOSECURITY", clean_cookie_val, ".roblox.com", "/", expiry_ms, now_us, now_us, now_us),
                )
            conn.commit()
            conn.close()

            prefs_path = os.path.join(profile_dir, "prefs.js")
            with open(prefs_path, "w") as f:
                f.write('user_pref("browser.shell.checkDefaultBrowser", false);\n')
                f.write('user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n')
                f.write('user_pref("datareporting.policy.dataSubmissionEnabled", false);\n')
                f.write('user_pref("browser.startup.homepage_override.mstone", "ignore");\n')
                f.write('user_pref("browser.aboutwelcome.enabled", false);\n')
                f.write('user_pref("trailhead.firstrun.branches", "nofirstrun-empty");\n')
                f.write('user_pref("browser.startup.page", 1);\n')
                f.write('user_pref("browser.newtabpage.enabled", false);\n')
                f.write('user_pref("browser.uitour.enabled", false);\n')
                f.write('user_pref("startup.homepage_welcome_url", "");\n')
                f.write('user_pref("startup.homepage_welcome_url.additional", "");\n')
                f.write('user_pref("browser.sessionstore.resume_from_crash", false);\n')
                # Anti-Arkose stealth: Ensure navigator.webdriver is false and WebGL/canvas are authentic
                f.write('user_pref("dom.webdriver.enabled", false);\n')
                f.write('user_pref("marionette.enabled", false);\n')
                f.write('user_pref("privacy.resistFingerprinting", false);\n')
                f.write('user_pref("webgl.disabled", false);\n')
                f.write('user_pref("layers.acceleration.force-enabled", true);\n')

            proc = subprocess.Popen(
                [browser_binary, "--profile", profile_dir, "--new-instance", target_url],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"Launched Firefox with session profile at {profile_dir}")

            def _watch_firefox_cookies():
                initial_val = clean_cookie_val
                snap_dir = tempfile.mkdtemp(prefix="aeram_snap_")
                snap_db = os.path.join(snap_dir, "cookies.sqlite")
                snap_wal = snap_db + "-wal"
                wal_path = db_path + "-wal"

                try:
                    for _ in range(600):  # Poll up to 10 minutes
                        time.sleep(1.0)
                        if not os.path.exists(db_path):
                            if proc.poll() is not None:
                                break
                            continue

                        if on_cookie_changed:
                            try:
                                # Snapshot database files to avoid SQLite WAL locking while Firefox is running
                                shutil.copy2(db_path, snap_db)
                                if os.path.exists(wal_path):
                                    shutil.copy2(wal_path, snap_wal)
                                elif os.path.exists(snap_wal):
                                    try:
                                        os.remove(snap_wal)
                                    except OSError:
                                        pass

                                w_conn = sqlite3.connect(snap_db, timeout=1.0)
                                w_cur = w_conn.cursor()
                                w_cur.execute(
                                    "SELECT value FROM moz_cookies WHERE (name='.ROBLOSECURITY' OR name='ROBLOSECURITY') AND host LIKE '%roblox.com' ORDER BY lastAccessed DESC LIMIT 1;"
                                )
                                row = w_cur.fetchone()
                                w_conn.close()

                                if row and row[0]:
                                    current_val = clean_cookie(row[0])
                                    if current_val and current_val != initial_val:
                                        log.info("Captured new .ROBLOSECURITY session cookie from Firefox session.")
                                        sync_mocktail_auth_cookie(current_val)
                                        on_cookie_changed(current_val)
                                        break
                            except Exception as ex:
                                log.debug(f"Snapshot read error: {ex}")

                        # If Firefox has closed, perform one final check on the actual database and exit
                        if proc.poll() is not None:
                            if on_cookie_changed:
                                try:
                                    w_conn = sqlite3.connect(db_path, timeout=2.0)
                                    w_cur = w_conn.cursor()
                                    w_cur.execute(
                                        "SELECT value FROM moz_cookies WHERE (name='.ROBLOSECURITY' OR name='ROBLOSECURITY') AND host LIKE '%roblox.com' ORDER BY lastAccessed DESC LIMIT 1;"
                                    )
                                    row = w_cur.fetchone()
                                    w_conn.close()
                                    if row and row[0]:
                                        current_val = clean_cookie(row[0])
                                        if current_val and current_val != initial_val:
                                            log.info("Captured new .ROBLOSECURITY session cookie from Firefox on exit.")
                                            sync_mocktail_auth_cookie(current_val)
                                            on_cookie_changed(current_val)
                                except Exception:
                                    pass
                            break
                finally:
                    shutil.rmtree(snap_dir, ignore_errors=True)
                    if profile_dir:
                        shutil.rmtree(profile_dir, ignore_errors=True)
                        _unregister_temp_profile(profile_dir)

            threading.Thread(target=_watch_firefox_cookies, daemon=True).start()

            return True
        except Exception as e:
            if profile_dir:
                shutil.rmtree(profile_dir, ignore_errors=True)
                _unregister_temp_profile(profile_dir)
            log.error(f"Failed to launch Firefox with injected profile: {e}")

    # 2. Chromium / Chrome / Brave profile launch
    if any(c in binary_name for c in ["chrome", "chromium", "brave"]):
        profile_dir = None
        try:
            profile_dir = tempfile.mkdtemp(prefix="aeram_chrome_")
            try:
                os.chmod(profile_dir, 0o700)
            except OSError:
                pass
            _register_temp_profile(profile_dir)
            cmd = [
                browser_binary,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--password-store=basic",
                target_url,
            ]
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"Launched Chromium-based browser with data dir {profile_dir}")

            def _watch_chromium_exit():
                try:
                    proc.wait()
                except Exception:
                    pass
                finally:
                    if profile_dir:
                        shutil.rmtree(profile_dir, ignore_errors=True)
                        _unregister_temp_profile(profile_dir)

            threading.Thread(target=_watch_chromium_exit, daemon=True).start()
            return True
        except Exception as e:
            if profile_dir:
                shutil.rmtree(profile_dir, ignore_errors=True)
                _unregister_temp_profile(profile_dir)
            log.error(f"Failed to launch Chromium browser: {e}")

    # 3. Generic fallback
    try:
        subprocess.Popen(
            [browser_binary, target_url],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        log.error(f"Failed to launch browser via {browser_binary}: {e}")
        return False
