"""
Storage manager: SQLite database persistence, authenticated vault integration,
and isolated Flatpak container filesystem provisioning with asset deduplication.
"""

import os
import time
import json
import sqlite3
import shutil
import subprocess
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any, Generator, Set, Union

from .models import RobloxAccount, FavoriteGame
from .vault import Vault
from .platform import get_platform

log = logging.getLogger("StorageManager")

DEFAULT_BASE_DIR = get_platform().get_default_base_dir()
DEFAULT_SOBER_SYSTEM_VAR = Path.home() / ".var" / "app" / "org.vinegarhq.Sober"
DEFAULT_SHARED_MOCKTAIL = Path.home() / ".local" / "share" / "mocktail"

# Frame cap for alt accounts when the user has not chosen one in Settings.
DEFAULT_ALT_FPS = "6"


def get_default_mocktail_bin() -> Path:
    try:
        from .mocktail_manager import MocktailManager
        info = MocktailManager().detect()
        if info.is_installed and info.bin_path:
            return info.bin_path
    except Exception:
        pass
    return Path.home() / ".local" / "lib" / "mocktail" / "mocktail"


def clean_cookie(raw: str) -> str:
    """Extracts and normalizes a .ROBLOSECURITY token."""
    raw = raw.strip()
    if ".ROBLOSECURITY=" in raw:
        parts = raw.split(".ROBLOSECURITY=")
        cookie_val = parts[1].split(";")[0].strip()
        return cookie_val.strip('"').strip("'")
    return raw.strip('"').strip("'")


