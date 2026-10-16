#!/usr/bin/env python3
"""
AERAM Targeted Anti-AFK Engine (Interrupt-Free & Mocktail-Isolated).
Keeps background alts connected without ever interfering with active gameplay or desktop work:

- Strictly Isolated: Targets ONLY Mocktail Roblox processes via in-process SDL3 event injection
  and PID-verified desktop fallback. NEVER touches browser tabs, Discord, code editors, or system windows.
- Non-Disruptive: In-process injection requires ZERO window focus switches, preserves Wayland
  pointer grab, and never steals focus while you are typing or working.
- Active Work/Play Immunity: While physical input occurs or user is busy, desktop window cycling
  is completely suppressed.
- Safe 12-14 Minute Cadence: Well within Roblox's 20-minute AFK timeout.
"""

import os
import sys

# Prevent core/ directory from shadowing the standard library 'platform' module
_script_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _script_dir:
    sys.path.pop(0)
_project_root = os.path.dirname(_script_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import platform  # Pre-import standard library platform before evdev uses it
import glob
import time
import json
import signal
import select
import logging
import argparse
from pathlib import Path
from typing import Optional, List


from core.compositor import get_compositor

try:
    import dbus
except ImportError:
    dbus = None

try:
    from evdev import InputDevice, UInput, ecodes
except ImportError:
    InputDevice = UInput = ecodes = None

log = logging.getLogger("AntiAFK")


class PhysicalIdleMonitor:
    """Monitors physical hardware mouse and keyboard events to prevent disrupting active work."""

    def __init__(self):
        self.devices = []
        if InputDevice is not None:
            for p in glob.glob("/dev/input/event*"):
                try:
                    dev = InputDevice(p)
                    caps = dev.capabilities()
                    if ecodes.EV_KEY in caps or ecodes.EV_REL in caps:
                        if "AERAM" not in dev.name:
                            self.devices.append(dev)
                except Exception:
                    pass
            log.info(f"Physical hardware monitor attached to {len(self.devices)} input device(s).")
        self.last_activity = time.time()

    def update(self) -> float:
        """Polls for pending physical hardware events and returns idle duration in seconds."""
        if not self.devices:
            return time.time() - self.last_activity
        try:
            r, _, _ = select.select(self.devices, [], [], 0)
            for dev in r:
                try:
                    for ev in dev.read():
                        if ev.type in (ecodes.EV_KEY, ecodes.EV_REL):
                            self.last_activity = time.time()
                except Exception:
                    pass
        except Exception:
            pass
        return time.time() - self.last_activity

    def is_user_busy(self, threshold_sec: float = 45.0) -> bool:
        """Returns True if the user has touched mouse or keyboard recently."""
        return self.update() < threshold_sec


class AntiAfkEngine:
    """
    Targeted Anti-AFK Engine.
    Operates primarily via zero-disruption in-process SDL3 injection, with a strictly
    PID-filtered fallback for unhooked processes.
    """

    def __init__(
        self,
        interval_seconds: int = 900,
        idle_threshold_seconds: int = 0,
        key_name: str = "SPACE",
        verbose: bool = False,
    ):
        self.interval = max(60, int(interval_seconds))
        self.idle_threshold = max(0, int(idle_threshold_seconds))
        self.key_name = key_name.upper()
        self.key_code = getattr(ecodes, f"KEY_{self.key_name}", ecodes.KEY_SPACE) if ecodes else 57
        self.verbose = verbose
        self.running = False

        self._bus = None
        self._kwin_iface = None
        self._uinput = None
        self.compositor = get_compositor()
        self.monitor = PhysicalIdleMonitor()

    def _ensure_uinput(self):
        if not self._uinput and UInput and ecodes:
            try:
                self._uinput = UInput(
                    {ecodes.EV_KEY: [self.key_code, ecodes.KEY_SPACE]},
                    name="AERAM-AntiAFK-Keyboard",
                )
            except PermissionError:
                log.warning("Permission denied to /dev/uinput. In-process IPC will still work 100%.")
            except Exception as e:
                log.debug(f"UInput setup note: {e}")

    def _ensure_kwin(self):
        if not self._bus and dbus:
            try:
                self._bus = dbus.SessionBus()
                kwin_obj = self._bus.get_object("org.kde.KWin", "/Scripting")
                self._kwin_iface = dbus.Interface(kwin_obj, "org.kde.kwin.Scripting")
            except Exception as e:
                log.debug(f"KWin DBus connection note: {e}")

    def _execute_kwin_script(self, js_code: str) -> None:
        if not dbus:
            return
        self._ensure_kwin()
        if not self._kwin_iface:
            return
        script_file = Path(f"/tmp/aeram_afk_{os.getpid()}.js")
        try:
            script_file.write_text(js_code, encoding="utf-8")
            sid = self._kwin_iface.loadScript(str(script_file))
            script_obj = self._bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}")
            dbus.Interface(script_obj, "org.kde.kwin.Script").run()
            self._kwin_iface.unloadScript(f"/Scripting/Script{sid}")
        except Exception as e:
            if self.verbose:
                log.warning(f"KWin script note: {e}")
        finally:
            try:
                script_file.unlink(missing_ok=True)
            except Exception:
                pass

    def get_mocktail_pids(self) -> List[int]:
        """
        Scans /proc to retrieve the EXACT process IDs of all running Mocktail Roblox instances.
        Never matches arbitrary processes or other applications.
        """
        pids = []
        my_uid = os.getuid()
        try:
            with os.scandir("/proc") as it:
                for entry in it:
                    if not entry.name.isdigit():
                        continue
                    try:
                        if entry.stat().st_uid != my_uid:
                            continue
                        with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                            raw = f.read()
                        if b"mocktail_acc_" in raw or b"/mocktail" in raw:
                            pids.append(int(entry.name))
                    except Exception:
                        continue
        except Exception:
            pass
        return pids

    def get_roblox_windows_count(self) -> int:
        """Returns the number of active Mocktail instances."""
        return len(self.get_mocktail_pids())

    def trigger_ipc_pulse(self, pids: List[int]) -> int:
        """
        Dispatches an in-process Anti-AFK event directly to Mocktail processes via shared memory and signals.
        - ZERO window focus switches
        - ZERO effect on non-Roblox windows (Discord, Firefox, IDE, terminal)
        - Preserves camera grab and Shift Lock
        """
        # 1. Update shared memory trigger file
        try:
            pulse_file = Path("/dev/shm/aeram_afk_pulse")
            pulse_file.write_text(str(time.time()), encoding="utf-8")
            try:
                os.chmod(str(pulse_file), 0o666)
            except Exception:
                pass
        except Exception as e:
            log.debug(f"Could not touch /dev/shm/aeram_afk_pulse: {e}")

        # 2. Notify processes via SIGUSR1 for immediate next-frame execution
        signaled = 0
        for pid in pids:
            try:
                os.kill(pid, signal.SIGUSR1)
                signaled += 1
            except Exception:
                pass

        return signaled

    def pulse_all_instances(self, force: bool = True) -> int:
        """
        Executes a targeted Anti-AFK pulse affecting ONLY Mocktail Roblox instances.
        - Primary method: Direct In-Process IPC pulse.
        - If user is actively working/playing, window focus is NEVER modified.
        - Strict safety: Desktop fallback verifies windows strictly by Mocktail PID, NEVER by window caption.
        """
        pids = self.get_mocktail_pids()
        if not pids:
            log.info("No active Mocktail instances detected. Nothing to pulse.")
            return 0

        # Primary non-disruptive pulse (Targeted ONLY to Mocktail processes)
        signaled = self.trigger_ipc_pulse(pids)
        log.info(f"Dispatched targeted in-process Anti-AFK pulse to {len(pids)} Mocktail instance(s) (signals sent: {signaled}).")

        # If user is actively working or gaming, do NOT touch desktop windows or switch focus
        if not force and self.monitor.is_user_busy(threshold_sec=45.0):
            log.info("User is actively working/gaming. In-process pulse complete; skipping desktop window cycling to prevent disruption.")
            return len(pids)

        # Fallback desktop window cycling (ONLY used if user has been idle and uinput is active)
        if self._uinput and not self.monitor.is_user_busy(threshold_sec=30.0):
            try:
                self.compositor.cycle_mocktail_windows(pids)
            except Exception as e:
                log.debug(f"Desktop fallback cycle note: {e}")

        return len(pids)

    def start(self):
        self.running = True
        log.info(f"Targeted Anti-AFK started. Target interval: {self.interval}s ({self.interval//60}m).")
        log.info("Mocktail Isolation ACTIVE: Only Mocktail Roblox processes receive input. System and other windows are 100% immune.")

        elapsed = 0
        while self.running:
            try:
                time.sleep(2)
                elapsed += 2

                if elapsed >= self.interval:
                    self.pulse_all_instances(force=False)
                    elapsed = 0
            except KeyboardInterrupt:
                break
        self.stop()

    def stop(self):
        self.running = False
        if self._uinput:
            try:
                self._uinput.close()
            except Exception:
                pass
            self._uinput = None
        log.info("Anti-AFK engine stopped.")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [AntiAFK] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="AERAM Targeted Anti-AFK Engine")
    parser.add_argument("--interval", "-i", type=int, default=900, help="Seconds between pulses (default: 900 = 15m)")
    parser.add_argument("--idle-threshold", "-t", type=int, default=0, help="Idle threshold in seconds")
    parser.add_argument("--key", "-k", type=str, default="SPACE", help="Key to pulse (default: SPACE)")
    parser.add_argument("--once", action="store_true", help="Run a single pulse immediately and exit")
    parser.add_argument("--force", action="store_true", help="Force pulse even if user is currently active")
    args = parser.parse_args()

    engine = AntiAfkEngine(
        interval_seconds=args.interval,
        idle_threshold_seconds=args.idle_threshold,
        key_name=args.key,
    )

    def _sig_handler(sig, frame):
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    if args.once:
        engine.pulse_all_instances(force=args.force)
    else:
        engine.start()


if __name__ == "__main__":
    main()
