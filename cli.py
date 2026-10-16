"""
Command-line interface (CLI) for Linux Roblox Account Manager.
Uses the identical underlying core logic as the GUI. Never exposes or logs secrets.
"""

import sys
import os
import argparse
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional, List

from core.models import RobloxAccount
from core.storage import StorageManager, clean_cookie
from core.roblox_api import RobloxAPI
from core.instance_runner import InstanceRunner
from core.ram_importer import import_from_file_or_text

PROJECT_ROOT = Path(__file__).parent.resolve()


def install_desktop_launcher(project_root: Path):
    """Installs .desktop file and application icon into the user's desktop environment."""
    apps_dir = Path.home() / ".local" / "share" / "applications"
    icons_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    icons_512_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "512x512" / "apps"
    bin_dir = Path.home() / ".local" / "bin"

    apps_dir.mkdir(parents=True, exist_ok=True)
    icons_dir.mkdir(parents=True, exist_ok=True)
    icons_512_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    # 1. Install Icons (Scalable SVG + 512x512 PNG)
    src_svg = project_root / "assets" / "aeram.svg"
    if not src_svg.exists():
        src_svg = project_root / "assets" / "roblox-account-manager.svg"
    if src_svg.exists():
        shutil.copy2(src_svg, icons_dir / "aeram.svg")
        print(f"[OK] Installed scalable icon: {icons_dir / 'aeram.svg'}")

    src_png = project_root / "assets" / "aeram.png"
    if src_png.exists():
        shutil.copy2(src_png, icons_512_dir / "aeram.png")
        print(f"[OK] Installed 512x512 raster icon: {icons_512_dir / 'aeram.png'}")

    # 2. Install executable links to ~/.local/bin
    executable_src = project_root / "aeram"
    for name in ("aeram", "aem", "ram-linux"):
        symlink_dest = bin_dir / name
        try:
            if symlink_dest.exists() or symlink_dest.is_symlink():
                symlink_dest.unlink()
            symlink_dest.symlink_to(executable_src)
        except Exception as e:
            print(f"[WARN] Could not create symlink {name} in ~/.local/bin: {e}")
    print(f"[OK] Linked CLI executable to: {bin_dir / 'aeram'}")

    # 3. Install .desktop file
    desktop_file = apps_dir / "aeram.desktop"
    exec_path = str(bin_dir / "aeram")
    content = f"""[Desktop Entry]
Type=Application
Name=AERAM
GenericName=Roblox Account Manager
Comment=Native Linux multi-instance account manager for Roblox (Mocktail runtime)
Exec={exec_path} --gui %u
Icon=aeram
Terminal=false
Categories=Game;
Keywords=aeram;aem;roblox;ram;mocktail;multi-instance;accounts;hyprland;kde;
StartupNotify=true
StartupWMClass=aeram
Actions=StopAll;ListAccounts;Diagnostics;

[Desktop Action StopAll]
Name=Stop All Running Instances
Exec={exec_path} --stop-all

[Desktop Action ListAccounts]
Name=List Accounts (Terminal)
Exec={exec_path} --list

[Desktop Action Diagnostics]
Name=Run System Diagnostics
Exec={exec_path} --diagnostics
"""
    with open(desktop_file, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(desktop_file, 0o644)
    print(f"[OK] Installed desktop entry: {desktop_file}")

    # Remove legacy desktop entries if present
    for old_file in (apps_dir / "ram-linux.desktop", apps_dir / "roblox-account-manager.desktop"):
        if old_file.exists():
            try:
                old_file.unlink()
            except Exception:
                pass

    # 4. Refresh desktop and icon databases
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", str(apps_dir)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    if shutil.which("gtk-update-icon-cache"):
        try:
            subprocess.run(["gtk-update-icon-cache", "-q", "-t", "-f", str(Path.home() / ".local" / "share" / "icons" / "hicolor")], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    print("[OK] AERAM successfully integrated into your desktop environment!")


def run_cli(args: argparse.Namespace, storage: StorageManager, api: RobloxAPI, runner: InstanceRunner):
    # Desktop installation
    if args.install_desktop:
        install_desktop_launcher(PROJECT_ROOT)
        return

    # List accounts
    if args.list:
        accounts = storage.list_accounts()
        print(f"\n==================== ROBLOX ACCOUNTS ({len(accounts)}) ====================")
        if not accounts:
            print("No accounts configured yet. Run 'aeram add <cookie>' or 'aeram import <file>'.")
        else:
            fmt = "{:<4} {:<20} {:<18} {:<14} {:<14} {:<12}"
            print(fmt.format("#", "USERNAME", "ALIAS", "GROUP", "MODE", "STATUS"))
            print("-" * 86)
            for i, acc in enumerate(accounts, 1):
                is_running = runner.is_instance_running(acc.account_id)
                inst = runner.get_running_instance(acc.account_id)
                status = f"RUNNING({inst.pid})" if (is_running and inst) else "IDLE"
                alias_str = acc.alias or acc.display_name or "-"
                is_main = storage.is_main_account(acc)
                fps_val = storage.get_account_target_fps(acc.account_id)
                mode_str = "High (330 FPS)" if is_main else (f"Low ({fps_val} FPS)" if fps_val else "Low (15 FPS)")
                print(fmt.format(i, f"@{acc.username}", alias_str, acc.group or "Default", mode_str, status))
        print("====================================================================\n")
        return

    # Status of running instances
    if args.status:
        active = runner.active_instances
        print(f"\nActive Instances ({len(active)}):")
        if not active:
            print("No Roblox instances currently active.")
        else:
            for aid, inst in active.items():
                place_info = f" | Place ID: {inst.place_id}" if inst.place_id else ""
                print(f"• @{inst.username} (PID: {inst.pid}){place_info} [Elapsed: {int(time.time() - inst.start_time)}s]")
        print()
        return

    # Stop specific instance
    if args.stop:
        acc = storage.find_account_by_username(args.stop)
        aid = acc.account_id if acc else args.stop
        if runner.terminate_instance(aid):
            print(f"[OK] Terminated instance for account: {args.stop}")
        else:
            print(f"Account {args.stop} was not running.")
        return

    # Stop all instances
    if args.stop_all:
        count = runner.terminate_all_instances()
        print(f"[OK] Terminated {count} active instances.")
        return

    # Add account directly
    if args.add:
        if args.add == "-":
            if sys.stdin.isatty():
                import getpass
                raw_input_cookie = getpass.getpass("Enter .ROBLOSECURITY cookie: ").strip()
            else:
                raw_input_cookie = sys.stdin.read().strip()
        else:
            raw_input_cookie = args.add

        cookie = clean_cookie(raw_input_cookie)
        if not cookie:
            print("[ERROR] No valid cookie provided.")
            sys.exit(1)
        print("Validating session cookie against Roblox API...")
        data = api.enrich_account(cookie)
        if not data:
            print("[ERROR] Invalid, expired, or offline cookie.")
            sys.exit(1)

        existing = storage.find_account_by_username(data["username"])
        account_id = existing.account_id if existing else f"acc_{uuid.uuid4().hex[:12]}"
        acc = RobloxAccount(
            account_id=account_id,
            username=data["username"],
            display_name=data["display_name"],
            user_id=data["user_id"],
            cookie=cookie,
            avatar_url=data["avatar_url"],
            robux_balance=data["robux_balance"],
            group=args.group or "Default",
            notes=getattr(args, "notes", "") or "",
        )
        storage.save_account(acc)
        print(f"[OK] Added account @{acc.username} ({acc.display_name})!")
        return

    # Import RAM or cookies
    if args.import_file:
        source_path = Path(args.import_file)
        if not source_path.exists():
            print(f"[ERROR] File not found: {source_path}")
            sys.exit(1)

        pwd = args.password
        if pwd == "-":
            if sys.stdin.isatty():
                import getpass
                pwd = getpass.getpass("Enter decryption password: ")
            else:
                pwd = sys.stdin.readline().strip()

        print(f"Reading {source_path}...")
        accounts, pw_needed = import_from_file_or_text(str(source_path), pwd)
        if pw_needed and not pwd and sys.stdin.isatty():
            import getpass
            pwd = getpass.getpass("Encrypted file detected. Enter password: ")
            accounts, pw_needed = import_from_file_or_text(str(source_path), pwd)
        if pw_needed:
            print("[ERROR] Encrypted RAM file detected. Please provide password via prompt or --password.")
            sys.exit(1)

        print(f"Found {len(accounts)} accounts. Importing...")
        count = 0
        accounts_to_save = []
        for item in accounts:
            cookie = clean_cookie(item["cookie"])
            username = item.get("username") or f"Account_{count+1}"
            display_name = item.get("display_name") or username
            user_id = item.get("user_id") or 0
            group = item.get("group") or "Imported"
            notes = item.get("notes") or ""

            # Enrich if possible
            avatar_url = ""
            robux_balance = 0
            try:
                data = api.enrich_account(cookie)
                if data:
                    username = data["username"]
                    display_name = data["display_name"]
                    user_id = data["user_id"]
                    avatar_url = data.get("avatar_url", "")
                    robux_balance = data.get("robux_balance", 0)
            except Exception:
                pass

            existing = storage.find_account_by_username(username)
            aid = existing.account_id if existing else f"acc_{uuid.uuid4().hex[:12]}"
            password = item.get("password", "") or (existing.password if existing else "")
            totp_secret = item.get("totp_secret", "") or (existing.totp_secret if existing else "")
            acc = RobloxAccount(
                account_id=aid,
                username=username,
                display_name=display_name,
                user_id=user_id,
                cookie=cookie,
                password=password,
                avatar_url=avatar_url,
                robux_balance=robux_balance,
                group=group,
                notes=notes,
                totp_secret=totp_secret,
            )
            accounts_to_save.append(acc)
            count += 1
            print(f"  [{count}/{len(accounts)}] Added @{username}")

        if accounts_to_save:
            if hasattr(storage, "save_accounts_batch"):
                storage.save_accounts_batch(accounts_to_save)
            else:
                for a in accounts_to_save:
                    storage.save_account(a)

        print(f"[OK] Successfully imported {count} accounts into local vault!")
        return

    # Encrypted Export
    if getattr(args, "export_file", None):
        accounts = storage.list_accounts()
        if not accounts:
            print("[WARN] No accounts found to export.")
            return
        pwd = args.password
        if not pwd or pwd == "-":
            if sys.stdin.isatty():
                import getpass
                pwd = getpass.getpass("Enter master password to encrypt export archive: ")
            else:
                pwd = sys.stdin.readline().strip()
        from core.migration import export_accounts_encrypted
        out_path = export_accounts_encrypted(accounts, pwd, Path(args.export_file))
        print(f"[OK] Successfully exported {len(accounts)} accounts into encrypted archive: {out_path}")
        return

    # Single Launch
    if args.launch:
        acc = storage.find_account_by_username(args.launch)
        if not acc:
            acc = storage.get_account(args.launch)
        if not acc:
            print(f"[ERROR] Account '{args.launch}' not found in database.")
            sys.exit(1)

        print(f"Launching isolated instance for @{acc.username}...")
        inst = runner.launch_instance(
            account=acc,
            place_id=args.place,
            job_id=args.job,
            private_server_link=args.server,
        )
        print(f"[LAUNCH] Started instance PID: {inst.pid}")
        return

    # Multi-Launch
    if args.multi:
        usernames = [u.strip().lstrip("@") for u in args.multi.split(",") if u.strip()]
        accounts = []
        for u in usernames:
            acc = storage.find_account_by_username(u) or storage.get_account(u)
            if acc:
                accounts.append(acc)
            else:
                print(f"[WARN] Account '{u}' not found, skipping.")

        if not accounts:
            print("[ERROR] No matching accounts to launch.")
            sys.exit(1)

        print(f"Multi-launching {len(accounts)} accounts with delay {args.delay}s...")
        instances = runner.launch_multi(
            accounts=accounts,
            place_id=args.place,
            job_id=args.job,
            stagger_delay=args.delay,
        )
        print(f"[LAUNCH] Successfully launched {len(instances)} concurrent instances:")
        for inst in instances:
            print(f"  * @{inst.username} -> PID {inst.pid}")
        return
