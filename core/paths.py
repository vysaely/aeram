"""
Locates AERAM's data directory (~/.local/share/aeram).
"""

import sqlite3
from pathlib import Path


def _has_accounts(base_dir: Path) -> bool:
    """True when base_dir/manager.db exists and contains at least one account (read-only check)."""
    db_path = base_dir / "manager.db"
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            return conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def default_base_dir() -> Path:
    # Installs from before the AERAM rename keep their accounts in ~/.local/share/ram-linux.
    legacy = Path.home() / ".local" / "share" / "ram-linux"
    aeram_dir = Path.home() / ".local" / "share" / "aeram"
    # Prefer whichever directory actually holds accounts: an empty aeram/ directory (created by
    # a repair step or a first launch) must not hide an existing legacy ram-linux vault.
    for candidate in (aeram_dir, legacy):
        if _has_accounts(candidate):
            return candidate
    if aeram_dir.exists():
        return aeram_dir
    if legacy.exists():
        return legacy
    return aeram_dir
