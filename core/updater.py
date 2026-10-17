"""
Dedicated Application Updater for AERAM.
Provides safe, background update checks and one-click upgrades directly
from the official GitHub repository (https://github.com/vysaely/aeram.git).

Supports both:
1. Git checkouts: updates via fast-forward git pull.
2. Standalone zip installations: downloads GitHub archive and safely merges
   new files while strictly preserving manager.db, vault.key, and instance sandboxes.
"""

import os
import shutil
import logging
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Callable

log = logging.getLogger("AppUpdater")

REPO_URL = "https://github.com/vysaely/aeram.git"
ARCHIVE_URL = "https://github.com/vysaely/aeram/archive/refs/heads/main.zip"


class AppUpdater:
    """Manages update checks and safe atomic application updates from GitHub."""

    def __init__(self, app_root: Optional[Path] = None):
        if app_root:
            self.app_root = Path(app_root).resolve()
        else:
            # Locate root directory containing main.py
            self.app_root = Path(__file__).resolve().parent.parent

    def is_git_repo(self) -> bool:
        """Returns True if the application directory is a valid git repository."""
        return (self.app_root / ".git").is_dir() and shutil.which("git") is not None

    def get_local_commit(self) -> str:
        """Returns the current local commit hash (or version stamp)."""
        if self.is_git_repo():
            try:
                res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self.app_root),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                return res.stdout.strip()
            except Exception:
                pass

        ver_file = self.app_root / ".version"
        if ver_file.exists():
            try:
                return ver_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return "unknown"

    def check_for_updates(self, timeout_sec: int = 8) -> Dict[str, Any]:
        """
        Inspects GitHub remote for the latest commit on main.
        Returns a dictionary with status, shas, and commit message.
        """
        local_sha = self.get_local_commit()
        remote_sha = None
        commit_msg = ""
        behind_count = 0

        # Method 1: Git remote inspection
        if shutil.which("git"):
            try:
                res = subprocess.run(
                    ["git", "ls-remote", REPO_URL, "refs/heads/main"],
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=True,
                )
                parts = res.stdout.strip().split()
                if parts:
                    remote_sha = parts[0]
            except Exception as e:
                log.debug(f"git ls-remote failed: {e}")

        # Method 2: If git ls-remote failed, try GitHub API (works for public repos)
        if not remote_sha:
            try:
                req = urllib.request.Request(
                    "https://api.github.com/repos/vysaely/aeram/commits/main",
                    headers={"User-Agent": "AERAM-Updater/1.0"},
                )
                import json
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    remote_sha = data.get("sha")
                    commit_msg = data.get("commit", {}).get("message", "").splitlines()[0]
            except Exception as e:
                log.debug(f"GitHub API query failed: {e}")

        if not remote_sha:
            return {
                "success": False,
                "error": "Could not connect to GitHub. Please check your network connection.",
                "local_sha": local_sha,
                "update_available": False,
            }

        # Compare commits
        update_available = (local_sha != remote_sha) and (local_sha != "unknown")
        if local_sha == "unknown":
            update_available = True

        if self.is_git_repo() and update_available:
            try:
                subprocess.run(["git", "fetch", "origin", "main", "--quiet"], cwd=str(self.app_root), timeout=8)
                rev_res = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD..origin/main"],
                    cwd=str(self.app_root),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                behind_count = int(rev_res.stdout.strip() or "0")
                log_res = subprocess.run(
                    ["git", "log", "-1", "--pretty=format:%s", "origin/main"],
                    cwd=str(self.app_root),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                commit_msg = log_res.stdout.strip()
            except Exception:
                pass

        return {
            "success": True,
            "update_available": update_available,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "commit_msg": commit_msg,
            "behind_count": behind_count,
            "is_git": self.is_git_repo(),
        }

    def apply_update(self, progress_callback: Optional[Callable[[str, int], None]] = None) -> Tuple[bool, str]:
        """
        Executes update in-place from GitHub, guaranteeing data preservation.
        Returns (success: bool, status_message: str).
        """
        def report(msg: str, pct: int):
            if progress_callback:
                progress_callback(msg, pct)
            log.info(f"[Updater] {msg} ({pct}%)")

        report("Checking update method...", 10)

        # ─── Method A: Git Repository Update ───
        if self.is_git_repo():
            try:
                report("Fetching latest commits from GitHub...", 30)
                subprocess.run(
                    ["git", "fetch", "origin", "main", "--quiet"],
                    cwd=str(self.app_root),
                    check=True,
                    timeout=20,
                )
                report("Applying updates...", 70)
                # Pull with fast-forward; if dirty, stash changes
                pull_res = subprocess.run(
                    ["git", "pull", "--ff-only", "origin", "main"],
                    cwd=str(self.app_root),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if pull_res.returncode != 0:
                    err_detail = (pull_res.stderr or pull_res.stdout or "non-fast-forward or uncommitted local changes").strip()
                    report("Update aborted to preserve local changes.", 100)
                    return False, f"Git fast-forward update declined to preserve local changes: {err_detail}"

                native_dir = self.app_root / "native"
                build_warn = ""
                if (native_dir / "Makefile").exists() and shutil.which("make"):
                    report("Rebuilding native companion library...", 90)
                    make_res = subprocess.run(
                        ["make", "-C", str(native_dir)],
                        cwd=str(self.app_root),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if make_res.returncode != 0:
                        err_out = (make_res.stderr or make_res.stdout or "Compilation failed").strip()
                        log.warning(f"[Updater] Native helper rebuild warning: {err_out}")
                        build_warn = f" (Native helper build note: {err_out[:60]})"
                    else:
                        helper_so = native_dir / "libmocktail_mouse_helper.so"
                        if helper_so.exists():
                            try:
                                dest_helper_dir = Path.home() / ".local" / "lib" / "mocktail"
                                dest_helper_dir.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(helper_so, dest_helper_dir / "libmocktail_mouse_helper.so")
                            except Exception as e:
                                log.debug(f"[Updater] Could not sync helper library: {e}")

                report("Update completed successfully.", 100)
                return True, f"Updated successfully via Git.{build_warn}"
            except Exception as e:
                log.warning(f"Git update failed: {e}")
                return False, f"Git update failed: {e}"

        # ─── Method B: Standalone Archive Download & Merge ───
        try:
            report("Downloading latest release archive from GitHub...", 30)
            with tempfile.TemporaryDirectory() as tmp_dir:
                zip_path = Path(tmp_dir) / "latest.zip"
                extract_path = Path(tmp_dir) / "extracted"

                req = urllib.request.Request(
                    ARCHIVE_URL,
                    headers={"User-Agent": "AERAM-Updater/1.0"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp, open(zip_path, "wb") as out_f:
                    shutil.copyfileobj(resp, out_f)

                report("Extracting archive...", 60)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_path)

                # Find root extracted directory (usually aeram-main)
                contents = list(extract_path.iterdir())
                src_root = contents[0] if len(contents) == 1 and contents[0].is_dir() else extract_path

                # Validate archive integrity: must contain at least core/ or aeram
                if not ((src_root / "core").exists() or (src_root / "aeram").exists()):
                    raise ValueError("Downloaded archive lacks valid AERAM package structure.")

                report("Merging application files (preserving database & credentials)...", 85)

                # Excluded from overwrite: databases, vault secrets, sandboxes, logs, settings
                PRESERVED_NAMES = {
                    "manager.db",
                    "manager.db-wal",
                    "manager.db-shm",
                    "vault.key",
                    "mains.txt",
                    "no_gamescope",
                    "instances",
                    ".git",
                    ".cache",
                    "__pycache__",
                }

                copied_count = 0
                for root, dirs, files in os.walk(src_root):
                    rel_dir = Path(root).relative_to(src_root)
                    dest_dir = self.app_root / rel_dir

                    # Filter out preserved directories
                    dirs[:] = [d for d in dirs if d not in PRESERVED_NAMES]
                    dest_dir.mkdir(parents=True, exist_ok=True)

                    for file_name in files:
                        if file_name in PRESERVED_NAMES or file_name.endswith((".db", ".key", ".log", ".pyc")):
                            continue
                        src_file = Path(root) / file_name
                        dest_file = dest_dir / file_name

                        shutil.copy2(src_file, dest_file)
                        copied_count += 1
                        # Maintain executable permissions for entrypoints and shell scripts
                        if file_name.endswith((".py", ".sh")):
                            try:
                                dest_file.chmod(dest_file.stat().st_mode | 0o755)
                            except Exception:
                                pass

                # Rebuild native helper if available
                native_dir = self.app_root / "native"
                if (native_dir / "Makefile").exists() and shutil.which("make"):
                    try:
                        make_res = subprocess.run(
                            ["make", "-C", str(native_dir)],
                            cwd=str(self.app_root),
                            capture_output=True,
                            timeout=30,
                        )
                        if make_res.returncode == 0:
                            helper_so = native_dir / "libmocktail_mouse_helper.so"
                            if helper_so.exists():
                                dest_helper_dir = Path.home() / ".local" / "lib" / "mocktail"
                                dest_helper_dir.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(helper_so, dest_helper_dir / "libmocktail_mouse_helper.so")
                    except Exception:
                        pass

                # Write version stamp
                check_info = self.check_for_updates()
                if check_info.get("remote_sha"):
                    (self.app_root / ".version").write_text(check_info["remote_sha"], encoding="utf-8")

                report("Update finished successfully.", 100)
                return True, f"Updated successfully ({copied_count} files refreshed)."
        except Exception as e:
            err_msg = f"Failed to apply update: {e}"
            log.error(err_msg)
            return False, err_msg
