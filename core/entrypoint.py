"""
Unified application entry point for AERAM (GUI and CLI) on Linux and Windows.
"""

import sys
import os
import argparse
from pathlib import Path

# Ensure project root is on sys.path
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys._MEIPASS)
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.storage import StorageManager
from core.roblox_api import RobloxAPI
from core.instance_runner import InstanceRunner
from cli import run_cli


def _excepthook(exc_type, exc_val, exc_tb):
    import traceback
    err_msg = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
    try:
        from core.platform import get_platform
        log_file = get_platform().get_default_base_dir() / "crash.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(err_msg, encoding="utf-8")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_val, exc_tb)

sys.excepthook = _excepthook


def _set_process_name(name: str = "aeram") -> None:
    """Sets the Linux kernel process name (/proc/self/comm) to avoid generic 'python3' labeling."""
    if sys.platform != "linux":
        return
    try:
        import ctypes
        import ctypes.util
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(libc_name)
        # PR_SET_NAME = 15
        libc.prctl(15, name.encode("utf-8")[:15], 0, 0, 0)
    except Exception:
        pass
    try:
        import setproctitle
        setproctitle.setproctitle(name)
    except Exception:
        pass


def main():
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    _set_process_name("aeram")

    # Normalize natural CLI subcommands (e.g. 'aeram doctor', 'aeram update', 'aeram list')
    raw_args = sys.argv[1:]
    if raw_args:
        cmd0 = raw_args[0].lower()
        subcmd_map = {
            "doctor": "--diagnostics",
            "diagnostics": "--diagnostics",
            "update": "--update",
            "list": "--list",
            "ls": "--list",
            "status": "--status",
            "ps": "--status",
            "stop": "--stop",
            "stop-all": "--stop-all",
            "kill-all": "--stop-all",
            "launch": "--launch",
            "start": "--launch",
            "add": "--add",
            "import": "--import",
            "export": "--export",
        }
        if cmd0 in subcmd_map and not cmd0.startswith("-"):
            raw_args = [subcmd_map[cmd0]] + raw_args[1:]

    parser = argparse.ArgumentParser(
        prog="aeram",
        description="AERAM — Roblox Account Manager"
    )
    parser.add_argument("--gui", action="store_true", help="Launch PyQt6 graphical desktop interface (default)")
    parser.add_argument("--list", action="store_true", help="List all accounts and status in terminal")
    parser.add_argument("--status", action="store_true", help="Inspect all actively running instances")
    parser.add_argument("--launch", metavar="USER", help="Launch an isolated instance for the given username or ID")
    parser.add_argument("--multi", metavar="USERS", help="Launch multiple accounts simultaneously (comma-separated, e.g. user1,user2)")
    parser.add_argument("--place", type=int, metavar="PLACE_ID", help="Launch directly into the specified Roblox Place ID")
    parser.add_argument("--job", metavar="JOB_ID", help="Connect to specific server instance Job ID")
    parser.add_argument("--server", metavar="LINK", help="Connect via private server VIP link or link code")
    parser.add_argument("--delay", type=float, default=2.0, help="Launch delay between accounts in multi-launch mode (seconds, default: 2.0)")
    parser.add_argument("--stop", metavar="USER", help="Terminate running instance for given account")
    parser.add_argument("--stop-all", action="store_true", help="Terminate all actively running instances")
    parser.add_argument("--add", nargs="?", const="-", metavar="COOKIE", help="Add a new account via .ROBLOSECURITY cookie (omit argument or pass '-' to input securely without shell exposure)")
    parser.add_argument("--group", metavar="GROUP", help="Group tag when adding an account (default: Default)")
    parser.add_argument("--import", dest="import_file", metavar="FILE", help="Import Windows RAM AccountData.json, encrypted archive, or cookie list")
    parser.add_argument("--export", dest="export_file", metavar="FILE", help="Export all accounts into an encrypted archive")
    parser.add_argument("--password", nargs="?", const="-", metavar="PWD", help="Master password for encrypted archive (omit argument or pass '-' to prompt securely)")
    parser.add_argument("--install-desktop", action="store_true", help="Install .desktop application launcher and desktop icon")
    parser.add_argument("--diagnostics", "-d", action="store_true", help="Run system diagnostics and display portability report")
    parser.add_argument("--update", "-u", action="store_true", help="Check and apply updates from GitHub")

    args = parser.parse_args(raw_args)

    # Reject incomplete modifier combinations before touching the vault or any instance state
    orphan_errors = []
    if (args.place or args.job or args.server) and not (args.launch or args.multi):
        orphan_errors.append("Options --place, --job, and --server require --launch <USER> or --multi <USERS>.")
    if args.group and not args.add:
        orphan_errors.append("Option --group requires --add <COOKIE>.")
    if args.password and not (args.import_file or args.export_file):
        orphan_errors.append("Option --password requires --import <FILE> or --export <FILE>.")

    if orphan_errors:
        for err in orphan_errors:
            print(f"aeram: error: {err}", file=sys.stderr)
        sys.exit(2)

    if args.diagnostics:
        from core.diagnostics import DiagnosticsRunner
        report = DiagnosticsRunner().run_all_checks()
        print(report.to_text())
        sys.exit(0 if report.overall_ok else 1)

    if args.update:
        from core.updater import AppUpdater
        updater = AppUpdater()
        print("Checking GitHub remote for updates...")
        res = updater.check_for_updates()
        if not res.get("remote_sha"):
            print(f"[ERROR] Could not check remote repository: {res.get('error', 'Network error')}")
            sys.exit(1)
        if not res.get("update_available"):
            print(f"[OK] AERAM is already up to date ({res.get('local_sha', '')[:7]}).")
            sys.exit(0)
        print(f"[UPDATE AVAILABLE] Remote commit: {res.get('remote_sha')[:7]}")
        if res.get("behind_count", 0) > 0:
            print(f"Behind by {res['behind_count']} commit(s).")
        if res.get("commit_msg"):
            print(f"Latest commit: {res['commit_msg']}")
        print("Applying update...")
        ok, msg = updater.apply_update()
        if ok:
            print(f"[SUCCESS] {msg}")
            sys.exit(0)
        else:
            print(f"[FAILED] {msg}")
            sys.exit(1)

    storage = StorageManager()
    api = RobloxAPI()
    runner = InstanceRunner(storage, api=api)

    # Check if any CLI command flags are active
    cli_flags = [
        args.list, args.status, args.launch, args.multi, args.stop,
        args.stop_all, args.add, args.import_file, args.export_file, args.install_desktop
    ]

    if not args.gui and any(cli_flags):
        run_cli(args, storage, api, runner)
        return

    # Check if graphical display server is active
    has_display = bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))

    if not has_display:
        print("No Wayland or X11 display server detected. Running in CLI mode:")
        args.list = True
        run_cli(args, storage, api, runner)
        return

    # Launch PyQt6 Graphical UI
    from PyQt6 import QtWidgets, QtGui, QtCore
    from ui.main_window import MainWindow

    # Conditionally enable OpenGL context sharing if PyQt6-WebEngine is available
    try:
        from PyQt6 import QtWebEngineWidgets  # noqa: F401
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    except Exception:
        pass

    from ui.theme import DARK_THEME_QSS

    # Set canonical application metadata across Wayland, X11, and desktop environments
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("aeram")
    app.setOrganizationName("aeram")
    app.setApplicationDisplayName("AERAM")
    app.setDesktopFileName("aeram")
    app.setStyleSheet(DARK_THEME_QSS)

    # Set window icon (check system theme first, fallback to bundled assets)
    app_icon = QtGui.QIcon.fromTheme("aeram")
    if app_icon.isNull():
        for icon_name in ("aeram.svg", "aeram.png", "roblox-account-manager.svg", "aeram.ico"):
            icon_path = PROJECT_ROOT / "assets" / icon_name
            if icon_path.exists():
                app_icon = QtGui.QIcon(str(icon_path))
                break

    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    window = MainWindow(storage, api, runner)
    window.setObjectName("aeram")
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
