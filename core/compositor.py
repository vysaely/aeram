"""
Compositor & Desktop Window Management Abstraction for AERAM.
Provides capability-based window discovery, focus management, and desktop integration
across KDE Plasma Wayland, Hyprland Wayland, X11, and Generic Wayland compositors.
"""

import os
import shutil
import json
import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any

log = logging.getLogger("Compositor")


class CompositorType(str, Enum):
    KDE_WAYLAND = "kde_wayland"
    HYPRLAND = "hyprland"
    X11 = "x11"
    GENERIC_WAYLAND = "generic_wayland"
    UNKNOWN = "unknown"


class CompositorCapabilities:
    def __init__(
        self,
        can_query_windows: bool,
        can_focus_windows: bool,
        can_keep_below: bool,
        compositor_name: str,
    ):
        self.can_query_windows = can_query_windows
        self.can_focus_windows = can_focus_windows
        self.can_keep_below = can_keep_below
        self.compositor_name = compositor_name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compositor": self.compositor_name,
            "can_query_windows": self.can_query_windows,
            "can_focus_windows": self.can_focus_windows,
            "can_keep_below": self.can_keep_below,
        }


class BaseCompositor:
    """Base class for desktop compositor integrations."""

    def get_capabilities(self) -> CompositorCapabilities:
        return CompositorCapabilities(
            can_query_windows=False,
            can_focus_windows=False,
            can_keep_below=False,
            compositor_name="Generic",
        )

    def cycle_mocktail_windows(self, pids: List[int]) -> int:
        """Cycles focus across Mocktail instances if safe and supported, then restores active window."""
        return 0

    def get_active_window_pid(self) -> Optional[int]:
        """Returns the PID owning the currently active window, if discoverable."""
        return None


class HyprlandCompositor(BaseCompositor):
    """Hyprland Wayland compositor integration via hyprctl JSON IPC."""

    def __init__(self):
        self.hyprctl = shutil.which("hyprctl")

    def get_capabilities(self) -> CompositorCapabilities:
        has_ctl = bool(self.hyprctl)
        return CompositorCapabilities(
            can_query_windows=has_ctl,
            can_focus_windows=has_ctl,
            can_keep_below=False,
            compositor_name="Hyprland (Wayland)",
        )

    def _run_hyprctl(self, args: List[str]) -> Optional[str]:
        if not self.hyprctl:
            return None
        try:
            res = subprocess.run(
                [self.hyprctl, *args],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0:
                return res.stdout
        except Exception as e:
            log.debug(f"hyprctl error: {e}")
        return None

    def get_active_window_pid(self) -> Optional[int]:
        out = self._run_hyprctl(["activewindow", "-j"])
        if out:
            try:
                data = json.loads(out)
                return data.get("pid")
            except Exception:
                pass
        return None

    def cycle_mocktail_windows(self, pids: List[int]) -> int:
        """Focuses verified Mocktail windows and restores original active window."""
        if not self.hyprctl or not pids:
            return 0

        # 1. Query active window to restore it later
        orig_active = self._run_hyprctl(["activewindow", "-j"])
        orig_addr = None
        if orig_active:
            try:
                orig_addr = json.loads(orig_active).get("address")
            except Exception:
                pass

        # 2. Query all clients
        clients_json = self._run_hyprctl(["clients", "-j"])
        if not clients_json:
            return 0

        try:
            clients = json.loads(clients_json)
        except Exception:
            return 0

        mocktail_clients = []
        for c in clients:
            c_pid = c.get("pid")
            c_class = str(c.get("class", "")).lower()
            if c_pid in pids or ("mocktail" in c_class and c_class != "python3"):
                mocktail_clients.append(c)

        if not mocktail_clients:
            return 0

        # Cycle focus safely
        pulsed = 0
        for c in mocktail_clients:
            addr = c.get("address")
            if addr:
                self._run_hyprctl(["dispatch", "focuswindow", f"address:{addr}"])
                pulsed += 1

        # Restore original window
        if orig_addr:
            self._run_hyprctl(["dispatch", "focuswindow", f"address:{orig_addr}"])

        return pulsed


class KWinWaylandCompositor(BaseCompositor):
    """KDE Plasma Wayland integration via KWin Scripting DBus."""

    def __init__(self):
        self._bus = None
        self._iface = None

    def _ensure_dbus(self) -> bool:
        if self._iface:
            return True
        try:
            import dbus
            self._bus = dbus.SessionBus()
            kwin_obj = self._bus.get_object("org.kde.KWin", "/Scripting")
            self._iface = dbus.Interface(kwin_obj, "org.kde.kwin.Scripting")
            return True
        except Exception:
            return False

    def get_capabilities(self) -> CompositorCapabilities:
        has_dbus = self._ensure_dbus()
        return CompositorCapabilities(
            can_query_windows=has_dbus,
            can_focus_windows=has_dbus,
            can_keep_below=has_dbus,
            compositor_name="KDE Plasma (KWin Wayland)",
        )

    def cycle_mocktail_windows(self, pids: List[int]) -> int:
        if not self._ensure_dbus() or not pids:
            return 0

        import tempfile
        pids_json = json.dumps(pids)
        cycle_js = f"""
        let targetPids = {pids_json};
        let wins = workspace.windowList();
        let rbx = [];
        let originalActive = workspace.activeWindow;

        for (let j = 0; j < wins.length; j++) {{
            let w = wins[j];
            let cls = (w.resourceClass || '').toLowerCase();
            let resName = (w.resourceName || '').toLowerCase();

            let isMocktail = (targetPids.indexOf(w.pid) !== -1) ||
                             (cls.indexOf('mocktail') !== -1) ||
                             (resName.indexOf('mocktail') !== -1);

            if (cls === 'python3' || cls === 'firefox' || cls === 'antigravity' ||
                cls === 'discord' || cls === 'konsole' || cls.indexOf('overlay') !== -1) {{
                continue;
            }}

            if (isMocktail) {{
                rbx.push(w);
            }}
        }}

        if (rbx.length > 0) {{
            for (let k = 0; k < rbx.length; k++) {{
                let target = rbx[k];
                let isMain = (target.width >= 1200 || target.height >= 900);
                if (!isMain) {{
                    target.keepBelow = true;
                }}
            }}
            if (originalActive) {{
                workspace.activeWindow = originalActive;
            }}
        }}
        """
        script_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", prefix="aeram_kwin_", delete=False, encoding="utf-8") as tf:
                tf.write(cycle_js)
                script_path = Path(tf.name)
            try:
                os.chmod(script_path, 0o600)
            except OSError:
                pass
            sid = self._iface.loadScript(str(script_path))
            script_obj = self._bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}")
            import dbus
            dbus.Interface(script_obj, "org.kde.kwin.Script").run()
            self._iface.unloadScript(f"/Scripting/Script{sid}")
            return len(pids)
        except Exception as e:
            log.debug(f"KWin cycle note: {e}")
            return 0
        finally:
            if script_path:
                try:
                    script_path.unlink(missing_ok=True)
                except Exception:
                    pass


