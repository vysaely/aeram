"""
AERAM Platform Abstraction Layer.
Authoritative LinuxPlatformProvider instance for Linux operations.
"""

from .base import AbstractPlatformProvider
from .linux import LinuxPlatformProvider

_platform_instance: AbstractPlatformProvider = LinuxPlatformProvider()


def get_platform() -> AbstractPlatformProvider:
    """Returns the platform provider instance for the host system."""
    return _platform_instance
