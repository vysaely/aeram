"""
System Diagnostics and Auto-Repair Engine for AERAM.
Performs capability-based health checks across Python runtime, dependencies,
Mocktail installations, native helper libraries, desktop compositors (Hyprland/KDE/X11),
and Linux cgroups/uinput subsystems.
"""

import os
import sys
import shutil
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

from core.mocktail_manager import MocktailManager, MocktailInstallationInfo
from core.compositor import detect_compositor_type, get_compositor, CompositorType

log = logging.getLogger("Diagnostics")


@dataclass
class DiagnosticCheck:
    name: str
    category: str  # "Dependencies", "Mocktail", "System", "Compositor"
    status: str    # "OK", "WARNING", "ERROR", "INFO"
    message: str
    details: Optional[str] = None
    fix_suggestion: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "message": self.message,
            "details": self.details,
            "fix_suggestion": self.fix_suggestion,
        }


@dataclass
class DiagnosticReport:
    checks: List[DiagnosticCheck]
    overall_ok: bool
    summary: str
    arch_install_commands: List[str]

    def to_text(self) -> str:
        lines = [
            "============================================================",
            "AERAM System Diagnostics & Portability Report",
            "============================================================",
            f"Overall Status: {'PASSED (Ready)' if self.overall_ok else 'ATTENTION REQUIRED'}",
            f"Summary: {self.summary}",
            "",
            "DETAILED CHECKS:",
        ]
        curr_cat = None
        for c in self.checks:
            if c.category != curr_cat:
                curr_cat = c.category
                lines.append(f"\n[{curr_cat}]")
            symbol = "+" if c.status == "OK" else ("!" if c.status == "WARNING" else ("-" if c.status == "ERROR" else "i"))
            lines.append(f"  [{symbol}] {c.name}: {c.message}")
            if c.details:
                lines.append(f"      Details: {c.details}")
            if c.fix_suggestion:
                lines.append(f"      Suggested Action: {c.fix_suggestion}")

        if self.arch_install_commands:
            lines.append("\nRECOMMENDED ARCH LINUX / AUR FIX COMMANDS:")
            for cmd in self.arch_install_commands:
                lines.append(f"  $ {cmd}")

        lines.append("============================================================")
        return "\n".join(lines)


