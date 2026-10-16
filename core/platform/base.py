"""
Abstract Platform Provider interface for AERAM cross-platform support.
Defines platform-specific behavior for Linux (Mocktail/Sober) and Windows (Native Roblox).
"""

import abc
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any


class AbstractPlatformProvider(abc.ABC):
    """Abstract base class defining OS-level primitives."""

    @abc.abstractmethod
    def get_default_base_dir(self) -> Path:
        """Returns the default base directory for AERAM data."""
        pass

    @abc.abstractmethod
    def secure_directory(self, path: Path) -> None:
        """Ensures the directory exists with restricted access permissions."""
        pass

    @abc.abstractmethod
    def secure_file(self, path: Path) -> None:
        """Applies restricted permissions to a sensitive file."""
        pass

    @abc.abstractmethod
    def find_roblox_client(self) -> Optional[Path]:
        """Discovers the installed Roblox executable if present."""
        pass

    @abc.abstractmethod
    def is_process_alive(self, pid: int) -> bool:
        """Returns True if the process with the given PID is actively running."""
        pass

    @abc.abstractmethod
    def terminate_process(self, pid: int) -> bool:
        """Gracefully terminates or kills the process with the given PID."""
        pass

    @abc.abstractmethod
    def scan_running_instances(self, accounts: List[Any]) -> Dict[str, int]:
        """
        Scans system processes to discover running Roblox instances.
        Returns a dictionary mapping account_id -> pid.
        """
        pass

    @abc.abstractmethod
    def launch_client(
        self,
        account: Any,
        target_fps: int,
        place_id: Optional[int],
        job_id: Optional[str],
        link_code: Optional[str],
        ticket: Optional[str],
        cookie: str,
        storage: Any,
    ) -> subprocess.Popen:
        """Launches the Roblox client for the specified account."""
        pass

    @abc.abstractmethod
    def get_client_log_dir(self) -> Optional[Path]:
        """Returns the directory containing Roblox client log files."""
        pass

    @abc.abstractmethod
    def handle_multi_instance_lock(self) -> bool:
        """
        Prepares the environment to allow simultaneous client execution.
        On Windows, addresses the singleton mutex if applicable.
        On Linux, provisions isolated runtime roots and FNV locks.
        """
        pass

    @abc.abstractmethod
    def get_physical_idle_time(self) -> float:
        """Returns the number of seconds since physical mouse/keyboard hardware input."""
        pass

    @abc.abstractmethod
    def pulse_anti_afk(self) -> bool:
        """Sends a harmless input pulse to keep Roblox sessions active without interrupting user."""
        pass
