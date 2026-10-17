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

# When run as a script, import project modules from the repo root rather than core/
_script_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _script_dir:
    sys.path.pop(0)
_project_root = os.path.dirname(_script_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import glob
import time
import signal
import select
import logging
import argparse
from pathlib import Path
from typing import List


from core.compositor import get_compositor

try:
    from evdev import InputDevice, ecodes
except ImportError:
    InputDevice = ecodes = None

log = logging.getLogger("AntiAFK")

# Polled by native/libmocktail_mouse_helper.c (mtime change => pulse)
AFK_PULSE_PATH = Path("/dev/shm/aeram_afk_pulse")


def is_mocktail_game_cmdline(raw: bytes) -> bool:
    """True only for a Mocktail game process: argv[0] is the mocktail binary or a per-account copy.

    SIGUSR1 kills any process that does not handle it, so matching "/mocktail" anywhere in the
    command line would also hit editors, builds or `tail -f` on Mocktail paths, and Mocktail's
    own WebKit login window, where JavaScriptCore uses SIGUSR1 internally.
    """
    argv0 = raw.split(b"\0", 1)[0]
    name = os.path.basename(argv0)
    return name == b"mocktail" or name.startswith(b"mocktail_acc_")


def write_pulse_file(timestamp: float, path: Path = AFK_PULSE_PATH) -> None:
    """Writes the pulse file as owner-only (0600), never following a planted symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)  # tighten files left 0666 by earlier versions
        os.write(fd, str(timestamp).encode("utf-8"))
    finally:
        os.close(fd)


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

        self._uinput = None
        self.compositor = get_compositor()
        self.monitor = PhysicalIdleMonitor()

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
                        if is_mocktail_game_cmdline(raw):
                            pids.append(int(entry.name))
                    except Exception:
                        continue
        except Exception:
            pass
        return pids

    def trigger_ipc_pulse(self, pids: List[int]) -> int:
        """
        Dispatches an in-process Anti-AFK event directly to Mocktail processes via shared memory and signals.
        - ZERO window focus switches
        - ZERO effect on non-Roblox windows (Discord, Firefox, IDE, terminal)
        - Preserves camera grab and Shift Lock
        """
        # 1. Update shared memory trigger file (owner-only: /dev/shm is shared by all local users,
        #    and the native helper ignores a pulse file it does not own)
        try:
            write_pulse_file(time.time())
        except Exception as e:
            log.debug(f"Could not touch {AFK_PULSE_PATH}: {e}")

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
