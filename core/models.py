"""
Core domain data models for Linux Roblox Account Manager.
"""

from dataclasses import dataclass, field
from typing import Optional, List
import time
import re


def sanitize_filename(name: str) -> str:
    """Sanitizes a string to be safely used as a directory or file name."""
    sanitized = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)
    return sanitized.strip('._') or "unnamed"


@dataclass
class RobloxAccount:
    account_id: str
    username: str
    display_name: str
    user_id: int
    cookie: str
    password: str = ""
    avatar_url: str = ""
    robux_balance: int = 0
    group: str = "Default"
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_played: float = 0.0
    sort_order: int = 0
    web_display_name: str = ""
    totp_secret: str = ""
    pending_robux: int = 0
    join_privacy: str = "Unknown"

    @property
    def alias(self) -> str:
        """Alias property matching display_name."""
        return self.display_name

    @property
    def robux(self) -> int:
        """Convenience property for robux_balance."""
        return self.robux_balance

    @robux.setter
    def robux(self, val: int):
        self.robux_balance = val

    @property
    def two_step_secret(self) -> str:
        """RAM compatibility property for 2FA TOTP secret key."""
        return self.totp_secret

    @two_step_secret.setter
    def two_step_secret(self, val: str):
        self.totp_secret = val

    @property
    def profile_dir_name(self) -> str:
        """Returns safe directory name for the isolated profile."""
        clean_user = sanitize_filename(self.username)
        return f"{clean_user}_{self.account_id[:8]}"


@dataclass
class RunningInstance:
    account_id: str
    username: str
    pid: int
    start_time: float
    place_id: Optional[int] = None
    job_id: Optional[str] = None
    log_path: Optional[str] = None


@dataclass
class FavoriteGame:
    place_id: int
    name: str
    universe_id: Optional[int] = None
    icon_url: Optional[str] = None
    private_server_link: Optional[str] = None
    notes: str = ""
    created_at: float = field(default_factory=time.time)