class DiagnosticsRunner:
    """Evaluates host system health, library availability, and integration states."""

    def __init__(self, project_root: Optional[Path] = None):
        if project_root is None:
            self.project_root = Path(__file__).resolve().parent.parent
        else:
            self.project_root = Path(project_root)

    def run_all_checks(self) -> DiagnosticReport:
        checks: List[DiagnosticCheck] = []
        arch_cmds: List[str] = []

        # 1. Python runtime
        py_ver = sys.version.split()[0]
        if sys.version_info >= (3, 10):
            checks.append(DiagnosticCheck(
                name="Python Runtime",
                category="Dependencies",
                status="OK",
                message=f"Python {py_ver} (>= 3.10 required)",
            ))
        else:
            checks.append(DiagnosticCheck(
                name="Python Runtime",
                category="Dependencies",
                status="ERROR",
                message=f"Python {py_ver} is unsupported. Version 3.10+ required.",
                fix_suggestion="Upgrade Python via your system package manager.",
            ))

        # 2. Mandatory Dependencies
        # PyQt6
        try:
            import PyQt6.QtCore
            from PyQt6.QtCore import PYQT_VERSION_STR
            checks.append(DiagnosticCheck(
                name="PyQt6",
                category="Dependencies",
                status="OK",
                message=f"PyQt6 {PYQT_VERSION_STR} installed",
            ))
        except ImportError as e:
            checks.append(DiagnosticCheck(
                name="PyQt6",
                category="Dependencies",
                status="ERROR",
                message="PyQt6 is missing",
                details=str(e),
                fix_suggestion="Install via 'sudo pacman -S python-pyqt6' or 'pip install PyQt6'",
            ))
            arch_cmds.append("sudo pacman -S --needed python-pyqt6")

        # Security Vault (PyNaCl or system libsodium)
        try:
            import nacl
            checks.append(DiagnosticCheck(
                name="Security Vault",
                category="Dependencies",
                status="OK",
                message=f"PyNaCl {getattr(nacl, '__version__', 'detected')} installed",
            ))
        except ImportError:
            try:
                from core.vault import Vault
                checks.append(DiagnosticCheck(
                    name="Security Vault",
                    category="Dependencies",
                    status="OK",
                    message="Native libsodium (libsodium.so) active via ctypes",
                ))
            except Exception as e:
                checks.append(DiagnosticCheck(
                    name="Security Vault",
                    category="Dependencies",
                    status="ERROR",
                    message="Neither PyNaCl nor native libsodium is available.",
                    details=str(e),
                    fix_suggestion="Install via 'sudo pacman -S libsodium python-pynacl' or 'pip install pynacl'",
                ))
                arch_cmds.append("sudo pacman -S --needed libsodium python-pynacl")

        # PyQt6-WebEngine (Browser Login)
        try:
            import PyQt6.QtWebEngineWidgets
            checks.append(DiagnosticCheck(
                name="PyQt6-WebEngine (Browser Login)",
                category="Dependencies",
                status="OK",
                message="PyQt6-WebEngine installed and functional",
            ))
        except ImportError:
            checks.append(DiagnosticCheck(
                name="PyQt6-WebEngine (Browser Login)",
                category="Dependencies",
                status="ERROR",
                message="PyQt6-WebEngine is missing. Required for in-app browser login.",
                fix_suggestion="Install via 'sudo pacman -S python-pyqt6-webengine' or 'pip install PyQt6-WebEngine'",
            ))
            arch_cmds.append("sudo pacman -S --needed python-pyqt6-webengine")

        # evdev (Hardware Idle Monitor)
        try:
            import evdev
            checks.append(DiagnosticCheck(
                name="evdev (Input Idle Monitor)",
                category="Dependencies",
                status="OK",
                message="evdev installed (monitors hardware activity for zero-disruption Anti-AFK)",
            ))
        except ImportError:
            checks.append(DiagnosticCheck(
                name="evdev (Input Idle Monitor)",
                category="Dependencies",
                status="ERROR",
                message="evdev is missing. Required for hardware idle sensing.",
                fix_suggestion="Install via 'sudo pacman -S python-evdev' or 'pip install evdev'",
            ))
            arch_cmds.append("sudo pacman -S --needed python-evdev")

        # dbus-python
        try:
            import dbus
            checks.append(DiagnosticCheck(
                name="dbus-python (Desktop IPC)",
                category="Dependencies",
                status="OK",
                message="dbus-python installed (enables KWin Wayland scripting integration)",
            ))
        except ImportError:
            checks.append(DiagnosticCheck(
                name="dbus-python (Desktop IPC)",
                category="Dependencies",
                status="INFO",
                message="dbus-python not installed. Note: Hyprland uses hyprctl IPC and does not need dbus-python.",
                fix_suggestion="Install via 'sudo pacman -S python-dbus' if running KDE Plasma",
            ))

        # 4. Mocktail Installation Check
        mgr = MocktailManager()
        mocktail_info = mgr.detect()
        if mocktail_info.is_installed:
            details_str = f"Binary: {mocktail_info.bin_path}"
            ver = mocktail_info.version_name or mocktail_info.payload_id
            if ver:
                details_str += f" | Version: {ver}"
            checks.append(DiagnosticCheck(
                name="Mocktail Roblox Client",
                category="Mocktail",
                status="OK",
                message="Mocktail executable detected and ready",
                details=details_str,
            ))
        else:
            checks.append(DiagnosticCheck(
                name="Mocktail Roblox Client",
                category="Mocktail",
                status="WARNING",
                message="Mocktail executable was not found on PATH or standard locations.",
                details="Searched: PATH, ~/.local/bin/mocktail, ~/.local/lib/mocktail/mocktail, /usr/bin/mocktail, /opt/mocktail/bin/mocktail",
                fix_suggestion="Install via AUR 'paru -S mocktail-bin' or 'yay -S mocktail-bin', or set path in ~/.config/aeram/config.json",
            ))
            arch_cmds.append("paru -S --needed mocktail-bin  # or yay -S mocktail-bin")

        # Native mouse companion helper
        helper_path = mocktail_info.helper_path or (self.project_root / "native" / "libmocktail_mouse_helper.so")
        if helper_path.exists():
            checks.append(DiagnosticCheck(
                name="In-Process Mouse Helper (Native)",
                category="Mocktail",
                status="OK",
                message=f"Compiled helper library present ({helper_path.name})",
                details=str(helper_path),
            ))
        else:
            # Check if compiler is available to build it
            gcc_found = bool(shutil.which("gcc"))
            make_found = bool(shutil.which("make"))
            buildable = gcc_found and make_found
            checks.append(DiagnosticCheck(
                name="In-Process Mouse Helper (Native)",
                category="Mocktail",
                status="WARNING",
                message="libmocktail_mouse_helper.so is not compiled.",
                details="Used for zero-disruption in-process mouse & anti-AFK event injection.",
                fix_suggestion="Run 'make -C native' in AERAM root, or click 'Auto-Repair' below." if buildable else "Install base-devel ('sudo pacman -S base-devel') then run 'make -C native'",
            ))
            if not buildable:
                arch_cmds.append("sudo pacman -S --needed base-devel")

        # 5. Linux Subsystem & Permissions
        # Cgroups v2
        cgroup_path = Path("/sys/fs/cgroup/cgroup.controllers")
        systemd_run = shutil.which("systemd-run")
        if cgroup_path.exists() and systemd_run:
            checks.append(DiagnosticCheck(
                name="Cgroups v2 Resource Isolation",
                category="System",
                status="OK",
                message="Cgroups v2 hierarchy and systemd-run available (enables alt CPU/memory throttling)",
            ))
        elif systemd_run:
            checks.append(DiagnosticCheck(
                name="Cgroups v2 Resource Isolation",
                category="System",
                status="INFO",
                message="systemd-run present, but unified cgroup controller file not directly readable.",
            ))
        else:
            checks.append(DiagnosticCheck(
                name="Cgroups v2 Resource Isolation",
                category="System",
                status="WARNING",
                message="systemd-run utility not found. Memory and CPU limits will be inactive.",
            ))

        # Shared memory /dev/shm
        can_shm = False
        try:
            from core.anti_afk import AFK_PULSE_PATH
            can_shm = os.access(AFK_PULSE_PATH.parent, os.W_OK) and (
                not AFK_PULSE_PATH.exists() or AFK_PULSE_PATH.stat().st_uid == os.getuid()
            )
        except Exception:
            can_shm = False

        if can_shm:
            checks.append(DiagnosticCheck(
                name="Shared Memory IPC (/dev/shm)",
                category="System",
                status="OK",
                message="/dev/shm accessible for real-time inter-process Anti-AFK triggers",
            ))
        else:
            checks.append(DiagnosticCheck(
                name="Shared Memory IPC (/dev/shm)",
                category="System",
                status="WARNING",
                message="Cannot write to /dev/shm. In-process signal fallbacks will be used.",
                fix_suggestion="Check permissions on /dev/shm mount.",
            ))

        # /dev/uinput permissions
        uinput_path = Path("/dev/uinput")
        if uinput_path.exists():
            can_uinput = os.access(uinput_path, os.W_OK)
            if can_uinput:
                checks.append(DiagnosticCheck(
                    name="Virtual Input (/dev/uinput)",
                    category="System",
                    status="OK",
                    message="/dev/uinput writable (hardware keyboard simulation enabled)",
                ))
            else:
                checks.append(DiagnosticCheck(
                    name="Virtual Input (/dev/uinput)",
                    category="System",
                    status="INFO",
                    message="/dev/uinput write permission not granted to unprivileged user.",
                    details="In-process SDL3 injection works 100% without /dev/uinput. Only needed if simulating hardware keys when Roblox is unfocused.",
                    fix_suggestion="Add user to 'input' group: 'sudo usermod -a -G input $USER'",
                ))
        else:
            checks.append(DiagnosticCheck(
                name="Virtual Input (/dev/uinput)",
                category="System",
                status="INFO",
                message="/dev/uinput not present (in-process SDL3 injection active).",
            ))

        # 6. Desktop Compositor Check
        comp_type = detect_compositor_type()
        compositor = get_compositor()
        caps = compositor.get_capabilities()

        if comp_type == CompositorType.HYPRLAND:
            hyprctl_path = shutil.which("hyprctl")
            if hyprctl_path:
                checks.append(DiagnosticCheck(
                    name="Desktop Compositor (Hyprland)",
                    category="Compositor",
                    status="OK",
                    message=f"Hyprland Wayland detected with active hyprctl IPC ({hyprctl_path})",
                    details=f"Can query: {caps.can_query_windows} | Can focus: {caps.can_focus_windows}",
                ))
            else:
                checks.append(DiagnosticCheck(
                    name="Desktop Compositor (Hyprland)",
                    category="Compositor",
                    status="WARNING",
                    message="Hyprland environment detected, but 'hyprctl' executable not in PATH.",
                    fix_suggestion="Ensure hyprland package is installed and hyprctl is on PATH.",
                ))
        elif comp_type == CompositorType.KDE_WAYLAND:
            checks.append(DiagnosticCheck(
                name="Desktop Compositor (KDE Plasma)",
                category="Compositor",
                status="OK",
                message="KDE Plasma Wayland detected",
                details=f"KWin DBus: {caps.can_focus_windows} | Can keep below: {caps.can_keep_below}",
            ))
        elif comp_type == CompositorType.X11:
            xdotool = shutil.which("xdotool")
            checks.append(DiagnosticCheck(
                name="Desktop Compositor (X11)",
                category="Compositor",
                status="OK" if xdotool else "INFO",
                message=f"X11 Display Server detected ({'xdotool available' if xdotool else 'xdotool not installed'})",
            ))
            if not xdotool:
                arch_cmds.append("sudo pacman -S --needed xdotool")
        else:
            checks.append(DiagnosticCheck(
                name="Desktop Compositor",
                category="Compositor",
                status="INFO",
                message=f"Session: {comp_type.value} (Generic Wayland / Unknown). In-process SDL3 injection works universally.",
            ))

        # Determine overall status
        errors = [c for c in checks if c.status == "ERROR"]
        warnings = [c for c in checks if c.status == "WARNING"]
        overall_ok = len(errors) == 0

        if errors:
            summary = f"{len(errors)} critical issue(s) preventing full functionality."
        elif warnings:
            summary = f"Core requirements met with {len(warnings)} optional feature warning(s)."
        else:
            summary = "All checks passed! System fully configured and ready."

        return DiagnosticReport(
            checks=checks,
            overall_ok=overall_ok,
            summary=summary,
            arch_install_commands=list(dict.fromkeys(arch_cmds)),
        )

    def auto_repair(self) -> List[str]:
        """Attempts safe, automated fixes for common setup gaps."""
        log_messages: List[str] = []

        # 1. Compile native mouse helper if missing
        native_dir = self.project_root / "native"
        helper_so = native_dir / "libmocktail_mouse_helper.so"
        if not helper_so.exists() and (native_dir / "Makefile").exists():
            if shutil.which("make") and shutil.which("gcc"):
                try:
                    res = subprocess.run(
                        ["make", "-C", str(native_dir)],
                        capture_output=True,
                        text=True,
                        timeout=15.0,
                    )
                    if res.returncode == 0 and helper_so.exists():
                        log_messages.append("Successfully compiled native/libmocktail_mouse_helper.so")
                    else:
                        log_messages.append(f"Make helper build failed: {res.stderr.strip()[:100]}")
                except Exception as e:
                    log_messages.append(f"Auto-compile error: {e}")
            else:
                log_messages.append("Cannot compile native helper: 'gcc' or 'make' not found.")
        elif helper_so.exists():
            log_messages.append("Native helper library already present.")

        # 2. Ensure runtime directories exist
        # Use the directory the app will actually open; hardcoding aeram/ here used to create an
        # empty directory that then took precedence over an existing ram-linux vault.
        from core.platform import get_platform
        app_data = get_platform().get_default_base_dir()
        config_dir = Path.home() / ".config" / "aeram"
        try:
            app_data.mkdir(parents=True, exist_ok=True)
            config_dir.mkdir(parents=True, exist_ok=True)
            log_messages.append(f"Verified runtime data directories at {app_data}")
        except Exception as e:
            log_messages.append(f"Directory creation note: {e}")

        # 3. Create the pulse file owner-only (the native helper ignores pulse files it does not own)
        try:
            from core.anti_afk import AFK_PULSE_PATH, write_pulse_file
            write_pulse_file(0.0)
            log_messages.append(f"Configured {AFK_PULSE_PATH} as owner-only (0600).")
        except Exception as e:
            log_messages.append(f"Pulse file setup note: {e}")

        return log_messages