class StorageManager:
    def __init__(
        self,
        base_dir: Optional[Path] = None,
        sober_var: Optional[Path] = None,
        mocktail_data: Optional[Path] = None,
        mocktail_bin: Optional[Path] = None,
    ):
        self.base_dir = Path(base_dir or DEFAULT_BASE_DIR)
        self.instances_dir = self.base_dir / "instances"
        self.db_path = self.base_dir / "manager.db"
        self.key_path = self.base_dir / "vault.key"
        self.sober_system_var = Path(sober_var or DEFAULT_SOBER_SYSTEM_VAR)
        self.mocktail_data = Path(mocktail_data or DEFAULT_SHARED_MOCKTAIL)
        self.mocktail_bin = Path(mocktail_bin or get_default_mocktail_bin())

        # Ensure root directories exist with restricted permissions
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
            os.chmod(self.instances_dir, 0o700)
        except OSError:
            pass

        self.vault = Vault(self.key_path)
        self._settings_cache: Dict[str, str] = {}
        self._mains_cache: Set[str] = set()
        self._mains_mtime: float = -1.0
        self._init_db()

        # Ensure default mains.txt exists
        mains_file = self.base_dir / "mains.txt"
        if not mains_file.exists():
            try:
                mains_file.write_text(
                    "# List of main accounts: full frame rate and graphics quality.\n"
                    "# All other accounts run as low-power alts (capped FPS, compact 800x600, muted).\n"
                    "# Add one username per line below:\n"
                    "# YourMainUsername\n",
                    encoding="utf-8"
                )
            except Exception as e:
                log.warning(f"Failed to create default mains.txt: {e}")

    def _get_mains_set(self) -> Set[str]:
        """Returns the set of main usernames, caching by mtime with debouncing to eliminate repeated disk I/O."""
        now = time.monotonic()
        if (now - getattr(self, "_mains_last_checked", 0.0)) < 3.0 and self._mains_cache:
            return self._mains_cache
        self._mains_last_checked = now

        mains_file = self.base_dir / "mains.txt"
        try:
            mtime = mains_file.stat().st_mtime
            if mtime != self._mains_mtime:
                self._mains_cache = {
                    line.strip().lower()
                    for line in mains_file.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                }
                self._mains_mtime = mtime
        except Exception:
            pass
        return self._mains_cache

    def is_main_account(self, account: Any) -> bool:
        """Determines if an account is a main account (full FPS, high fidelity) or alt (capped FPS, ultra-low resource)."""
        if isinstance(account, str):
            acc_obj = self.get_account(account)
            if not acc_obj:
                return account.lower() in self._get_mains_set()
            account = acc_obj

        tags_lower = [t.lower() for t in (account.tags or [])]
        if "main" in tags_lower or "165fps" in tags_lower or "320fps" in tags_lower or "330fps" in tags_lower:
            return True
        if "alt" in tags_lower or "24fps" in tags_lower:
            return False

        names = self._get_mains_set()
        if account.username.lower() in names:
            return True

        return False

    def get_account_target_fps(self, account: Any) -> int:
        """Returns the specific configured target FPS for an account, falling back to role defaults."""
        if isinstance(account, str):
            account = self.get_account(account) or self.find_account_by_username(account)
        if not account:
            try:
                return int(self.get_setting("main_target_fps", "330"))
            except (ValueError, TypeError):
                return 330

        # Check explicit fps tags e.g. "fps:330", "fps:240", "fps:165", "fps:60"
        for tag in (account.tags or []):
            if tag.lower().startswith("fps:"):
                try:
                    return int(tag.split(":", 1)[1])
                except ValueError:
                    pass

        if self.is_main_account(account):
            try:
                return int(self.get_setting("main_target_fps", "330"))
            except (ValueError, TypeError):
                return 330
        else:
            try:
                return int(self.get_setting("alt_fps", DEFAULT_ALT_FPS))
            except (ValueError, TypeError):
                return int(DEFAULT_ALT_FPS)

    def set_account_target_fps(self, account_id: str, fps: int):
        """Sets an explicit target FPS tag (e.g. 'fps:330', 'fps:60') on an account."""
        acc = self.get_account(account_id)
        if not acc:
            return
        tags = [t for t in (acc.tags or []) if not t.lower().startswith("fps:")]
        tags.append(f"fps:{fps}")
        acc.tags = tags
        self.save_account(acc)

    def set_account_profile_mode(self, account_id: str, mode: str):
        """Sets an account profile mode ('main' or 'alt') and updates tags and mains.txt."""
        acc = self.get_account(account_id)
        if not acc:
            return

        tags = [t for t in (acc.tags or []) if t.lower() not in ("main", "alt", "165fps", "320fps", "330fps", "24fps")]
        if mode == "main":
            tags.append("main")
        else:
            tags.append("alt")
        acc.tags = tags
        self.save_account(acc)

        # Update mains.txt
        mains_file = self.base_dir / "mains.txt"
        current_mains = set()
        if mains_file.exists():
            try:
                current_mains = {line.strip() for line in mains_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")}
            except Exception:
                pass

        username_exact = acc.username
        username_lower = acc.username.lower()

        if mode == "main":
            if not any(m.lower() == username_lower for m in current_mains):
                current_mains.add(username_exact)
        else:
            current_mains = {m for m in current_mains if m.lower() != username_lower}

        try:
            mains_file.write_text(
                "# List of main accounts: full frame rate and graphics quality.\n"
                "# All other accounts run as low-power alts (capped FPS, compact 800x600, muted).\n"
                + "\n".join(sorted(current_mains)) + "\n",
                encoding="utf-8"
            )
            self._mains_mtime = -1.0
        except Exception as e:
            log.warning(f"Failed to update mains.txt: {e}")

        try:
            self._provision_engine_settings(self.get_runtime_dir(acc), acc)
        except Exception:
            pass

    @contextmanager
    def _db(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            with conn:
                yield conn.cursor()
        finally:
            conn.close()

    def _reload_settings_cache(self) -> None:
        """Loads all persisted key-value settings into memory to avoid repeated SQLite queries."""
        try:
            with self._db() as cursor:
                cursor.execute("SELECT key, value FROM settings")
                self._settings_cache = {row[0]: row[1] for row in cursor.fetchall()}
        except Exception as e:
            log.warning(f"Failed to load settings cache: {e}")

    def _init_db(self):
        """Initializes SQLite schema and sets file permissions."""
        with self._db() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    user_id INTEGER,
                    encrypted_cookie BLOB NOT NULL,
                    avatar_url TEXT,
                    robux_balance INTEGER DEFAULT 0,
                    group_name TEXT DEFAULT 'Default',
                    notes TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    created_at REAL,
                    last_played REAL DEFAULT 0.0,
                    sort_order INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    place_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    universe_id INTEGER,
                    icon_url TEXT,
                    private_server_link TEXT,
                    notes TEXT DEFAULT '',
                    created_at REAL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # Ensure sort_order, encrypted_password, encrypted_totp_secret, pending_robux, and join_privacy columns exist on existing databases
            cursor.execute("PRAGMA table_info(accounts)")
            columns = [c[1] for c in cursor.fetchall()]
            if "sort_order" not in columns:
                cursor.execute("ALTER TABLE accounts ADD COLUMN sort_order INTEGER DEFAULT 0")
            if "encrypted_password" not in columns:
                cursor.execute("ALTER TABLE accounts ADD COLUMN encrypted_password BLOB")
            if "encrypted_totp_secret" not in columns:
                cursor.execute("ALTER TABLE accounts ADD COLUMN encrypted_totp_secret BLOB")
            if "pending_robux" not in columns:
                cursor.execute("ALTER TABLE accounts ADD COLUMN pending_robux INTEGER DEFAULT 0")
            if "join_privacy" not in columns:
                cursor.execute("ALTER TABLE accounts ADD COLUMN join_privacy TEXT DEFAULT 'Unknown'")

        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)

        self._reload_settings_cache()

    # --- Account Methods ---

    def save_account(self, account: RobloxAccount) -> None:
        """Encrypts cookie, password, and TOTP secret and saves/updates account record in SQLite."""
        tags_json = json.dumps(account.tags)
        clean_tok = clean_cookie(account.cookie)
        encrypted_cookie = self.vault.encrypt_string(clean_tok)
        encrypted_password = self.vault.encrypt_string(account.password.strip()) if account.password else None
        encrypted_totp = self.vault.encrypt_string(account.totp_secret.strip()) if account.totp_secret else None

        with self._db() as cursor:
            cursor.execute("SELECT sort_order, encrypted_password, encrypted_totp_secret FROM accounts WHERE account_id = ?", (account.account_id,))
            row = cursor.fetchone()
            if row is not None:
                sort_order = account.sort_order if account.sort_order != 0 else (row[0] or 0)
                # Keep existing password if new one not provided
                if encrypted_password is None and row[1] is not None:
                    encrypted_password = row[1]
                # Keep existing TOTP secret if new one not provided
                if encrypted_totp is None and len(row) > 2 and row[2] is not None:
                    encrypted_totp = row[2]
            else:
                cursor.execute("SELECT MAX(sort_order) FROM accounts")
                max_val = cursor.fetchone()[0]
                sort_order = account.sort_order if account.sort_order != 0 else ((max_val or 0) + 1)

            account.sort_order = sort_order

            cursor.execute("""
                INSERT INTO accounts (
                    account_id, username, display_name, user_id, encrypted_cookie,
                    avatar_url, robux_balance, group_name, notes, tags, created_at, last_played, sort_order,
                    encrypted_password, encrypted_totp_secret, pending_robux, join_privacy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    username=excluded.username,
                    display_name=excluded.display_name,
                    user_id=excluded.user_id,
                    encrypted_cookie=excluded.encrypted_cookie,
                    avatar_url=excluded.avatar_url,
                    robux_balance=excluded.robux_balance,
                    group_name=excluded.group_name,
                    notes=excluded.notes,
                    tags=excluded.tags,
                    last_played=excluded.last_played,
                    sort_order=excluded.sort_order,
                    encrypted_password=COALESCE(excluded.encrypted_password, accounts.encrypted_password),
                    encrypted_totp_secret=COALESCE(excluded.encrypted_totp_secret, accounts.encrypted_totp_secret),
                    pending_robux=excluded.pending_robux,
                    join_privacy=excluded.join_privacy
            """, (
                account.account_id, account.username, account.display_name,
                account.user_id, encrypted_cookie, account.avatar_url,
                account.robux_balance, account.group, account.notes, tags_json,
                account.created_at, account.last_played, sort_order,
                encrypted_password, encrypted_totp,
                account.pending_robux, account.join_privacy
            ))

        # Prepare isolated sandbox directory structure
        self.init_profile_sandbox(account)

    def save_accounts_batch(self, accounts: List[RobloxAccount]) -> None:
        """Encrypts and persists multiple accounts within a single SQLite transaction."""
        if not accounts:
            return

        with self._db() as cursor:
            cursor.execute("SELECT MAX(sort_order) FROM accounts")
            max_val = cursor.fetchone()[0] or 0

            for account in accounts:
                tags_json = json.dumps(account.tags)
                clean_tok = clean_cookie(account.cookie)
                encrypted_cookie = self.vault.encrypt_string(clean_tok)
                encrypted_password = self.vault.encrypt_string(account.password.strip()) if account.password else None
                encrypted_totp = self.vault.encrypt_string(account.totp_secret.strip()) if account.totp_secret else None

                cursor.execute(
                    "SELECT sort_order, encrypted_password, encrypted_totp_secret FROM accounts WHERE account_id = ?",
                    (account.account_id,)
                )
                row = cursor.fetchone()
                if row is not None:
                    sort_order = account.sort_order if account.sort_order != 0 else (row[0] or 0)
                    if encrypted_password is None and row[1] is not None:
                        encrypted_password = row[1]
                    if encrypted_totp is None and len(row) > 2 and row[2] is not None:
                        encrypted_totp = row[2]
                else:
                    max_val += 1
                    sort_order = account.sort_order if account.sort_order != 0 else max_val

                account.sort_order = sort_order

                cursor.execute("""
                    INSERT INTO accounts (
                        account_id, username, display_name, user_id, encrypted_cookie,
                        avatar_url, robux_balance, group_name, notes, tags, created_at, last_played, sort_order,
                        encrypted_password, encrypted_totp_secret, pending_robux, join_privacy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        username=excluded.username,
                        display_name=excluded.display_name,
                        user_id=excluded.user_id,
                        encrypted_cookie=excluded.encrypted_cookie,
                        avatar_url=excluded.avatar_url,
                        robux_balance=excluded.robux_balance,
                        group_name=excluded.group_name,
                        notes=excluded.notes,
                        tags=excluded.tags,
                        last_played=excluded.last_played,
                        sort_order=excluded.sort_order,
                        encrypted_password=COALESCE(excluded.encrypted_password, accounts.encrypted_password),
                        encrypted_totp_secret=COALESCE(excluded.encrypted_totp_secret, accounts.encrypted_totp_secret),
                        pending_robux=excluded.pending_robux,
                        join_privacy=excluded.join_privacy
                """, (
                    account.account_id, account.username, account.display_name,
                    account.user_id, encrypted_cookie, account.avatar_url,
                    account.robux_balance, account.group, account.notes, tags_json,
                    account.created_at, account.last_played, sort_order,
                    encrypted_password, encrypted_totp,
                    account.pending_robux, account.join_privacy
                ))

        for account in accounts:
            self.init_profile_sandbox(account)

    def get_account(self, account_id: str) -> Optional[RobloxAccount]:
        """Retrieves and decrypts an account by ID."""
        with self._db() as cursor:
            cursor.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_account(row)
        return None

    def clear_account_password(self, account_id: str) -> bool:
        """Removes the saved encrypted password for an account without touching cookies or profile data."""
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET encrypted_password = NULL WHERE account_id = ?", (account_id,))
            return cursor.rowcount > 0

    def purge_all_passwords(self) -> int:
        """Purges all encrypted passwords across all accounts in the vault (cookie-only privacy mode)."""
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET encrypted_password = NULL WHERE encrypted_password IS NOT NULL")
            return cursor.rowcount

    def find_account_by_username(self, username: str) -> Optional[RobloxAccount]:
        """Finds an account by case-insensitive username."""
        wanted = username.strip().lower()
        with self._db() as cursor:
            cursor.execute("SELECT * FROM accounts WHERE LOWER(username) = ?", (wanted,))
            row = cursor.fetchone()
            if row:
                return self._row_to_account(row)
        return None

    def list_accounts(self) -> List[RobloxAccount]:
        """Lists all stored accounts ordered by custom sort order."""
        accounts = []
        with self._db() as cursor:
            cursor.execute("SELECT * FROM accounts ORDER BY sort_order ASC, rowid ASC")
            for row in cursor.fetchall():
                acc = self._row_to_account(row)
                if acc:
                    accounts.append(acc)
        return accounts

    # Backward compatibility alias
    load_accounts = list_accounts

    def reorder_accounts(self, account_ids: List[str]) -> None:
        """Persists custom order of accounts using batch SQLite execution."""
        with self._db() as cursor:
            cursor.executemany(
                "UPDATE accounts SET sort_order = ? WHERE account_id = ?",
                [(idx, aid) for idx, aid in enumerate(account_ids)]
            )

    def delete_account(self, account_id: str) -> None:
        """Deletes an account from the database and removes its isolated container directory."""
        self.cleanup_runtime_cookie(account_id)
        acc = self.get_account(account_id)
        with self._db() as cursor:
            cursor.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))

        if acc:
            candidate_dirs = {self.base_dir / "bin", self.mocktail_bin.parent}
            try:
                from .mocktail_manager import MocktailManager
                info = MocktailManager(self.mocktail_bin).detect()
                if info.lib_dir and info.lib_dir.exists():
                    candidate_dirs.add(info.lib_dir)
            except Exception:
                pass

            for cand_dir in candidate_dirs:
                inst_bin = cand_dir / f"mocktail_{acc.account_id}"
                if inst_bin.exists():
                    try:
                        inst_bin.unlink(missing_ok=True)
                    except Exception:
                        pass

            profile_dir = self.get_profile_dir(acc)
            if profile_dir.exists():
                try:
                    shutil.rmtree(profile_dir)
                    log.info(f"Cleaned up profile directory: {profile_dir}")
                except Exception as e:
                    log.error(f"Failed to remove profile directory: {e}")

    def update_last_played(self, account_id: str, timestamp: float) -> None:
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET last_played = ? WHERE account_id = ?", (timestamp, account_id))

    def update_metadata(self, account_id: str, display_name: str, avatar_url: str, robux_balance: int) -> None:
        with self._db() as cursor:
            cursor.execute("""
                UPDATE accounts SET display_name = ?, avatar_url = ?, robux_balance = ?
                WHERE account_id = ?
            """, (display_name, avatar_url, robux_balance, account_id))

    def update_metadata_preserve_alias(self, account_id: str, display_name: str, avatar_url: str, robux_balance: int) -> None:
        """Updates avatar and Robux balance while preserving user's custom alias."""
        with self._db() as cursor:
            cursor.execute("""
                UPDATE accounts SET
                    avatar_url = ?,
                    robux_balance = ?,
                    display_name = CASE WHEN (display_name IS NULL OR TRIM(display_name) = '') THEN ? ELSE display_name END
                WHERE account_id = ?
            """, (avatar_url, robux_balance, display_name, account_id))

    def update_profile_assets(self, account_id: str, avatar_url: str, robux_balance: int) -> None:
        """Updates only avatar thumbnail and Robux balance, preserving custom alias."""
        with self._db() as cursor:
            cursor.execute("""
                UPDATE accounts SET avatar_url = ?, robux_balance = ?
                WHERE account_id = ?
            """, (avatar_url, robux_balance, account_id))

    def update_alias(self, account_id: str, alias: str) -> None:
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET display_name = ? WHERE account_id = ?", (alias, account_id))

    def update_notes(self, account_id: str, notes: str) -> None:
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET notes = ? WHERE account_id = ?", (notes, account_id))

    def update_group(self, account_id: str, group_name: str) -> None:
        clean_group = group_name.strip() or "Default"
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET group_name = ? WHERE account_id = ?", (clean_group, account_id))

    def update_accounts_group(self, account_ids: List[str], group_name: str) -> None:
        if not account_ids:
            return
        clean_group = group_name.strip() or "Default"
        placeholders = ",".join("?" for _ in account_ids)
        with self._db() as cursor:
            cursor.execute(
                f"UPDATE accounts SET group_name = ? WHERE account_id IN ({placeholders})",
                [clean_group, *account_ids]
            )

    def update_currency(self, account_id: str, robux: int, pending_robux: int = 0) -> None:
        with self._db() as cursor:
            cursor.execute(
                "UPDATE accounts SET robux_balance = ?, pending_robux = ? WHERE account_id = ?",
                (robux, pending_robux, account_id)
            )

    def update_join_privacy(self, account_id: str, join_privacy: str) -> None:
        with self._db() as cursor:
            cursor.execute(
                "UPDATE accounts SET join_privacy = ? WHERE account_id = ?",
                (join_privacy, account_id)
            )

    def get_group_order(self) -> List[str]:
        val = self.get_setting("group_order", "")
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return []

    def set_group_order(self, group_names: List[str]) -> None:
        self.set_setting("group_order", json.dumps(group_names))

    def list_groups(self) -> List[str]:
        with self._db() as cursor:
            cursor.execute("SELECT DISTINCT group_name FROM accounts")
            rows = cursor.fetchall()
            account_groups = set(r[0] for r in rows if r[0])

        custom_raw = self.get_setting("custom_groups", "[]")
        try:
            custom_groups = set(json.loads(custom_raw))
        except Exception:
            custom_groups = set()

        all_groups = account_groups.union(custom_groups)
        if not all_groups:
            all_groups = {"Default"}

        saved_order = self.get_group_order()
        ordered = [g for g in saved_order if g in all_groups]
        remaining = sorted(all_groups - set(ordered))
        result = ordered + remaining
        return result if result else ["Default"]

    def add_custom_group(self, group_name: str) -> None:
        clean = group_name.strip()
        if not clean:
            return
        groups = self.list_groups()
        if clean not in groups:
            groups.append(clean)
            self.set_group_order(groups)
            custom_raw = self.get_setting("custom_groups", "[]")
            try:
                custom = set(json.loads(custom_raw))
            except Exception:
                custom = set()
            custom.add(clean)
            self.set_setting("custom_groups", json.dumps(list(custom)))

    def rename_group(self, old_name: str, new_name: str) -> None:
        clean_new = new_name.strip()
        if not clean_new or clean_new == old_name:
            return
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET group_name = ? WHERE group_name = ?", (clean_new, old_name))
        custom_raw = self.get_setting("custom_groups", "[]")
        try:
            custom = json.loads(custom_raw)
            if old_name in custom:
                custom.remove(old_name)
                custom.append(clean_new)
                self.set_setting("custom_groups", json.dumps(custom))
        except Exception:
            pass
        groups = self.list_groups()
        new_order = [clean_new if g == old_name else g for g in groups]
        self.set_group_order(new_order)

    def delete_group(self, group_name: str, fallback_group: str = "Default") -> None:
        with self._db() as cursor:
            cursor.execute("UPDATE accounts SET group_name = ? WHERE group_name = ?", (fallback_group, group_name))
        custom_raw = self.get_setting("custom_groups", "[]")
        try:
            custom = json.loads(custom_raw)
            if group_name in custom:
                custom.remove(group_name)
                self.set_setting("custom_groups", json.dumps(custom))
        except Exception:
            pass
        groups = [g for g in self.list_groups() if g != group_name]
        self.set_group_order(groups)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if key in self._settings_cache:
            return self._settings_cache[key]
        return default

    def set_setting(self, key: str, value: str) -> None:
        self._settings_cache[key] = str(value)
        with self._db() as cursor:
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, str(value)))

    def _row_to_account(self, row) -> Optional[RobloxAccount]:
        tags = []
        try:
            tags = json.loads(row[9]) if row[9] else []
        except Exception:
            pass

        # Decrypt cookie
        try:
            decrypted_cookie = self.vault.decrypt_string(row[4])
        except Exception as e:
            log.warning(f"Failed to decrypt cookie for account {row[1]}: {e}")
            decrypted_cookie = ""

        sort_order = row[12] if len(row) > 12 and row[12] is not None else 0

        # Decrypt password if present
        decrypted_password = ""
        if len(row) > 13 and row[13]:
            try:
                decrypted_password = self.vault.decrypt_string(row[13])
            except Exception as e:
                log.warning(f"Failed to decrypt password for account {row[1]}: {e}")
                decrypted_password = ""

        # Decrypt TOTP secret if present
        decrypted_totp = ""
        if len(row) > 14 and row[14]:
            try:
                decrypted_totp = self.vault.decrypt_string(row[14])
            except Exception as e:
                log.warning(f"Failed to decrypt totp secret for account {row[1]}: {e}")
                decrypted_totp = ""

        pending_robux = int(row[15]) if len(row) > 15 and row[15] is not None else 0
        join_privacy = str(row[16]) if len(row) > 16 and row[16] is not None else "Unknown"

        return RobloxAccount(
            account_id=row[0],
            username=row[1],
            display_name=row[2],
            user_id=row[3],
            cookie=decrypted_cookie,
            password=decrypted_password,
            avatar_url=row[5] or "",
            robux_balance=row[6] or 0,
            group=row[7] or "Default",
            notes=row[8] or "",
            tags=tags,
            created_at=row[10] or 0.0,
            last_played=row[11] or 0.0,
            sort_order=sort_order,
            totp_secret=decrypted_totp,
            pending_robux=pending_robux,
            join_privacy=join_privacy,
        )

    # --- Mocktail Sandbox Filesystem Management ---

    def get_profile_dir(self, account: RobloxAccount) -> Path:
        """Returns the isolated home directory for the account."""
        return self.instances_dir / account.profile_dir_name

    def ensure_mocktail_companion_libraries(self, target_dir: Optional[Path] = None) -> Optional[Path]:
        """
        Ensures all Mocktail companion shared objects (*.so*) and helper binaries from the
        Mocktail library directory are properly mirrored/symlinked into target_dir so that
        the ELF dynamic linker ($ORIGIN) can locate libmocktail_audio_sdl.so,
        libmocktail_mouse_helper.so, etc.
        """
        lib_dir = None
        try:
            from .mocktail_manager import MocktailManager
            info = MocktailManager(self.mocktail_bin).detect()
            if info.lib_dir and info.lib_dir.exists():
                lib_dir = info.lib_dir
        except Exception:
            pass

        if not lib_dir or not lib_dir.exists():
            candidates = [
                self.mocktail_bin.parent,
                Path.home() / ".local" / "lib" / "mocktail",
                Path("/usr/lib/mocktail"),
                Path("/usr/local/lib/mocktail"),
            ]
            for c in candidates:
                if (c / "libmocktail_audio_sdl.so").exists():
                    lib_dir = c
                    break
            if not lib_dir and (Path.home() / ".local" / "lib" / "mocktail").exists():
                lib_dir = Path.home() / ".local" / "lib" / "mocktail"

        if not lib_dir or not lib_dir.exists():
            return None

        dest_dir = target_dir or (self.base_dir / "bin")
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            if dest_dir.resolve() != lib_dir.resolve():
                for item in lib_dir.iterdir():
                    if item.name.startswith("mocktail_acc_"):
                        continue
                    if ".so" in item.name or item.name.startswith("mocktail_"):
                        dest_item = dest_dir / item.name
                        if dest_item.is_symlink() and not dest_item.exists():
                            dest_item.unlink(missing_ok=True)
                        if not dest_item.exists():
                            try:
                                target = item.resolve()
                                if target.exists():
                                    dest_item.symlink_to(target)
                            except Exception:
                                pass
        except Exception as e:
            log.warning(f"Failed to mirror companion shared libraries to {dest_dir}: {e}")

        return lib_dir

    def get_instance_binary(self, account: RobloxAccount) -> Path:
        """
        Returns a dedicated per-account binary in the Mocktail library directory.
        By hardlinking mocktail_<account_id> alongside the companion shared libraries,
        Mocktail's /proc/self/exe resolution derives a unique FNV-1a lock key while
        guaranteeing that all relative companion libraries (libmocktail_audio_sdl.so,
        libvulkan, libandroid, etc.) are located immediately.
        """
        source_bin = self.mocktail_bin
        lib_dir = None
        try:
            from .mocktail_manager import MocktailManager
            info = MocktailManager(self.mocktail_bin).detect()
            if info.real_bin_path and info.real_bin_path.exists():
                source_bin = info.real_bin_path
            if info.lib_dir and info.lib_dir.exists():
                lib_dir = info.lib_dir
        except Exception:
            pass

        if not lib_dir or not lib_dir.exists():
            candidates = [
                source_bin.parent,
                self.mocktail_bin.parent,
                Path.home() / ".local" / "lib" / "mocktail",
                Path("/usr/lib/mocktail"),
            ]
            for c in candidates:
                if (c / "libmocktail_audio_sdl.so").exists():
                    lib_dir = c
                    break
            if not lib_dir:
                lib_dir = source_bin.parent

        # Try in lib_dir if writable, otherwise use user's runtime bin directory
        target_dir = lib_dir if (lib_dir.exists() and os.access(lib_dir, os.W_OK)) else (self.base_dir / "bin")
        target_dir.mkdir(parents=True, exist_ok=True)

        # If target_dir is outside lib_dir (e.g. system install or non-writable lib_dir),
        # proactively mirror all companion *.so* and helper files so $ORIGIN dynamic linking succeeds.
        if lib_dir.exists() and target_dir.resolve() != lib_dir.resolve():
            self.ensure_mocktail_companion_libraries(target_dir)

        inst_bin = target_dir / f"mocktail_{account.account_id}"

        # Re-provision when inst_bin is an outdated non-ELF wrapper while source_bin is ELF, or when
        # it no longer mirrors source_bin. A Mocktail upgrade that replaces the binary (new inode)
        # leaves earlier hardlinks pointing at the old client, so every account would keep
        # launching the stale version.
        needs_provision = not inst_bin.exists()
        if not needs_provision and source_bin.exists():
            try:
                with open(source_bin, "rb") as sf:
                    source_is_elf = sf.read(4) == b"\x7fELF"
                with open(inst_bin, "rb") as df:
                    inst_is_elf = df.read(4) == b"\x7fELF"
                if source_is_elf and not (inst_is_elf and self._binary_is_current(source_bin, inst_bin)):
                    needs_provision = True
            except Exception:
                pass

        if needs_provision and source_bin.exists():
            # Stage beside the target and rename over it so a running instance keeps its inode
            # and a concurrent launch never observes a missing or partially copied binary.
            staging = inst_bin.with_name(inst_bin.name + ".provision")
            try:
                staging.unlink(missing_ok=True)
                try:
                    os.link(source_bin, staging)
                except OSError:
                    self._copy_or_reflink(source_bin, staging)
                    os.chmod(staging, 0o755)
                os.replace(staging, inst_bin)
            except Exception as e:
                staging.unlink(missing_ok=True)
                log.warning(f"Could not provision isolated mocktail binary: {e}")
                return source_bin

        if inst_bin.exists():
            try:
                os.chmod(inst_bin, 0o755)
            except Exception:
                pass
            return inst_bin

        return source_bin

    @staticmethod
    def _binary_is_current(source: Path, dest: Path) -> bool:
        """True when dest is a hardlink of source, or a copy with identical size and mtime."""
        try:
            if os.path.samefile(source, dest):
                return True
            src_st, dst_st = source.stat(), dest.stat()
            return src_st.st_size == dst_st.st_size and int(src_st.st_mtime) == int(dst_st.st_mtime)
        except OSError:
            return False

    def get_effective_fflags(self, is_alt: bool, texture_quality: int = 1, frm_quality: int = 1) -> dict:
        """Constructs effective FastFlags dictionary combining safety baselines and user overrides."""
        if is_alt:
            ff = {
                "FStringRenderTextureBudgetByRam": "",
                "DFFlagTextureQualityOverrideEnabled": True,
                "DFIntTextureQualityOverride": 1,
                "DFIntDebugFRMQualityLevelOverride": 1,
                "FFlagDebugDisableDRS": True,
                "FFlagAutomaticDRS": False,
                "FFlagAutomaticDRSDisableHighDPIFramebufferHalving": True,
                "DFFlagSkipHighResolutiontextureMipsOnLowMemoryDevices2": True,
                "DFFlagEnableRequestAsyncCompression": False,
                "DFIntCanHideGuiGroupId": 32380007,
            }
        else:
            ff = {
                "FStringRenderTextureBudgetByRam": "",
                "DFFlagTextureQualityOverrideEnabled": True,
                "DFIntTextureQualityOverride": texture_quality,
                "DFIntDebugFRMQualityLevelOverride": frm_quality,
                "FFlagDebugDisableDRS": True,
                "FFlagAutomaticDRS": False,
                "FFlagAutomaticDRSDisableHighDPIFramebufferHalving": True,
                "FFlagDebugGraphicsPreferOpenGL": True,
                "DFFlagSkipHighResolutiontextureMipsOnLowMemoryDevices2": False,
                "DFFlagEnableRequestAsyncCompression": False,
                "DFIntCanHideGuiGroupId": 32380007,
            }

        # Apply user graphical/engine preferences
        api_pref = self.get_setting("fflag_graphics_api", "Auto")
        if api_pref == "Vulkan":
            ff["FFlagDebugGraphicsPreferVulkan"] = True
            ff.pop("FFlagDebugGraphicsPreferOpenGL", None)
        elif api_pref == "OpenGL":
            ff["FFlagDebugGraphicsPreferOpenGL"] = True
            ff.pop("FFlagDebugGraphicsPreferVulkan", None)

        if self.get_setting("fflag_disable_drs", "true").lower() == "true":
            ff["FFlagDebugDisableDRS"] = True
            ff["FFlagAutomaticDRS"] = False

        if self.get_setting("fflag_disable_telemetry", "true").lower() == "true":
            ff["FFlagDebugDisableCrashReporting"] = True
            ff["FStringRobloxAnalyticsURL"] = ""

        # Graphical quality overrides from videogame settings menu
        tex_pref = self.get_setting("fflag_texture_quality", "")
        if tex_pref:
            try:
                ff["DFIntTextureQualityOverride"] = int(tex_pref)
            except ValueError:
                pass

        gfx_lvl = self.get_setting("fflag_graphics_level", "")
        if gfx_lvl:
            try:
                lvl = int(gfx_lvl)
                ff["DFIntDebugFRMQualityLevelOverride"] = lvl
                ff["FIntRomarkStartWithGraphicQualityLevel"] = lvl
            except ValueError:
                pass

        shadows = self.get_setting("fflag_shadows", "")
        if shadows == "off":
            ff["FFlagDebugGraphicsDisableShadows"] = True
            ff["DFFlagDebugPauseVoxelizer"] = True
        elif shadows == "low":
            ff["FFlagDebugGraphicsDisableShadows"] = False
            ff["DFFlagDebugPauseVoxelizer"] = True
        elif shadows == "full":
            ff["FFlagDebugGraphicsDisableShadows"] = False
            ff["DFFlagDebugPauseVoxelizer"] = False

        postfx = self.get_setting("fflag_postfx", "")
        if postfx == "off":
            ff["FFlagDisablePostFx"] = True
        elif postfx == "on":
            ff["FFlagDisablePostFx"] = False

        particles = self.get_setting("fflag_particles", "")
        if particles == "disabled":
            ff["FFlagDebugDisableParticleEmitters"] = True
        elif particles in ("reduced", "full"):
            ff["FFlagDebugDisableParticleEmitters"] = False

        msaa = self.get_setting("fflag_msaa", "")
        if msaa:
            try:
                ff["FIntDebugForceMSAASamples"] = int(msaa)
            except ValueError:
                pass

        # Merge custom JSON flags if provided (for backwards compatibility)
        custom_raw = self.get_setting("custom_fastflags_json", "")
        if custom_raw.strip():
            try:
                parsed = json.loads(custom_raw)
                if isinstance(parsed, dict):
                    # AERAM's FPS setting reaches Mocktail as frame_rate_limit, and Mocktail
                    # refuses to start when a FastFlag sets these to anything else.
                    for owned in ("DFIntTaskSchedulerTargetFps", "FFlagGameBasicSettingsFramerateCap5"):
                        if parsed.pop(owned, None) is not None:
                            log.warning(f"Ignoring custom {owned}: set the FPS cap in AERAM's settings instead")
                    ff.update(parsed)
            except Exception as e:
                log.warning(f"Failed to parse custom_fastflags_json: {e}")

        return ff

    def init_profile_sandbox(self, account: RobloxAccount, write_cookie: bool = False) -> Path:
        """
        Sets up the isolated profile filesystem for native Mocktail:
        <profile_dir>/
          ├── bin/mocktail (dedicated hardlinked binary for instance lock decoupling)
          ├── data/mocktail/
          │     ├── payloads -> (symlink to shared payload storage)
          │     └── current.json
          ├── config/mocktail/config.yaml (mode 0600, automatic updates disabled)
          ├── cache/mocktail/
          ├── state/mocktail/
          └── roblox.cookie (mode 0600, written only on active launch)
        """
        profile_dir = self.get_profile_dir(account)
        data_dir = profile_dir / "data" / "mocktail"
        config_dir = profile_dir / "config" / "mocktail"
        cache_dir = profile_dir / "cache" / "mocktail"
        state_dir = profile_dir / "state" / "mocktail"
        bin_dir = profile_dir / "bin"

        for d in (profile_dir, data_dir, config_dir, cache_dir, state_dir, bin_dir):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass

        # 1. Provision dedicated binary hardlink to decouple single-instance FNV-1a lock (Linux)
        self.get_instance_binary(account)

        # 2. Write the cookie file securely with mode 0600 ONLY if requested for active launch
        if write_cookie and account.cookie:
            self.write_runtime_cookie(account)

        def _link_directory(src: Path, dest: Path) -> None:
            """Creates a directory symlink."""
            if not src.exists() or dest.exists():
                return
            try:
                dest.symlink_to(src, target_is_directory=True)
            except Exception:
                pass


        # 3. Link master payloads and active payload manifest
        payloads_dest = data_dir / "payloads"
        shared_payloads = self.mocktail_data / "payloads"
        if shared_payloads.exists() and not payloads_dest.exists():
            _link_directory(shared_payloads, payloads_dest)

        shared_current = self.mocktail_data / "current.json"
        for cur_dest in (
            profile_dir / "data" / "current.json",
            profile_dir / "data" / "mocktail" / "current.json",
        ):
            if shared_current.exists():
                try:
                    cur_dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(shared_current, cur_dest)
                except Exception as e:
                    log.warning(f"Could not sync payload manifest {cur_dest}: {e}")

        # Ensure direct profile_dir / data also has valid symlinks
        direct_data = profile_dir / "data"
        direct_payloads = direct_data / "payloads"
        if direct_payloads.is_dir() and not direct_payloads.is_symlink():
            try:
                direct_payloads.rmdir()
            except Exception:
                pass
        if shared_payloads.exists() and not direct_payloads.exists():
            _link_directory(shared_payloads, direct_payloads)

        # 3b. Link shared content asset cache (rbx-storage) to eliminate redundant downloads and CDN HTTP 429
        shared_rbx = self.base_dir / "shared_rbx_storage"
        shared_rbx.mkdir(mode=0o700, parents=True, exist_ok=True)
        for r_dest in (data_dir / "rbx-storage", profile_dir / "data" / "rbx-storage"):
            if r_dest.exists() and not r_dest.is_symlink():
                try:
                    for item in r_dest.iterdir():
                        target = shared_rbx / item.name
                        if not target.exists():
                            shutil.move(str(item), str(target))
                    shutil.rmtree(r_dest, ignore_errors=True)
                except Exception as e:
                    log.warning(f"Failed to migrate rbx-storage to shared cache: {e}")
            if not r_dest.exists() and not r_dest.is_symlink():
                _link_directory(shared_rbx, r_dest)

        # 3c. Link shared Mesa shader cache to eliminate redundant GPU shader compiling
        shared_shaders = self.base_dir / "shared_mesa_shader_cache"
        shared_shaders.mkdir(mode=0o700, parents=True, exist_ok=True)
        for s_dest in (cache_dir / "mesa_shader_cache", profile_dir / "cache" / "mesa_shader_cache"):
            if s_dest.exists() and not s_dest.is_symlink():
                try:
                    for item in s_dest.iterdir():
                        target = shared_shaders / item.name
                        if not target.exists():
                            shutil.move(str(item), str(target))
                    shutil.rmtree(s_dest, ignore_errors=True)
                except Exception as e:
                    log.warning(f"Failed to migrate shader cache to shared cache: {e}")
            if not s_dest.exists() and not s_dest.is_symlink():
                _link_directory(shared_shaders, s_dest)

        is_main = self.is_main_account(account)
        is_alt = not is_main
        target_fps = self.get_account_target_fps(account)
        is_unlimited = (target_fps >= 1000 or target_fps <= 0)
        fps_limit_str = "1000" if is_unlimited else str(target_fps)
        frm_quality = 21 if is_main else 1
        texture_quality = 3 if is_main else 1
        win_w = 1920 if is_main else 800
        win_h = 1080 if is_main else 600

        gfx_backend = self.get_setting("fflag_graphics_api", "Auto").lower()
        if gfx_backend not in ("opengl", "vulkan"):
            gfx_backend = "opengl"

        # 4. Write isolated configuration (dynamic FPS and physics mode per account role)
        config_content = (
            "version: 1\n"
            "device: pc-windows-11\n"
            "graphics:\n"
            f"  backend: {gfx_backend}\n"
            f"  frame_rate_limit: {fps_limit_str}\n"
            "  vsync: off\n"
            "performance:\n"
            "  multithreaded_rendering: true\n"
            f"  physics_worker_mode: {'throughput' if is_main else 'auto'}\n"
            # Alts are deliberately low-priority and confined to their own cores; GameMode would
            # boost them and may re-pin their threads, so only the main requests it.
            f"  gamemode: {'auto' if is_main else 'off'}\n"
            "window:\n"
            f"  width: {win_w}\n"
            f"  height: {win_h}\n"
            "  high_dpi: false\n"
            "updates:\n"
            "  automatic: false\n"
        )
        for c_file in (
            config_dir / "config.yaml",
            config_dir / "mocktail" / "config.yaml",
            profile_dir / "config" / "config.yaml",
            profile_dir / "config" / "mocktail" / "config.yaml",
        ):
            c_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            c_file.write_text(config_content, encoding="utf-8")
            c_file.chmod(0o600)

        # 5. Enforce window state (fullscreen for main, windowed 800x600 for alts)
        window_state_content = json.dumps({
            "fullscreen": False if is_alt else True,
            "maximized": False,
            "schema_version": 1,
            "windowed": {
                "has_position": False,
                "height": win_h,
                "width": win_w
            }
        })
        for ws_file in (
            state_dir / "window-state.json",
            profile_dir / "state" / "window-state.json",
            profile_dir / "state" / "mocktail" / "window-state.json",
        ):
            ws_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ws_file.write_text(window_state_content, encoding="utf-8")
            ws_file.chmod(0o600)

        # 6. Write crisp desktop FFlags overrides (strictly compliant with Roblox official safety policy)
        fflags_dict = self.get_effective_fflags(is_alt=is_alt, texture_quality=texture_quality, frm_quality=frm_quality)
        fflags_content = json.dumps(fflags_dict, indent=2)
        for ff_file in (
            config_dir / "fflags.json",
            config_dir / "mocktail" / "fflags.json",
            profile_dir / "config" / "fflags.json",
            profile_dir / "config" / "mocktail" / "fflags.json",
            data_dir / "fflags.json",
            data_dir / "mocktail" / "fflags.json",
            profile_dir / "data" / "fflags.json",
        ):
            ff_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ff_file.write_text(fflags_content, encoding="utf-8")
            ff_file.chmod(0o600)

        # 7. Provision decoupled per-instance XDG_RUNTIME_DIR and engine settings
        run_dir = self._setup_isolated_runtime_dir(account)
        self._preprovision_android_cache(account)
        self._provision_engine_settings(run_dir, account)

        return profile_dir

    def write_runtime_cookie(self, account: RobloxAccount) -> List[Path]:
        """
        Writes the decrypted authentication cookie to the account's runtime sandbox
        with mode 0600 immediately prior to process launch.
        Validates target paths to ensure writes remain strictly within the account's profile directory.
        """
        if not account.cookie:
            return []

        profile_dir = self.get_profile_dir(account).resolve()
        instances_dir = self.instances_dir.resolve()

        try:
            profile_dir.relative_to(instances_dir)
        except ValueError:
            log.error(f"Refusing to write runtime cookie: {profile_dir} is outside {instances_dir}")
            return []

        data_dir = (profile_dir / "data" / "mocktail").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        cleaned = clean_cookie(account.cookie)
        if not cleaned:
            return []
        cookie_line = cleaned if cleaned.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={cleaned}"

        written_paths = []
        for c_path in (profile_dir / "roblox.cookie", data_dir / "roblox.cookie"):
            try:
                c_path.resolve().relative_to(profile_dir)
            except ValueError:
                continue
            tmp_cookies = c_path.with_suffix(".tmp")
            try:
                fd = os.open(tmp_cookies, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with open(fd, "w", encoding="utf-8") as f:
                    f.write(cookie_line + "\n")
                os.replace(tmp_cookies, c_path)
                try:
                    os.chmod(c_path, 0o600)
                except OSError:
                    pass
                written_paths.append(c_path)
            except Exception as e:
                log.warning(f"Could not write runtime cookie to {c_path}: {e}")

        return written_paths

    def cleanup_runtime_cookie(self, account_or_id: Union[RobloxAccount, str]) -> int:
        """
        Safely removes plaintext runtime cookie files for an account.
        Validates paths to prevent traversal outside the designated instance profile.
        Idempotent and safe to call multiple times.
        """
        if isinstance(account_or_id, RobloxAccount):
            account = account_or_id
        else:
            account = self.get_account(account_or_id)

        if account:
            profile_dir = self.get_profile_dir(account)
        else:
            clean_id = str(account_or_id).strip()
            if not clean_id or "/" in clean_id or "\\" in clean_id or ".." in clean_id:
                return 0
            matches = list(self.instances_dir.glob(f"*_{clean_id[:8]}"))
            if matches:
                profile_dir = matches[0]
            else:
                profile_dir = self.instances_dir / clean_id

        if not profile_dir.exists():
            return 0

        profile_dir_resolved = profile_dir.resolve()
        instances_dir_resolved = self.instances_dir.resolve()
        try:
            profile_dir_resolved.relative_to(instances_dir_resolved)
        except ValueError:
            log.error(f"Refusing to cleanup cookie: {profile_dir_resolved} is outside {instances_dir_resolved}")
            return 0

        target_candidates = [
            profile_dir_resolved / "roblox.cookie",
            profile_dir_resolved / "roblox.cookie.tmp",
            profile_dir_resolved / "data" / "mocktail" / "roblox.cookie",
            profile_dir_resolved / "data" / "mocktail" / "roblox.cookie.tmp",
        ]

        cleaned_count = 0
        for candidate in target_candidates:
            if candidate.is_symlink():
                try:
                    candidate.unlink(missing_ok=True)
                    cleaned_count += 1
                except Exception:
                    pass
                continue
            if candidate.exists() and candidate.is_file():
                try:
                    candidate.unlink(missing_ok=True)
                    cleaned_count += 1
                except Exception as e:
                    log.debug(f"Failed to unlink {candidate}: {e}")

        return cleaned_count

    def cleanup_stale_runtime_cookies(self, running_account_ids: Optional[Set[str]] = None) -> int:
        """
        Startup and recovery scanner: removes leftover plaintext cookie files for any account
        that is NOT currently executing in an active instance.
        """
        if running_account_ids is None:
            running_account_ids = set()

        if not self.instances_dir.exists():
            return 0

        instances_dir_resolved = self.instances_dir.resolve()
        cleaned_total = 0

        try:
            for entry in self.instances_dir.iterdir():
                if not entry.is_dir() or entry.is_symlink():
                    continue
                try:
                    entry.resolve().relative_to(instances_dir_resolved)
                except ValueError:
                    continue

                folder_name = entry.name
                is_running = False
                for aid in running_account_ids:
                    if folder_name.endswith(f"_{aid[:8]}") or folder_name == aid:
                        is_running = True
                        break

                if not is_running:
                    for cookie_name in ("roblox.cookie", "roblox.cookie.tmp"):
                        for cand in (entry / cookie_name, entry / "data" / "mocktail" / cookie_name):
                            if cand.exists() or cand.is_symlink():
                                try:
                                    cand.unlink(missing_ok=True)
                                    cleaned_total += 1
                                except Exception:
                                    pass
        except Exception as e:
            log.warning(f"Error during stale cookie recovery: {e}")

        return cleaned_total

    def get_runtime_dir(self, account: RobloxAccount) -> Path:
        """
        Returns the decoupled per-instance runtime directory.
        Prefers /run/user/<uid>/ram-linux/<account_id> (tmpfs) on Linux to guarantee
        that UNIX domain socket paths never exceed the 108-byte kernel limit
        (UNIX_PATH_MAX). Falls back to <profile_dir>/run.
        """
        my_uid = os.getuid() if hasattr(os, "getuid") else 1000
        host_xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{my_uid}"
        base = Path(host_xdg)
        if base.exists() and os.access(base, os.W_OK):
            target = base / "ram-linux" / account.account_id
        else:
            target = self.get_profile_dir(account) / "run"
        return target

    def _setup_isolated_runtime_dir(self, account: RobloxAccount) -> Path:
        """
        Provisions a decoupled XDG_RUNTIME_DIR for the instance container on Linux.
        """
        run_dir = self.get_runtime_dir(account)
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass

        # Clean stale mocktail runtime locks if present
        for lock_file in run_dir.glob("**/*.lock"):
            try:
                lock_file.unlink()
            except Exception:
                pass

        my_uid = os.getuid() if hasattr(os, "getuid") else 1000
        host_xdg = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{my_uid}")
        wayland_disp = os.environ.get("WAYLAND_DISPLAY", "wayland-0")

        sockets_to_link = ["bus", "systemd", "pulse", "pipewire-0", "pipewire-0-manager", "wayland-0", wayland_disp]
        for name in set(sockets_to_link):
            if not name:
                continue
            src = host_xdg / name
            dest = run_dir / name
            if dest.is_symlink() and not dest.exists():
                try:
                    dest.unlink()
                except Exception:
                    pass
            if src.exists() and not dest.exists():
                try:
                    dest.symlink_to(src)
                except Exception as e:
                    log.warning(f"Could not symlink host socket {name} into sandbox: {e}")

        self._provision_engine_settings(run_dir, account)
        return run_dir

    ENGINE_PRESET_MARKER = ".aeram-engine-preset"

    def engine_preset_generation(self) -> int:
        try:
            return int(self.get_setting("engine_preset_generation", "0") or 0)
        except (ValueError, TypeError):
            return 0

    def request_engine_preset(self, graphics_level: int, mouse_sensitivity: float) -> int:
        """Stores an in-game settings preset and marks it for every account's next launch.

        The preset is applied once per request: afterwards Roblox's own in-game settings
        (graphics slider, sensitivity) belong to the player until the preset is applied again.
        Returns the new preset generation.
        """
        self.set_setting("graphics_quality_level", str(max(1, min(10, int(graphics_level)))))
        self.set_setting("mouse_sensitivity", f"{float(mouse_sensitivity):.2f}")
        generation = self.engine_preset_generation() + 1
        self.set_setting("engine_preset_generation", str(generation))
        return generation

    def _provision_engine_settings(self, run_dir: Path, account: Optional[RobloxAccount] = None):
        """
        Prepares Roblox's GlobalBasicSettings_13.xml before a launch.

        Every launch: FramerateCap follows AERAM's FPS setting; alts stay at graphics level 1
        and muted. The in-game preset (graphics level, camera sensitivity, shift-lock switch)
        is written when a settings file is created and once after each "Apply Preset", so
        changes made in Roblox's own menu persist between launches. Master volume is only
        set when the file is created.
        """
        import re
        is_main = self.is_main_account(account) if account else True
        raw_fps = self.get_account_target_fps(account)
        fps_cap = 10000 if (raw_fps >= 1000 or raw_fps <= 0) else raw_fps
        vol = "1" if is_main else "0"
        try:
            preset_gfx = max(1, min(10, int(self.get_setting("graphics_quality_level", "7"))))
        except (ValueError, TypeError):
            preset_gfx = 7
        gfx_lvl = preset_gfx if is_main else 1
        try:
            sens = float(self.get_setting("mouse_sensitivity", "0.20"))
        except (ValueError, TypeError):
            sens = 0.20

        target_files = [run_dir / "data" / "files" / "appData" / "GlobalBasicSettings_13.xml"]
        marker = None
        if account:
            prof_dir = self.instances_dir / account.profile_dir_name
            marker = prof_dir / self.ENGINE_PRESET_MARKER
            target_files.extend([
                prof_dir / "data" / "mocktail" / "android" / "data" / "files" / "appData" / "GlobalBasicSettings_13.xml",
                prof_dir / "data" / "android" / "data" / "files" / "appData" / "GlobalBasicSettings_13.xml",
                prof_dir / "data" / "files" / "appData" / "GlobalBasicSettings_13.xml",
            ])

        generation = self.engine_preset_generation()
        applied = 0
        if marker is not None:
            try:
                applied = int(marker.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                applied = 0
        apply_preset = applied < generation

        for xml_file in target_files:
            xml_file.parent.mkdir(parents=True, exist_ok=True)
            if not xml_file.exists():
                content = f"""<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">
	<External>null</External>
	<External>nil</External>
	<Item class="UserGameSettings" referent="RBX16e4b21d820447a6adf2f8b7d933dc0b">
		<Properties>
			<token name="ControlMode">1</token>
			<int name="FramerateCap">{fps_cap}</int>
			<bool name="Fullscreen">false</bool>
			<token name="GraphicsOptimizationMode">1</token>
			<int name="GraphicsQualityLevel">{gfx_lvl}</int>
			<float name="MasterVolume">{vol}</float>
			<bool name="MaxQualityEnabled">false</bool>
			<float name="MouseSensitivity">{sens:.6f}</float>
			<Vector2 name="MouseSensitivityFirstPerson">
				<X>{sens:.6f}</X>
				<Y>{sens:.6f}</Y>
			</Vector2>
			<Vector2 name="MouseSensitivityThirdPerson">
				<X>{sens:.6f}</X>
				<Y>{sens:.6f}</Y>
			</Vector2>
			<token name="SavedQualityLevel">{gfx_lvl}</token>
			<string name="Name">GameSettings</string>
		</Properties>
	</Item>
</roblox>"""
                try:
                    xml_file.write_text(content, encoding="utf-8")
                except Exception:
                    pass
                continue
            try:
                txt = xml_file.read_text(encoding="utf-8")
                original = txt
                txt = re.sub(r'<(?:int|token) name="FramerateCap">-?\d+</(?:int|token)>', f'<int name="FramerateCap">{fps_cap}</int>', txt)
                if '<int name="FramerateCap">' not in txt:
                    txt = txt.replace('<Properties>', f'<Properties>\n\t\t\t<int name="FramerateCap">{fps_cap}</int>')
                if apply_preset or not is_main:
                    txt = re.sub(r'<token name="GraphicsOptimizationMode">\d+</token>', '<token name="GraphicsOptimizationMode">1</token>', txt)
                    txt = re.sub(r'<token name="SavedQualityLevel">\d+</token>', f'<token name="SavedQualityLevel">{gfx_lvl}</token>', txt)
                    txt = re.sub(r'<(?:int|token) name="GraphicsQualityLevel">\d+</(?:int|token)>', f'<int name="GraphicsQualityLevel">{gfx_lvl}</int>', txt)
                    txt = re.sub(r'<bool name="MaxQualityEnabled">(?:true|false)</bool>', '<bool name="MaxQualityEnabled">false</bool>', txt)
                if not is_main:
                    txt = re.sub(r'<float name="MasterVolume">[\d\.]+</float>', '<float name="MasterVolume">0</float>', txt)
                if apply_preset:
                    txt = re.sub(r'<token name="ControlMode">\d+</token>', '<token name="ControlMode">1</token>', txt)
                    if '<token name="ControlMode">' not in txt:
                        txt = txt.replace('<Properties>', '<Properties>\n\t\t\t<token name="ControlMode">1</token>')
                    txt = re.sub(r'<float name="MouseSensitivity">[\d\.]+</float>', f'<float name="MouseSensitivity">{sens:.6f}</float>', txt)
                    txt = re.sub(r'<X>[\d\.]+</X>\s*<Y>[\d\.]+</Y>', f'<X>{sens:.6f}</X>\n\t\t\t\t<Y>{sens:.6f}</Y>', txt)
                if txt != original:
                    xml_file.write_text(txt, encoding="utf-8")
            except Exception:
                pass

        if marker is not None and applied != generation:
            try:
                marker.write_text(str(generation), encoding="utf-8")
            except OSError:
                pass

    def _preprovision_android_cache(self, account: RobloxAccount) -> None:
        """
        Pre-provisions Android platform metadata and Desktop app-policy files
        into the instance profile before Mocktail runs. This prevents Mocktail's
        built-in MigratePlatformProfileCaches and ApplyDesktopAppPolicy from
        failing with 'cannot atomically store platform cache metadata' /
        'cannot atomically store desktop app-policy' race conditions during
        concurrent or batch multi-instance startups.
        """
        prof_dir = self.get_profile_dir(account)
        # Clear any stale mocktail runtime or migration lock files
        for lock_file in prof_dir.glob("**/*.lock"):
            try:
                lock_file.unlink()
            except Exception:
                pass

        target_dirs = [
            prof_dir / "data" / "mocktail" / "android" / "data",
            prof_dir / "data" / "android" / "data",
            prof_dir / "data",
        ]

        platform_profile_json = json.dumps({
            "profile_revision": "device-v1-pc-windows-11-t0-m1-k1",
            "schema_version": 1
        }, separators=(',', ':'))

        template_path = Path(__file__).parent / "templates" / "appStorage.template.json"
        template_data = {}
        if template_path.exists():
            try:
                template_data = json.loads(template_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"Failed to load appStorage template: {e}")

        for base_dir in target_dirs:
            base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(base_dir, 0o700)
            except Exception:
                pass

            # 1. Platform profile metadata (.mocktail-platform-profile.json)
            meta_file = base_dir / ".mocktail-platform-profile.json"
            if not meta_file.exists():
                try:
                    meta_file.write_text(platform_profile_json, encoding="utf-8")
                    meta_file.chmod(0o600)
                except Exception:
                    pass

            # 2. LocalStorage appStorage.json (pre-populated with desktop app-policy)
            storage_dir = base_dir / "files" / "appData" / "LocalStorage"
            storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(storage_dir, 0o700)
            except Exception:
                pass

            app_storage_file = storage_dir / "appStorage.json"
            if not app_storage_file.exists() and template_data:
                account_data = dict(template_data)
                account_data["UserId"] = str(account.user_id) if account.user_id else ""
                account_data["Username"] = account.username
                try:
                    tmp = app_storage_file.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(account_data), encoding="utf-8")
                    tmp.chmod(0o600)
                    tmp.replace(app_storage_file)
                except Exception:
                    pass

    def _copy_or_reflink(self, src: Path, dest: Path):
        """
        Clones a directory or file using copy-on-write (reflink) if supported,
        falling back to standard copy. If dest is an existing symlink, removes it first.
        """
        if dest.is_symlink():
            try:
                dest.unlink()
            except Exception:
                pass

        if not dest.exists():
            try:
                ret = subprocess.run(
                    ["cp", "-a", "--reflink=auto", str(src), str(dest)],
                    capture_output=True
                )
                if ret.returncode == 0:
                    return
            except Exception:
                pass

            try:
                if src.is_dir():
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
            except Exception as e:
                log.warning(f"Could not clone {src} to {dest}: {e}")

    # --- Favorites Methods ---

    def list_favorites(self) -> List[FavoriteGame]:
        favorites = []
        with self._db() as cursor:
            cursor.execute("SELECT * FROM favorites ORDER BY name ASC")
            for row in cursor.fetchall():
                favorites.append(FavoriteGame(
                    place_id=row[0],
                    name=row[1],
                    universe_id=row[2],
                    icon_url=row[3],
                    private_server_link=row[4],
                    notes=row[5] or "",
                    created_at=row[6] or 0.0,
                ))
        return favorites

    def save_favorite(self, fav: FavoriteGame):
        with self._db() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO favorites (
                    place_id, name, universe_id, icon_url, private_server_link, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (fav.place_id, fav.name, fav.universe_id, fav.icon_url, fav.private_server_link, fav.notes, fav.created_at))

    def delete_favorite(self, place_id: int):
        with self._db() as cursor:
            cursor.execute("DELETE FROM favorites WHERE place_id = ?", (place_id,))
