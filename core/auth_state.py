"""
AERAM Authentication Lifecycle State Machine.
Tracks per-account authentication status, enforces safety policies,
suppresses automated retries on CAPTCHA / Arkose challenges,
and deduplicates concurrent launch requests.
"""

from enum import Enum
from dataclasses import dataclass, field
import threading
import time
import logging
from typing import Optional, Dict, Tuple, Set

log = logging.getLogger("AuthState")


class AccountAuthState(str, Enum):
    """
    Formal lifecycle states for Roblox account authentication.
    """
    STORED = "STORED"                           # Vault-persisted, not yet validated
    VALIDATING = "VALIDATING"                   # Pre-flight API check in-flight
    READY = "READY"                             # Verified healthy, idle, ready to launch
    AUTHENTICATING = "AUTHENTICATING"           # Requesting single-use auth ticket
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"  # Arkose CAPTCHA / 2FA challenge received; RETRIES HALTED
    LAUNCHING = "LAUNCHING"                     # Ticket acquired, process starting
    RUNNING = "RUNNING"                         # Client process confirmed alive
    SESSION_EXPIRED = "SESSION_EXPIRED"         # HTTP 401 Unauthorized / token expired; cookie preserved
    ERROR = "ERROR"                             # Transient network or unhandled fault


@dataclass
class AuthStateInfo:
    """Current state snapshot and challenge metadata for an account."""
    state: AccountAuthState = AccountAuthState.STORED
    last_check_time: float = field(default_factory=time.time)
    last_verification_time: float = 0.0
    error_message: str = ""
    challenge_type: Optional[str] = None       # e.g. "captcha", "2stepverification", "securityquestions"
    challenge_id: Optional[str] = None         # Roblox challenge UUID
    challenge_metadata: Optional[str] = None   # Base64 challenge blob for Arkose Labs


class AccountAuthStateManager:
    """
    Thread-safe state machine manager for all managed Roblox accounts.
    Guarantees:
    - Never retries automatically upon Arkose CAPTCHA / 2FA challenge
    - Preserves stored cookies without deletion on transient or challenge errors
    - Deduplicates concurrent launch attempts for the same account
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._states: Dict[str, AuthStateInfo] = {}
        self._launch_in_progress: Set[str] = set()

    def get_state_info(self, account_id: str) -> AuthStateInfo:
        """Returns the current state info object for an account (defaults to STORED)."""
        with self._lock:
            if account_id not in self._states:
                self._states[account_id] = AuthStateInfo(state=AccountAuthState.STORED)
            return self._states[account_id]

    def get_state(self, account_id: str) -> AccountAuthState:
        """Returns the current AccountAuthState enum for an account."""
        return self.get_state_info(account_id).state

    def set_state(
        self,
        account_id: str,
        state: AccountAuthState,
        error_message: str = "",
        challenge_type: Optional[str] = None,
        challenge_id: Optional[str] = None,
        challenge_metadata: Optional[str] = None,
    ) -> None:
        """Sets the state and updates associated metadata."""
        with self._lock:
            info = self.get_state_info(account_id)
            prev_state = info.state
            info.state = state
            info.last_check_time = time.time()
            if error_message:
                info.error_message = error_message
            if challenge_type is not None:
                info.challenge_type = challenge_type
            if challenge_id is not None:
                info.challenge_id = challenge_id
            if challenge_metadata is not None:
                info.challenge_metadata = challenge_metadata
            if state == AccountAuthState.VERIFICATION_REQUIRED:
                info.last_verification_time = time.time()

            if prev_state != state:
                log.info(f"Account '{account_id}' auth state changed: {prev_state.value} -> {state.value}")

    def acquire_launch_lock(self, account_id: str) -> bool:
        """
        Attempts to acquire a deduplication lock for launching an account.
        Returns True if acquired, False if launch already in-flight.
        """
        with self._lock:
            if account_id in self._launch_in_progress:
                return False
            self._launch_in_progress.add(account_id)
            return True

    def release_launch_lock(self, account_id: str) -> None:
        """Releases the launch deduplication lock for an account."""
        with self._lock:
            self._launch_in_progress.discard(account_id)

    def is_launching(self, account_id: str) -> bool:
        """Checks if a launch is currently in-flight for an account."""
        with self._lock:
            return account_id in self._launch_in_progress

    def can_launch(self, account_id: str) -> Tuple[bool, str]:
        """
        Enforces safety rules before launching:
        - Rejects launch if verification required (Arkose CAPTCHA / 2FA)
        - Rejects launch if session expired (HTTP 401)
        - Rejects launch if another launch operation is currently active
        """
        with self._lock:
            info = self.get_state_info(account_id)
            if info.state == AccountAuthState.VERIFICATION_REQUIRED:
                ch_desc = info.challenge_type or "CAPTCHA"
                return False, f"Verification required ({ch_desc}). Automated launch halted to protect session."

            if info.state == AccountAuthState.SESSION_EXPIRED:
                return False, "Roblox session expired (HTTP 401). Please update the .ROBLOSECURITY cookie."

            if account_id in self._launch_in_progress:
                return False, "Launch operation is already in progress for this account."

            return True, ""

    def mark_verification_required(
        self,
        account_id: str,
        challenge_type: Optional[str] = "captcha",
        challenge_id: Optional[str] = None,
        challenge_metadata: Optional[str] = None,
    ) -> None:
        """
        Transitions an account to VERIFICATION_REQUIRED state.
        Automated retries are immediately stopped. Stored cookies are NOT discarded.
        """
        with self._lock:
            self.set_state(
                account_id=account_id,
                state=AccountAuthState.VERIFICATION_REQUIRED,
                error_message=f"Roblox verification required ({challenge_type or 'CAPTCHA'}). Manual check needed.",
                challenge_type=challenge_type or "captcha",
                challenge_id=challenge_id,
                challenge_metadata=challenge_metadata,
            )

    def mark_session_expired(self, account_id: str, reason: str = "Cookie expired (HTTP 401)") -> None:
        """
        Transitions an account to SESSION_EXPIRED state.
        Stored cookies are NOT deleted, allowing user to view details or update.
        """
        with self._lock:
            self.set_state(
                account_id=account_id,
                state=AccountAuthState.SESSION_EXPIRED,
                error_message=reason,
            )

    def mark_ready(self, account_id: str) -> None:
        """Transitions an account to READY state with cleared error messages."""
        with self._lock:
            info = self.get_state_info(account_id)
            info.state = AccountAuthState.READY
            info.error_message = ""
            info.challenge_type = None
            info.challenge_id = None
            info.challenge_metadata = None
            info.last_check_time = time.time()

    def mark_running(self, account_id: str) -> None:
        """Transitions an account to RUNNING state."""
        with self._lock:
            self.set_state(account_id, AccountAuthState.RUNNING)

    def reset_verification_status(self, account_id: str) -> None:
        """
        Allows the user to manually reset a VERIFICATION_REQUIRED state back to READY
        after completing verification in their browser.
        """
        with self._lock:
            info = self.get_state_info(account_id)
            if info.state in (AccountAuthState.VERIFICATION_REQUIRED, AccountAuthState.ERROR):
                info.state = AccountAuthState.READY
                info.error_message = ""
                info.challenge_type = None
                info.challenge_id = None
                info.challenge_metadata = None
                log.info(f"Account '{account_id}' verification state reset to READY by user.")