class X11Compositor(BaseCompositor):
    """X11 window manager integration using xdotool / wmctrl."""

    def __init__(self):
        self.xdotool = shutil.which("xdotool")

    def get_capabilities(self) -> CompositorCapabilities:
        has_tool = bool(self.xdotool)
        return CompositorCapabilities(
            can_query_windows=has_tool,
            can_focus_windows=has_tool,
            can_keep_below=False,
            compositor_name="X11 Display Server",
        )

    def cycle_mocktail_windows(self, pids: List[int]) -> int:
        if not self.xdotool or not pids:
            return 0
        try:
            # Query active window
            active_out = subprocess.run([self.xdotool, "getactivewindow"], capture_output=True, text=True)
            orig_win = active_out.stdout.strip() if active_out.returncode == 0 else None

            # Cycle mocktail pids
            count = 0
            for pid in pids:
                res = subprocess.run([self.xdotool, "search", "--pid", str(pid)], capture_output=True, text=True)
                if res.returncode == 0 and res.stdout.strip():
                    for wid in res.stdout.strip().split():
                        subprocess.run([self.xdotool, "windowactivate", "--sync", wid], capture_output=True)
                        count += 1

            if orig_win:
                subprocess.run([self.xdotool, "windowactivate", "--sync", orig_win], capture_output=True)

            return count
        except Exception:
            return 0


def detect_compositor_type() -> CompositorType:
    """Detects active desktop session and compositor architecture."""
    # 1. Hyprland
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower() == "hyprland":
        return CompositorType.HYPRLAND

    # 2. KDE Plasma
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if "kde" in desktop or "plasma" in desktop or "kwin" in os.environ.get("KWIN_RUNNING", ""):
        return CompositorType.KDE_WAYLAND

    # 3. Generic Wayland
    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return CompositorType.GENERIC_WAYLAND

    # 4. X11
    if session == "x11" or os.environ.get("DISPLAY"):
        return CompositorType.X11

    return CompositorType.UNKNOWN


def get_compositor() -> BaseCompositor:
    """Factory returning the appropriate compositor provider for the current session."""
    comp_type = detect_compositor_type()
    if comp_type == CompositorType.HYPRLAND:
        return HyprlandCompositor()
    if comp_type == CompositorType.KDE_WAYLAND:
        return KWinWaylandCompositor()
    if comp_type == CompositorType.X11:
        return X11Compositor()
    return BaseCompositor()
