#!/usr/bin/env python3
"""
AERAM Self-Healing Watchdog Engine (Auto-Reconnect Farm).
Monitors running background alts, detects server disconnects or crashes,
and automatically rejoins them without interrupting your main game session.
"""

import os
import time
import shutil
import logging
import threading
import subprocess
from pathlib import Path
import re
from typing import Dict, Set, Optional

log = logging.getLogger("SelfHeal")

ROBLOX_ERROR_CODES = {
    277: "Lost connection to the server",
    268: "Kicked due to unexpected client behavior",
    267: "Kicked by game script or anti-cheat",
    273: "Disconnected: Launched from another device",
    279: "Failed to connect to game server",
    260: "Error receiving data",
    261: "Server is full",
    262: "Connection timed out",
    274: "Server has shut down",
    271: "Kicked because player was lost",
    524: "Not authorized to join",
    529: "Roblox technical difficulties / outage",
}

DISCONNECT_PATTERNS = [
    "Lost connection to the server",
    "Disconnect event received",
    "ID_DISCONNECTION_NOTIFICATION",
    "Connection lost",
    "Error 277",
    "Error Code: 277",
    "DisconnectionNotification",
    "Server has shut down",
    "You were kicked",
    "kicked from this experience",
    "launched from another device",
]


class SelfHealEngine:
    def __init__(self, runner, storage, api=None, check_interval: int = 12):
        self.runner = runner
        self.storage = storage
        self.api = api
        self.check_interval = max(5, check_interval)
        self.running = False
        self.monitored_account_ids: Set[str] = set()
        self.reconnect_cooldowns: Dict[str, float] = {}
        self._cached_follow_target: Optional[str] = None
        self._cached_follow_id: Optional[int] = None
        self._cached_friend_ids: Dict[str, int] = {}

    def enable(self):
        self.running = True
        log.info("Self-Healing Watchdog ENABLED. Auto-monitoring active alts.")

    def disable(self):
        self.running = False
        log.info("Self-Healing Watchdog DISABLED.")

    def register_account(self, account_id: str):
        self.monitored_account_ids.add(account_id)

    def unregister_account(self, account_id: str):
        self.monitored_account_ids.discard(account_id)
        self.reconnect_cooldowns.pop(account_id, None)

    def get_disconnect_reason(self, log_path: Path) -> Optional[str]:
        """Reads the tail of instance.log and returns detailed error reason if disconnected."""
        if not log_path.exists():
            return None
        try:
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="ignore")

                # 1. Match explicit Error Code patterns (e.g., Error Code: 277, Error 273)
                m = re.search(r"Error\s*(?:Code)?:?\s*(\d{3})", tail, re.IGNORECASE)
                if m:
                    code = int(m.group(1))
                    desc = ROBLOX_ERROR_CODES.get(code, "Connection error")
                    return f"Error {code} ({desc})"

                # 2. Match standard disconnect event strings
                for pattern in DISCONNECT_PATTERNS:
                    if pattern.lower() in tail.lower():
                        return pattern
        except Exception:
            pass
        return None

    def is_disconnected_log(self, log_path: Path) -> bool:
        """Reads the tail of instance.log to detect Roblox disconnection events."""
        return self.get_disconnect_reason(log_path) is not None

    def simulate_disconnect(self, account_id: str, error_code: int = 277) -> bool:
        """Helper to test and verify Self-Heal in real-time by appending an error code to instance.log."""
        if hasattr(self.storage, "load_accounts"):
            accounts_list = self.storage.load_accounts()
        else:
            accounts_list = self.storage.list_accounts()
        target = next((a for a in accounts_list if a.account_id == account_id), None)
        if not target:
            return False
        prof_dir = self.storage.get_profile_dir(target)
        log_path = prof_dir / "instance.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        desc = ROBLOX_ERROR_CODES.get(error_code, "Lost connection to the server")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[AERAM-TEST] Error Code: {error_code} ({desc})\n")
        return True

    def check_and_heal(self) -> int:
        """
        Polls monitored accounts. If an alt is crashed or disconnected,
        restarts it automatically. Returns number of healed instances.
        """
        if not self.running:
            return 0

        now = time.time()
        healed_count = 0
        if hasattr(self.storage, "load_accounts"):
            accounts_list = self.storage.load_accounts()
        else:
            accounts_list = self.storage.list_accounts()
        all_accounts = {a.account_id: a for a in accounts_list}
        caller_cookie = next((a.cookie for a in all_accounts.values() if a.cookie), None)

        def _is_running(aid: str) -> bool:
            if hasattr(self.runner, "is_account_running"):
                return self.runner.is_account_running(aid)
            if hasattr(self.runner, "is_instance_running"):
                return self.runner.is_instance_running(aid)
            return False

        def _stop_instance(aid: str):
            if hasattr(self.runner, "stop_instance"):
                return self.runner.stop_instance(aid)
            if hasattr(self.runner, "terminate_instance"):
                return self.runner.terminate_instance(aid)

        # Automatically monitor all running alts
        if hasattr(self.runner, "get_running_account_ids"):
            active_ids = self.runner.get_running_account_ids()
        else:
            active_ids = list(getattr(self.runner, "active_instances", {}).keys())

        for acc_id in active_ids:
            if not self.storage.is_main_account(acc_id):
                self.monitored_account_ids.add(acc_id)

        for acc_id in list(self.monitored_account_ids):
            if self.storage.is_main_account(acc_id):
                continue

            acc = all_accounts.get(acc_id)
            if not acc:
                continue

            # Respect reconnect cooldown (minimum 25s between reconnect attempts per account)
            last_reconnect = self.reconnect_cooldowns.get(acc_id, 0)
            if now - last_reconnect < 25:
                continue

            profile_dir = self.storage.get_profile_dir(acc)
            log_path = profile_dir / "instance.log"
            is_proc_running = _is_running(acc_id)
            disconnect_reason = self.get_disconnect_reason(log_path)
            has_disconnect_error = (disconnect_reason is not None)

            if not is_proc_running or has_disconnect_error:
                # Suppress reconnect if account requires verification or session expired
                auth_mgr = getattr(self.runner, "auth_state_manager", None)
                if auth_mgr is not None:
                    try:
                        res = auth_mgr.can_launch(acc_id)
                        if isinstance(res, tuple) and len(res) == 2:
                            can_launch, reason = res
                            if not can_launch:
                                log.warning(f"[Self-Heal] Suppressed reconnect for '{acc.username}': {reason}")
                                continue
                    except Exception:
                        pass

                trigger_reason = disconnect_reason if has_disconnect_error else "Process terminated / crashed"
                log.warning(f"[Self-Heal] Triggered for account '{acc.username}' ({trigger_reason})")
                self.reconnect_cooldowns[acc_id] = now


                # 1. Terminate stale process if still hanging on disconnect popup
                if is_proc_running:
                    try:
                        _stop_instance(acc_id)
                    except Exception:
                        pass
                    time.sleep(1.0)

                # 2. Retrieve destination Place ID, Job ID, and Follow Target
                place_id = self.storage.get_setting("SavedPlaceId", "")
                job_id = self.storage.get_setting("SavedJobId", "")
                follow_target = self.storage.get_setting("SavedFollowTarget", "").strip()

                follow_user_id = None
                follow_name = None

                # Priority A: Check if the configured follow target is alive & connected
                if follow_target:
                    target_acc = next((a for a in all_accounts.values() if a.username.lower() == follow_target.lower()), None)
                    if target_acc:
                        if _is_running(target_acc.account_id):
                            target_log = self.storage.get_profile_dir(target_acc) / "instance.log"
                            if not self.is_disconnected_log(target_log):
                                follow_user_id = target_acc.user_id
                                follow_name = target_acc.username
                                if self.api and target_acc.user_id:
                                    try:
                                        p = self.api.get_user_presence(target_acc.user_id, cookie=caller_cookie)
                                        if p and p.get("placeId"):
                                            place_id = str(p["placeId"])
                                    except Exception:
                                        pass
                    else:
                        # Target is an external user/friend: resolve user ID and check presence
                        t_id = self._cached_follow_id if (self._cached_follow_target == follow_target) else None
                        if not t_id and self.api:
                            try:
                                resolved = self.api.resolve_username_to_user_id(follow_target)
                                if resolved:
                                    self._cached_follow_target = follow_target
                                    self._cached_follow_id = resolved
                                    t_id = resolved
                            except Exception:
                                pass
                        if t_id:
                            is_in_game = True
                            if self.api:
                                try:
                                    p = self.api.get_user_presence(t_id, cookie=caller_cookie)
                                    if p and p.get("userPresenceType") != 2:
                                        is_in_game = False
                                        log.info(f"[Self-Heal] Target '{follow_target}' is not in-game (presence={p.get('userPresenceType')}). Falling back.")
                                    elif p and p.get("placeId"):
                                        place_id = str(p["placeId"])
                                except Exception:
                                    pass
                            if is_in_game:
                                follow_user_id = t_id
                                follow_name = follow_target

                # 3. Clean up log so error pattern does not immediately re-trigger
                try:
                    if log_path.exists():
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(f"\n[AERAM Self-Heal] Auto-reconnecting at {time.strftime('%X')}...\n")
                except Exception:
                    pass

                # 4. Relaunch instance seamlessly (via follow if target specified/found, or job_id / place_id)
                try:
                    self.runner.launch_instance(
                        acc,
                        place_id=place_id,
                        job_id=job_id,
                        follow_user_id=follow_user_id,
                    )
                    healed_count += 1
                    target_desc = f"Follow @{follow_name}" if follow_user_id else (f"Job {job_id[:8]}" if job_id else f"Place {place_id}")
                    log.info(f"[Self-Heal] Successfully relaunched '{acc.username}' via {target_desc}.")
                    if shutil.which("notify-send"):
                        def _notify(m_title, m_desc):
                            try:
                                subprocess.run(
                                    ["notify-send", "-a", "AERAM", "-u", "normal", m_title, m_desc],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=5,
                                )
                            except Exception:
                                pass
                        threading.Thread(
                            target=_notify,
                            args=("AERAM Self-Heal", f"Reconnected @{acc.username} ({target_desc})"),
                            daemon=True
                        ).start()
                except Exception as e:
                    log.error(f"[Self-Heal] Failed to relaunch '{acc.username}': {e}")
                    self.monitored_account_ids.discard(acc_id)

        return healed_count
