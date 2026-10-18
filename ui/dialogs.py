"""
Dialog modals for Account Adding, Windows RAM Importing, Game Joining, and Multi-Launching.
"""

import os
import uuid
import time
import json
import secrets
import string
import threading
import random
from typing import Optional, List, Dict, Any
from PyQt6 import QtWidgets, QtCore, QtGui

from core.models import RobloxAccount
from core.storage import StorageManager, clean_cookie, DEFAULT_ALT_FPS
from core.roblox_api import RobloxAPI
from core.ram_importer import import_from_file_or_text
from core.diagnostics import DiagnosticsRunner
from core.updater import AppUpdater
from ui.theme import DARK_THEME_QSS
from ui.web_login_dialog import WebLoginDialog
from ui.borderless_dialog import BorderlessDialog, BorderlessInputDialog, BorderlessMessageBox as QMessageBox
from ui.thread_keeper import run_in_thread
from core.totp import generate_totp, get_totp_remaining_seconds


class AddAccountDialog(BorderlessDialog):
    def __init__(self, storage: StorageManager, api: RobloxAPI, parent=None):
        super().__init__(parent, title="Add Roblox Account", can_maximize=False, resizable=False)
        self.storage = storage
        self.api = api
        self.new_account: Optional[RobloxAccount] = None

        self.setWindowTitle("Add Roblox Account - AERAM")
        self.setFixedWidth(460)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        info = QtWidgets.QLabel(
            "<b>Add via .ROBLOSECURITY Session Cookie</b><br>"
            "<span style='color: #888888;'>Your cookie will be encrypted locally and stored securely with 0600 permissions. Passwords are never stored or requested.</span>"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # Cookie input
        layout.addWidget(QtWidgets.QLabel("Roblox Session Cookie (.ROBLOSECURITY):"))
        self.cookie_input = QtWidgets.QLineEdit()
        self.cookie_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.cookie_input.setPlaceholderText("_|WARNING:-DO-NOT-SHARE-THIS...")
        layout.addWidget(self.cookie_input)

        # Group input
        layout.addWidget(QtWidgets.QLabel("Account Group (Optional):"))
        self.group_input = QtWidgets.QLineEdit()
        self.group_input.setPlaceholderText("Default")
        self.group_input.setText("Default")
        layout.addWidget(self.group_input)

        # Notes input
        layout.addWidget(QtWidgets.QLabel("Notes (Optional):"))
        self.notes_input = QtWidgets.QLineEdit()
        self.notes_input.setPlaceholderText("e.g. Main alt, Trading, AFK")
        layout.addWidget(self.notes_input)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #ffffff; font-weight: bold; font-size: 11px;")
        layout.addWidget(self.status_label)

        # Button Box
        btn_box = QtWidgets.QHBoxLayout()
        btn_box.addStretch()

        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(self.cancel_btn)

        self.add_btn = QtWidgets.QPushButton("Validate && Add")
        self.add_btn.setObjectName("primaryBtn")
        self.add_btn.clicked.connect(self._validate_and_save)
        btn_box.addWidget(self.add_btn)

        layout.addLayout(btn_box)

    def _validate_and_save(self):
        cookie = clean_cookie(self.cookie_input.text().strip())
        if not cookie:
            self.status_label.setText("Please paste a valid session cookie.")
            return

        self.add_btn.setEnabled(False)
        self.status_label.setStyleSheet("color: #cccccc;")
        self.status_label.setText("Validating cookie against Roblox API...")
        self._pending_cookie = cookie
        run_in_thread(self, lambda: self.api.enrich_account(cookie), self._on_cookie_enriched)

    def _on_cookie_enriched(self, data):
        cookie = self._pending_cookie
        if isinstance(data, Exception):
            data = None
        if not data:
            self.status_label.setStyleSheet("color: #ffffff; font-weight: bold;")
            self.status_label.setText("Cookie is invalid, expired, or offline. Could not authenticate.")
            self.add_btn.setEnabled(True)
            return

        group = self.group_input.text().strip() or "Default"
        notes = self.notes_input.text().strip()

        # Check existing account
        existing = self.storage.find_account_by_username(data["username"])
        account_id = existing.account_id if existing else f"acc_{uuid.uuid4().hex[:12]}"

        self.new_account = RobloxAccount(
            account_id=account_id,
            username=data["username"],
            display_name=data["display_name"],
            user_id=data["user_id"],
            cookie=cookie,
            avatar_url=data["avatar_url"],
            robux_balance=data["robux_balance"],
            group=group,
            notes=notes,
        )

        self.storage.save_account(self.new_account)
        self.accept()


class ImportRAMDialog(BorderlessDialog):
    def __init__(self, storage: StorageManager, api: RobloxAPI, parent=None):
        super().__init__(parent, title="Import Windows RAM / Cookie Data", can_maximize=True, resizable=True)
        self.storage = storage
        self.api = api
        self.parsed_accounts: List[Dict[str, Any]] = []

        self.setWindowTitle("Import Windows RAM / Cookie Data - AERAM")
        self.resize(600, 480)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        desc = QtWidgets.QLabel(
            "<b>Import accounts from Windows Roblox Account Manager</b><br>"
            "<span style='color: #888888;'>Supports original <code>AccountData.json</code> (unencrypted or password-encrypted), raw cookie lists, and CSV dumps.</span>"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        file_row = QtWidgets.QHBoxLayout()
        self.file_path_input = QtWidgets.QLineEdit()
        self.file_path_input.setPlaceholderText("Select AccountData.json or cookie text file...")
        file_row.addWidget(self.file_path_input)

        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        # Master Password row (for encrypted RAM files)
        pw_row = QtWidgets.QHBoxLayout()
        pw_row.addWidget(QtWidgets.QLabel("Master Password (if encrypted):"))
        self.pw_input = QtWidgets.QLineEdit()
        self.pw_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.pw_input.setPlaceholderText("Leave empty if unencrypted")
        self.pw_input.returnPressed.connect(self._parse_preview)
        pw_row.addWidget(self.pw_input)
        layout.addLayout(pw_row)

        # Or Paste Text
        layout.addWidget(QtWidgets.QLabel("Or paste raw JSON / cookie lines directly:"))
        self.text_area = QtWidgets.QPlainTextEdit()
        self.text_area.setPlaceholderText("Paste AccountData.json contents or cookie list here...")
        layout.addWidget(self.text_area, stretch=1)

        preview_row = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("Ready to parse.")
        self.status_label.setStyleSheet("color: #888888;")
        preview_row.addWidget(self.status_label)
        preview_row.addStretch()

        self.btn_parse = QtWidgets.QPushButton("Parse && Preview")
        self.btn_parse.clicked.connect(self._parse_preview)
        preview_row.addWidget(self.btn_parse)
        layout.addLayout(preview_row)

        # Results table / summary
        self.results_label = QtWidgets.QLabel("")
        self.results_label.setStyleSheet("color: #ffffff; font-weight: bold;")
        layout.addWidget(self.results_label)

        # Action buttons
        btn_box = QtWidgets.QHBoxLayout()
        btn_box.addStretch()

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        self.btn_import_all = QtWidgets.QPushButton("Import Accounts")
        self.btn_import_all.setObjectName("primaryBtn")
        self.btn_import_all.setEnabled(False)
        self.btn_import_all.clicked.connect(self._import_all)
        btn_box.addWidget(self.btn_import_all)

        layout.addLayout(btn_box)

    def _browse_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select RAM Account Data File", "", "All Supported (*.aeram *.json *.enc *.txt *.csv);;AERAM Vault (*.aeram);;RAM Accounts (*.json *.enc);;Cookie Lists (*.txt *.csv);;All Files (*)"
        )
        if path:
            self.file_path_input.setText(path)
            self._parse_preview()

    def _parse_preview(self):
        source = self.file_path_input.text().strip() or self.text_area.toPlainText().strip()
        if not source:
            self.status_label.setText("Please select a file or paste account data.")
            return

        password = self.pw_input.text().strip() or None

        try:
            accounts, pw_needed = import_from_file_or_text(source, password)
            if pw_needed:
                self.status_label.setStyleSheet("color: #cccccc; font-weight: bold;")
                self.status_label.setText("Password-encrypted RAM archive detected! Enter the master password.")
                return

            self.parsed_accounts = accounts
            if accounts:
                self.status_label.setStyleSheet("color: #ffffff; font-weight: bold;")
                self.status_label.setText(f"Successfully identified {len(accounts)} accounts!")
                self.results_label.setText(f"Found {len(accounts)} valid accounts ready to import.")
                self.btn_import_all.setEnabled(True)
            else:
                self.status_label.setStyleSheet("color: #ffffff; font-weight: bold;")
                self.status_label.setText("No accounts found in provided input.")
                self.btn_import_all.setEnabled(False)
        except Exception as e:
            self.status_label.setStyleSheet("color: #ffffff; font-weight: bold;")
            self.status_label.setText(f"Parsing error: {e}")
            self.btn_import_all.setEnabled(False)

    def _import_all(self):
        if not self.parsed_accounts or getattr(self, "_import_worker", None) is not None:
            return

        self.btn_import_all.setEnabled(False)
        self.btn_parse.setEnabled(False)
        self._import_progress = QtWidgets.QProgressDialog(
            "Importing accounts and enriching metadata...", "Cancel", 0, len(self.parsed_accounts), self
        )
        self._import_progress.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Dialog)
        self._import_progress.setStyleSheet(DARK_THEME_QSS + "QProgressDialog { background-color: #000000; border: 1px solid #1a1a1a; }")
        self._import_progress.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self._import_progress.setMinimumDuration(0)
        self._import_progress.setValue(0)

        # Each account costs several Roblox requests; doing them here froze the window.
        worker = ImportEnrichWorker(self.api, list(self.parsed_accounts), self)
        worker.progress.connect(self._import_progress.setValue)
        worker.done.connect(self._finish_import)
        self._import_progress.canceled.connect(worker.cancel)
        self.finished.connect(worker.cancel)  # closing the window stops further lookups
        self._import_worker = worker
        worker.start()

    def _finish_import(self, results: list):
        self._import_worker = None
        imported_count = 0
        accounts_to_save = []
        for i, item, data in results:
            cookie = clean_cookie(item["cookie"])
            username = item.get("username") or f"Account_{i+1}"
            display_name = item.get("display_name") or username
            user_id = item.get("user_id") or 0
            group = item.get("group") or "Imported"
            notes = item.get("notes") or ""
            avatar_url = ""
            robux = 0
            if data:
                username = data["username"]
                display_name = data["display_name"]
                user_id = data["user_id"]
                avatar_url = data["avatar_url"]
                robux = data["robux_balance"]

            existing = self.storage.find_account_by_username(username)
            account_id = existing.account_id if existing else f"acc_{uuid.uuid4().hex[:12]}"
            password = item.get("password", "") or (existing.password if existing else "")
            totp_secret = item.get("totp_secret", "") or (existing.totp_secret if existing else "")

            acc = RobloxAccount(
                account_id=account_id,
                username=username,
                display_name=display_name,
                user_id=user_id,
                cookie=cookie,
                password=password,
                avatar_url=avatar_url,
                robux_balance=robux,
                group=group,
                notes=notes,
                totp_secret=totp_secret,
            )
            accounts_to_save.append(acc)
            imported_count += 1

        if accounts_to_save:
            self.storage.save_accounts_batch(accounts_to_save)

        self._import_progress.setValue(len(self.parsed_accounts))
        self._import_progress.close()
        QMessageBox.information(
            self, "Import Complete", f"Successfully imported {imported_count} accounts into the local vault."
        )
        self.accept()


class ImportEnrichWorker(QtCore.QThread):
    """Looks up each imported cookie's profile off the GUI thread, a few accounts at a time."""
    progress = QtCore.pyqtSignal(int)
    done = QtCore.pyqtSignal(list)  # [(index, item, enrichment or None)] in input order

    PARALLEL = 4

    def __init__(self, api: RobloxAPI, items: List[Dict[str, Any]], parent=None):
        super().__init__(parent)
        self.api = api
        self.items = items
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def _enrich(self, item):
        if self._cancelled.is_set():
            return None
        try:
            return self.api.enrich_account(clean_cookie(item["cookie"]))
        except Exception:
            return None

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = {}
        with ThreadPoolExecutor(max_workers=self.PARALLEL) as pool:
            futures = {pool.submit(self._enrich, item): i for i, item in enumerate(self.items)}
            for future in as_completed(futures):
                if not self._cancelled.is_set():
                    results[futures[future]] = future.result()
                    self.progress.emit(len(results))
        # Cancel keeps the accounts finished so far, like the old one-by-one loop.
        self.done.emit([(i, self.items[i], results[i]) for i in sorted(results)])


class EditAccountDialog(BorderlessDialog):
    def __init__(self, account: RobloxAccount, parent=None):
        super().__init__(parent, title=f"Edit Account — @{account.username}", can_maximize=False, resizable=False)
        self.account = account
        self.new_cookie: Optional[str] = None

        self.setWindowTitle(f"Edit Account — @{account.username}")
        self.setFixedWidth(440)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QtWidgets.QLabel("Display Name / Alias:"))
        self.name_input = QtWidgets.QLineEdit(self.account.display_name)
        layout.addWidget(self.name_input)

        layout.addWidget(QtWidgets.QLabel("Group:"))
        self.group_input = QtWidgets.QLineEdit(self.account.group)
        layout.addWidget(self.group_input)

        layout.addWidget(QtWidgets.QLabel("Notes:"))
        self.notes_input = QtWidgets.QLineEdit(self.account.notes)
        layout.addWidget(self.notes_input)

        layout.addWidget(QtWidgets.QLabel("Password (Optional):"))
        self.password_input = QtWidgets.QLineEdit(self.account.password or "")
        self.password_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Stored password for quick copy")
        layout.addWidget(self.password_input)

        layout.addWidget(QtWidgets.QLabel("2FA Authenticator Secret (Optional):"))
        totp_row = QtWidgets.QHBoxLayout()
        self.totp_input = QtWidgets.QLineEdit(self.account.totp_secret or "")
        self.totp_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.totp_input.setPlaceholderText("Base32 secret (e.g. JBSWY3DPEHPK3PXP) or otpauth://")
        totp_row.addWidget(self.totp_input, stretch=1)

        self.btn_show_totp = QtWidgets.QPushButton("Show")
        self.btn_show_totp.setFixedWidth(50)
        self.btn_show_totp.clicked.connect(self._toggle_totp_echo)
        totp_row.addWidget(self.btn_show_totp)

        self.btn_test_totp = QtWidgets.QPushButton("Test Code")
        self.btn_test_totp.clicked.connect(self._test_totp_code)
        totp_row.addWidget(self.btn_test_totp)
        layout.addLayout(totp_row)

        self.lbl_totp_test = QtWidgets.QLabel("")
        self.lbl_totp_test.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace;")
        self.lbl_totp_test.setVisible(False)
        layout.addWidget(self.lbl_totp_test)

        layout.addWidget(QtWidgets.QLabel("Update .ROBLOSECURITY Cookie (Optional):"))
        self.cookie_input = QtWidgets.QLineEdit()
        self.cookie_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.cookie_input.setPlaceholderText("Leave blank to keep existing encrypted cookie")
        layout.addWidget(self.cookie_input)

        hint = QtWidgets.QLabel("<span style='color: #888888; font-size: 11px;'>Paste a fresh cookie here if the session expired (HTTP 401).</span>")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_box = QtWidgets.QHBoxLayout()
        btn_box.addStretch()

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        self.btn_auto_login = QtWidgets.QPushButton("Auto Log In")
        self.btn_auto_login.setToolTip("Save credentials and automatically log into Roblox (handles 2FA)")
        self.btn_auto_login.setStyleSheet(
            "QPushButton { background-color: #1a3d28; border: 1px solid #2da157; color: #4ee489; font-weight: bold; } "
            "QPushButton:hover { background-color: #245638; border-color: #38c76c; color: #ffffff; }"
        )
        self.btn_auto_login.clicked.connect(self._auto_login_from_edit)
        btn_box.addWidget(self.btn_auto_login)

        save_btn = QtWidgets.QPushButton("Save Changes")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        btn_box.addWidget(save_btn)

        layout.addLayout(btn_box)

    def _toggle_totp_echo(self):
        if self.totp_input.echoMode() == QtWidgets.QLineEdit.EchoMode.Password:
            self.totp_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
            self.btn_show_totp.setText("Hide")
        else:
            self.totp_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            self.btn_show_totp.setText("Show")

    def _test_totp_code(self):
        from core.totp import parse_universal_2fa_input, generate_totp, get_totp_remaining_seconds
        raw = self.totp_input.text().strip()
        if not raw:
            self.lbl_totp_test.setText("No 2FA secret entered.")
            self.lbl_totp_test.setStyleSheet("color: #707070; font-size: 11px;")
            self.lbl_totp_test.setVisible(True)
            return

        parsed = parse_universal_2fa_input(raw)
        secret = parsed[0]["secret_b32"] if parsed else raw.replace(" ", "").upper()
        try:
            code = generate_totp(secret)
            rem = get_totp_remaining_seconds()
            self.lbl_totp_test.setText(f"Valid 2FA Secret! Current code: {code} (expires in {rem}s)")
            self.lbl_totp_test.setStyleSheet("color: #FFFFFF; font-size: 11px; font-weight: bold;")
            self.lbl_totp_test.setVisible(True)
        except Exception as e:
            self.lbl_totp_test.setText(f"Invalid secret: {e}")
            self.lbl_totp_test.setStyleSheet("color: #707070; font-size: 11px;")
            self.lbl_totp_test.setVisible(True)

    def _save(self):
        self.account.display_name = self.name_input.text().strip() or self.account.username
        self.account.group = self.group_input.text().strip() or "Default"
        self.account.notes = self.notes_input.text().strip()
        self.account.password = self.password_input.text().strip()

        from core.totp import parse_universal_2fa_input
        raw_totp = self.totp_input.text().strip()
        if raw_totp:
            parsed = parse_universal_2fa_input(raw_totp)
            self.account.totp_secret = parsed[0]["secret_b32"] if parsed else raw_totp.replace(" ", "").upper()
        else:
            self.account.totp_secret = ""

        raw_cookie = self.cookie_input.text().strip()
        if raw_cookie:
            self.new_cookie = clean_cookie(raw_cookie)
            self.account.cookie = self.new_cookie
        self.accept()

    def _auto_login_from_edit(self):
        self._save()
        parent_win = self.parent()
        if parent_win and hasattr(parent_win, "_auto_login_account"):
            parent_win._auto_login_account(self.account)



class FastFlagsDialog(BorderlessDialog):
    """
    Video game style graphics and engine settings panel for configuring
    FastFlags, textures, shadows, post-processing, and renderer backend.
    """
    def __init__(self, storage: StorageManager, parent=None):
        super().__init__(parent, title="Engine & Graphics Settings", can_maximize=False, resizable=False)
        self.storage = storage
        self.setWindowTitle("Engine & Graphics Settings - AERAM")
        self.setFixedWidth(560)
        self._updating_preset = False
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(16, 12, 16, 14)

        # ── 1. Global Preset Bar ──
        grp_preset = QtWidgets.QGroupBox("GRAPHICS PRESET")
        h_preset = QtWidgets.QHBoxLayout(grp_preset)
        h_preset.setContentsMargins(12, 12, 12, 10)
        h_preset.setSpacing(10)

        lbl_preset = QtWidgets.QLabel("Active Preset:")
        lbl_preset.setStyleSheet("font-weight: bold; color: #E8E8E8;")
        h_preset.addWidget(lbl_preset)

        self.combo_preset = QtWidgets.QComboBox()
        self.combo_preset.addItems([
            "Competitive Potato (Maximum FPS & Minimum VRAM)",
            "Balanced (Recommended Default)",
            "High Fidelity (Maximum Visual Quality)",
            "Custom",
        ])
        self.combo_preset.setFixedHeight(28)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        h_preset.addWidget(self.combo_preset, stretch=1)
        layout.addWidget(grp_preset)

        # ── 2. Video & Rendering Quality ──
        grp_video = QtWidgets.QGroupBox("VIDEO && RENDERING QUALITY")
        grid = QtWidgets.QGridLayout(grp_video)
        grid.setContentsMargins(12, 14, 12, 12)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)

        # Texture Quality (1 to 4)
        grid.addWidget(QtWidgets.QLabel("Texture Resolution:"), 0, 0)
        self.combo_texture = QtWidgets.QComboBox()
        self.combo_texture.addItems([
            "1 • Potato (Lowest VRAM)",
            "2 • Low",
            "3 • Medium",
            "4 • High (Full Resolution)",
        ])
        self.combo_texture.setFixedWidth(240)
        self.combo_texture.setFixedHeight(26)
        self.combo_texture.currentIndexChanged.connect(self._on_control_changed)
        grid.addWidget(self.combo_texture, 0, 1)

        # Graphics Level (1 to 10)
        grid.addWidget(QtWidgets.QLabel("Render Quality Level:"), 1, 0)
        self.combo_level = QtWidgets.QComboBox()
        self.combo_level.addItems([
            "Level 1 (Minimum / Alt Potato)",
            "Level 2",
            "Level 3",
            "Level 4 (Standard)",
            "Level 5",
            "Level 6",
            "Level 7",
            "Level 8",
            "Level 9",
            "Level 10 (Ultra / Maximum Draw Distance)",
        ])
        self.combo_level.setFixedWidth(240)
        self.combo_level.setFixedHeight(26)
        self.combo_level.currentIndexChanged.connect(self._on_control_changed)
        grid.addWidget(self.combo_level, 1, 1)

        # Shadows & Lighting
        grid.addWidget(QtWidgets.QLabel("Shadows & Lighting:"), 2, 0)
        self.combo_shadows = QtWidgets.QComboBox()
        self.combo_shadows.addItems([
            "Disabled (No Shadows, Frozen Lighting)",
            "Simple (Frozen Lighting)",
            "Full (Live Lighting & Shadows)",
        ])
        self.combo_shadows.setToolTip(
            "Disabled and Simple pause Roblox's voxel lighting (DFFlagDebugPauseVoxelizer) for more FPS, "
            "so torches, lamps and spells stop lighting their surroundings and dark areas can turn pitch black. "
            "Use Full for dark games."
        )
        self.combo_shadows.setFixedWidth(240)
        self.combo_shadows.setFixedHeight(26)
        self.combo_shadows.currentIndexChanged.connect(self._on_control_changed)
        grid.addWidget(self.combo_shadows, 2, 1)

        # Post-Processing & Effects
        grid.addWidget(QtWidgets.QLabel("Post-Processing & Bloom:"), 3, 0)
        self.combo_postfx = QtWidgets.QComboBox()
        self.combo_postfx.addItems([
            "Disabled (Sharp / Maximum Clarity)",
            "Enabled",
        ])
        self.combo_postfx.setToolTip(
            "Disabling removes bloom and the color correction many games use to brighten dark scenes."
        )
        self.combo_postfx.setFixedWidth(240)
        self.combo_postfx.setFixedHeight(26)
        self.combo_postfx.currentIndexChanged.connect(self._on_control_changed)
        grid.addWidget(self.combo_postfx, 3, 1)

        # Particle Emitters
        grid.addWidget(QtWidgets.QLabel("Particle Emitters:"), 4, 0)
        self.combo_particles = QtWidgets.QComboBox()
        self.combo_particles.addItems([
            "Disabled (No Particles)",
            "Reduced Particles",
            "Full Particles",
        ])
        self.combo_particles.setFixedWidth(240)
        self.combo_particles.setFixedHeight(26)
        self.combo_particles.currentIndexChanged.connect(self._on_control_changed)
        grid.addWidget(self.combo_particles, 4, 1)

        # Anti-Aliasing (MSAA)
        grid.addWidget(QtWidgets.QLabel("Anti-Aliasing (MSAA):"), 5, 0)
        self.combo_msaa = QtWidgets.QComboBox()
        self.combo_msaa.addItems([
            "Off (Crisp Raw Pixels)",
            "MSAA 2x",
            "MSAA 4x (Smooth Edges)",
        ])
        self.combo_msaa.setFixedWidth(240)
        self.combo_msaa.setFixedHeight(26)
        self.combo_msaa.currentIndexChanged.connect(self._on_control_changed)
        grid.addWidget(self.combo_msaa, 5, 1)

        layout.addWidget(grp_video)

        # ── 3. Engine & System Diagnostics ──
        grp_engine = QtWidgets.QGroupBox("ENGINE && SYSTEM")
        grid_eng = QtWidgets.QGridLayout(grp_engine)
        grid_eng.setContentsMargins(12, 14, 12, 10)
        grid_eng.setHorizontalSpacing(14)
        grid_eng.setVerticalSpacing(8)
        grid_eng.setColumnStretch(0, 1)
        grid_eng.setColumnStretch(1, 0)

        # Graphics API
        grid_eng.addWidget(QtWidgets.QLabel("Graphics Backend:"), 0, 0)
        self.combo_api = QtWidgets.QComboBox()
        self.combo_api.addItems(["Auto (System Default)", "Vulkan", "OpenGL"])
        self.combo_api.setFixedWidth(240)
        self.combo_api.setFixedHeight(26)
        self.combo_api.setToolTip(
            "Vulkan runs the Main account on Mocktail's direct-vulkan backend for the lowest input latency "
            "(experimental: the chat text overlay and in-game Anti-AFK need OpenGL). Alts always use OpenGL."
        )
        self.combo_api.currentIndexChanged.connect(self._on_control_changed)
        grid_eng.addWidget(self.combo_api, 0, 1)

        # Dynamic Resolution Scaling
        grid_eng.addWidget(QtWidgets.QLabel("Dynamic Resolution (DRS):"), 1, 0)
        self.combo_drs = QtWidgets.QComboBox()
        self.combo_drs.addItems(["Disabled (Crisp Native)", "Enabled (Roblox Auto)"])
        self.combo_drs.setFixedWidth(240)
        self.combo_drs.setFixedHeight(26)
        self.combo_drs.currentIndexChanged.connect(self._on_control_changed)
        grid_eng.addWidget(self.combo_drs, 1, 1)

        # Telemetry & Crash Reporting
        grid_eng.addWidget(QtWidgets.QLabel("Roblox Analytics & Telemetry:"), 2, 0)
        self.combo_telemetry = QtWidgets.QComboBox()
        self.combo_telemetry.addItems(["Blocked (Private & Lean)", "Enabled"])
        self.combo_telemetry.setFixedWidth(240)
        self.combo_telemetry.setFixedHeight(26)
        self.combo_telemetry.currentIndexChanged.connect(self._on_control_changed)
        grid_eng.addWidget(self.combo_telemetry, 2, 1)

        # Main account cursor
        grid_eng.addWidget(QtWidgets.QLabel("Main Cursor:"), 3, 0)
        self.combo_cursor = QtWidgets.QComboBox()
        self.combo_cursor.addItems(["Hardware (Lowest Latency)", "Roblox Drawn"])
        self.combo_cursor.setFixedWidth(240)
        self.combo_cursor.setFixedHeight(26)
        self.combo_cursor.setToolTip(
            "Hardware shows the desktop cursor, which moves at the display's refresh instead of "
            "waiting for the next rendered game frame. Needs AERAM's Mocktail build; games with a "
            "custom mouse icon may show both cursors. Applies on next launch."
        )
        grid_eng.addWidget(self.combo_cursor, 3, 1)

        # Custom shift-lock icon (kept across Roblox updates)
        grid_eng.addWidget(QtWidgets.QLabel("Shift-Lock Cursor:"), 4, 0)
        shiftlock_row = QtWidgets.QHBoxLayout()
        shiftlock_row.setSpacing(4)
        self.btn_shiftlock = QtWidgets.QPushButton()
        self.btn_shiftlock.setFixedWidth(208)
        self.btn_shiftlock.setFixedHeight(26)
        self.btn_shiftlock.setToolTip(
            "PNG shown instead of Roblox's shift-lock icon in every account, including after Roblox "
            "updates. Needs AERAM's Mocktail build. Applies on next launch."
        )
        self.btn_shiftlock.clicked.connect(self._browse_shiftlock_cursor)
        shiftlock_row.addWidget(self.btn_shiftlock)
        btn_shiftlock_reset = QtWidgets.QPushButton("✕")
        btn_shiftlock_reset.setFixedSize(28, 26)
        btn_shiftlock_reset.setToolTip("Use Roblox's shift-lock icon")
        btn_shiftlock_reset.clicked.connect(lambda: self._set_shiftlock_cursor(""))
        shiftlock_row.addWidget(btn_shiftlock_reset)
        grid_eng.addLayout(shiftlock_row, 4, 1)

        layout.addWidget(grp_engine)

        # ── 4. Action Buttons ──
        btn_box = QtWidgets.QHBoxLayout()
        btn_box.setSpacing(8)

        btn_defaults = QtWidgets.QPushButton("Reset Defaults")
        btn_defaults.setFixedHeight(28)
        btn_defaults.setFixedWidth(120)
        btn_defaults.clicked.connect(self._reset_defaults)
        btn_box.addWidget(btn_defaults)

        btn_box.addStretch()

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.setFixedHeight(28)
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        save_btn = QtWidgets.QPushButton("Apply Settings")
        save_btn.setObjectName("primaryBtn")
        save_btn.setFixedHeight(28)
        save_btn.setFixedWidth(140)
        save_btn.clicked.connect(self._save)
        btn_box.addWidget(save_btn)

        layout.addLayout(btn_box)
        self._load_saved_settings()

    def _load_saved_settings(self):
        saved_preset = self.storage.get_setting("fflag_preset", "Balanced")
        if saved_preset == "Potato":
            self.combo_preset.setCurrentIndex(0)
        elif saved_preset == "High":
            self.combo_preset.setCurrentIndex(2)
        elif saved_preset == "Custom":
            self.combo_preset.setCurrentIndex(3)
            # Load individual custom options
            tex = self.storage.get_setting("fflag_texture_quality", "2")
            try:
                self.combo_texture.setCurrentIndex(max(0, min(3, int(tex) - 1)))
            except ValueError:
                pass

            lvl = self.storage.get_setting("fflag_graphics_level", "4")
            try:
                self.combo_level.setCurrentIndex(max(0, min(9, int(lvl) - 1)))
            except ValueError:
                pass

            shadows = self.storage.get_setting("fflag_shadows", "low")
            self.combo_shadows.setCurrentIndex(0 if shadows == "off" else (1 if shadows == "low" else 2))

            postfx = self.storage.get_setting("fflag_postfx", "off")
            self.combo_postfx.setCurrentIndex(0 if postfx == "off" else 1)

            particles = self.storage.get_setting("fflag_particles", "reduced")
            self.combo_particles.setCurrentIndex(0 if particles == "disabled" else (1 if particles == "reduced" else 2))

            msaa = self.storage.get_setting("fflag_msaa", "0")
            self.combo_msaa.setCurrentIndex(0 if msaa == "0" else (1 if msaa == "2" else 2))
        else:
            self.combo_preset.setCurrentIndex(1)

        saved_api = self.storage.get_setting("fflag_graphics_api", "Auto")
        for i in range(self.combo_api.count()):
            if saved_api.lower() in self.combo_api.itemText(i).lower():
                self.combo_api.setCurrentIndex(i)
                break

        if self.storage.get_setting("fflag_disable_drs", "true").lower() == "false":
            self.combo_drs.setCurrentIndex(1)
        if self.storage.get_setting("fflag_disable_telemetry", "true").lower() == "false":
            self.combo_telemetry.setCurrentIndex(1)
        if self.storage.get_setting("main_host_cursor", "true").lower() == "false":
            self.combo_cursor.setCurrentIndex(1)
        self._set_shiftlock_cursor(self.storage.get_setting("shiftlock_cursor_path", "") or "")

    def _on_preset_changed(self, index: int):
        if self._updating_preset or index == 3:  # Custom
            return
        self._updating_preset = True
        try:
            if index == 0:  # Potato
                self.combo_texture.setCurrentIndex(0)  # Potato (1)
                self.combo_level.setCurrentIndex(0)    # Level 1
                self.combo_shadows.setCurrentIndex(0)  # Disabled
                self.combo_postfx.setCurrentIndex(0)  # Disabled
                self.combo_particles.setCurrentIndex(0) # Disabled
                self.combo_msaa.setCurrentIndex(0)     # Off
                self.combo_drs.setCurrentIndex(0)      # Disabled
            elif index == 1:  # Balanced
                self.combo_texture.setCurrentIndex(1)  # Low (2)
                self.combo_level.setCurrentIndex(3)    # Level 4
                self.combo_shadows.setCurrentIndex(1)  # Simple
                self.combo_postfx.setCurrentIndex(0)  # Disabled
                self.combo_particles.setCurrentIndex(1) # Reduced
                self.combo_msaa.setCurrentIndex(0)     # Off
                self.combo_drs.setCurrentIndex(0)      # Disabled
            elif index == 2:  # High Fidelity
                self.combo_texture.setCurrentIndex(3)  # High (4)
                self.combo_level.setCurrentIndex(9)    # Level 10
                self.combo_shadows.setCurrentIndex(2)  # Full
                self.combo_postfx.setCurrentIndex(1)  # Enabled
                self.combo_particles.setCurrentIndex(2) # Full
                self.combo_msaa.setCurrentIndex(2)     # MSAA 4x
                self.combo_drs.setCurrentIndex(0)      # Disabled
        finally:
            self._updating_preset = False

    def _on_control_changed(self):
        if not self._updating_preset:
            self._updating_preset = True
            try:
                self.combo_preset.setCurrentIndex(3)  # Custom
            finally:
                self._updating_preset = False

    def _reset_defaults(self):
        self.combo_preset.setCurrentIndex(1)  # Balanced
        self.combo_api.setCurrentIndex(0)
        self.combo_drs.setCurrentIndex(0)
        self.combo_telemetry.setCurrentIndex(0)
        self.combo_cursor.setCurrentIndex(0)
        self._set_shiftlock_cursor("")

    def _set_shiftlock_cursor(self, path: str):
        self._shiftlock_cursor_path = path
        self.btn_shiftlock.setText(os.path.basename(path) if path else "Roblox Default")

    def _browse_shiftlock_cursor(self):
        start = os.path.dirname(self._shiftlock_cursor_path) if self._shiftlock_cursor_path else os.path.expanduser("~")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Shift-Lock Cursor", start, "PNG Images (*.png)")
        if path:
            self._set_shiftlock_cursor(path)

    def _save(self):
        preset_idx = self.combo_preset.currentIndex()
        preset_name = "Potato" if preset_idx == 0 else ("Balanced" if preset_idx == 1 else ("High" if preset_idx == 2 else "Custom"))
        self.storage.set_setting("fflag_preset", preset_name)

        tex_val = str(self.combo_texture.currentIndex() + 1)
        self.storage.set_setting("fflag_texture_quality", tex_val)

        lvl_val = str(self.combo_level.currentIndex() + 1)
        self.storage.set_setting("fflag_graphics_level", lvl_val)

        sh_idx = self.combo_shadows.currentIndex()
        self.storage.set_setting("fflag_shadows", "off" if sh_idx == 0 else ("low" if sh_idx == 1 else "full"))

        self.storage.set_setting("fflag_postfx", "off" if self.combo_postfx.currentIndex() == 0 else "on")

        p_idx = self.combo_particles.currentIndex()
        self.storage.set_setting("fflag_particles", "disabled" if p_idx == 0 else ("reduced" if p_idx == 1 else "full"))

        m_idx = self.combo_msaa.currentIndex()
        self.storage.set_setting("fflag_msaa", "0" if m_idx == 0 else ("2" if m_idx == 1 else "4"))

        api_text = self.combo_api.currentText()
        if "Vulkan" in api_text:
            self.storage.set_setting("fflag_graphics_api", "Vulkan")
        elif "OpenGL" in api_text:
            self.storage.set_setting("fflag_graphics_api", "OpenGL")
        else:
            self.storage.set_setting("fflag_graphics_api", "Auto")

        self.storage.set_setting("fflag_disable_drs", "true" if self.combo_drs.currentIndex() == 0 else "false")
        self.storage.set_setting("fflag_disable_telemetry", "true" if self.combo_telemetry.currentIndex() == 0 else "false")
        self.storage.set_setting("main_host_cursor", "true" if self.combo_cursor.currentIndex() == 0 else "false")
        self.storage.set_setting("shiftlock_cursor_path", self._shiftlock_cursor_path)
        self.accept()


class SettingsDialog(BorderlessDialog):
    def __init__(self, storage: StorageManager, parent=None):
        super().__init__(parent, title="Settings", can_maximize=False, resizable=False)
        self.storage = storage
        self.setWindowTitle("Settings - AERAM")
        self.setFixedWidth(500)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(16, 12, 16, 14)

        # ── 1. Performance & Graphics ──
        grp_perf = QtWidgets.QGroupBox("PERFORMANCE && GRAPHICS")
        grid_perf = QtWidgets.QGridLayout(grp_perf)
        grid_perf.setContentsMargins(12, 14, 12, 10)
        grid_perf.setHorizontalSpacing(14)
        grid_perf.setVerticalSpacing(8)
        grid_perf.setColumnStretch(0, 1)
        grid_perf.setColumnStretch(1, 0)

        # Graphics preset (applied once with "Apply Preset", then Roblox's own menu owns it)
        lbl_gfx = QtWidgets.QLabel("Graphics Preset:")
        lbl_gfx.setToolTip("Roblox in-game graphics level for main accounts, applied with Apply Preset")
        self.combo_gfx = QtWidgets.QComboBox()
        preset_names = {1: "Potato", 3: "Low", 5: "Balanced", 7: "High", 10: "Ultra"}
        for level in range(1, 11):
            name = preset_names.get(level)
            self.combo_gfx.addItem(f"{level} - {name}" if name else str(level), level)
        self.combo_gfx.setFixedWidth(130)
        self.combo_gfx.setFixedHeight(26)
        try:
            saved_gfx = int(self.storage.get_setting("graphics_quality_level", "7"))
        except (ValueError, TypeError):
            saved_gfx = 7
        self.combo_gfx.setCurrentIndex(max(0, min(9, saved_gfx - 1)))
        grid_perf.addWidget(lbl_gfx, 0, 0)
        grid_perf.addWidget(self.combo_gfx, 0, 1)

        # Main Framerate Cap
        lbl_fps = QtWidgets.QLabel("Main Account FPS Cap:")
        lbl_fps.setToolTip("Maximum target framerate for Main (High profile) accounts")
        self.combo_fps = QtWidgets.QComboBox()
        self.combo_fps.addItems(["330 FPS", "240 FPS", "165 FPS", "Unlimited"])
        self.combo_fps.setFixedWidth(130)
        self.combo_fps.setFixedHeight(26)
        saved_fps = self.storage.get_setting("fps_cap", "330 FPS")
        idx = -1
        for i in range(self.combo_fps.count()):
            item_text = self.combo_fps.itemText(i)
            if item_text in saved_fps or (saved_fps and item_text.split()[0] in saved_fps):
                idx = i
                break
        if idx >= 0:
            self.combo_fps.setCurrentIndex(idx)
        grid_perf.addWidget(lbl_fps, 1, 0)
        grid_perf.addWidget(self.combo_fps, 1, 1)

        # Alt Target FPS
        lbl_alt_fps = QtWidgets.QLabel("Alt Accounts Target FPS:")
        lbl_alt_fps.setToolTip("Target framerate cap for background / alt accounts (15-24 recommended for loading screens)")
        self.spin_alt_fps = QtWidgets.QSpinBox()
        self.spin_alt_fps.setRange(1, 60)
        self.spin_alt_fps.setSuffix(" FPS")
        self.spin_alt_fps.setFixedWidth(130)
        self.spin_alt_fps.setFixedHeight(26)
        try:
            saved_alt_fps = int(self.storage.get_setting("alt_fps", DEFAULT_ALT_FPS))
        except (ValueError, TypeError):
            saved_alt_fps = int(DEFAULT_ALT_FPS)
        self.spin_alt_fps.setValue(saved_alt_fps)
        grid_perf.addWidget(lbl_alt_fps, 2, 0)
        grid_perf.addWidget(self.spin_alt_fps, 2, 1)

        # Alt Memory Target
        lbl_alt_mem = QtWidgets.QLabel("Alt Memory Target (Swap):")
        lbl_alt_mem.setToolTip("Virtual memory threshold that pages inactive alts into swap to keep physical RAM free")
        self.spin_alt_mem = QtWidgets.QSpinBox()
        self.spin_alt_mem.setRange(100, 4000)
        self.spin_alt_mem.setSingleStep(50)
        self.spin_alt_mem.setSuffix(" MB")
        self.spin_alt_mem.setFixedWidth(130)
        self.spin_alt_mem.setFixedHeight(26)
        try:
            saved_alt_mem = int(self.storage.get_setting("alt_memory_high_mb", "350"))
        except (ValueError, TypeError):
            saved_alt_mem = 350
        self.spin_alt_mem.setValue(saved_alt_mem)
        grid_perf.addWidget(lbl_alt_mem, 3, 0)
        grid_perf.addWidget(self.spin_alt_mem, 3, 1)

        # Camera Sensitivity
        lbl_sens = QtWidgets.QLabel("Camera Sensitivity:")
        lbl_sens.setToolTip("Roblox camera sensitivity (Escape -> Settings), applied with Apply Preset")
        self.spin_sens = QtWidgets.QDoubleSpinBox()
        self.spin_sens.setRange(0.01, 5.0)
        self.spin_sens.setSingleStep(0.05)
        self.spin_sens.setDecimals(2)
        self.spin_sens.setFixedWidth(130)
        self.spin_sens.setFixedHeight(26)
        try:
            self.spin_sens.setValue(float(self.storage.get_setting("mouse_sensitivity", "0.20")))
        except Exception:
            self.spin_sens.setValue(0.20)
        grid_perf.addWidget(lbl_sens, 4, 0)
        grid_perf.addWidget(self.spin_sens, 4, 1)

        # Apply the in-game preset once; Roblox's own menu changes persist afterwards
        self.lbl_preset = QtWidgets.QLabel("In-Game Preset:")
        self.lbl_preset.setToolTip(
            "Writes the graphics preset, camera sensitivity and shift-lock switch into every account at its "
            "next launch. Changes you make in Roblox's menu afterwards are kept; apply again to reset them."
        )
        btn_apply_preset = QtWidgets.QPushButton("Apply Preset")
        btn_apply_preset.setFixedWidth(130)
        btn_apply_preset.setFixedHeight(26)
        btn_apply_preset.clicked.connect(self._apply_engine_preset)
        grid_perf.addWidget(self.lbl_preset, 5, 0)
        grid_perf.addWidget(btn_apply_preset, 5, 1)

        # In-Game Engine & FastFlags
        lbl_ff = QtWidgets.QLabel("Client Engine & FastFlags:")
        lbl_ff.setToolTip("Advanced in-game graphics overrides, Vulkan/OpenGL renderer toggles, and custom FastFlags")
        self.btn_ff = QtWidgets.QPushButton("Configure...")
        self.btn_ff.setFixedWidth(130)
        self.btn_ff.setFixedHeight(26)
        self.btn_ff.clicked.connect(self._open_fastflags)
        grid_perf.addWidget(lbl_ff, 6, 0)
        grid_perf.addWidget(self.btn_ff, 6, 1)

        layout.addWidget(grp_perf)

        # ── 2. Automation & Launching ──
        grp_launch = QtWidgets.QGroupBox("AUTOMATION && LAUNCHING")
        grid_launch = QtWidgets.QGridLayout(grp_launch)
        grid_launch.setContentsMargins(12, 14, 12, 10)
        grid_launch.setHorizontalSpacing(14)
        grid_launch.setVerticalSpacing(8)
        grid_launch.setColumnStretch(0, 1)
        grid_launch.setColumnStretch(1, 0)

        # Stagger Delay
        lbl_stagger = QtWidgets.QLabel("Launch Stagger Delay:")
        lbl_stagger.setToolTip("Delay between launching consecutive accounts to prevent Roblox CAPTCHA and rate-limits")
        self.spin_stagger = QtWidgets.QDoubleSpinBox()
        self.spin_stagger.setRange(1.0, 60.0)
        self.spin_stagger.setSingleStep(1.0)
        self.spin_stagger.setDecimals(1)
        self.spin_stagger.setSuffix(" s")
        self.spin_stagger.setFixedWidth(130)
        self.spin_stagger.setFixedHeight(26)
        try:
            self.spin_stagger.setValue(float(self.storage.get_setting("stagger_delay", "15.0")))
        except Exception:
            self.spin_stagger.setValue(15.0)
        grid_launch.addWidget(lbl_stagger, 0, 0)
        grid_launch.addWidget(self.spin_stagger, 0, 1)

        # Trusted Friends
        lbl_friends = QtWidgets.QLabel("Trusted Friends (Auto-Follow):")
        lbl_friends.setToolTip("Comma-separated list of trusted friends. Self-Heal will automatically rejoin them if an alt disconnects.")
        self.input_trusted_friends = QtWidgets.QLineEdit()
        self.input_trusted_friends.setPlaceholderText("e.g. friend1, friend2, friend3")
        self.input_trusted_friends.setText(self.storage.get_setting("trusted_friends", ""))
        self.input_trusted_friends.setFixedHeight(26)
        grid_launch.addWidget(lbl_friends, 1, 0, 1, 2)
        grid_launch.addWidget(self.input_trusted_friends, 2, 0, 1, 2)

        # Check cookies on startup
        self.chk_startup_cookies = QtWidgets.QCheckBox("Validate account cookies on startup (background)")
        self.chk_startup_cookies.setToolTip("Automatically check if cookies are active and fetch updated Robux balances on launch")
        is_startup_check = self.storage.get_setting("check_cookies_on_startup", "true").lower() == "true"
        self.chk_startup_cookies.setChecked(is_startup_check)
        grid_launch.addWidget(self.chk_startup_cookies, 3, 0, 1, 2)

        layout.addWidget(grp_launch)

        # ── 3. Web Browser Integration ──
        grp_browser = QtWidgets.QGroupBox("WEB BROWSER INTEGRATION")
        grid_browser = QtWidgets.QGridLayout(grp_browser)
        grid_browser.setContentsMargins(12, 14, 12, 10)
        grid_browser.setHorizontalSpacing(14)
        grid_browser.setVerticalSpacing(8)
        grid_browser.setColumnStretch(0, 1)
        grid_browser.setColumnStretch(1, 0)

        lbl_engine = QtWidgets.QLabel("Browser Engine:")
        lbl_engine.setToolTip("Select the browser used for Web Login, Captcha verification, and account web sessions")
        self.combo_browser = QtWidgets.QComboBox()
        self.combo_browser.addItem("Embedded Chromium (WebEngine)", "internal")
        self.combo_browser.addItem("Default System Browser (Firefox)", "external")
        self.combo_browser.setFixedWidth(240)
        self.combo_browser.setFixedHeight(26)
        saved_engine = self.storage.get_setting("browser_engine", "internal")
        idx = self.combo_browser.findData(saved_engine)
        if idx >= 0:
            self.combo_browser.setCurrentIndex(idx)
        grid_browser.addWidget(lbl_engine, 0, 0)
        grid_browser.addWidget(self.combo_browser, 0, 1)
        layout.addWidget(grp_browser)

        # ── 4. System Diagnostics ──
        grp_diag = QtWidgets.QGroupBox("SYSTEM DIAGNOSTICS")
        h_diag = QtWidgets.QHBoxLayout(grp_diag)
        h_diag.setContentsMargins(12, 14, 12, 10)
        lbl_diag = QtWidgets.QLabel("Mocktail, Compositor & Drivers:")
        lbl_diag.setStyleSheet("color: #888888; font-size: 11px;")
        self.btn_open_diag = QtWidgets.QPushButton("Run Diagnostics...")
        self.btn_open_diag.setToolTip("Inspect Mocktail runtime, Python dependencies, desktop compositor, and kernel cgroups")
        self.btn_open_diag.setFixedHeight(26)
        self.btn_open_diag.setFixedWidth(160)
        self.btn_open_diag.clicked.connect(self._open_diagnostics)
        h_diag.addWidget(lbl_diag)
        h_diag.addStretch()
        h_diag.addWidget(self.btn_open_diag)
        layout.addWidget(grp_diag)

        # ── 4. Application Updates ──
        grp_update = QtWidgets.QGroupBox("APPLICATION UPDATES")
        h_update = QtWidgets.QHBoxLayout(grp_update)
        h_update.setContentsMargins(12, 14, 12, 10)
        lbl_update = QtWidgets.QLabel("GitHub Releases & Synchronization:")
        lbl_update.setStyleSheet("color: #888888; font-size: 11px;")
        self.btn_open_update = QtWidgets.QPushButton("Check for Updates...")
        self.btn_open_update.setToolTip("Check for updates and synchronize directly from GitHub")
        self.btn_open_update.setFixedHeight(26)
        self.btn_open_update.setFixedWidth(160)
        self.btn_open_update.clicked.connect(self._open_updater)
        h_update.addWidget(lbl_update)
        h_update.addStretch()
        h_update.addWidget(self.btn_open_update)
        layout.addWidget(grp_update)

        # ── 5. Action Buttons ──
        btn_box = QtWidgets.QHBoxLayout()
        btn_box.setSpacing(8)
        btn_box.addStretch()

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.setFixedHeight(28)
        cancel_btn.setFixedWidth(90)
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        save_btn = QtWidgets.QPushButton("Save Settings")
        save_btn.setObjectName("primaryBtn")
        save_btn.setFixedHeight(28)
        save_btn.setFixedWidth(120)
        save_btn.clicked.connect(self._save)
        btn_box.addWidget(save_btn)

        layout.addLayout(btn_box)

    def _open_fastflags(self):
        dlg = FastFlagsDialog(self.storage, self)
        dlg.exec()

    def _open_diagnostics(self):
        dlg = DiagnosticsDialog(self)
        dlg.exec()

    def _open_updater(self):
        dlg = UpdateDialog(self)
        dlg.exec()

    def _apply_engine_preset(self):
        self.storage.request_engine_preset(self.combo_gfx.currentData(), self.spin_sens.value())
        self.lbl_preset.setText("In-Game Preset: applied on next launch")

    def _save(self):
        selected_cap = self.combo_fps.currentText()
        self.storage.set_setting("fps_cap", selected_cap)
        if "330" in selected_cap:
            self.storage.set_setting("main_target_fps", "330")
        elif "165" in selected_cap:
            self.storage.set_setting("main_target_fps", "165")
        elif "240" in selected_cap:
            self.storage.set_setting("main_target_fps", "240")
        elif "Unlimited" in selected_cap:
            self.storage.set_setting("main_target_fps", "0")
        self.storage.set_setting("stagger_delay", str(self.spin_stagger.value()))
        self.storage.set_setting("alt_fps", str(self.spin_alt_fps.value()))
        self.storage.set_setting("alt_memory_high_mb", str(self.spin_alt_mem.value()))
        self.storage.set_setting("trusted_friends", self.input_trusted_friends.text().strip())
        self.storage.set_setting("check_cookies_on_startup", "true" if self.chk_startup_cookies.isChecked() else "false")
        self.storage.set_setting("browser_engine", self.combo_browser.currentData() or "external")

        # Synchronize sandbox profiles
        for acc in self.storage.list_accounts():
            self.storage.init_profile_sandbox(acc)

        self.accept()


class DiagnosticsDialog(BorderlessDialog):
    """System Health, Dependencies, and Portability Diagnostics Dialog."""

    def __init__(self, parent=None):
        super().__init__(parent, title="System Diagnostics & Portability", can_maximize=True, resizable=True)
        self.setWindowTitle("System Diagnostics & Portability - AERAM")
        self.setMinimumSize(620, 520)
        self.runner = DiagnosticsRunner()
        self.current_report = None
        self._init_ui()
        self._run_diagnostics()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # Header
        hdr = QtWidgets.QLabel("<b>System Health & Portability Diagnostics</b>")
        layout.addWidget(hdr)

        self.summary_lbl = QtWidgets.QLabel("Evaluating system environment...")
        self.summary_lbl.setWordWrap(True)
        layout.addWidget(self.summary_lbl)

        # Monospace Results Area
        self.results_view = QtWidgets.QTextEdit()
        self.results_view.setReadOnly(True)
        self.results_view.setStyleSheet(
            "font-family: monospace; font-size: 11px; background-color: #000000; color: #E8E8E8; border: 1px solid #353535; padding: 8px; border-radius: 0px;"
        )
        layout.addWidget(self.results_view, stretch=1)

        # Action Buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_repair = QtWidgets.QPushButton("Auto-Repair")
        self.btn_repair.setToolTip("Attempts safe automated fixes (e.g. compile native helper, ensure permissions)")
        self.btn_repair.clicked.connect(self._auto_repair)
        btn_row.addWidget(self.btn_repair)

        self.btn_copy = QtWidgets.QPushButton("Copy Report")
        self.btn_copy.clicked.connect(self._copy_report)
        btn_row.addWidget(self.btn_copy)

        self.btn_refresh = QtWidgets.QPushButton("Re-check")
        self.btn_refresh.clicked.connect(self._run_diagnostics)
        btn_row.addWidget(self.btn_refresh)

        btn_row.addStretch()

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def _run_diagnostics(self):
        self.current_report = self.runner.run_all_checks()
        if self.current_report.overall_ok:
            self.summary_lbl.setText(f"<span style='color: #FFFFFF; font-weight: bold;'>All systems ready:</span> {self.current_report.summary}")
        else:
            self.summary_lbl.setText(f"<span style='color: #FFFFFF; font-weight: bold;'>[!] Attention required:</span> {self.current_report.summary}")

        html_lines = []
        curr_cat = None
        for c in self.current_report.checks:
            if c.category != curr_cat:
                curr_cat = c.category
                html_lines.append(f"<br><b style='color: #FFFFFF;'>[{curr_cat}]</b><br>")

            if c.status == "OK":
                badge = "<span style='color: #FFFFFF; font-weight: bold;'>[  OK  ]</span>"
            elif c.status == "WARNING":
                badge = "<span style='color: #A8A8A8; font-weight: bold;'>[ WARN ]</span>"
            elif c.status == "ERROR":
                badge = "<span style='color: #FFFFFF; font-weight: bold; background-color: #353535;'>[ ERR  ]</span>"
            else:
                badge = "<span style='color: #707070;'>[ INFO ]</span>"

            html_lines.append(f"{badge} <b>{c.name}</b>: {c.message}")
            if c.details:
                html_lines.append(f"<div style='color: #707070; margin-left: 50px;'>Details: {c.details}</div>")
            if c.fix_suggestion:
                html_lines.append(f"<div style='color: #A8A8A8; margin-left: 50px;'>Fix: <code>{c.fix_suggestion}</code></div>")

        if self.current_report.arch_install_commands:
            html_lines.append("<br><b style='color: #FFFFFF;'>[RECOMMENDED ARCH / AUR COMMANDS]</b><br>")
            for cmd in self.current_report.arch_install_commands:
                html_lines.append(f"<span style='color: #E8E8E8;'>$ {cmd}</span><br>")

        self.results_view.setHtml("<body style='font-family: monospace;'>" + "".join(html_lines) + "</body>")

    def _auto_repair(self):
        fixes = self.runner.auto_repair()
        msg = "\n".join(fixes) if fixes else "No automated repairs needed."
        QMessageBox.information(self, "Auto-Repair Results", msg)
        self._run_diagnostics()

    def _copy_report(self):
        if self.current_report:
            QtWidgets.QApplication.clipboard().setText(self.current_report.to_text())
            QMessageBox.information(self, "Copied", "Diagnostic report copied to clipboard.")


class UpdateCheckWorker(QtCore.QThread):
    finished = QtCore.pyqtSignal(dict)

    def __init__(self, updater: AppUpdater, parent=None):
        super().__init__(parent)
        self.updater = updater

    def run(self):
        res = self.updater.check_for_updates()
        self.finished.emit(res)


class UpdateApplyWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str, int)
    finished = QtCore.pyqtSignal(bool, str)

    def __init__(self, updater: AppUpdater, parent=None):
        super().__init__(parent)
        self.updater = updater

    def run(self):
        success, msg = self.updater.apply_update(progress_callback=self._on_progress)
        self.finished.emit(success, msg)

    def _on_progress(self, text: str, pct: int):
        self.progress.emit(text, pct)


class UpdateDialog(BorderlessDialog):
    """GitHub Application Update and Synchronization Dialog."""

    def __init__(self, parent=None, auto_check: bool = True):
        super().__init__(parent, title="Application Updates", can_maximize=False, resizable=False)
        self.setWindowTitle("Application Updates - AERAM")
        self.setFixedWidth(500)
        self.updater = AppUpdater()
        self._check_worker: Optional[UpdateCheckWorker] = None
        self._apply_worker: Optional[UpdateApplyWorker] = None
        self._latest_info: Optional[Dict[str, Any]] = None
        self._init_ui()
        if auto_check:
            self._start_check()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(18, 16, 18, 16)

        hdr = QtWidgets.QLabel("<b>GitHub Synchronization & Updates</b>")
        hdr.setStyleSheet("font-size: 13px; color: #ffffff;")
        layout.addWidget(hdr)

        desc = QtWidgets.QLabel(
            "<span style='color: #888888; font-size: 11px;'>"
            "Checks the official repository (vysaely/aeram) for new releases and updates. "
            "Local database credentials and settings are strictly preserved."
            "</span>"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Version & Status Box
        box = QtWidgets.QGroupBox("SYNCHRONIZATION STATUS")
        box_layout = QtWidgets.QGridLayout(box)
        box_layout.setContentsMargins(12, 14, 12, 12)
        box_layout.setHorizontalSpacing(14)
        box_layout.setVerticalSpacing(8)

        lbl_loc_title = QtWidgets.QLabel("Local Version:")
        lbl_loc_title.setStyleSheet("color: #888888;")
        self.lbl_local = QtWidgets.QLabel(self.updater.get_local_commit()[:12] if self.updater.get_local_commit() != "unknown" else "Unknown")
        self.lbl_local.setStyleSheet("font-family: monospace; color: #ffffff;")
        box_layout.addWidget(lbl_loc_title, 0, 0)
        box_layout.addWidget(self.lbl_local, 0, 1)

        lbl_rem_title = QtWidgets.QLabel("Remote Version:")
        lbl_rem_title.setStyleSheet("color: #888888;")
        self.lbl_remote = QtWidgets.QLabel("Checking...")
        self.lbl_remote.setStyleSheet("font-family: monospace; color: #ffffff;")
        box_layout.addWidget(lbl_rem_title, 1, 0)
        box_layout.addWidget(self.lbl_remote, 1, 1)

        lbl_stat_title = QtWidgets.QLabel("Status:")
        lbl_stat_title.setStyleSheet("color: #888888;")
        self.lbl_status = QtWidgets.QLabel("Querying GitHub...")
        self.lbl_status.setStyleSheet("color: #ffffff; font-weight: bold;")
        self.lbl_status.setWordWrap(True)
        box_layout.addWidget(lbl_stat_title, 2, 0)
        box_layout.addWidget(self.lbl_status, 2, 1)

        lbl_commit_title = QtWidgets.QLabel("Latest Commit:")
        lbl_commit_title.setStyleSheet("color: #888888;")
        self.lbl_commit = QtWidgets.QLabel("-")
        self.lbl_commit.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        self.lbl_commit.setWordWrap(True)
        box_layout.addWidget(lbl_commit_title, 3, 0)
        box_layout.addWidget(self.lbl_commit, 3, 1)

        layout.addWidget(box)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(18)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #000000;
                border: 1px solid #353535;
                border-radius: 0px;
                text-align: center;
                color: #E8E8E8;
                font-size: 10px;
                font-family: monospace;
            }
            QProgressBar::chunk {
                background-color: #E8E8E8;
            }
        """)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.lbl_log = QtWidgets.QLabel("")
        self.lbl_log.setStyleSheet("color: #888888; font-size: 11px;")
        self.lbl_log.setWordWrap(True)
        layout.addWidget(self.lbl_log)

        # Buttons
        btn_box = QtWidgets.QHBoxLayout()
        btn_box.setSpacing(8)

        self.btn_check = QtWidgets.QPushButton("Check Again")
        self.btn_check.setFixedHeight(28)
        self.btn_check.clicked.connect(self._start_check)
        btn_box.addWidget(self.btn_check)

        self.btn_update = QtWidgets.QPushButton("Update Now")
        self.btn_update.setObjectName("primaryBtn")
        self.btn_update.setFixedHeight(28)
        self.btn_update.setEnabled(False)
        self.btn_update.clicked.connect(self._start_update)
        btn_box.addWidget(self.btn_update)

        self.btn_restart = QtWidgets.QPushButton("Restart AERAM")
        self.btn_restart.setObjectName("primaryBtn")
        self.btn_restart.setFixedHeight(28)
        self.btn_restart.setVisible(False)
        self.btn_restart.clicked.connect(self._restart_app)
        btn_box.addWidget(self.btn_restart)

        btn_box.addStretch()

        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.setFixedHeight(28)
        self.btn_close.setFixedWidth(80)
        self.btn_close.clicked.connect(self.accept)
        btn_box.addWidget(self.btn_close)

        layout.addLayout(btn_box)

    def _start_check(self):
        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.lbl_remote.setText("Checking...")
        self.lbl_status.setText("Connecting to GitHub...")
        self.lbl_status.setStyleSheet("color: #ffffff;")
        self.lbl_commit.setText("-")
        self.lbl_log.setText("")
        self.progress_bar.setVisible(False)

        self._check_worker = UpdateCheckWorker(self.updater, self)
        self._check_worker.finished.connect(self._on_check_finished)
        self._check_worker.start()

    def _on_check_finished(self, res: Dict[str, Any]):
        self.btn_check.setEnabled(True)
        self._latest_info = res

        if not res.get("success"):
            self.lbl_remote.setText("Unavailable")
            self.lbl_status.setText("Failed to check for updates")
            self.lbl_status.setStyleSheet("color: #ff5555; font-weight: bold;")
            self.lbl_log.setText(res.get("error", "Network error."))
            return

        remote_sha = res.get("remote_sha", "")
        self.lbl_remote.setText(remote_sha[:12] if remote_sha else "Unknown")

        commit_msg = res.get("commit_msg", "")
        if commit_msg:
            self.lbl_commit.setText(commit_msg)

        if res.get("update_available"):
            behind = res.get("behind_count", 0)
            behind_str = f" ({behind} commits behind)" if behind > 0 else ""
            self.lbl_status.setText(f"Update available{behind_str}")
            self.lbl_status.setStyleSheet("color: #ffffff; font-weight: bold;")
            self.btn_update.setEnabled(True)
        else:
            self.lbl_status.setText("AERAM is up to date.")
            self.lbl_status.setStyleSheet("color: #ffffff; font-weight: bold;")
            self.btn_update.setEnabled(False)

    def _start_update(self):
        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(5)
        self.lbl_log.setText("Preparing update...")

        self._apply_worker = UpdateApplyWorker(self.updater, self)
        self._apply_worker.progress.connect(self._on_update_progress)
        self._apply_worker.finished.connect(self._on_update_finished)
        self._apply_worker.start()

    def _on_update_progress(self, msg: str, pct: int):
        self.progress_bar.setValue(pct)
        self.lbl_log.setText(msg)

    def _on_update_finished(self, success: bool, msg: str):
        self.btn_close.setEnabled(True)
        if success:
            self.progress_bar.setValue(100)
            self.lbl_status.setText("Update installed successfully.")
            self.lbl_status.setStyleSheet("color: #ffffff; font-weight: bold;")
            self.lbl_log.setText(msg + " Please restart AERAM to apply all changes.")
            self.btn_update.setVisible(False)
            self.btn_restart.setVisible(True)
        else:
            self.lbl_status.setText("Update failed.")
            self.lbl_status.setStyleSheet("color: #ff5555; font-weight: bold;")
            self.lbl_log.setText(msg)
            self.btn_check.setEnabled(True)
            self.btn_update.setEnabled(True)

    def _restart_app(self):
        import sys
        QtCore.QProcess.startDetached(sys.executable, sys.argv)
        QtWidgets.QApplication.quit()

    def closeEvent(self, event):
        if self._check_worker and self._check_worker.isRunning():
            self._check_worker.wait(1000)
        if self._apply_worker and self._apply_worker.isRunning():
            self._apply_worker.wait(1000)
        super().closeEvent(event)

    def reject(self):
        if self._check_worker and self._check_worker.isRunning():
            self._check_worker.wait(1000)
        if self._apply_worker and self._apply_worker.isRunning():
            self._apply_worker.wait(1000)
        super().reject()


class GroupManagerDialog(BorderlessDialog):
    """
    Dialog for managing and reordering account groups with drag-and-drop.
    Supports:
    - Drag & drop reordering of groups
    - Move Up / Move Down buttons
    - + New Group
    - Rename Group
    - Delete Group (moves accounts in that group to Default)
    """
    def __init__(self, storage, parent=None):
        super().__init__(parent, title="Manage & Reorder Groups", can_maximize=False, resizable=True)
        self.storage = storage
        self.setWindowTitle("Manage & Reorder Groups - AERAM")
        self.setMinimumSize(420, 480)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QtWidgets.QLabel("Account Groups Order")
        header.setStyleSheet("font-size: 13px; font-weight: bold; color: #ffffff;")
        layout.addWidget(header)

        desc = QtWidgets.QLabel(
            "<span style='color: #888888; font-size: 11px;'>"
            "Drag and drop groups up or down to set their custom display order. "
            "The order is automatically saved and applied across AERAM."
            "</span>"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        main_box = QtWidgets.QHBoxLayout()
        main_box.setSpacing(10)

        # Draggable List Widget
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.setDropIndicatorShown(True)
        self.list_widget.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        main_box.addWidget(self.list_widget, stretch=1)

        # Right-side button toolbar
        btn_layout = QtWidgets.QVBoxLayout()
        btn_layout.setSpacing(8)

        self.btn_up = QtWidgets.QPushButton("Move Up")
        self.btn_up.clicked.connect(self._move_up)
        btn_layout.addWidget(self.btn_up)

        self.btn_down = QtWidgets.QPushButton("Move Down")
        self.btn_down.clicked.connect(self._move_down)
        btn_layout.addWidget(self.btn_down)

        btn_layout.addSpacing(10)

        self.btn_new = QtWidgets.QPushButton("+ New Group")
        self.btn_new.clicked.connect(self._new_group)
        btn_layout.addWidget(self.btn_new)

        self.btn_rename = QtWidgets.QPushButton("Rename")
        self.btn_rename.clicked.connect(self._rename_group)
        btn_layout.addWidget(self.btn_rename)

        self.btn_delete = QtWidgets.QPushButton("Delete")
        self.btn_delete.clicked.connect(self._delete_group)
        btn_layout.addWidget(self.btn_delete)

        btn_layout.addStretch()
        main_box.addLayout(btn_layout)

        layout.addLayout(main_box)

        # Bottom Close button
        bottom_box = QtWidgets.QHBoxLayout()
        bottom_box.addStretch()
        btn_close = QtWidgets.QPushButton("Done")
        btn_close.setObjectName("primaryBtn")
        btn_close.clicked.connect(self.accept)
        bottom_box.addWidget(btn_close)
        layout.addLayout(bottom_box)

        self._load_groups()

    def _load_groups(self):
        self.list_widget.clear()
        groups = self.storage.list_groups()
        for g in groups:
            item = QtWidgets.QListWidgetItem(f"≡  {g}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, g)
            self.list_widget.addItem(item)
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def _on_rows_moved(self, *args):
        self._save_order()

    def _save_order(self):
        new_order = [
            self.list_widget.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
            for i in range(self.list_widget.count())
        ]
        self.storage.set_group_order(new_order)

    def _move_up(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            return
        item = self.list_widget.takeItem(row)
        self.list_widget.insertItem(row - 1, item)
        self.list_widget.setCurrentRow(row - 1)
        self._save_order()

    def _move_down(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= self.list_widget.count() - 1:
            return
        item = self.list_widget.takeItem(row)
        self.list_widget.insertItem(row + 1, item)
        self.list_widget.setCurrentRow(row + 1)
        self._save_order()

    def _new_group(self):
        name, ok = BorderlessInputDialog.getText(self, "New Group", "Enter group name:")
        if ok and name.strip():
            clean = name.strip()
            self.storage.add_custom_group(clean)
            self._load_groups()
            for i in range(self.list_widget.count()):
                if self.list_widget.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == clean:
                    self.list_widget.setCurrentRow(i)
                    break

    def _rename_group(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        old_name = self.list_widget.item(row).data(QtCore.Qt.ItemDataRole.UserRole)
        name, ok = BorderlessInputDialog.getText(self, "Rename Group", f"Rename group '{old_name}' to:", text=old_name)
        if ok and name.strip() and name.strip() != old_name:
            clean = name.strip()
            self.storage.rename_group(old_name, clean)
            self._load_groups()
            self.list_widget.setCurrentRow(row)

    def _delete_group(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        name = self.list_widget.item(row).data(QtCore.Qt.ItemDataRole.UserRole)
        if name == "Default":
            QMessageBox.warning(self, "Cannot Delete", "The 'Default' group cannot be deleted.")
            return
        reply = QMessageBox.question(
            self, "Delete Group",
            f"Are you sure you want to delete group '{name}'?\nAccounts in this group will be moved to 'Default'.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.storage.delete_group(name, fallback_group="Default")
            self._load_groups()


class UnblockWorker(QtCore.QThread):
    """Background worker that unblocks users sequentially with respectful rate-limit pacing."""
    progress = QtCore.pyqtSignal(int, int, int)  # current, total, user_id
    finished = QtCore.pyqtSignal(int, int)       # unblocked_count, failed_count

    def __init__(self, api: RobloxAPI, cookie: str, blocked_users: List[Dict[str, Any]], parent=None):
        super().__init__(parent)
        self.api = api
        self.cookie = cookie
        self.blocked_users = blocked_users
        self.stopped = False

    def run(self):
        total = len(self.blocked_users)
        unblocked = 0
        failed = 0
        csrf_token = None

        for idx, u in enumerate(self.blocked_users):
            if self.stopped:
                break
            u_id = u["id"]
            self.progress.emit(idx + 1, total, u_id)
            success, fresh_csrf = self.api.unblock_user(u_id, self.cookie, csrf_token=csrf_token)
            if fresh_csrf:
                csrf_token = fresh_csrf
            if success:
                unblocked += 1
            else:
                failed += 1
            time.sleep(0.25)  # Respectful 250ms spacing to avoid Roblox 429 rate limits

        self.finished.emit(unblocked, failed)

    def stop(self):
        self.stopped = True


class UnblockEveryoneDialog(BorderlessDialog):
    """
    Borderless dialog allowing one-click automatic unblocking of all blocked users
    on a selected Roblox account without needing any browser extensions.
    """

    def __init__(self, account: RobloxAccount, api: RobloxAPI, parent=None):
        super().__init__(parent, title="Unblock Everyone", can_maximize=False, resizable=False)
        self.account = account
        self.api = api
        self.blocked_users: List[Dict[str, Any]] = []
        self.worker: Optional[UnblockWorker] = None

        self.setFixedWidth(460)
        self._init_ui()
        QtCore.QTimer.singleShot(100, self._load_blocked_users)

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(18, 16, 18, 16)

        acc_lbl = QtWidgets.QLabel(f"Target Account: <b>@{self.account.username}</b> ({self.account.display_name})")
        acc_lbl.setStyleSheet("color: #ffffff; font-size: 11px;")
        layout.addWidget(acc_lbl)

        self.status_lbl = QtWidgets.QLabel("Connecting to Roblox API & scanning blocked users list...")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #888888; font-size: 11px; line-height: 1.4;")
        layout.addWidget(self.status_lbl)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(12)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #0c0c0c; border: 1px solid #1a1a1a; } "
            "QProgressBar::chunk { background-color: #ffffff; }"
        )
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.btn_row = QtWidgets.QHBoxLayout()
        self.btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        self.btn_row.addWidget(self.btn_cancel)

        self.btn_unblock = QtWidgets.QPushButton("Unblock All")
        self.btn_unblock.setObjectName("primaryBtn")
        self.btn_unblock.setFixedHeight(28)
        self.btn_unblock.setEnabled(False)
        self.btn_unblock.clicked.connect(self._start_unblock)
        self.btn_row.addWidget(self.btn_unblock)

        layout.addLayout(self.btn_row)

    def _load_blocked_users(self):
        if self.worker is not None:
            return
        cookie = self.account.cookie
        run_in_thread(self, lambda: self.api.get_blocked_users(cookie), self._on_blocked_users_loaded)

    def _on_blocked_users_loaded(self, result):
        try:
            if isinstance(result, Exception):
                raise result
            self.blocked_users = result
            count = len(self.blocked_users)
            if count == 0:
                self.status_lbl.setText("No blocked users found for this account. Your block list is completely clean!")
                self.btn_cancel.setText("Close")
            else:
                self.status_lbl.setText(
                    f"Found <b>{count}</b> blocked user(s) on @{self.account.username}.<br>"
                    "<span style='color: #888888;'>Click 'Unblock All' to remove everyone from your block list automatically.</span>"
                )
                self.btn_unblock.setEnabled(True)
        except Exception as e:
            self.status_lbl.setText(f"<span style='color: #ff5555;'>Failed to load blocked users: {e}</span>")
            self.btn_cancel.setText("Close")

    def _start_unblock(self):
        if not self.blocked_users:
            return

        self.btn_unblock.setEnabled(False)
        self.btn_cancel.setText("Stop")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(self.blocked_users))
        self.progress_bar.setValue(0)

        self.worker = UnblockWorker(self.api, self.account.cookie, self.blocked_users, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, current: int, total: int, user_id: int):
        self.progress_bar.setValue(current)
        self.status_lbl.setText(f"Unblocking user <b>{current} / {total}</b> (ID: {user_id})...")

    def _on_finished(self, unblocked: int, failed: int):
        self.progress_bar.setValue(len(self.blocked_users))
        if failed > 0:
            self.status_lbl.setText(f"Finished! Unblocked <b>{unblocked}</b> user(s) ({failed} failed).")
        else:
            self.status_lbl.setText(f"Successfully unblocked all <b>{unblocked}</b> user(s)!")
        self.btn_cancel.setText("Done")
        self.btn_unblock.setVisible(False)

    def _on_cancel_clicked(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1000)
            self.status_lbl.setText("Unblock process stopped by user.")
            self.btn_cancel.setText("Close")
            return
        self.accept()


class BatchSignOutWorker(QtCore.QThread):
    request_2fa = QtCore.pyqtSignal(dict)
    account_progress = QtCore.pyqtSignal(int, int, object)  # current_idx, total, account
    account_finished = QtCore.pyqtSignal(object, bool, str, str)  # account, ok, new_cookie, msg
    batch_complete = QtCore.pyqtSignal(int, int, list)  # success_count, fail_count, summary

    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, parent=None):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self._is_stopped = False
        self._2fa_event = threading.Event()
        self._2fa_code: Optional[str] = None

    def provide_2fa_code(self, code: Optional[str]):
        self._2fa_code = code
        self._2fa_event.set()

    def stop(self):
        self._is_stopped = True
        self._2fa_code = None
        self._2fa_event.set()

    def run(self):
        total = len(self.accounts)
        success_count = 0
        fail_count = 0
        summary_results = []

        for idx, acc in enumerate(self.accounts):
            if self._is_stopped:
                break

            # Polite pacing delay (2.0s) between accounts to respect Roblox's 25 req/min rate limit
            if idx > 0 and not self._is_stopped:
                for _ in range(20):
                    if self._is_stopped:
                        break
                    time.sleep(0.1)

            if self._is_stopped:
                break

            self.account_progress.emit(idx + 1, total, acc)

            def two_fa_callback(info: Dict[str, Any]) -> Optional[str]:
                if self._is_stopped:
                    return None

                attempt = info.get("attempt", 1)
                # If first attempt and account has a saved 2FA secret key, auto-generate TOTP
                if acc.totp_secret and attempt == 1:
                    try:
                        from core.totp import generate_totp
                        code = generate_totp(acc.totp_secret)
                        return code
                    except Exception as e:
                        pass

                self._2fa_event.clear()
                self._2fa_code = None
                # stop() may have fired between the check above and clear(); its wake-up is lost
                if self._is_stopped:
                    return None
                self.request_2fa.emit({
                    "account": acc,
                    "index": idx + 1,
                    "total": total,
                    "info": info,
                })
                self._2fa_event.wait()
                return self._2fa_code

            ok, new_cookie, msg = self.api.sign_out_of_all_sessions(acc.cookie, two_fa_callback=two_fa_callback)

            if ok:
                success_count += 1
                summary_results.append((acc, True, "Signed out & session rotated" if new_cookie else "Signed out"))
            else:
                fail_count += 1
                summary_results.append((acc, False, msg))
                # If rate limited, pause 15s to allow Roblox's sliding rate-limit window to clear
                if ("429" in msg or "Rate limit" in msg) and not self._is_stopped:
                    for _ in range(150):
                        if self._is_stopped:
                            break
                        time.sleep(0.1)

            self.account_finished.emit(acc, ok, new_cookie or "", msg)

        self.batch_complete.emit(success_count, fail_count, summary_results)


class BatchSignOutDialog(BorderlessDialog):
    """
    Dedicated dialog for signing out of all other sessions across one or multiple accounts.
    Automatically handles 2-Step Verification by prompting the user for 6-digit TOTP / backup
    codes account-by-account, allowing lightning-fast sequential code entry.
    """
    def __init__(
        self,
        accounts: List[RobloxAccount],
        api: RobloxAPI,
        storage: StorageManager,
        runner: Any = None,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent, title="Sign Out of Other Sessions", can_maximize=False, resizable=False)
        self.accounts = [a for a in accounts if a.cookie]
        self.api = api
        self.storage = storage
        self.runner = runner

        self.setWindowTitle(f"Sign Out of Other Sessions ({len(self.accounts)} Accounts) - AERAM")
        self.setFixedWidth(520)
        self.setFixedHeight(390)

        self.worker: Optional[BatchSignOutWorker] = None
        self._current_account: Optional[RobloxAccount] = None
        self._init_ui()
        QtCore.QTimer.singleShot(250, self._start_batch)

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(18, 16, 18, 16)

        desc = QtWidgets.QLabel(
            "<b>Log Out of All Other Sessions</b><br>"
            "<span style='color: #888888;'>Remotely terminates active sessions across other devices. If an account has Two-Step Verification, enter the 6-digit authenticator code below to proceed to the next account.</span>"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #141414; border: none; } "
            "QProgressBar::chunk { background-color: #ffffff; }"
        )
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Account info frame
        self.acc_frame = QtWidgets.QFrame()
        self.acc_frame.setStyleSheet("QFrame { background-color: #0a0a0a; border: 1px solid #1a1a1a; padding: 6px; }")
        acc_layout = QtWidgets.QVBoxLayout(self.acc_frame)
        acc_layout.setContentsMargins(10, 8, 10, 8)
        acc_layout.setSpacing(4)

        acc_header = QtWidgets.QHBoxLayout()
        acc_header.setSpacing(8)

        self.lbl_acc_num = QtWidgets.QLabel("Account 1 of 1:")
        self.lbl_acc_num.setStyleSheet("color: #777777; font-size: 11px;")
        acc_header.addWidget(self.lbl_acc_num)

        self.lbl_acc_name = QtWidgets.QLabel("@username")
        self.lbl_acc_name.setStyleSheet("color: #ffffff; font-weight: bold; font-size: 12px;")
        acc_header.addWidget(self.lbl_acc_name)

        self.lbl_acc_alias = QtWidgets.QLabel("[Alias]")
        self.lbl_acc_alias.setStyleSheet(
            "background-color: #11281a; border: 1px solid #205030; color: #4ee489; "
            "font-size: 10px; font-weight: bold; padding: 1px 6px; border-radius: 2px;"
        )
        acc_header.addWidget(self.lbl_acc_alias)
        acc_header.addStretch()
        acc_layout.addLayout(acc_header)

        self.lbl_status = QtWidgets.QLabel("Initializing session logout...")
        self.lbl_status.setStyleSheet("color: #888888; font-size: 11px;")
        self.lbl_status.setWordWrap(True)
        acc_layout.addWidget(self.lbl_status)

        layout.addWidget(self.acc_frame)

        # 2FA Input Container
        self.two_fa_frame = QtWidgets.QFrame()
        self.two_fa_frame.setStyleSheet("QFrame { background-color: #101010; border: 1px solid #262626; padding: 8px; }")
        tfa_layout = QtWidgets.QVBoxLayout(self.two_fa_frame)
        tfa_layout.setContentsMargins(10, 10, 10, 10)
        tfa_layout.setSpacing(8)

        tfa_title = QtWidgets.QLabel("<b style='color: #4ee489;'>2-STEP VERIFICATION REQUIRED</b>")
        tfa_title.setStyleSheet("font-size: 11px;")
        tfa_layout.addWidget(tfa_title)

        tfa_desc = QtWidgets.QLabel("Enter the 6-digit code from your Authenticator app (or 8-character backup code):")
        tfa_desc.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        tfa_layout.addWidget(tfa_desc)

        input_row = QtWidgets.QHBoxLayout()
        input_row.setSpacing(8)

        self.code_input = QtWidgets.QLineEdit()
        self.code_input.setPlaceholderText("000000")
        self.code_input.setMaxLength(12)
        self.code_input.setStyleSheet(
            "QLineEdit { background-color: #161616; border: 1px solid #333333; color: #ffffff; "
            "font-family: monospace; font-size: 15px; font-weight: bold; letter-spacing: 4px; padding: 6px 10px; } "
            "QLineEdit:focus { border-color: #4ee489; background-color: #1c1c1c; }"
        )
        self.code_input.returnPressed.connect(self._on_submit_code)
        input_row.addWidget(self.code_input, stretch=1)

        self.btn_submit_code = QtWidgets.QPushButton("Verify && Next (Enter)")
        self.btn_submit_code.setObjectName("primaryBtn")
        self.btn_submit_code.setStyleSheet(
            "QPushButton#primaryBtn { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; padding: 6px 14px; font-size: 11px; } "
            "QPushButton#primaryBtn:hover { background-color: #e0e0e0; } "
            "QPushButton#primaryBtn:pressed { background-color: #888888; }"
        )
        self.btn_submit_code.clicked.connect(self._on_submit_code)
        input_row.addWidget(self.btn_submit_code)

        self.btn_skip_acc = QtWidgets.QPushButton("Skip")
        self.btn_skip_acc.setToolTip("Skip this account and proceed to the next")
        self.btn_skip_acc.setStyleSheet(
            "QPushButton { background-color: #1a1a1a; border: 1px solid #333333; color: #888888; padding: 6px 12px; font-size: 11px; } "
            "QPushButton:hover { background-color: #242424; color: #ffffff; }"
        )
        self.btn_skip_acc.clicked.connect(self._on_skip_acc)
        input_row.addWidget(self.btn_skip_acc)

        tfa_layout.addLayout(input_row)

        self.lbl_error = QtWidgets.QLabel("")
        self.lbl_error.setStyleSheet("color: #ff5555; font-size: 11px; font-weight: bold;")
        self.lbl_error.setVisible(False)
        tfa_layout.addWidget(self.lbl_error)

        layout.addWidget(self.two_fa_frame)
        self.two_fa_frame.setVisible(False)

        # Bottom buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel All")
        self.btn_cancel.setStyleSheet(
            "QPushButton { background-color: #141414; border: 1px solid #2a2a2a; color: #888888; padding: 5px 14px; font-size: 11px; } "
            "QPushButton:hover { background-color: #222222; color: #ffffff; }"
        )
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self.btn_cancel)

        layout.addLayout(btn_row)

    def _start_batch(self):
        if not self.accounts:
            self.lbl_status.setText("No valid accounts selected.")
            self.btn_cancel.setText("Close")
            return

        self.worker = BatchSignOutWorker(self.accounts, self.api, self)
        self.worker.account_progress.connect(self._on_account_progress)
        self.worker.request_2fa.connect(self._on_request_2fa)
        self.worker.account_finished.connect(self._on_account_finished)
        self.worker.batch_complete.connect(self._on_batch_complete)
        self.worker.start()

    def _on_account_progress(self, current: int, total: int, acc: RobloxAccount):
        self._current_account = acc
        self.progress_bar.setValue(current - 1)
        self.lbl_acc_num.setText(f"Account {current} of {total}:")
        self.lbl_acc_name.setText(f"@{acc.username}")
        if acc.alias and acc.alias != acc.username:
            self.lbl_acc_alias.setText(f"[{acc.alias}]")
            self.lbl_acc_alias.setVisible(True)
        else:
            self.lbl_acc_alias.setVisible(False)

        self.lbl_status.setStyleSheet("color: #888888; font-size: 11px;")
        self.lbl_status.setText("Submitting sign out request...")
        self.two_fa_frame.setVisible(False)
        self.lbl_error.setVisible(False)

    def _on_request_2fa(self, data: Dict[str, Any]):
        acc = data["account"]
        info = data["info"]
        self.two_fa_frame.setVisible(True)
        self.btn_submit_code.setEnabled(True)
        self.code_input.setEnabled(True)
        self.code_input.clear()

        err = info.get("error")
        if err:
            self.lbl_error.setText(f"{err}")
            self.lbl_error.setVisible(True)
        else:
            self.lbl_error.setVisible(False)

        self.lbl_status.setStyleSheet("color: #ffd700; font-size: 11px; font-weight: bold;")
        self.lbl_status.setText("Two-Step Verification required. Enter code to proceed.")
        self.code_input.setFocus()

    def _on_submit_code(self):
        code = self.code_input.text().strip()
        if not code:
            return
        self.lbl_error.setVisible(False)
        self.btn_submit_code.setEnabled(False)
        self.code_input.setEnabled(False)
        self.lbl_status.setStyleSheet("color: #4ec9b0; font-size: 11px;")
        self.lbl_status.setText("Verifying code with Roblox...")
        if self.worker:
            self.worker.provide_2fa_code(code)

    def _on_skip_acc(self):
        self.two_fa_frame.setVisible(False)
        self.lbl_status.setStyleSheet("color: #888888; font-size: 11px;")
        self.lbl_status.setText("Skipping account...")
        if self.worker:
            self.worker.provide_2fa_code(None)

    def _on_account_finished(self, acc: RobloxAccount, ok: bool, new_cookie: str, msg: str):
        if ok:
            if new_cookie:
                acc.cookie = new_cookie
                self.storage.save_account(acc)
            if self.runner and hasattr(self.runner, "auth_state_manager"):
                self.runner.auth_state_manager.mark_ready(acc.account_id)
            self.lbl_status.setStyleSheet("color: #4ee489; font-size: 11px; font-weight: bold;")
            self.lbl_status.setText("Signed out successfully! Fresh session token saved to vault.")
        else:
            self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
            self.lbl_status.setText(f"Sign out failed: {msg}")

    def _on_batch_complete(self, success_count: int, fail_count: int, summary: list):
        self.progress_bar.setValue(len(self.accounts))
        self.two_fa_frame.setVisible(False)

        total = len(self.accounts)
        self.lbl_acc_num.setText("Batch Completed:")
        self.lbl_acc_name.setText(f"{success_count} / {total} Succeeded")
        self.lbl_acc_alias.setVisible(False)

        if fail_count == 0:
            self.lbl_status.setStyleSheet("color: #4ee489; font-size: 11px; font-weight: bold;")
            self.lbl_status.setText(
                f"Successfully signed out of all other sessions for all {total} account(s)!<br>"
                "<span style='color: #888888;'>All other devices invalidated. Fresh rotated sessions active in vault.</span>"
            )
        else:
            self.lbl_status.setStyleSheet("color: #ffffff; font-size: 11px;")
            self.lbl_status.setText(
                f"Finished processing {total} accounts:<br>"
                f"• <span style='color: #4ee489;'>{success_count} succeeded</span> (sessions rotated)<br>"
                f"• <span style='color: #ff5555;'>{fail_count} failed or skipped</span>"
            )

        self.btn_cancel.setText("Done")
        self.btn_cancel.setObjectName("primaryBtn")
        self.btn_cancel.setStyleSheet(
            "QPushButton#primaryBtn { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; padding: 6px 18px; font-size: 11px; } "
            "QPushButton#primaryBtn:hover { background-color: #e0e0e0; }"
        )

    def _on_cancel_clicked(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1000)
            self.reject()
            return
        self.accept()


class GoogleAuthImportDialog(BorderlessDialog):
    """
    Dialog for importing 2-Step Verification / TOTP secret keys from any standard Authenticator.
    Decodes 'otpauth-migration://' QR code screenshots or raw URIs, correlates accounts
    with AERAM's vault, and previews live 6-digit codes in real time.
    """
    def __init__(self, accounts: List[RobloxAccount], storage: StorageManager, parent=None):
        super().__init__(parent, title="Authenticator (2FA) Importer", can_maximize=False, resizable=True)
        self.accounts = accounts
        self.storage = storage
        self.parsed_records: List[Dict[str, Any]] = []

        self.setWindowTitle("Authenticator (2FA) Importer - AERAM")
        self.setFixedWidth(660)
        self.setMinimumHeight(540)
        self.setAcceptDrops(True)

        self._init_ui()

        self._live_timer = QtCore.QTimer(self)
        self._live_timer.timeout.connect(self._refresh_live_codes)
        self._live_timer.start(1000)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        image_paths = [p for p in paths if p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
        if image_paths:
            from core.totp import decode_qr_image_files
            decoded_list = decode_qr_image_files(image_paths)
            if decoded_list:
                self._process_multiple_payloads(decoded_list, source_label=f"{len(image_paths)} dropped image(s)")
            else:
                self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
                self.lbl_status.setText(f"No QR code found in {len(image_paths)} dropped image(s). Ensure QR codes are clearly visible.")

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 16, 18, 16)

        banner = QtWidgets.QLabel(
            "<b>Automatic 2FA Verification Setup (Universal TOTP)</b><br>"
            "<span style='color: #888888; font-size: 11px;'>"
            "1. Open your authenticator app (Google Authenticator, Microsoft Authenticator, Aegis, 2FAS, etc.) on your phone.<br>"
            "2. Tap <b>Transfer / Export accounts</b> to reveal the export QR code(s).<br>"
            "3. If you have multiple accounts, take screenshots of each QR code.<br>"
            "4. Select <b>all PNG screenshots at once</b> (or drag-and-drop multiple PNGs into this window)."
            "</span>"
        )
        banner.setStyleSheet("background-color: #0d0d0d; border: 1px solid #222222; padding: 10px; color: #cccccc;")
        banner.setWordWrap(True)
        layout.addWidget(banner)

        # Quick Import Actions Row
        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(8)

        self.btn_open_files = QtWidgets.QPushButton("Open QR Screenshot(s) / PNG(s)...")
        self.btn_open_files.setObjectName("primaryBtn")
        self.btn_open_files.setToolTip("Select one or multiple PNG/image files at once")
        self.btn_open_files.setStyleSheet(
            "QPushButton#primaryBtn { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; padding: 7px 14px; font-size: 11px; } "
            "QPushButton#primaryBtn:hover { background-color: #e0e0e0; }"
        )
        self.btn_open_files.clicked.connect(self._on_open_files)
        action_row.addWidget(self.btn_open_files)

        self.btn_paste_clipboard = QtWidgets.QPushButton("Paste from Clipboard")
        self.btn_paste_clipboard.setToolTip("Paste copied QR image or export URL from clipboard")
        self.btn_paste_clipboard.setStyleSheet(
            "QPushButton { background-color: #1a1a1a; border: 1px solid #333333; color: #cccccc; padding: 7px 12px; font-size: 11px; } "
            "QPushButton:hover { background-color: #242424; color: #ffffff; }"
        )
        self.btn_paste_clipboard.clicked.connect(self._on_paste_clipboard)
        action_row.addWidget(self.btn_paste_clipboard)

        self.btn_clear = QtWidgets.QPushButton("Clear List")
        self.btn_clear.setToolTip("Clear all imported 2FA records from table")
        self.btn_clear.setStyleSheet(
            "QPushButton { background-color: #141414; border: 1px solid #282828; color: #777777; padding: 7px 12px; font-size: 11px; } "
            "QPushButton:hover { background-color: #1e1e1e; color: #ff5555; }"
        )
        self.btn_clear.clicked.connect(self._on_clear_records)
        action_row.addWidget(self.btn_clear)

        layout.addLayout(action_row)

        # Direct text / link input row
        text_row = QtWidgets.QHBoxLayout()
        text_row.setSpacing(6)

        self.txt_manual = QtWidgets.QLineEdit()
        self.txt_manual.setPlaceholderText("Or paste 'otpauth-migration://' link or Base32 secret here...")
        self.txt_manual.returnPressed.connect(self._on_parse_text)
        text_row.addWidget(self.txt_manual, stretch=1)

        btn_parse = QtWidgets.QPushButton("Parse")
        btn_parse.clicked.connect(self._on_parse_text)
        text_row.addWidget(btn_parse)

        layout.addLayout(text_row)

        # Status / Feedback label
        self.lbl_status = QtWidgets.QLabel("Waiting for QR screenshot(s), file(s), or link...")
        self.lbl_status.setStyleSheet("color: #777777; font-size: 11px;")
        layout.addWidget(self.lbl_status)

        # Table of Parsed Secrets & Matches
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Authenticator Label", "Assign to AERAM Account", "Live Code Preview"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #0d0d0d; border: 1px solid #222222; } "
            "QHeaderView::section { background-color: #141414; color: #888888; border: 1px solid #1a1a1a; padding: 4px 6px; font-size: 10px; font-weight: bold; }"
        )
        layout.addWidget(self.table, stretch=1)

        # Bottom buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)

        btn_row.addStretch()

        self.btn_save = QtWidgets.QPushButton("Save 2FA Keys to Vault")
        self.btn_save.setObjectName("primaryBtn")
        self.btn_save.setEnabled(False)
        self.btn_save.setStyleSheet(
            "QPushButton#primaryBtn { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; padding: 6px 18px; font-size: 11px; } "
            "QPushButton#primaryBtn:hover { background-color: #e0e0e0; } "
            "QPushButton#primaryBtn:disabled { background-color: #222222; border-color: #222222; color: #555555; }"
        )
        self.btn_save.clicked.connect(self._on_save_to_vault)
        btn_row.addWidget(self.btn_save)

        layout.addLayout(btn_row)

    def _on_clear_records(self):
        self.parsed_records.clear()
        self.table.setRowCount(0)
        self.btn_save.setEnabled(False)
        self.lbl_status.setStyleSheet("color: #777777; font-size: 11px;")
        self.lbl_status.setText("Cleared. Waiting for QR screenshot(s) or link...")

    def _on_paste_clipboard(self):
        """Reads image or URI text from system clipboard and decodes it."""
        clipboard = QtWidgets.QApplication.clipboard()
        image = clipboard.image()
        if not image.isNull():
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                image.save(tmp_path, "PNG")
                from core.totp import decode_qr_image_files
                decoded_list = decode_qr_image_files([tmp_path])
                if decoded_list:
                    self._process_multiple_payloads(decoded_list, source_label="clipboard image")
                    return
                else:
                    self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
                    self.lbl_status.setText("Could not detect a valid QR code in clipboard image. Try saving to a file.")
                    return
            finally:
                try:
                    import os
                    os.unlink(tmp_path)
                except Exception:
                    pass

        # Try text from clipboard
        text = clipboard.text().strip()
        if text:
            self._process_raw_text(text)
        else:
            self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
            self.lbl_status.setText("Clipboard does not contain an image or text.")

    def _on_open_files(self):
        """Prompts user to select one or multiple QR code screenshot files."""
        file_paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Select Google Authenticator Export QR Screenshot(s)",
            "",
            "Image Files (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*)",
        )
        if not file_paths:
            return

        from core.totp import decode_qr_image_files
        decoded_list = decode_qr_image_files(file_paths)
        if decoded_list:
            self._process_multiple_payloads(decoded_list, source_label=f"{len(file_paths)} image file(s)")
        else:
            names = ", ".join(QtCore.QFileInfo(p).fileName() for p in file_paths[:3])
            if len(file_paths) > 3:
                names += f" and {len(file_paths) - 3} more"
            self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
            self.lbl_status.setText(f"No QR code found in {names}. Ensure the QR code is clearly visible.")

    def _on_parse_text(self):
        text = self.txt_manual.text().strip()
        if not text:
            return
        self._process_raw_text(text)

    def _merge_new_records(self, new_records: List[Dict[str, Any]]) -> int:
        """Merges new 2FA records into parsed_records, avoiding duplicate secrets."""
        existing_secrets = {r.get("secret_b32") for r in self.parsed_records if r.get("secret_b32")}
        added = 0
        for rec in new_records:
            sec = rec.get("secret_b32")
            if sec and sec in existing_secrets:
                continue
            self.parsed_records.append(rec)
            if sec:
                existing_secrets.add(sec)
            added += 1
        return added

    def _process_multiple_payloads(self, payloads: List[str], source_label: str = ""):
        from core.totp import parse_universal_2fa_input
        new_records = []
        for payload in payloads:
            recs = parse_universal_2fa_input(payload)
            if recs:
                new_records.extend(recs)

        if not new_records:
            self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
            self.lbl_status.setText("Could not extract any 2FA accounts from the provided QR payload(s).")
            return

        added = self._merge_new_records(new_records)
        self._populate_table()
        if source_label:
            self.lbl_status.setText(
                f"Decoded {len(new_records)} secret(s) from {source_label} ({added} new). Total: {len(self.parsed_records)}"
            )

    def _process_raw_text(self, text: str):
        from core.totp import parse_universal_2fa_input
        records = parse_universal_2fa_input(text)
        if not records:
            self.lbl_status.setStyleSheet("color: #ff5555; font-size: 11px;")
            self.lbl_status.setText("Invalid input. Provide 'otpauth-migration://' link(s), QR screenshot(s), or Base32 secret.")
            return

        added = self._merge_new_records(records)
        self._populate_table()
        self.lbl_status.setText(
            f"Parsed {len(records)} secret(s) ({added} new). Total: {len(self.parsed_records)}"
        )

    def _populate_table(self):
        # Preserve user assignments across re-renders
        prev_assignments = {}
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            if isinstance(combo, QtWidgets.QComboBox):
                acc = combo.currentData()
                if acc and row < len(self.parsed_records):
                    sec = self.parsed_records[row].get("secret_b32")
                    if sec:
                        prev_assignments[sec] = acc

        self.table.setRowCount(len(self.parsed_records))
        matched_count = 0

        for row, rec in enumerate(self.parsed_records):
            # Column 0: Label
            label_text = rec.get("name") or rec.get("clean_account_name") or "Secret Key"
            if rec.get("issuer") and rec["issuer"].lower() != "roblox":
                label_text += f" ({rec['issuer']})"
            item_label = QtWidgets.QTableWidgetItem(label_text)
            item_label.setForeground(QtGui.QColor("#cccccc"))
            self.table.setItem(row, 0, item_label)

            # Column 1: Match ComboBox
            combo = QtWidgets.QComboBox()
            combo.addItem("-- Do Not Assign --", None)

            clean_name = rec.get("clean_account_name", "").lower()
            rec_sec = rec.get("secret_b32", "")
            target_acc = prev_assignments.get(rec_sec)
            selected_idx = 0

            for idx, acc in enumerate(self.accounts):
                display = f"@{acc.username}"
                if acc.alias and acc.alias != acc.username:
                    display += f" [{acc.alias}]"
                if acc.totp_secret:
                    display += " (already has 2FA key)"
                combo.addItem(display, acc)

                if target_acc and acc.account_id == target_acc.account_id:
                    selected_idx = idx + 1
                elif selected_idx == 0:
                    acc_user = acc.username.lower()
                    acc_alias = (acc.alias or "").lower()
                    if clean_name and (clean_name == acc_user or clean_name == acc_alias or clean_name in acc_user):
                        selected_idx = idx + 1

            combo.setCurrentIndex(selected_idx)
            if selected_idx > 0:
                matched_count += 1
            self.table.setCellWidget(row, 1, combo)

            # Column 2: Live Code Preview
            code_lbl = QtWidgets.QLabel("------")
            code_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            code_lbl.setStyleSheet("font-family: monospace; font-size: 12px; font-weight: bold; color: #4ee489; padding: 2px 8px;")
            self.table.setCellWidget(row, 2, code_lbl)

        self._refresh_live_codes()
        self.lbl_status.setStyleSheet("color: #4ee489; font-size: 11px; font-weight: bold;")
        self.lbl_status.setText(f"Total: {len(self.parsed_records)} 2FA secret(s) decoded ({matched_count} auto-matched)")
        self.btn_save.setEnabled(len(self.parsed_records) > 0)

    def _refresh_live_codes(self):
        """Updates the live 6-digit TOTP code and remaining seconds in the table."""
        if not self.parsed_records or self.table.rowCount() == 0:
            return

        from core.totp import generate_totp, get_totp_remaining_seconds
        rem = get_totp_remaining_seconds()

        for row, rec in enumerate(self.parsed_records):
            secret = rec.get("secret_b32", "")
            widget = self.table.cellWidget(row, 2)
            if isinstance(widget, QtWidgets.QLabel) and secret:
                try:
                    code = generate_totp(secret)
                    formatted_code = f"{code[:3]} {code[3:]}"
                    widget.setText(f"{formatted_code} ({rem}s)")
                except Exception:
                    widget.setText("ERROR")

    def _on_save_to_vault(self):
        """Saves mapped 2FA secrets into the accounts database."""
        assigned_count = 0
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            if isinstance(combo, QtWidgets.QComboBox):
                acc: Optional[RobloxAccount] = combo.currentData()
                if acc and row < len(self.parsed_records):
                    secret = self.parsed_records[row].get("secret_b32", "")
                    if secret:
                        acc.totp_secret = secret
                        self.storage.save_account(acc)
                        assigned_count += 1

        if assigned_count > 0:
            QMessageBox.information(
                self,
                "2FA Keys Saved",
                f"Successfully linked 2FA Authenticator secret keys for {assigned_count} account(s)!\n\n"
                "• 'Log Out of All Other Sessions' will now automatically complete 2FA verification.\n"
                "• All secrets are securely encrypted in your local vault.",
            )
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "No Accounts Assigned",
                "Please assign at least one authenticator secret to an AERAM account using the dropdown menus.",
            )


class TwoFactorExportDialog(BorderlessDialog):
    """
    Dialog for viewing, copying, and exporting saved 2FA / TOTP secret keys
    to standard otpauth:// URIs, JSON, CSV, or text for safe backup.
    """
    def __init__(self, accounts: List[RobloxAccount], parent=None):
        super().__init__(parent, title="Export 2FA Secret Keys", can_maximize=False, resizable=True)
        self.accounts_with_2fa = [a for a in accounts if a.totp_secret]
        self._hide_keys = True

        self.setWindowTitle(f"Export 2FA Secret Keys ({len(self.accounts_with_2fa)} Accounts) - AERAM")
        self.setFixedWidth(660)
        self.setMinimumHeight(480)

        self._init_ui()

        self._live_timer = QtCore.QTimer(self)
        self._live_timer.timeout.connect(self._refresh_live_codes)
        self._live_timer.start(1000)

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 16, 18, 16)

        desc = QtWidgets.QLabel(
            "<b>Two-Step Verification (2FA) Secret Keys</b><br>"
            "<span style='color: #888888; font-size: 11px;'>"
            "Your 2FA keys are securely stored and encrypted in the local AERAM vault database. "
            "You can export them to standard <code>otpauth://</code> URIs, CSV, or JSON for backup or to import into authenticator apps (2FAS, Bitwarden, KeePassXC, Aegis)."
            "</span>"
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Top Control Row
        ctrl_row = QtWidgets.QHBoxLayout()
        ctrl_row.setSpacing(10)

        self.chk_hide_keys = QtWidgets.QCheckBox("Mask Secret Keys")
        self.chk_hide_keys.setChecked(True)
        self.chk_hide_keys.toggled.connect(self._on_toggle_mask)
        ctrl_row.addWidget(self.chk_hide_keys)

        ctrl_row.addStretch()

        self.lbl_count = QtWidgets.QLabel(f"{len(self.accounts_with_2fa)} account(s) have 2FA enabled")
        self.lbl_count.setStyleSheet("color: #777777; font-size: 11px;")
        ctrl_row.addWidget(self.lbl_count)

        layout.addLayout(ctrl_row)

        # Accounts Table
        self.table = QtWidgets.QTableWidget(len(self.accounts_with_2fa), 4)
        self.table.setHorizontalHeaderLabels(["Account", "Group", "2FA Secret Key (Base32)", "Live 6-Digit Code"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #0d0d0d; border: 1px solid #222222; } "
            "QHeaderView::section { background-color: #141414; color: #888888; border: 1px solid #1a1a1a; padding: 4px 6px; font-size: 10px; font-weight: bold; }"
        )

        for row, acc in enumerate(self.accounts_with_2fa):
            # Col 0: Account / Alias
            disp = f"@{acc.username}"
            if acc.alias and acc.alias != acc.username:
                disp += f" [{acc.alias}]"
            it_name = QtWidgets.QTableWidgetItem(disp)
            it_name.setForeground(QtGui.QColor("#ffffff"))
            self.table.setItem(row, 0, it_name)

            # Col 1: Group
            it_grp = QtWidgets.QTableWidgetItem(acc.group or "Default")
            it_grp.setForeground(QtGui.QColor("#888888"))
            self.table.setItem(row, 1, it_grp)

            # Col 2: Secret Key
            it_sec = QtWidgets.QTableWidgetItem(self._format_secret(acc.totp_secret))
            it_sec.setFont(QtGui.QFont("monospace"))
            it_sec.setForeground(QtGui.QColor("#cccccc"))
            self.table.setItem(row, 2, it_sec)

            # Col 3: Live Code
            lbl_code = QtWidgets.QLabel("------")
            lbl_code.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            lbl_code.setStyleSheet("font-family: monospace; font-size: 12px; font-weight: bold; color: #4ee489; padding: 2px 8px;")
            self.table.setCellWidget(row, 3, lbl_code)

        layout.addWidget(self.table, stretch=1)

        # Status feedback
        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setStyleSheet("color: #4ee489; font-size: 11px;")
        layout.addWidget(self.lbl_status)

        # Export Buttons Row
        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(8)

        self.btn_copy_uris = QtWidgets.QPushButton("Copy All (otpauth://)")
        self.btn_copy_uris.setToolTip("Copy standard otpauth:// URIs to clipboard (compatible with all 2FA apps)")
        self.btn_copy_uris.clicked.connect(self._copy_all_uris)
        action_row.addWidget(self.btn_copy_uris)

        self.btn_copy_pairs = QtWidgets.QPushButton("Copy User:Key Pairs")
        self.btn_copy_pairs.setToolTip("Copy plain username:secret pairs to clipboard")
        self.btn_copy_pairs.clicked.connect(self._copy_all_pairs)
        action_row.addWidget(self.btn_copy_pairs)

        self.btn_save_file = QtWidgets.QPushButton("Save to File...")
        self.btn_save_file.setObjectName("primaryBtn")
        self.btn_save_file.setStyleSheet(
            "QPushButton#primaryBtn { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; padding: 6px 14px; font-size: 11px; } "
            "QPushButton#primaryBtn:hover { background-color: #e0e0e0; }"
        )
        self.btn_save_file.clicked.connect(self._save_to_file)
        action_row.addWidget(self.btn_save_file)

        action_row.addStretch()

        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        action_row.addWidget(self.btn_close)

        layout.addLayout(action_row)

        self._refresh_live_codes()

    def _format_secret(self, secret: str) -> str:
        if self._hide_keys:
            if len(secret) > 8:
                return secret[:4] + "••••••••" + secret[-4:]
            return "••••••••"
        return secret

    def _on_toggle_mask(self, checked: bool):
        self._hide_keys = checked
        for row, acc in enumerate(self.accounts_with_2fa):
            it = self.table.item(row, 2)
            if it:
                it.setText(self._format_secret(acc.totp_secret))

    def _refresh_live_codes(self):
        from core.totp import generate_totp, get_totp_remaining_seconds
        rem = get_totp_remaining_seconds()
        for row, acc in enumerate(self.accounts_with_2fa):
            widget = self.table.cellWidget(row, 3)
            if isinstance(widget, QtWidgets.QLabel) and acc.totp_secret:
                try:
                    code = generate_totp(acc.totp_secret)
                    widget.setText(f"{code[:3]} {code[3:]} ({rem}s)")
                except Exception:
                    widget.setText("ERROR")

    def _generate_otpauth_uri(self, acc: RobloxAccount) -> str:
        sec = acc.totp_secret.replace(" ", "").replace("-", "").upper()
        return f"otpauth://totp/Roblox:{acc.username}?secret={sec}&issuer=Roblox"

    def _copy_all_uris(self):
        if not self.accounts_with_2fa:
            return
        uris = [self._generate_otpauth_uri(a) for a in self.accounts_with_2fa]
        QtWidgets.QApplication.clipboard().setText("\n".join(uris))
        self.lbl_status.setText(f"Copied {len(uris)} otpauth:// URI(s) to clipboard.")

    def _copy_all_pairs(self):
        if not self.accounts_with_2fa:
            return
        lines = [f"{a.username}: {a.totp_secret}" for a in self.accounts_with_2fa]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.lbl_status.setText(f"Copied {len(lines)} username:key pair(s) to clipboard.")

    def _save_to_file(self):
        if not self.accounts_with_2fa:
            return
        file_path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save 2FA Keys to File",
            "aeram_2fa_keys.txt",
            "otpauth URIs (*.txt);;JSON Format (*.json);;CSV Format (*.csv);;User:Key Pairs (*.txt);;All Files (*)",
        )
        if not file_path:
            return

        try:
            if "json" in selected_filter.lower() or file_path.endswith(".json"):
                data = [
                    {
                        "username": a.username,
                        "display_name": a.display_name or a.username,
                        "user_id": a.user_id,
                        "group": a.group,
                        "totp_secret": a.totp_secret,
                        "otpauth_uri": self._generate_otpauth_uri(a),
                    }
                    for a in self.accounts_with_2fa
                ]
                content = json.dumps(data, indent=2)
            elif "csv" in selected_filter.lower() or file_path.endswith(".csv"):
                lines = ["username,alias,group,totp_secret,otpauth_uri"]
                for a in self.accounts_with_2fa:
                    lines.append(f'"{a.username}","{a.alias}","{a.group}","{a.totp_secret}","{self._generate_otpauth_uri(a)}"')
                content = "\n".join(lines)
            elif "pairs" in selected_filter.lower():
                content = "\n".join(f"{a.username}: {a.totp_secret}" for a in self.accounts_with_2fa)
            else:
                # Default: otpauth URIs
                content = "\n".join(self._generate_otpauth_uri(a) for a in self.accounts_with_2fa)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            self.lbl_status.setText(f"Successfully saved {len(self.accounts_with_2fa)} keys to {QtCore.QFileInfo(file_path).fileName()}!")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to save file:\n{e}")

    def closeEvent(self, event):
        if hasattr(self, "_live_timer") and self._live_timer.isActive():
            self._live_timer.stop()
        super().closeEvent(event)

    def reject(self):
        if hasattr(self, "_live_timer") and self._live_timer.isActive():
            self._live_timer.stop()
        super().reject()



def generate_strong_password(length: int = 16) -> str:
    """Generates a cryptographically strong, compliant random password with letters, digits, and special characters."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd)
                and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd)
                and any(c in "!@#$%^&*()-_=+" for c in pwd)):
            return pwd


class ChangePasswordWorker(QtCore.QThread):
    request_2fa = QtCore.pyqtSignal(dict)
    finished = QtCore.pyqtSignal(bool, str, str)  # success, new_cookie, status_message

    def __init__(self, account: RobloxAccount, current_pass: str, new_pass: str, api: RobloxAPI, parent=None):
        super().__init__(parent)
        self.account = account
        self.current_pass = current_pass
        self.new_pass = new_pass
        self.api = api
        self._2fa_event = threading.Event()
        self._2fa_code: Optional[str] = None
        self._is_stopped = False

    def provide_2fa_code(self, code: Optional[str]):
        self._2fa_code = code
        self._2fa_event.set()

    def stop(self):
        self._is_stopped = True
        self._2fa_code = None
        self._2fa_event.set()

    def run(self):
        def two_fa_callback(info: Dict[str, Any]) -> Optional[str]:
            if self._is_stopped:
                return None
            attempt = info.get("attempt", 1)
            # 1. If account has saved totp_secret and first attempt, auto-generate TOTP code
            if self.account.totp_secret and attempt == 1:
                try:
                    from core.totp import generate_totp
                    code = generate_totp(self.account.totp_secret)
                    if code:
                        return code
                except Exception:
                    pass

            # 2. Otherwise request 2FA code from GUI thread
            self._2fa_event.clear()
            self._2fa_code = None
            # stop() may have fired between the check above and clear(); its wake-up is lost
            if self._is_stopped:
                return None
            self.request_2fa.emit(info)
            self._2fa_event.wait()
            return self._2fa_code

        try:
            ok, new_cookie, msg = self.api.change_password(
                cookie=self.account.cookie,
                current_password=self.current_pass,
                new_password=self.new_pass,
                two_fa_callback=two_fa_callback,
            )
            self.finished.emit(ok, new_cookie or "", msg)
        except Exception as e:
            self.finished.emit(False, "", str(e))


class ChangePasswordDialog(BorderlessDialog):
    """
    Borderless dialog allowing fast and secure Roblox account password changes directly from AERAM.
    Handles Two-Step Verification (2SV / TOTP) automatically via linked Google Authenticator
    keys or interactive prompt, and synchronizes updated credentials and rotated cookies
    into the encrypted vault.
    """

    def __init__(self, account: RobloxAccount, api: RobloxAPI, storage: StorageManager, parent=None):
        super().__init__(parent, title="Change Password", can_maximize=False, resizable=False)
        self.account = account
        self.api = api
        self.storage = storage
        self.worker: Optional[ChangePasswordWorker] = None

        self.setFixedWidth(480)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(20, 16, 20, 16)

        # Header info
        header_lbl = QtWidgets.QLabel(f"Target Account: <b>@{self.account.username}</b> ({self.account.display_name})")
        header_lbl.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: 600;")
        layout.addWidget(header_lbl)

        sub_lbl = QtWidgets.QLabel("Updates password directly on auth.roblox.com and synchronizes AERAM encrypted vault.")
        sub_lbl.setStyleSheet("color: #707070; font-size: 10px; font-family: monospace;")
        sub_lbl.setWordWrap(True)
        layout.addWidget(sub_lbl)

        # ── 1. Current Password ──
        curr_hdr_layout = QtWidgets.QHBoxLayout()
        curr_lbl = QtWidgets.QLabel("Current Password:")
        curr_lbl.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; font-weight: 600;")
        curr_hdr_layout.addWidget(curr_lbl)
        curr_hdr_layout.addStretch()

        self.lbl_auto_detected = QtWidgets.QLabel("")
        self.lbl_auto_detected.setStyleSheet("color: #4EAA50; font-size: 10px; font-family: monospace; font-weight: 600;")
        curr_hdr_layout.addWidget(self.lbl_auto_detected)
        layout.addLayout(curr_hdr_layout)

        curr_row = QtWidgets.QHBoxLayout()
        curr_row.setSpacing(6)
        self.curr_pwd_input = QtWidgets.QLineEdit()
        self.curr_pwd_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.curr_pwd_input.setFixedHeight(28)
        self.curr_pwd_input.setPlaceholderText("Enter current account password...")
        if self.account.password:
            self.curr_pwd_input.setText(self.account.password)
            self.lbl_auto_detected.setText("Loaded from secure vault")
        curr_row.addWidget(self.curr_pwd_input, stretch=1)

        self.btn_toggle_curr = QtWidgets.QPushButton("Show")
        self.btn_toggle_curr.setFixedSize(56, 28)
        self.btn_toggle_curr.clicked.connect(lambda: self._toggle_echo(self.curr_pwd_input, self.btn_toggle_curr))
        curr_row.addWidget(self.btn_toggle_curr)
        layout.addLayout(curr_row)

        # ── 2. New Password ──
        new_hdr_layout = QtWidgets.QHBoxLayout()
        new_lbl = QtWidgets.QLabel("New Password:")
        new_lbl.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; font-weight: 600;")
        new_hdr_layout.addWidget(new_lbl)
        new_hdr_layout.addStretch()

        self.btn_generate = QtWidgets.QPushButton("Generate Strong")
        self.btn_generate.setFixedHeight(22)
        self.btn_generate.setStyleSheet("background-color: #000000; color: #E8E8E8; border: 1px solid #444444; padding: 1px 8px; font-size: 10px; font-family: monospace; font-weight: 600;")
        self.btn_generate.setToolTip("Generate cryptographically secure 16-character password and copy to clipboard")
        self.btn_generate.clicked.connect(self._on_generate_strong_password)
        new_hdr_layout.addWidget(self.btn_generate)
        layout.addLayout(new_hdr_layout)

        new_row = QtWidgets.QHBoxLayout()
        new_row.setSpacing(6)
        self.new_pwd_input = QtWidgets.QLineEdit()
        self.new_pwd_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.new_pwd_input.setFixedHeight(28)
        self.new_pwd_input.setPlaceholderText("Enter new password (min. 8 characters)...")
        new_row.addWidget(self.new_pwd_input, stretch=1)

        self.btn_toggle_new = QtWidgets.QPushButton("Show")
        self.btn_toggle_new.setFixedSize(56, 28)
        self.btn_toggle_new.clicked.connect(lambda: self._toggle_echo(self.new_pwd_input, self.btn_toggle_new))
        new_row.addWidget(self.btn_toggle_new)
        layout.addLayout(new_row)

        # ── 3. Confirm New Password ──
        conf_lbl = QtWidgets.QLabel("Confirm New Password:")
        conf_lbl.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; font-weight: 600;")
        layout.addWidget(conf_lbl)

        conf_row = QtWidgets.QHBoxLayout()
        conf_row.setSpacing(6)
        self.conf_pwd_input = QtWidgets.QLineEdit()
        self.conf_pwd_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.conf_pwd_input.setFixedHeight(28)
        self.conf_pwd_input.setPlaceholderText("Re-enter new password...")
        conf_row.addWidget(self.conf_pwd_input, stretch=1)

        self.btn_toggle_conf = QtWidgets.QPushButton("Show")
        self.btn_toggle_conf.setFixedSize(56, 28)
        self.btn_toggle_conf.clicked.connect(lambda: self._toggle_echo(self.conf_pwd_input, self.btn_toggle_conf))
        conf_row.addWidget(self.btn_toggle_conf)
        layout.addLayout(conf_row)

        # ── 4. Two-Step Verification Status Box ──
        self.auth_frame = QtWidgets.QFrame()
        self.auth_frame.setStyleSheet("background-color: #080808; border: 1px solid #222222; padding: 6px;")
        auth_box = QtWidgets.QVBoxLayout(self.auth_frame)
        auth_box.setSpacing(4)
        auth_box.setContentsMargins(8, 6, 8, 6)

        if self.account.totp_secret:
            auth_status = QtWidgets.QLabel("Google Authenticator Linked — Auto-Verification Active")
            auth_status.setStyleSheet("color: #4EAA50; font-size: 11px; font-family: monospace; font-weight: bold; border: none;")
            auth_desc = QtWidgets.QLabel("Roblox 2FA challenges will be solved automatically using your saved TOTP key.")
            auth_desc.setStyleSheet("color: #888888; font-size: 10px; font-family: monospace; border: none;")
            auth_desc.setWordWrap(True)
        else:
            auth_status = QtWidgets.QLabel("Two-Step Verification (2FA)")
            auth_status.setStyleSheet("color: #A0A0A0; font-size: 11px; font-family: monospace; font-weight: bold; border: none;")
            auth_desc = QtWidgets.QLabel("If 2FA is active on this account, you will be prompted for your 6-digit code.")
            auth_desc.setStyleSheet("color: #707070; font-size: 10px; font-family: monospace; border: none;")
            auth_desc.setWordWrap(True)

        auth_box.addWidget(auth_status)
        auth_box.addWidget(auth_desc)

        # Embedded interactive 2FA prompt container (shown only if challenge arises)
        self.prompt_2fa_widget = QtWidgets.QWidget()
        self.prompt_2fa_widget.setVisible(False)
        p2fa_layout = QtWidgets.QVBoxLayout(self.prompt_2fa_widget)
        p2fa_layout.setContentsMargins(0, 4, 0, 0)
        p2fa_layout.setSpacing(4)

        self.lbl_2fa_prompt = QtWidgets.QLabel("Enter 6-Digit Authenticator or Email Code:")
        self.lbl_2fa_prompt.setStyleSheet("color: #E8E8E8; font-size: 10px; font-family: monospace; font-weight: bold; border: none;")
        p2fa_layout.addWidget(self.lbl_2fa_prompt)

        p2fa_row = QtWidgets.QHBoxLayout()
        p2fa_row.setSpacing(6)
        self.input_2fa_code = QtWidgets.QLineEdit()
        self.input_2fa_code.setFixedHeight(28)
        self.input_2fa_code.setPlaceholderText("6-digit code or backup code...")
        p2fa_row.addWidget(self.input_2fa_code, stretch=1)

        self.btn_submit_2fa = QtWidgets.QPushButton("Submit 2FA")
        self.btn_submit_2fa.setFixedHeight(28)
        self.btn_submit_2fa.setStyleSheet("background-color: #E8E8E8; color: #000000; font-weight: bold; padding: 2px 10px;")
        self.btn_submit_2fa.clicked.connect(self._on_submit_2fa_clicked)
        p2fa_row.addWidget(self.btn_submit_2fa)
        p2fa_layout.addLayout(p2fa_row)

        auth_box.addWidget(self.prompt_2fa_widget)
        layout.addWidget(self.auth_frame)

        # Status & Telemetry label
        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: #A0A0A0; font-size: 10px; font-family: monospace;")
        layout.addWidget(self.lbl_status)

        # Action Buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.setFixedWidth(80)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)

        self.btn_submit = QtWidgets.QPushButton("Change Password")
        self.btn_submit.setFixedHeight(28)
        self.btn_submit.setFixedWidth(140)
        self.btn_submit.setStyleSheet("background-color: #E8E8E8; color: #000000; font-weight: bold; border: 1px solid #E8E8E8;")
        self.btn_submit.clicked.connect(self._on_submit_clicked)
        btn_row.addWidget(self.btn_submit)

        layout.addLayout(btn_row)

    def _toggle_echo(self, line_edit: QtWidgets.QLineEdit, btn: QtWidgets.QPushButton):
        if line_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password:
            line_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
            btn.setText("Hide")
        else:
            line_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            btn.setText("Show")

    def _on_generate_strong_password(self):
        strong_pwd = generate_strong_password(16)
        self.new_pwd_input.setText(strong_pwd)
        self.conf_pwd_input.setText(strong_pwd)
        self.new_pwd_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
        self.conf_pwd_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
        self.btn_toggle_new.setText("Hide")
        self.btn_toggle_conf.setText("Hide")
        QtWidgets.QApplication.clipboard().setText(strong_pwd)
        self.lbl_status.setStyleSheet("color: #4EAA50; font-size: 10px; font-family: monospace;")
        self.lbl_status.setText("Generated strong 16-character password and copied to clipboard!")

    def _on_submit_clicked(self):
        curr_pass = self.curr_pwd_input.text().strip()
        new_pass = self.new_pwd_input.text().strip()
        conf_pass = self.conf_pwd_input.text().strip()

        if not curr_pass:
            self.lbl_status.setStyleSheet("color: #E57373; font-size: 10px; font-family: monospace;")
            self.lbl_status.setText("Error: Current password is required.")
            self.curr_pwd_input.setFocus()
            return

        if len(new_pass) < 8:
            self.lbl_status.setStyleSheet("color: #E57373; font-size: 10px; font-family: monospace;")
            self.lbl_status.setText("Error: New password must be at least 8 characters long.")
            self.new_pwd_input.setFocus()
            return

        if new_pass == curr_pass:
            self.lbl_status.setStyleSheet("color: #E57373; font-size: 10px; font-family: monospace;")
            self.lbl_status.setText("Error: New password cannot be the same as the current password.")
            self.new_pwd_input.setFocus()
            return

        if new_pass != conf_pass:
            self.lbl_status.setStyleSheet("color: #E57373; font-size: 10px; font-family: monospace;")
            self.lbl_status.setText("Error: New password and confirm password do not match.")
            self.conf_pwd_input.setFocus()
            return

        self._set_inputs_enabled(False)
        self.lbl_status.setStyleSheet("color: #E8E8E8; font-size: 10px; font-family: monospace;")
        self.lbl_status.setText("Connecting to auth.roblox.com and requesting password update...")

        self.worker = ChangePasswordWorker(self.account, curr_pass, new_pass, self.api, self)
        self.worker.request_2fa.connect(self._on_worker_request_2fa)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _set_inputs_enabled(self, enabled: bool):
        self.curr_pwd_input.setEnabled(enabled)
        self.btn_toggle_curr.setEnabled(enabled)
        self.new_pwd_input.setEnabled(enabled)
        self.btn_toggle_new.setEnabled(enabled)
        self.btn_generate.setEnabled(enabled)
        self.conf_pwd_input.setEnabled(enabled)
        self.btn_toggle_conf.setEnabled(enabled)
        self.btn_submit.setEnabled(enabled)

    def _on_worker_request_2fa(self, info: dict):
        self.lbl_status.setStyleSheet("color: #FFA726; font-size: 10px; font-family: monospace;")
        err = info.get("error")
        if err:
            self.lbl_status.setText(f"2FA Challenge: {err}")
        else:
            self.lbl_status.setText("Roblox Two-Step Verification challenge triggered. Enter code below:")

        self.prompt_2fa_widget.setVisible(True)
        self.input_2fa_code.clear()
        self.input_2fa_code.setFocus()

    def _on_submit_2fa_clicked(self):
        code = self.input_2fa_code.text().strip()
        if not code:
            self.lbl_status.setStyleSheet("color: #E57373; font-size: 10px; font-family: monospace;")
            self.lbl_status.setText("Please enter the 6-digit verification code.")
            return

        self.lbl_status.setStyleSheet("color: #E8E8E8; font-size: 10px; font-family: monospace;")
        self.lbl_status.setText("Verifying Two-Step authentication code with Roblox...")
        if self.worker:
            self.worker.provide_2fa_code(code)

    def _on_worker_finished(self, ok: bool, new_cookie: str, msg: str):
        self._set_inputs_enabled(True)
        self.prompt_2fa_widget.setVisible(False)

        if ok:
            new_pass = self.new_pwd_input.text().strip()
            self.account.password = new_pass
            if new_cookie:
                self.account.cookie = new_cookie
            self.storage.save_account(self.account)

            self.lbl_status.setStyleSheet("color: #4EAA50; font-size: 11px; font-family: monospace; font-weight: bold;")
            self.lbl_status.setText("Password updated successfully and saved to AERAM encrypted vault!")
            QMessageBox.information(
                self,
                "Password Changed",
                f"Password for @{self.account.username} was successfully changed on Roblox and updated in your AERAM vault."
            )
            self.accept()
        else:
            self.lbl_status.setStyleSheet("color: #E57373; font-size: 11px; font-family: monospace;")
            self.lbl_status.setText(f"Failed: {msg}")
            QMessageBox.warning(self, "Change Password Failed", f"Roblox rejected the password change:\n\n{msg}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().closeEvent(event)

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().reject()


class BatchPasswordChangeWorker(QtCore.QThread):
    account_started = QtCore.pyqtSignal(int, int, object)  # idx, total, account
    account_finished = QtCore.pyqtSignal(object, bool, str, str, str)  # acc, ok, new_pass, new_cookie, msg
    request_2fa = QtCore.pyqtSignal(object, dict)  # acc, info
    batch_complete = QtCore.pyqtSignal(int, int, list)  # success_cnt, fail_cnt, results

    def __init__(
        self,
        accounts: List[RobloxAccount],
        api: RobloxAPI,
        storage: StorageManager,
        mode: str = "unique_random",
        common_password: str = "",
        fallback_current_password: str = "",
        pacing_delay: float = 3.0,
        parent=None,
    ):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self.storage = storage
        self.mode = mode
        self.common_password = common_password
        self.fallback_current_password = fallback_current_password
        self.pacing_delay = max(0.5, float(pacing_delay))
        self._2fa_event = threading.Event()
        self._2fa_code: Optional[str] = None
        self._is_stopped = False

    def provide_2fa_code(self, code: Optional[str]):
        self._2fa_code = code
        self._2fa_event.set()

    def skip_current_account(self):
        self._2fa_code = None
        self._2fa_event.set()

    def stop(self):
        self._is_stopped = True
        self._2fa_code = None
        self._2fa_event.set()

    def run(self):
        total = len(self.accounts)
        success_count = 0
        fail_count = 0
        results = []

        for idx, acc in enumerate(self.accounts):
            if self._is_stopped:
                break

            # Polite pacing delay between accounts to prevent HTTP 429 rate limits
            if idx > 0 and not self._is_stopped:
                sleep_slices = int(self.pacing_delay * 10)
                for _ in range(sleep_slices):
                    if self._is_stopped:
                        break
                    time.sleep(0.1)

            if self._is_stopped:
                break

            self.account_started.emit(idx + 1, total, acc)

            curr_pass = acc.password or self.fallback_current_password
            if not curr_pass:
                fail_count += 1
                res = {"account": acc, "ok": False, "new_pass": "", "msg": "Current password missing in vault"}
                results.append(res)
                self.account_finished.emit(acc, False, "", "", "Current password missing")
                continue

            if self.mode == "unique_random":
                new_pass = generate_strong_password(16)
            else:
                new_pass = self.common_password

            def two_fa_callback(info: Dict[str, Any]) -> Optional[str]:
                if self._is_stopped:
                    return None
                attempt = info.get("attempt", 1)
                # 1. If account has saved totp_secret and first attempt, auto-generate TOTP code
                if acc.totp_secret and attempt == 1:
                    try:
                        from core.totp import generate_totp
                        code = generate_totp(acc.totp_secret)
                        if code:
                            return code
                    except Exception:
                        pass

                # 2. Otherwise request 2FA code from GUI thread
                self._2fa_event.clear()
                self._2fa_code = None
                # stop() may have fired between the check above and clear(); its wake-up is lost
                if self._is_stopped:
                    return None
                self.request_2fa.emit(acc, info)
                self._2fa_event.wait()
                return self._2fa_code

            try:
                ok, new_cookie, msg = self.api.change_password(
                    cookie=acc.cookie,
                    current_password=curr_pass,
                    new_password=new_pass,
                    two_fa_callback=two_fa_callback,
                )
                if ok:
                    acc.password = new_pass
                    if new_cookie:
                        acc.cookie = new_cookie
                    self.storage.save_account(acc)
                    success_count += 1
                    res = {"account": acc, "ok": True, "new_pass": new_pass, "new_cookie": new_cookie, "msg": "Success"}
                    results.append(res)
                    self.account_finished.emit(acc, True, new_pass, new_cookie or "", "Success")
                else:
                    fail_count += 1
                    res = {"account": acc, "ok": False, "new_pass": "", "msg": msg}
                    results.append(res)
                    self.account_finished.emit(acc, False, "", "", msg)
            except Exception as e:
                fail_count += 1
                res = {"account": acc, "ok": False, "new_pass": "", "msg": str(e)}
                results.append(res)
                self.account_finished.emit(acc, False, "", "", str(e))

        self.batch_complete.emit(success_count, fail_count, results)


class BatchPasswordChangeDialog(BorderlessDialog):
    """
    Dedicated dialog for updating passwords across multiple selected accounts sequentially.
    Enforces rate-limit pacing delays, automatically solves 2-Step Verification using saved
    TOTP keys, updates rotated session cookies, and records fresh credentials into the vault.
    """

    def __init__(
        self,
        accounts: List[RobloxAccount],
        api: RobloxAPI,
        storage: StorageManager,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent, title="Batch Password Change", can_maximize=True, resizable=True)
        self.accounts = [a for a in accounts if a.cookie]
        self.api = api
        self.storage = storage
        self.worker: Optional[BatchPasswordChangeWorker] = None
        self._results: List[dict] = []

        self.setMinimumWidth(680)
        self.setMinimumHeight(540)
        self.resize(720, 580)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        # Header info
        header_lbl = QtWidgets.QLabel(f"// BATCH PASSWORD CHANGE — {len(self.accounts)} ACCOUNTS")
        header_lbl.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: 700; letter-spacing: 0.8px;")
        layout.addWidget(header_lbl)

        sub_lbl = QtWidgets.QLabel("Automated one-by-one password updates with rate-limit pacing and Google Authenticator (2FA) resolution.")
        sub_lbl.setStyleSheet("color: #707070; font-size: 10px; font-family: monospace;")
        sub_lbl.setWordWrap(True)
        layout.addWidget(sub_lbl)

        # Options Box
        opts_frame = QtWidgets.QFrame()
        opts_frame.setStyleSheet("background-color: #080808; border: 1px solid #222222; padding: 6px;")
        opts_layout = QtWidgets.QVBoxLayout(opts_frame)
        opts_layout.setSpacing(6)
        opts_layout.setContentsMargins(8, 6, 8, 6)

        # Mode Selection
        mode_hdr = QtWidgets.QLabel("PASSWORD GENERATION MODE:")
        mode_hdr.setStyleSheet("color: #A0A0A0; font-size: 9px; font-family: monospace; font-weight: bold; letter-spacing: 0.5px; border: none;")
        opts_layout.addWidget(mode_hdr)

        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(16)
        self.rb_unique = QtWidgets.QRadioButton("Unique Strong Password per Account (Recommended)")
        self.rb_unique.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        self.rb_unique.setChecked(True)
        self.rb_unique.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.rb_unique)

        self.rb_common = QtWidgets.QRadioButton("Same Password for All Accounts")
        self.rb_common.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        self.rb_common.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.rb_common)
        mode_row.addStretch()
        opts_layout.addLayout(mode_row)

        # Common password input row (hidden by default)
        self.common_pwd_widget = QtWidgets.QWidget()
        self.common_pwd_widget.setVisible(False)
        cp_layout = QtWidgets.QHBoxLayout(self.common_pwd_widget)
        cp_layout.setContentsMargins(0, 2, 0, 0)
        cp_layout.setSpacing(6)

        self.common_pwd_input = QtWidgets.QLineEdit()
        self.common_pwd_input.setFixedHeight(28)
        self.common_pwd_input.setPlaceholderText("Enter shared password for all accounts (min. 8 chars)...")
        cp_layout.addWidget(self.common_pwd_input, stretch=1)

        self.btn_gen_common = QtWidgets.QPushButton("Generate Strong")
        self.btn_gen_common.setFixedHeight(28)
        self.btn_gen_common.clicked.connect(self._on_generate_common_pwd)
        cp_layout.addWidget(self.btn_gen_common)
        opts_layout.addWidget(self.common_pwd_widget)

        # Pacing and fallback row
        pacing_row = QtWidgets.QHBoxLayout()
        pacing_row.setSpacing(12)

        delay_lbl = QtWidgets.QLabel("Rate-Limit Pacing Delay:")
        delay_lbl.setStyleSheet("color: #A0A0A0; font-size: 11px; font-family: monospace; border: none;")
        pacing_row.addWidget(delay_lbl)

        self.delay_spin = QtWidgets.QDoubleSpinBox()
        self.delay_spin.setFixedHeight(28)
        self.delay_spin.setRange(1.0, 15.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setValue(3.0)
        self.delay_spin.setSuffix(" sec")
        self.delay_spin.setToolTip("Polite cooldown delay between accounts to prevent HTTP 429 rate limits")
        pacing_row.addWidget(self.delay_spin)

        fallback_lbl = QtWidgets.QLabel("Fallback Current Pass:")
        fallback_lbl.setStyleSheet("color: #A0A0A0; font-size: 11px; font-family: monospace; border: none;")
        pacing_row.addWidget(fallback_lbl)

        self.fallback_input = QtWidgets.QLineEdit()
        self.fallback_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.fallback_input.setFixedHeight(28)
        self.fallback_input.setPlaceholderText("Used only if vault is empty...")
        self.fallback_input.setToolTip("Fallback current password applied only if an account has no password saved in the vault")
        pacing_row.addWidget(self.fallback_input, stretch=1)

        opts_layout.addLayout(pacing_row)
        layout.addWidget(opts_frame)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #0c0c0c; border: 1px solid #1a1a1a; } "
            "QProgressBar::chunk { background-color: #E8E8E8; }"
        )
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Account Table
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["#", "Account", "Current Pwd", "New Password", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #000000; border: 1px solid #222222; color: #E8E8E8; gridline-color: #1a1a1a; } "
            "QHeaderView::section { background-color: #0c0c0c; color: #888888; font-size: 10px; font-weight: bold; border: 1px solid #1a1a1a; padding: 4px; }"
        )
        self._populate_initial_table()
        layout.addWidget(self.table, stretch=1)

        # Interactive 2FA Prompt Frame (shown if account challenges and lacks saved secret)
        self.p2fa_frame = QtWidgets.QFrame()
        self.p2fa_frame.setStyleSheet("background-color: #0c0c0c; border: 1px solid #FFA726; padding: 6px;")
        self.p2fa_frame.setVisible(False)
        p2fa_layout = QtWidgets.QHBoxLayout(self.p2fa_frame)
        p2fa_layout.setContentsMargins(8, 4, 8, 4)
        p2fa_layout.setSpacing(8)

        self.lbl_p2fa = QtWidgets.QLabel("Two-Step Verification needed:")
        self.lbl_p2fa.setStyleSheet("color: #FFA726; font-size: 11px; font-family: monospace; font-weight: bold; border: none;")
        p2fa_layout.addWidget(self.lbl_p2fa)

        self.input_p2fa = QtWidgets.QLineEdit()
        self.input_p2fa.setFixedHeight(28)
        self.input_p2fa.setFixedWidth(140)
        self.input_p2fa.setPlaceholderText("6-digit code...")
        p2fa_layout.addWidget(self.input_p2fa)

        self.btn_submit_p2fa = QtWidgets.QPushButton("Submit 2FA")
        self.btn_submit_p2fa.setFixedHeight(28)
        self.btn_submit_p2fa.setStyleSheet("background-color: #FFA726; color: #000000; font-weight: bold; padding: 2px 10px;")
        self.btn_submit_p2fa.clicked.connect(self._on_submit_p2fa)
        p2fa_layout.addWidget(self.btn_submit_p2fa)

        self.btn_skip_p2fa = QtWidgets.QPushButton("Skip Account")
        self.btn_skip_p2fa.setFixedHeight(28)
        self.btn_skip_p2fa.clicked.connect(self._on_skip_p2fa)
        p2fa_layout.addWidget(self.btn_skip_p2fa)
        p2fa_layout.addStretch()

        layout.addWidget(self.p2fa_frame)

        # Status & Action Buttons
        bot_row = QtWidgets.QHBoxLayout()
        self.lbl_status = QtWidgets.QLabel(f"Selected {len(self.accounts)} accounts. Ready to start.")
        self.lbl_status.setStyleSheet("color: #A0A0A0; font-size: 11px; font-family: monospace;")
        bot_row.addWidget(self.lbl_status, stretch=1)

        self.btn_copy = QtWidgets.QPushButton("Copy User:Pass")
        self.btn_copy.setFixedHeight(28)
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_results_to_clipboard)
        bot_row.addWidget(self.btn_copy)

        self.btn_export = QtWidgets.QPushButton("Export .txt")
        self.btn_export.setFixedHeight(28)
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export_results_to_file)
        bot_row.addWidget(self.btn_export)

        self.btn_close = QtWidgets.QPushButton("Cancel")
        self.btn_close.setFixedHeight(28)
        self.btn_close.clicked.connect(self._on_close_clicked)
        bot_row.addWidget(self.btn_close)

        self.btn_start = QtWidgets.QPushButton("Start Batch Update")
        self.btn_start.setFixedHeight(28)
        self.btn_start.setStyleSheet("background-color: #E8E8E8; color: #000000; font-weight: bold; border: 1px solid #E8E8E8; padding: 2px 12px;")
        self.btn_start.clicked.connect(self._start_batch)
        bot_row.addWidget(self.btn_start)

        layout.addLayout(bot_row)

    def _on_mode_changed(self):
        self.common_pwd_widget.setVisible(self.rb_common.isChecked())

    def _on_generate_common_pwd(self):
        pwd = generate_strong_password(16)
        self.common_pwd_input.setText(pwd)
        QtWidgets.QApplication.clipboard().setText(pwd)
        self.lbl_status.setText("Generated strong password and copied to clipboard!")

    def _populate_initial_table(self):
        self.table.setRowCount(len(self.accounts))
        for r, acc in enumerate(self.accounts):
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(r + 1)))
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(f"@{acc.username}"))
            curr_pwd_disp = "●●●●●●●●" if acc.password else "<Missing>"
            self.table.setItem(r, 2, QtWidgets.QTableWidgetItem(curr_pwd_disp))
            self.table.setItem(r, 3, QtWidgets.QTableWidgetItem("-"))
            init_status = "Ready (Auto 2FA)" if acc.totp_secret else "Ready"
            st_item = QtWidgets.QTableWidgetItem(init_status)
            st_item.setForeground(QtGui.QColor("#888888"))
            self.table.setItem(r, 4, st_item)

    def _start_batch(self):
        mode = "unique_random" if self.rb_unique.isChecked() else "common_password"
        common_pwd = self.common_pwd_input.text().strip()
        fallback_curr = self.fallback_input.text().strip()
        delay = self.delay_spin.value()

        if mode == "common_password":
            if not common_pwd:
                QMessageBox.warning(self, "Password Required", "Please enter a shared password or generate one.")
                return
            if len(common_pwd) < 8:
                QMessageBox.warning(self, "Invalid Password", "Shared password must be at least 8 characters long.")
                return

        self.btn_start.setEnabled(False)
        self.rb_unique.setEnabled(False)
        self.rb_common.setEnabled(False)
        self.common_pwd_input.setEnabled(False)
        self.delay_spin.setEnabled(False)
        self.fallback_input.setEnabled(False)
        self.btn_close.setText("Stop")

        self.worker = BatchPasswordChangeWorker(
            accounts=self.accounts,
            api=self.api,
            storage=self.storage,
            mode=mode,
            common_password=common_pwd,
            fallback_current_password=fallback_curr,
            pacing_delay=delay,
            parent=self,
        )
        self.worker.account_started.connect(self._on_account_started)
        self.worker.account_finished.connect(self._on_account_finished)
        self.worker.request_2fa.connect(self._on_request_2fa)
        self.worker.batch_complete.connect(self._on_batch_complete)
        self.worker.start()

    def _on_account_started(self, idx: int, total: int, acc: RobloxAccount):
        self.progress_bar.setValue(idx - 1)
        self.lbl_status.setText(f"Updating password for @{acc.username} ({idx}/{total})...")
        r = self._row_for_account(acc)
        if r >= 0:
            item = self.table.item(r, 4)
            if item:
                item.setText("Updating...")
                item.setForeground(QtGui.QColor("#64B5F6"))

    def _on_account_finished(self, acc: RobloxAccount, ok: bool, new_pass: str, new_cookie: str, msg: str):
        self.p2fa_frame.setVisible(False)
        r = self._row_for_account(acc)
        if r >= 0:
            if ok:
                self.table.setItem(r, 3, QtWidgets.QTableWidgetItem(new_pass))
                item = self.table.item(r, 4)
                if item:
                    item.setText("Success")
                    item.setForeground(QtGui.QColor("#4EAA50"))
            else:
                item = self.table.item(r, 4)
                if item:
                    item.setText(f"Failed: {msg}")
                    item.setForeground(QtGui.QColor("#E57373"))

    def _on_request_2fa(self, acc: RobloxAccount, info: dict):
        self.lbl_p2fa.setText(f"Enter 6-digit 2FA code for @{acc.username}:")
        self.input_p2fa.clear()
        self.p2fa_frame.setVisible(True)
        self.input_p2fa.setFocus()
        r = self._row_for_account(acc)
        if r >= 0:
            item = self.table.item(r, 4)
            if item:
                item.setText("Awaiting 2FA...")
                item.setForeground(QtGui.QColor("#FFA726"))

    def _on_submit_p2fa(self):
        code = self.input_p2fa.text().strip()
        if not code:
            return
        if self.worker:
            self.worker.provide_2fa_code(code)

    def _on_skip_p2fa(self):
        if self.worker:
            self.worker.skip_current_account()

    def _on_batch_complete(self, success_cnt: int, fail_cnt: int, results: list):
        self._results = results
        self.progress_bar.setValue(len(self.accounts))
        self.btn_close.setText("Close")
        self.btn_copy.setEnabled(success_cnt > 0)
        self.btn_export.setEnabled(success_cnt > 0)
        self.lbl_status.setText(f"Batch complete: {success_cnt} succeeded, {fail_cnt} failed.")

        QMessageBox.information(
            self,
            "Batch Password Update Complete",
            f"Completed password update across {len(self.accounts)} accounts:\n\n"
            f"• Succeeded: {success_cnt}\n"
            f"• Failed: {fail_cnt}\n\n"
            "All changed passwords and rotated session cookies have been saved to your encrypted vault."
        )

    def _row_for_account(self, acc: RobloxAccount) -> int:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 1)
            if item and item.text() == f"@{acc.username}":
                return r
        return -1

    def _copy_results_to_clipboard(self):
        lines = []
        for res in self._results:
            if res.get("ok"):
                acc = res["account"]
                lines.append(f"{acc.username}:{res['new_pass']}")
        if lines:
            text = "\n".join(lines)
            QtWidgets.QApplication.clipboard().setText(text)
            self.lbl_status.setText(f"Copied {len(lines)} username:password credentials to clipboard!")

    def _export_results_to_file(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Updated Passwords", "aeram_updated_passwords.txt", "Text Files (*.txt);;CSV Files (*.csv)"
        )
        if not file_path:
            return
        try:
            lines = [f"# AERAM Updated Passwords — {time.strftime('%Y-%m-%d %H:%M:%S')}"]
            for res in self._results:
                acc = res["account"]
                status = "SUCCESS" if res.get("ok") else f"FAILED ({res.get('msg')})"
                lines.append(f"{acc.username}:{res.get('new_pass', '')} # {status}")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            QMessageBox.information(self, "Export Complete", f"Successfully exported credentials to:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to write file:\n{e}")

    def _on_close_clicked(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        self.accept()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().closeEvent(event)

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().reject()


class BatchUnblockWorker(QtCore.QThread):
    """Background worker unblocking users across multiple accounts sequentially with rate limits."""
    account_progress = QtCore.pyqtSignal(int, int, object, int)  # current_acc, total_acc, account, unblocked_cnt
    batch_finished = QtCore.pyqtSignal(int, int)  # total_unblocked, total_failed

    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, parent=None):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self.stopped = False

    def run(self):
        total_accounts = len(self.accounts)
        total_unblocked = 0
        total_failed = 0

        for idx, acc in enumerate(self.accounts):
            if self.stopped:
                break

            # Respectful pacing between accounts
            if idx > 0 and not self.stopped:
                time.sleep(1.5)

            try:
                blocked = self.api.get_blocked_users(acc.cookie)
            except Exception:
                blocked = []

            acc_unblocked = 0
            csrf_token = None
            for u in blocked:
                if self.stopped:
                    break
                u_id = u.get("id")
                if not u_id:
                    continue
                ok, fresh_csrf = self.api.unblock_user(u_id, acc.cookie, csrf_token=csrf_token)
                if fresh_csrf:
                    csrf_token = fresh_csrf
                if ok:
                    acc_unblocked += 1
                    total_unblocked += 1
                else:
                    total_failed += 1
                time.sleep(0.25)  # 250ms spacing per user unblocked

            self.account_progress.emit(idx + 1, total_accounts, acc, acc_unblocked)

        self.batch_finished.emit(total_unblocked, total_failed)

    def stop(self):
        self.stopped = True


class BatchUnblockDialog(BorderlessDialog):
    """
    Dedicated dialog allowing one-click sequential unblocking across multiple accounts
    with rate-limit pacing to clear block lists platform-wide.
    """

    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, parent=None):
        super().__init__(parent, title="Batch Unblock Everyone", can_maximize=False, resizable=False)
        self.accounts = [a for a in accounts if a.cookie]
        self.api = api
        self.worker: Optional[BatchUnblockWorker] = None

        self.setFixedWidth(500)
        self.setFixedHeight(340)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(12)
        layout.setContentsMargins(18, 16, 18, 16)

        hdr = QtWidgets.QLabel(f"// BATCH UNBLOCK — {len(self.accounts)} ACCOUNTS")
        hdr.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: bold;")
        layout.addWidget(hdr)

        self.status_lbl = QtWidgets.QLabel("Ready to scan and unblock users across all selected accounts one-by-one.")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #888888; font-size: 11px; font-family: monospace;")
        layout.addWidget(self.status_lbl)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #0c0c0c; border: 1px solid #1a1a1a; } "
            "QProgressBar::chunk { background-color: #E8E8E8; }"
        )
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #000000; border: 1px solid #222222; color: #E8E8E8; font-size: 10px; font-family: monospace;")
        layout.addWidget(self.log_view, stretch=1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.btn_cancel)

        self.btn_start = QtWidgets.QPushButton("Start Batch Unblock")
        self.btn_start.setFixedHeight(28)
        self.btn_start.setStyleSheet("background-color: #E8E8E8; color: #000000; font-weight: bold; border: 1px solid #E8E8E8;")
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start)

        layout.addLayout(btn_row)

    def _start_batch(self):
        self.btn_start.setEnabled(False)
        self.btn_cancel.setText("Stop")
        self.status_lbl.setText("Scanning and unblocking users with 250ms rate-limit spacing...")

        self.worker = BatchUnblockWorker(self.accounts, self.api, self)
        self.worker.account_progress.connect(self._on_account_progress)
        self.worker.batch_finished.connect(self._on_finished)
        self.worker.start()

    def _on_account_progress(self, idx: int, total: int, acc: RobloxAccount, unblocked: int):
        self.progress_bar.setValue(idx)
        self.log_view.append(f"[{idx}/{total}] @{acc.username}: Unblocked {unblocked} user(s).")
        self.status_lbl.setText(f"Processed @{acc.username} ({idx}/{total}).")

    def _on_finished(self, total_unblocked: int, total_failed: int):
        self.btn_cancel.setText("Close")
        self.status_lbl.setText(f"Completed! Total unblocked: {total_unblocked}, failed: {total_failed}.")
        self.log_view.append(f"\nFinished batch unblock! Total unblocked: {total_unblocked}.")
        QMessageBox.information(
            self,
            "Batch Unblock Complete",
            f"Finished unblocking across {len(self.accounts)} accounts:\n\n"
            f"• Total users unblocked: {total_unblocked}\n"
            f"• Failed / skipped: {total_failed}"
        )

    def _on_cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        self.accept()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().closeEvent(event)


class BatchLogoutWorker(QtCore.QThread):
    account_started = QtCore.pyqtSignal(int, int, object)  # idx, total, account
    account_finished = QtCore.pyqtSignal(object, bool, str)  # acc, ok, msg
    batch_complete = QtCore.pyqtSignal(int, int)  # success_cnt, fail_cnt

    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, storage: StorageManager, runner=None, pacing_delay: float = 1.5, parent=None):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self.storage = storage
        self.runner = runner
        self.pacing_delay = max(0.2, float(pacing_delay))
        self.stopped = False

    def stop(self):
        self.stopped = True

    def run(self):
        total = len(self.accounts)
        success_cnt = 0
        fail_cnt = 0

        for idx, acc in enumerate(self.accounts):
            if self.stopped:
                break

            if idx > 0 and not self.stopped:
                sleep_slices = int(self.pacing_delay * 10)
                for _ in range(sleep_slices):
                    if self.stopped:
                        break
                    time.sleep(0.1)

            if self.stopped:
                break

            self.account_started.emit(idx + 1, total, acc)

            if not acc.cookie:
                fail_cnt += 1
                self.account_finished.emit(acc, False, "No session cookie")
                continue

            try:
                ok, msg = self.api.logout_session(acc.cookie)
                if ok:
                    success_cnt += 1
                    acc.cookie = ""
                    self.storage.save_account(acc)
                    if self.runner and hasattr(self.runner, "auth_state_manager"):
                        self.runner.auth_state_manager.mark_session_expired(acc.account_id, reason="Logged out by user")
                    self.account_finished.emit(acc, True, "Session revoked")
                else:
                    fail_cnt += 1
                    self.account_finished.emit(acc, False, msg)
            except Exception as e:
                fail_cnt += 1
                self.account_finished.emit(acc, False, str(e))

        self.batch_complete.emit(success_cnt, fail_cnt)


class BatchLogoutDialog(BorderlessDialog):
    """
    Dedicated dialog for permanently invalidating session tokens on Roblox servers
    across multiple accounts sequentially.
    """
    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, storage: StorageManager, runner=None, parent=None):
        super().__init__(parent, title="Batch Complete Logout", can_maximize=False, resizable=False)
        self.accounts = [a for a in accounts if a.cookie]
        self.api = api
        self.storage = storage
        self.runner = runner
        self.worker: Optional[BatchLogoutWorker] = None

        self.setFixedWidth(560)
        self.setFixedHeight(440)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        hdr = QtWidgets.QLabel(f"// BATCH COMPLETE LOGOUT — {len(self.accounts)} ACCOUNTS")
        hdr.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: bold;")
        layout.addWidget(hdr)

        warn_box = QtWidgets.QFrame()
        warn_box.setStyleSheet("background-color: #080808; border: 1px solid #333333; padding: 6px;")
        warn_layout = QtWidgets.QVBoxLayout(warn_box)
        warn_layout.setContentsMargins(8, 6, 8, 6)
        warn_lbl = QtWidgets.QLabel(
            "PERMANENT SESSION REVOCATION:\n"
            "This will hit Roblox's /v2/logout endpoint to permanently destroy stored session tokens.\n"
            "Once revoked, these accounts will require fresh login / 2FA to be used again."
        )
        warn_lbl.setStyleSheet("color: #CCCCCC; font-size: 10px; font-family: monospace; font-weight: bold; border: none;")
        warn_lbl.setWordWrap(True)
        warn_layout.addWidget(warn_lbl)
        layout.addWidget(warn_box)

        # Pacing row
        pacing_row = QtWidgets.QHBoxLayout()
        pacing_lbl = QtWidgets.QLabel("Rate-Limit Pacing Delay:")
        pacing_lbl.setStyleSheet("color: #A0A0A0; font-size: 11px; font-family: monospace;")
        pacing_row.addWidget(pacing_lbl)

        self.delay_spin = QtWidgets.QDoubleSpinBox()
        self.delay_spin.setFixedHeight(28)
        self.delay_spin.setRange(0.5, 10.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setValue(1.5)
        self.delay_spin.setSuffix(" sec")
        pacing_row.addWidget(self.delay_spin)
        pacing_row.addStretch()
        layout.addLayout(pacing_row)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #FFFFFF; }")
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Table
        self.table = QtWidgets.QTableWidget(len(self.accounts), 3)
        self.table.setHorizontalHeaderLabels(["#", "Account", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet("QTableWidget { background-color: #050505; border: 1px solid #1c1c1c; font-family: monospace; font-size: 11px; color: #E8E8E8; }")
        for i, acc in enumerate(self.accounts):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(f"@{acc.username}"))
            item_st = QtWidgets.QTableWidgetItem("Pending")
            item_st.setForeground(QtGui.QColor("#888888"))
            self.table.setItem(i, 2, item_st)
        layout.addWidget(self.table, stretch=1)

        self.status_lbl = QtWidgets.QLabel(f"Ready to revoke sessions across {len(self.accounts)} accounts.")
        self.status_lbl.setStyleSheet("color: #888888; font-size: 11px; font-family: monospace;")
        layout.addWidget(self.status_lbl)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.btn_cancel)

        self.btn_start = QtWidgets.QPushButton("Revoke All Sessions")
        self.btn_start.setFixedHeight(28)
        self.btn_start.setStyleSheet("background-color: #FFFFFF; color: #000000; font-weight: bold; border: 1px solid #FFFFFF;")
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start)

        layout.addLayout(btn_row)

    def _start_batch(self):
        self.btn_start.setEnabled(False)
        self.delay_spin.setEnabled(False)
        self.btn_cancel.setText("Stop")
        self.status_lbl.setText("Executing session revocation one-by-one...")

        self.worker = BatchLogoutWorker(
            accounts=self.accounts,
            api=self.api,
            storage=self.storage,
            runner=self.runner,
            pacing_delay=self.delay_spin.value(),
            parent=self
        )
        self.worker.account_started.connect(self._on_account_started)
        self.worker.account_finished.connect(self._on_account_finished)
        self.worker.batch_complete.connect(self._on_batch_complete)
        self.worker.start()

    def _on_account_started(self, idx: int, total: int, acc: RobloxAccount):
        self.progress_bar.setValue(idx - 1)
        self.status_lbl.setText(f"Logging out @{acc.username} ({idx}/{total})...")
        r = self._row_for_account(acc)
        if r >= 0:
            it = self.table.item(r, 2)
            if it:
                it.setText("Logging out...")
                it.setForeground(QtGui.QColor("#CCCCCC"))

    def _on_account_finished(self, acc: RobloxAccount, ok: bool, msg: str):
        r = self._row_for_account(acc)
        if r >= 0:
            it = self.table.item(r, 2)
            if it:
                if ok:
                    it.setText("Revoked")
                    it.setForeground(QtGui.QColor("#FFFFFF"))
                else:
                    it.setText(f"Failed: {msg}")
                    it.setForeground(QtGui.QColor("#888888"))

    def _on_batch_complete(self, success_cnt: int, fail_cnt: int):
        self.progress_bar.setValue(len(self.accounts))
        self.btn_cancel.setText("Close")
        self.status_lbl.setText(f"Batch completed: {success_cnt} revoked, {fail_cnt} failed.")

    def _row_for_account(self, acc: RobloxAccount) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 1)
            if it and it.text() == f"@{acc.username}":
                return r
        return -1

    def _on_cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        self.accept()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().closeEvent(event)


class BatchFriendsWorker(QtCore.QThread):
    account_started = QtCore.pyqtSignal(int, int, object)  # idx, total, acc
    action_log = QtCore.pyqtSignal(str)
    account_finished = QtCore.pyqtSignal(object, int)  # acc, count
    batch_complete = QtCore.pyqtSignal(int, int)  # total_success, total_count

    def __init__(
        self,
        accounts: List[RobloxAccount],
        api: RobloxAPI,
        action_mode: str = "decline_all",  # "decline_all", "accept_all", "wipe_all"
        whitelist_users: Optional[List[str]] = None,
        pacing_delay: float = 1.5,
        parent=None,
    ):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self.action_mode = action_mode
        self.whitelist_users = {u.strip().lower() for u in (whitelist_users or []) if u.strip()}
        self.pacing_delay = max(0.5, float(pacing_delay))
        self.stopped = False

    def stop(self):
        self.stopped = True

    def run(self):
        total_accounts = len(self.accounts)
        grand_total_ops = 0

        for idx, acc in enumerate(self.accounts):
            if self.stopped:
                break

            if idx > 0 and not self.stopped:
                sleep_slices = int(self.pacing_delay * 10)
                for _ in range(sleep_slices):
                    if self.stopped:
                        break
                    time.sleep(0.1)

            if self.stopped:
                break

            self.account_started.emit(idx + 1, total_accounts, acc)
            ops_count = 0

            try:
                if self.action_mode == "decline_all":
                    ok, msg = self.api.decline_all_friend_requests(acc.cookie)
                    if ok:
                        ops_count += 1
                        grand_total_ops += 1
                        self.action_log.emit(f"[@{acc.username}] Declined all pending requests.")
                    else:
                        self.action_log.emit(f"[@{acc.username}] Decline failed: {msg}")

                elif self.action_mode == "accept_all":
                    reqs = self.api.get_friend_requests(acc.cookie)
                    csrf = None
                    for r in reqs:
                        if self.stopped:
                            break
                        u_id = r.get("id")
                        u_name = r.get("name", str(u_id))
                        ok, csrf = self.api.accept_friend_request(u_id, acc.cookie, csrf_token=csrf)
                        if ok:
                            ops_count += 1
                            grand_total_ops += 1
                            self.action_log.emit(f"[@{acc.username}] Accepted friend request from @{u_name}")
                        time.sleep(0.25)

                elif self.action_mode == "wipe_all":
                    friends = self.api.get_friends(acc.user_id, acc.cookie)
                    csrf = None
                    for f in friends:
                        if self.stopped:
                            break
                        f_id = f.get("id")
                        f_name = f.get("name", str(f_id))
                        if f_name.lower() in self.whitelist_users or str(f_id) in self.whitelist_users:
                            self.action_log.emit(f"[@{acc.username}] Preserved whitelisted friend: @{f_name}")
                            continue

                        ok, csrf = self.api.unfriend_user(f_id, acc.cookie, csrf_token=csrf)
                        if ok:
                            ops_count += 1
                            grand_total_ops += 1
                            self.action_log.emit(f"[@{acc.username}] Removed friend @{f_name}")
                        time.sleep(0.25)

            except Exception as e:
                self.action_log.emit(f"[@{acc.username}] Error: {e}")

            self.account_finished.emit(acc, ops_count)

        self.batch_complete.emit(len(self.accounts), grand_total_ops)


class BatchFriendsDialog(BorderlessDialog):
    """
    Dedicated dialog for bulk friends maintenance: declining requests, accepting requests,
    or unfriending everyone with whitelist protection.
    """
    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, parent=None):
        super().__init__(parent, title="Batch Friends Manager", can_maximize=False, resizable=False)
        self.accounts = [a for a in accounts if a.cookie]
        self.api = api
        self.worker: Optional[BatchFriendsWorker] = None

        self.setFixedWidth(580)
        self.setFixedHeight(480)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        hdr = QtWidgets.QLabel(f"// BATCH FRIENDS MANAGER — {len(self.accounts)} ACCOUNTS")
        hdr.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: bold;")
        layout.addWidget(hdr)

        opts_box = QtWidgets.QFrame()
        opts_box.setStyleSheet("background-color: #080808; border: 1px solid #222222; padding: 6px;")
        opts_layout = QtWidgets.QVBoxLayout(opts_box)
        opts_layout.setSpacing(6)
        opts_layout.setContentsMargins(8, 6, 8, 6)

        m_hdr = QtWidgets.QLabel("SELECT ACTION:")
        m_hdr.setStyleSheet("color: #A0A0A0; font-size: 9px; font-family: monospace; font-weight: bold; border: none;")
        opts_layout.addWidget(m_hdr)

        self.rb_decline = QtWidgets.QRadioButton("Decline All Pending Friend Requests (Clear Inbox)")
        self.rb_decline.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        self.rb_decline.setChecked(True)
        opts_layout.addWidget(self.rb_decline)

        self.rb_accept = QtWidgets.QRadioButton("Accept All Pending Friend Requests")
        self.rb_accept.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        opts_layout.addWidget(self.rb_accept)

        self.rb_wipe = QtWidgets.QRadioButton("Delete All Friends (Wipe Friends List)")
        self.rb_wipe.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        opts_layout.addWidget(self.rb_wipe)

        # Whitelist field
        wl_lbl = QtWidgets.QLabel("Whitelist Users to Keep (comma-separated):")
        wl_lbl.setStyleSheet("color: #A0A0A0; font-size: 10px; font-family: monospace; margin-top: 4px; border: none;")
        opts_layout.addWidget(wl_lbl)

        self.wl_input = QtWidgets.QLineEdit()
        self.wl_input.setFixedHeight(28)
        self.wl_input.setPlaceholderText("e.g. MyMainUser, TrustedTrader1 (never unfriended)...")
        opts_layout.addWidget(self.wl_input)

        layout.addWidget(opts_box)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #E8E8E8; }")
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Log view
        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #000000; border: 1px solid #222222; color: #E8E8E8; font-size: 10px; font-family: monospace;")
        layout.addWidget(self.log_view, stretch=1)

        self.status_lbl = QtWidgets.QLabel(f"Ready to process friends for {len(self.accounts)} accounts.")
        self.status_lbl.setStyleSheet("color: #888888; font-size: 11px; font-family: monospace;")
        layout.addWidget(self.status_lbl)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.btn_cancel)

        self.btn_start = QtWidgets.QPushButton("Start Friends Action")
        self.btn_start.setFixedHeight(28)
        self.btn_start.setStyleSheet("background-color: #E8E8E8; color: #000000; font-weight: bold; border: 1px solid #E8E8E8;")
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start)

        layout.addLayout(btn_row)

    def _start_batch(self):
        mode = "decline_all"
        if self.rb_accept.isChecked():
            mode = "accept_all"
        elif self.rb_wipe.isChecked():
            mode = "wipe_all"

        whitelist = [u.strip() for u in self.wl_input.text().split(",") if u.strip()]

        self.btn_start.setEnabled(False)
        self.btn_cancel.setText("Stop")
        self.status_lbl.setText("Running friends automation with rate-limit pacing...")

        self.worker = BatchFriendsWorker(
            accounts=self.accounts,
            api=self.api,
            action_mode=mode,
            whitelist_users=whitelist,
            pacing_delay=1.5,
            parent=self
        )
        self.worker.account_started.connect(self._on_account_started)
        self.worker.action_log.connect(self._on_action_log)
        self.worker.account_finished.connect(self._on_account_finished)
        self.worker.batch_complete.connect(self._on_batch_complete)
        self.worker.start()

    def _on_account_started(self, idx: int, total: int, acc: RobloxAccount):
        self.progress_bar.setValue(idx - 1)
        self.status_lbl.setText(f"Processing @{acc.username} ({idx}/{total})...")

    def _on_action_log(self, text: str):
        self.log_view.append(text)

    def _on_account_finished(self, acc: RobloxAccount, count: int):
        self.log_view.append(f"Completed @{acc.username}: {count} action(s).")

    def _on_batch_complete(self, total_acc: int, grand_total: int):
        self.progress_bar.setValue(len(self.accounts))
        self.btn_cancel.setText("Close")
        self.status_lbl.setText(f"Completed! Finished across {total_acc} accounts. Total operations: {grand_total}.")
        self.log_view.append(f"\nFinished batch run! Total operations: {grand_total}.")

    def _on_cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        self.accept()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().closeEvent(event)


class BatchPrivacyWorker(QtCore.QThread):
    account_started = QtCore.pyqtSignal(int, int, object)
    account_finished = QtCore.pyqtSignal(object, bool, str)
    batch_complete = QtCore.pyqtSignal(int, int)

    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, storage: StorageManager, setting: str = "NoOne", pacing_delay: float = 1.0, parent=None):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self.storage = storage
        self.setting = setting
        self.pacing_delay = max(0.2, float(pacing_delay))
        self.stopped = False

    def stop(self):
        self.stopped = True

    def run(self):
        total = len(self.accounts)
        success_cnt = 0
        fail_cnt = 0

        for idx, acc in enumerate(self.accounts):
            if self.stopped:
                break

            if idx > 0 and not self.stopped:
                time.sleep(self.pacing_delay)

            if self.stopped:
                break

            self.account_started.emit(idx + 1, total, acc)

            try:
                ok, msg = self.api.set_join_privacy(self.setting, acc.cookie)
                if ok:
                    success_cnt += 1
                    acc.join_privacy = self.setting
                    self.storage.save_account(acc)
                    self.account_finished.emit(acc, True, f"Privacy: {self.setting}")
                else:
                    fail_cnt += 1
                    self.account_finished.emit(acc, False, msg)
            except Exception as e:
                fail_cnt += 1
                self.account_finished.emit(acc, False, str(e))

        self.batch_complete.emit(success_cnt, fail_cnt)


class BatchPrivacyDialog(BorderlessDialog):
    """
    Dedicated dialog to update 'Who Can Join Me' privacy presets across multiple accounts.
    """
    def __init__(self, accounts: List[RobloxAccount], api: RobloxAPI, storage: StorageManager, parent=None):
        super().__init__(parent, title="Join Privacy Presets", can_maximize=False, resizable=False)
        self.accounts = [a for a in accounts if a.cookie]
        self.api = api
        self.storage = storage
        self.worker: Optional[BatchPrivacyWorker] = None

        self.setFixedWidth(520)
        self.setFixedHeight(360)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        hdr = QtWidgets.QLabel(f"// JOIN PRIVACY PRESETS — {len(self.accounts)} ACCOUNTS")
        hdr.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: bold;")
        layout.addWidget(hdr)

        box = QtWidgets.QFrame()
        box.setStyleSheet("background-color: #080808; border: 1px solid #222222; padding: 6px;")
        b_layout = QtWidgets.QVBoxLayout(box)
        b_layout.setSpacing(6)
        b_layout.setContentsMargins(8, 6, 8, 6)

        m_hdr = QtWidgets.QLabel("WHO CAN JOIN ME IN EXPERIENCES:")
        m_hdr.setStyleSheet("color: #A0A0A0; font-size: 9px; font-family: monospace; font-weight: bold; border: none;")
        b_layout.addWidget(m_hdr)

        self.rb_ghost = QtWidgets.QRadioButton("Ghost Mode (No One — completely private)")
        self.rb_ghost.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        self.rb_ghost.setChecked(True)
        b_layout.addWidget(self.rb_ghost)

        self.rb_friends = QtWidgets.QRadioButton("Friends Only (Only Roblox friends can join)")
        self.rb_friends.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        b_layout.addWidget(self.rb_friends)

        self.rb_public = QtWidgets.QRadioButton("Anyone (Public — anyone can join via profile)")
        self.rb_public.setStyleSheet("color: #E8E8E8; font-size: 11px; font-family: monospace; border: none;")
        b_layout.addWidget(self.rb_public)

        layout.addWidget(box)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #E8E8E8; }")
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_lbl = QtWidgets.QLabel(f"Ready to apply privacy preset to {len(self.accounts)} accounts.")
        self.status_lbl.setStyleSheet("color: #888888; font-size: 11px; font-family: monospace;")
        layout.addWidget(self.status_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.btn_cancel)

        self.btn_start = QtWidgets.QPushButton("Apply Preset")
        self.btn_start.setFixedHeight(28)
        self.btn_start.setStyleSheet("background-color: #E8E8E8; color: #000000; font-weight: bold; border: 1px solid #E8E8E8;")
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start)

        layout.addLayout(btn_row)

    def _start_batch(self):
        setting = "NoOne"
        if self.rb_friends.isChecked():
            setting = "Friends"
        elif self.rb_public.isChecked():
            setting = "AllUsers"

        self.btn_start.setEnabled(False)
        self.btn_cancel.setText("Stop")
        self.status_lbl.setText(f"Applying '{setting}' to all selected accounts...")

        self.worker = BatchPrivacyWorker(
            accounts=self.accounts,
            api=self.api,
            storage=self.storage,
            setting=setting,
            pacing_delay=1.0,
            parent=self
        )
        self.worker.account_started.connect(lambda idx, total, acc: self.status_lbl.setText(f"Updating @{acc.username} ({idx}/{total})..."))
        self.worker.account_finished.connect(self._on_account_finished)
        self.worker.batch_complete.connect(self._on_batch_complete)
        self.worker.start()

    def _on_account_finished(self, acc, success: bool, msg: str):
        if success:
            self.status_lbl.setText(f"@{acc.username}: {msg}")
        else:
            self._last_error = msg
            self.status_lbl.setText(f"@{acc.username} failed: {msg}")

    def _on_batch_complete(self, success_cnt: int, fail_cnt: int):
        self.progress_bar.setValue(len(self.accounts))
        self.btn_cancel.setText("Close")
        if fail_cnt > 0:
            err_hint = f" (Error: {self._last_error})" if getattr(self, "_last_error", None) else ""
            self.status_lbl.setText(f"Completed! {success_cnt} updated, {fail_cnt} failed.{err_hint}")
        else:
            self.status_lbl.setText(f"Completed! All {success_cnt} accounts updated successfully.")

    def _on_cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        self.accept()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        super().closeEvent(event)


class Live2FAHubDialog(BorderlessDialog):
    """
    Live 2FA Authenticator Hub for effortless multi-account mobile device logins.
    Displays real-time rotating 6-digit TOTP codes, account passwords, and 30s cycle
    countdown for all configured accounts simultaneously so users can glance and log in
    on phones without switching back and forth to Google Authenticator.
    """
    def __init__(self, accounts: List[RobloxAccount], storage: StorageManager, parent=None):
        super().__init__(parent, title="Live 2FA Authenticator Hub", can_maximize=True, resizable=True)
        self.accounts = accounts
        self.storage = storage
        self.visible_accounts: List[RobloxAccount] = []
        self._last_time_step = -1
        self._code_labels: Dict[str, QtWidgets.QLabel] = {}
        self._code_buttons: Dict[str, QtWidgets.QPushButton] = {}

        self.resize(780, 520)
        self.setMinimumSize(660, 400)
        self._init_ui()

        # Timer for countdown bar and code update
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

        self._apply_filter()
        self._on_tick()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        # Header
        hdr = QtWidgets.QLabel("// LIVE 2FA AUTHENTICATOR HUB — MOBILE LOGIN HELPER")
        hdr.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: bold;")
        layout.addWidget(hdr)

        sub = QtWidgets.QLabel("Glance and enter 2FA codes directly onto your phone without switching back and forth in Google Authenticator.")
        sub.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(sub)

        # Filter bar & Countdown
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setSpacing(10)

        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Filter by username, alias, or group...")
        self.search_input.setFixedHeight(28)
        self.search_input.setStyleSheet("background-color: #080808; border: 1px solid #222222; color: #FFFFFF; padding: 0 8px; font-family: monospace;")
        self.search_input.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.search_input, stretch=1)

        self.cb_only_2fa = QtWidgets.QCheckBox("Only Accounts with 2FA")
        self.cb_only_2fa.setChecked(True)
        self.cb_only_2fa.setStyleSheet("color: #CCCCCC; font-size: 11px;")
        self.cb_only_2fa.toggled.connect(self._apply_filter)
        filter_row.addWidget(self.cb_only_2fa)

        # Countdown block
        timer_box = QtWidgets.QHBoxLayout()
        timer_box.setSpacing(8)
        timer_box.setContentsMargins(12, 0, 0, 0)
        self.lbl_timer = QtWidgets.QLabel("Rotates in 30s")
        self.lbl_timer.setFixedWidth(115)
        self.lbl_timer.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.lbl_timer.setStyleSheet("color: #FFFFFF; font-size: 11px; font-family: monospace; font-weight: bold;")
        timer_box.addWidget(self.lbl_timer)

        self.cycle_bar = QtWidgets.QProgressBar()
        self.cycle_bar.setRange(0, 30)
        self.cycle_bar.setValue(30)
        self.cycle_bar.setFixedHeight(5)
        self.cycle_bar.setFixedWidth(75)
        self.cycle_bar.setTextVisible(False)
        self.cycle_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #FFFFFF; }")
        timer_box.addWidget(self.cycle_bar)

        filter_row.addLayout(timer_box)
        layout.addLayout(filter_row)

        # Main Table
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ACCOUNT", "GROUP", "PASSWORD", "CURRENT 2FA CODE (TOTP)"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #040404; alternate-background-color: #0A0A0A; border: 1px solid #1E1E1E; font-size: 11px; } "
            "QHeaderView::section { background-color: #0C0C0C; color: #888888; border: none; border-bottom: 1px solid #222222; font-size: 10px; font-weight: bold; font-family: monospace; padding: 4px; }"
        )
        hdr_view = self.table.horizontalHeader()
        hdr_view.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hdr_view.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 100)
        hdr_view.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 110)
        hdr_view.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 210)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table, stretch=1)

        # Footer
        footer = QtWidgets.QHBoxLayout()
        self.status_lbl = QtWidgets.QLabel("Ready | Click any code or button to copy")
        self.status_lbl.setStyleSheet("color: #707070; font-size: 11px; font-family: monospace;")
        footer.addWidget(self.status_lbl)
        footer.addStretch()

        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.setFixedHeight(28)
        self.btn_close.setFixedWidth(80)
        self.btn_close.clicked.connect(self.accept)
        footer.addWidget(self.btn_close)
        layout.addLayout(footer)

    def _apply_filter(self):
        query = self.search_input.text().strip().lower()
        only_2fa = self.cb_only_2fa.isChecked()

        filtered = []
        for acc in self.accounts:
            if only_2fa and not acc.totp_secret:
                continue
            if query:
                match = (
                    query in acc.username.lower() or
                    query in (acc.display_name or "").lower() or
                    query in (acc.group or "").lower()
                )
                if not match:
                    continue
            filtered.append(acc)

        self.visible_accounts = filtered
        self._populate_table()

    def _populate_table(self):
        self.table.clearContents()
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.visible_accounts))
        self._code_labels.clear()
        self._code_buttons.clear()

        for row, acc in enumerate(self.visible_accounts):
            # Col 0: Account name & alias
            name_text = f"@{acc.username}"
            if acc.display_name and acc.display_name != acc.username:
                name_text += f"  ({acc.display_name})"
            item_name = QtWidgets.QTableWidgetItem(name_text)
            item_name.setForeground(QtGui.QBrush(QtGui.QColor("#FFFFFF")))
            self.table.setItem(row, 0, item_name)

            # Col 1: Group
            item_grp = QtWidgets.QTableWidgetItem(acc.group or "Default")
            item_grp.setForeground(QtGui.QBrush(QtGui.QColor("#888888")))
            item_grp.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, item_grp)

            # Col 2: Password
            if acc.password:
                btn_pass = QtWidgets.QPushButton("Copy Pass")
                btn_pass.setFixedHeight(24)
                btn_pass.setStyleSheet("background-color: #121212; color: #CCCCCC; border: 1px solid #2A2A2A; font-size: 10px; font-weight: bold; border-radius: 0px;")
                btn_pass.clicked.connect(lambda _, a=acc, b=btn_pass: self._copy_password(a, b))
                self.table.setCellWidget(row, 2, btn_pass)
            else:
                lbl_no_pass = QtWidgets.QLabel("--")
                lbl_no_pass.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                lbl_no_pass.setStyleSheet("color: #444444; font-size: 11px;")
                self.table.setCellWidget(row, 2, lbl_no_pass)

            # Col 3: Live 2FA Code
            if acc.totp_secret:
                code_box = QtWidgets.QWidget()
                c_layout = QtWidgets.QHBoxLayout(code_box)
                c_layout.setContentsMargins(6, 2, 6, 2)
                c_layout.setSpacing(8)

                lbl_code = QtWidgets.QLabel("------")
                lbl_code.setFixedWidth(84)
                lbl_code.setStyleSheet("color: #FFFFFF; font-size: 14px; font-weight: bold; font-family: monospace; letter-spacing: 1px;")
                lbl_code.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                lbl_code.mousePressEvent = lambda _, a=acc: self._copy_code_for_account(a)
                c_layout.addWidget(lbl_code)

                btn_copy = QtWidgets.QPushButton("Copy")
                btn_copy.setFixedHeight(22)
                btn_copy.setFixedWidth(54)
                btn_copy.setStyleSheet("background-color: #161616; color: #E8E8E8; border: 1px solid #333333; font-size: 10px; font-weight: bold; border-radius: 0px;")
                btn_copy.clicked.connect(lambda _, a=acc: self._copy_code_for_account(a))
                c_layout.addWidget(btn_copy)

                self.table.setCellWidget(row, 3, code_box)
                self._code_labels[acc.account_id] = lbl_code
                self._code_buttons[acc.account_id] = btn_copy
            else:
                btn_add_2fa = QtWidgets.QPushButton("+ Add 2FA Key")
                btn_add_2fa.setFixedHeight(22)
                btn_add_2fa.setStyleSheet("background-color: #121212; color: #888888; border: 1px dashed #333333; font-size: 10px; border-radius: 0px;")
                btn_add_2fa.clicked.connect(lambda _, a=acc: self._edit_account_2fa(a))
                self.table.setCellWidget(row, 3, btn_add_2fa)

        self._refresh_all_codes()
        total_2fa = sum(1 for a in self.accounts if a.totp_secret)
        self.status_lbl.setText(f"Showing {len(self.visible_accounts)} accounts ({total_2fa} with 2FA) | Click any code or button to copy")

    def _on_tick(self):
        rem = get_totp_remaining_seconds(30)
        self.cycle_bar.setValue(rem)
        self.lbl_timer.setText(f"Rotates in {rem}s")
        if rem <= 5:
            self.lbl_timer.setStyleSheet("color: #888888; font-size: 11px; font-family: monospace; font-weight: bold;")
            self.cycle_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #888888; }")
        else:
            self.lbl_timer.setStyleSheet("color: #FFFFFF; font-size: 11px; font-family: monospace; font-weight: bold;")
            self.cycle_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #FFFFFF; }")

        current_step = int(time.time()) // 30
        if current_step != self._last_time_step:
            self._last_time_step = current_step
            self._refresh_all_codes()

    def _refresh_all_codes(self):
        for acc in self.visible_accounts:
            if not acc.totp_secret or acc.account_id not in self._code_labels:
                continue
            lbl = self._code_labels[acc.account_id]
            try:
                raw_code = generate_totp(acc.totp_secret)
                formatted = f"{raw_code[:3]} {raw_code[3:]}"
                lbl.setText(formatted)
            except Exception:
                lbl.setText("ERR KEY")
                lbl.setStyleSheet("color: #888888; font-size: 12px; font-family: monospace;")

    def _copy_password(self, acc: RobloxAccount, btn: QtWidgets.QPushButton):
        if not acc.password:
            return
        QtWidgets.QApplication.clipboard().setText(acc.password)
        btn.setText("COPIED!")
        btn.setStyleSheet("background-color: #FFFFFF; color: #000000; border: 1px solid #FFFFFF; font-size: 10px; font-weight: bold; border-radius: 0px;")
        QtCore.QTimer.singleShot(1400, lambda: self._reset_pass_btn(btn))
        self.status_lbl.setText(f"Copied password for @{acc.username}")

    def _reset_pass_btn(self, btn: QtWidgets.QPushButton):
        btn.setText("Copy Pass")
        btn.setStyleSheet("background-color: #121212; color: #CCCCCC; border: 1px solid #2A2A2A; font-size: 10px; font-weight: bold; border-radius: 0px;")

    def _copy_code_for_account(self, acc: RobloxAccount):
        if not acc.totp_secret:
            return
        try:
            code = generate_totp(acc.totp_secret)
            QtWidgets.QApplication.clipboard().setText(code)
            rem = get_totp_remaining_seconds(30)
            self.status_lbl.setText(f"Copied 2FA code {code} for @{acc.username} (expires in {rem}s)")

            btn = self._code_buttons.get(acc.account_id)
            if btn:
                btn.setText("COPIED!")
                btn.setStyleSheet("background-color: #FFFFFF; color: #000000; border: 1px solid #FFFFFF; font-size: 10px; font-weight: bold; border-radius: 0px;")
                QtCore.QTimer.singleShot(1400, lambda: self._reset_code_btn(btn))
        except Exception as e:
            self.status_lbl.setText(f"Failed to generate TOTP code: {e}")

    def _reset_code_btn(self, btn: QtWidgets.QPushButton):
        btn.setText("Copy")
        btn.setStyleSheet("background-color: #161616; color: #E8E8E8; border: 1px solid #333333; font-size: 10px; font-weight: bold; border-radius: 0px;")

    def _on_row_double_clicked(self, row: int, col: int):
        if 0 <= row < len(self.visible_accounts):
            acc = self.visible_accounts[row]
            self._copy_code_for_account(acc)

    def _edit_account_2fa(self, acc: RobloxAccount):
        dlg = EditAccountDialog(acc, parent=self)
        if dlg.exec():
            self.storage.save_account(acc)
            self._apply_filter()

    def closeEvent(self, event):
        if self.timer.isActive():
            self.timer.stop()
        super().closeEvent(event)


class BatchAutoLoginWorker(QtCore.QThread):
    account_started = QtCore.pyqtSignal(int, int, object)  # idx, total, acc
    account_status_update = QtCore.pyqtSignal(object, str, str)  # acc, status, color_hex
    account_finished = QtCore.pyqtSignal(object, bool, str)  # acc, success, msg
    request_browser_login = QtCore.pyqtSignal(object, str)  # acc, reason
    batch_complete = QtCore.pyqtSignal(int, int, int)  # success_cnt, fail_cnt, captcha_cnt

    def __init__(
        self,
        accounts: List[RobloxAccount],
        api: RobloxAPI,
        storage: StorageManager,
        runner=None,
        pacing_delay: float = 3.5,
        use_jitter: bool = True,
        auto_browser_captcha: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.accounts = accounts
        self.api = api
        self.storage = storage
        self.runner = runner
        self.pacing_delay = max(0.5, float(pacing_delay))
        self.use_jitter = use_jitter
        self.auto_browser_captcha = auto_browser_captcha
        self.stopped = False
        self._browser_event = threading.Event()
        self._skip_current_account = False

    def stop(self):
        self.stopped = True
        self._browser_event.set()

    def notify_browser_finished(self):
        self._browser_event.set()

    def skip_current_account(self):
        self._skip_current_account = True
        self._browser_event.set()

    def run(self):
        total = len(self.accounts)
        success_cnt = 0
        fail_cnt = 0
        captcha_cnt = 0

        for idx, acc in enumerate(self.accounts):
            if self.stopped:
                break

            # Pacing cooldown between sequential accounts to prevent Arkose burst detection
            if idx > 0 and not self.stopped:
                jitter = random.uniform(0.5, 2.5) if self.use_jitter else 0.0
                delay = self.pacing_delay + jitter
                self.account_status_update.emit(
                    acc, f"Pacing cooldown ({delay:.1f}s) to avoid Arkose detection...", "#888888"
                )
                slices = int(delay * 10)
                for _ in range(slices):
                    if self.stopped:
                        break
                    time.sleep(0.1)

            if self.stopped:
                break

            self.account_started.emit(idx + 1, total, acc)

            # Check required credentials
            if not acc.username or not acc.password:
                fail_cnt += 1
                self.account_finished.emit(acc, False, "Missing password in vault")
                continue

            self.account_status_update.emit(acc, "Authenticating with credentials & 2FA...", "#CCCCCC")

            # Perform direct API login
            ok, cookie, user_data, msg = self.api.login_with_credentials(
                acc.username, acc.password, acc.totp_secret
            )

            # Rate limit backoff (HTTP 429)
            if "RATE_LIMITED" in msg or "429" in msg or "too many requests" in msg.lower():
                retry_sec = 20
                if "RATE_LIMITED:" in msg:
                    try:
                        retry_sec = int(msg.split(":")[1])
                    except Exception:
                        pass
                self.account_status_update.emit(
                    acc, f"Rate limited (HTTP 429). Cooling down {retry_sec}s for Arkose...", "#A8A8A8"
                )
                for _ in range(retry_sec * 10):
                    if self.stopped:
                        break
                    time.sleep(0.1)

                if not self.stopped:
                    self.account_status_update.emit(acc, "Retrying authentication...", "#CCCCCC")
                    ok, cookie, user_data, msg = self.api.login_with_credentials(
                        acc.username, acc.password, acc.totp_secret
                    )

            # Roblox security challenge / Arkose challenge
            is_challenge = (
                not ok
                and (
                    msg == "CAPTCHA_REQUIRED"
                    or "captcha" in msg.lower()
                    or "challenge" in msg.lower()
                    or "verification" in msg.lower()
                )
            )
            if is_challenge:
                if self.auto_browser_captcha and not self.stopped:
                    self.account_status_update.emit(
                        acc, "Security challenge required. Verifying in browser...", "#A8A8A8"
                    )
                    self._browser_event.clear()
                    self._skip_current_account = False
                    self.request_browser_login.emit(acc, "Security challenge / browser verification required")

                    while not self._browser_event.is_set() and not self.stopped:
                        time.sleep(0.2)

                    if self._skip_current_account:
                        fail_cnt += 1
                        self.account_finished.emit(acc, False, "Skipped by user")
                        continue

                    refreshed = self.storage.get_account(acc.account_id)
                    if refreshed and refreshed.cookie:
                        acc.cookie = refreshed.cookie
                        success_cnt += 1
                        captcha_cnt += 1
                        self.account_finished.emit(acc, True, "Success (Web verified)")
                        continue
                    else:
                        fail_cnt += 1
                        self.account_finished.emit(acc, False, "Verification required (Click 'Verify Selected in Browser')")
                        continue
                else:
                    fail_cnt += 1
                    self.account_finished.emit(acc, False, "Verification required (Click 'Verify Selected in Browser')")
                    continue

            if ok and cookie:
                acc.cookie = cookie
                if user_data:
                    acc.user_id = user_data.get("id", acc.user_id)
                    acc.display_name = user_data.get("displayName", acc.display_name)
                self.storage.save_account(acc)
                if self.runner and hasattr(self.runner, "auth_state_manager"):
                    self.runner.auth_state_manager.mark_ready(acc.account_id)
                success_cnt += 1
                self.account_finished.emit(acc, True, "Success")
            else:
                fail_cnt += 1
                self.account_finished.emit(acc, False, msg or "Failed")

        self.batch_complete.emit(success_cnt, fail_cnt, captcha_cnt)


class BatchAutoLoginDialog(BorderlessDialog):
    """
    Dedicated dialog for sequential automated authentication across multiple accounts
    with Arkose anti-bot pacing, randomized jitter, and 429 rate-limit backoff.
    """
    def __init__(
        self,
        accounts: List[RobloxAccount],
        api: RobloxAPI,
        storage: StorageManager,
        runner=None,
        parent=None,
    ):
        super().__init__(parent, title="Batch Auto Log In", can_maximize=True, resizable=True)
        self.accounts = accounts
        self.api = api
        self.storage = storage
        self.runner = runner
        self.worker: Optional[BatchAutoLoginWorker] = None
        self._current_browser_dlg: Optional[WebLoginDialog] = None

        self.resize(740, 500)
        self.setMinimumSize(620, 380)
        self._init_ui()

    def _init_ui(self):
        layout = self.body_layout
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        hdr = QtWidgets.QLabel(f"// BATCH AUTO LOG IN — {len(self.accounts)} ACCOUNTS")
        hdr.setStyleSheet("color: #FFFFFF; font-size: 12px; font-family: monospace; font-weight: bold;")
        layout.addWidget(hdr)

        desc = QtWidgets.QLabel(
            "Sequential automated login pipeline. Authenticates credentials and solves 2FA challenges\n"
            "with anti-bot pacing jitter and automatic HTTP 429 rate-limit backoff to avoid Arkose penalties."
        )
        desc.setStyleSheet("color: #888888; font-size: 10px; font-family: monospace;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Anti-Arkose Configuration Frame
        opts_box = QtWidgets.QFrame()
        opts_box.setStyleSheet("background-color: #0c0c0c; border: 1px solid #1f1f1f; padding: 4px;")
        opts_layout = QtWidgets.QVBoxLayout(opts_box)
        opts_layout.setContentsMargins(10, 8, 10, 8)
        opts_layout.setSpacing(6)

        row1 = QtWidgets.QHBoxLayout()
        pacing_lbl = QtWidgets.QLabel("Pacing Delay:")
        pacing_lbl.setStyleSheet("color: #CCCCCC; font-size: 11px; font-family: monospace;")
        row1.addWidget(pacing_lbl)

        self.delay_spin = QtWidgets.QDoubleSpinBox()
        self.delay_spin.setFixedHeight(26)
        self.delay_spin.setRange(1.0, 15.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setValue(3.5)
        self.delay_spin.setSuffix(" sec")
        self.delay_spin.setStyleSheet("background-color: #161616; color: #FFFFFF; border: 1px solid #333333; font-family: monospace;")
        row1.addWidget(self.delay_spin)
        row1.addSpacing(16)

        self.cb_jitter = QtWidgets.QCheckBox("Anti-Arkose Jitter (+0.5s - 2.5s randomized intervals)")
        self.cb_jitter.setChecked(True)
        self.cb_jitter.setStyleSheet("color: #CCCCCC; font-size: 11px; font-family: monospace;")
        row1.addWidget(self.cb_jitter)
        row1.addStretch()
        opts_layout.addLayout(row1)

        self.cb_auto_browser = QtWidgets.QCheckBox("Launch browser automatically if verification / Captcha is required")
        self.cb_auto_browser.setChecked(True)
        self.cb_auto_browser.setStyleSheet("color: #CCCCCC; font-size: 11px; font-family: monospace;")
        opts_layout.addWidget(self.cb_auto_browser)

        layout.addWidget(opts_box)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("QProgressBar { background-color: #101010; border: 1px solid #222222; } QProgressBar::chunk { background-color: #E8E8E8; }")
        self.progress_bar.setRange(0, max(1, len(self.accounts)))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Table
        self.table = QtWidgets.QTableWidget(len(self.accounts), 5)
        self.table.setHorizontalHeaderLabels(["#", "Account", "Password", "2FA Key", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().resizeSection(1, 160)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet("QTableWidget { background-color: #050505; border: 1px solid #1c1c1c; font-family: monospace; font-size: 11px; color: #E8E8E8; }")

        for i, acc in enumerate(self.accounts):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(f"@{acc.username}"))

            pwd_item = QtWidgets.QTableWidgetItem("Present" if acc.password else "Missing")
            pwd_item.setForeground(QtGui.QColor("#E8E8E8" if acc.password else "#707070"))
            self.table.setItem(i, 2, pwd_item)

            totp_item = QtWidgets.QTableWidgetItem("Active (Auto 2FA)" if acc.totp_secret else "None")
            totp_item.setForeground(QtGui.QColor("#E8E8E8" if acc.totp_secret else "#707070"))
            self.table.setItem(i, 3, totp_item)

            st_item = QtWidgets.QTableWidgetItem("Ready")
            st_item.setForeground(QtGui.QColor("#888888"))
            self.table.setItem(i, 4, st_item)

        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self.table.itemSelectionChanged.connect(self._update_live_2fa)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        layout.addWidget(self.table, stretch=1)

        # Manual Mode & Real-Time 2FA Assistant Bar
        manual_box = QtWidgets.QFrame()
        manual_box.setObjectName("manualLoginBox")
        manual_box.setStyleSheet(
            "QFrame#manualLoginBox { background-color: #080808; border: 1px solid #222222; border-radius: 0px; } "
            "QPushButton { background-color: #121212; border: 1px solid #353535; color: #E8E8E8; padding: 4px 10px; font-size: 11px; font-family: monospace; font-weight: bold; border-radius: 0px; } "
            "QPushButton:hover { background-color: #E8E8E8; border-color: #E8E8E8; color: #000000; } "
            "QPushButton:disabled { color: #505050; border-color: #1A1A1A; background-color: #080808; }"
        )
        manual_layout = QtWidgets.QHBoxLayout(manual_box)
        manual_layout.setContentsMargins(8, 5, 8, 5)
        manual_layout.setSpacing(8)

        lbl_hdr = QtWidgets.QLabel("SELECTED ACCOUNT:")
        lbl_hdr.setStyleSheet("color: #A0A0A0; font-size: 10px; font-family: monospace; font-weight: bold; border: none;")
        manual_layout.addWidget(lbl_hdr)

        self.lbl_selected_acc = QtWidgets.QLabel("No selection")
        self.lbl_selected_acc.setStyleSheet("color: #FFFFFF; font-size: 11px; font-family: monospace; font-weight: bold; border: none;")
        manual_layout.addWidget(self.lbl_selected_acc)

        self.btn_copy_user = QtWidgets.QPushButton("Copy User")
        self.btn_copy_user.setToolTip("Copy username to clipboard (Shortcut: Ctrl+U)")
        self.btn_copy_user.clicked.connect(self._copy_selected_username)
        manual_layout.addWidget(self.btn_copy_user)

        self.btn_copy_pass = QtWidgets.QPushButton("Copy Password")
        self.btn_copy_pass.setToolTip("Copy password to clipboard (Shortcut: Ctrl+P)")
        self.btn_copy_pass.clicked.connect(self._copy_selected_password)
        manual_layout.addWidget(self.btn_copy_pass)

        self.btn_copy_2fa = QtWidgets.QPushButton("2FA: --- ---")
        self.btn_copy_2fa.setToolTip("Copy live 6-digit Authenticator TOTP code to clipboard (Shortcut: Ctrl+K)")
        self.btn_copy_2fa.clicked.connect(self._copy_selected_2fa)
        manual_layout.addWidget(self.btn_copy_2fa)

        manual_layout.addStretch()

        self.btn_next_account = QtWidgets.QPushButton("Next >")
        self.btn_next_account.setToolTip("Select next account in the table (Shortcut: Alt+Right)")
        self.btn_next_account.clicked.connect(self._select_next_account)
        manual_layout.addWidget(self.btn_next_account)

        layout.addWidget(manual_box)

        # Keyboard shortcuts
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+U"), self).activated.connect(self._copy_selected_username)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+P"), self).activated.connect(self._copy_selected_password)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+K"), self).activated.connect(self._copy_selected_2fa)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+O"), self).activated.connect(self._on_solve_selected_clicked)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Right"), self).activated.connect(self._select_next_account)

        # Real-time TOTP update timer
        self._totp_timer = QtCore.QTimer(self)
        self._totp_timer.setInterval(1000)
        self._totp_timer.timeout.connect(self._update_live_2fa)
        self._totp_timer.start()

        if self.accounts:
            self.table.selectRow(0)
            QtCore.QTimer.singleShot(100, self._update_live_2fa)

        # Bottom status and controls
        self.status_lbl = QtWidgets.QLabel(f"Ready to log in {len(self.accounts)} accounts sequentially.")
        self.status_lbl.setStyleSheet("color: #888888; font-size: 11px; font-family: monospace;")
        layout.addWidget(self.status_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_skip = QtWidgets.QPushButton("Skip Account")
        self.btn_skip.setEnabled(False)
        self.btn_skip.setFixedHeight(28)
        self.btn_skip.clicked.connect(self._on_skip_clicked)
        btn_row.addWidget(self.btn_skip)

        self.btn_solve_browser = QtWidgets.QPushButton("Verify Selected in Browser")
        self.btn_solve_browser.setFixedHeight(28)
        self.btn_solve_browser.setToolTip("Open embedded browser with auto-fill and 2FA solver to complete verification (Shortcut: Ctrl+O)")
        self.btn_solve_browser.clicked.connect(self._on_solve_selected_clicked)
        btn_row.addWidget(self.btn_solve_browser)

        btn_row.addStretch()

        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.setFixedHeight(28)
        self.btn_close.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self.btn_close)

        self.btn_start = QtWidgets.QPushButton("Start Batch Auto Log In")
        self.btn_start.setFixedHeight(28)
        self.btn_start.setStyleSheet(
            "QPushButton { background-color: #E8E8E8; border: 1px solid #E8E8E8; color: #000000; font-weight: bold; padding: 2px 12px; border-radius: 0px; } "
            "QPushButton:hover { background-color: #FFFFFF; border-color: #FFFFFF; color: #000000; }"
        )
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start)

        layout.addLayout(btn_row)

    def _start_batch(self):
        self.btn_start.setEnabled(False)
        self.delay_spin.setEnabled(False)
        self.cb_jitter.setEnabled(False)
        self.cb_auto_browser.setEnabled(False)
        self.btn_close.setText("Stop")
        self.btn_skip.setEnabled(False)
        self.status_lbl.setText("Starting sequential auto login...")

        self.worker = BatchAutoLoginWorker(
            accounts=self.accounts,
            api=self.api,
            storage=self.storage,
            runner=self.runner,
            pacing_delay=self.delay_spin.value(),
            use_jitter=self.cb_jitter.isChecked(),
            auto_browser_captcha=self.cb_auto_browser.isChecked(),
            parent=self,
        )
        self.worker.account_started.connect(self._on_account_started)
        self.worker.account_status_update.connect(self._on_account_status_update)
        self.worker.account_finished.connect(self._on_account_finished)
        self.worker.request_browser_login.connect(self._on_request_browser_login)
        self.worker.batch_complete.connect(self._on_batch_complete)
        self.worker.start()

    def _on_account_started(self, idx: int, total: int, acc: RobloxAccount):
        self.progress_bar.setValue(idx - 1)
        self.status_lbl.setText(f"Logging in @{acc.username} ({idx}/{total})...")
        r = self._row_for_account(acc)
        if r >= 0:
            it = self.table.item(r, 4)
            if it:
                it.setText("Connecting...")
                it.setForeground(QtGui.QColor("#A8A8A8"))

    def _on_account_status_update(self, acc: RobloxAccount, status_text: str, color_hex: str):
        self.status_lbl.setText(f"[@{acc.username}] {status_text}")
        r = self._row_for_account(acc)
        if r >= 0:
            it = self.table.item(r, 4)
            if it:
                it.setText(status_text)
                it.setForeground(QtGui.QColor(color_hex))

    def _on_account_finished(self, acc: RobloxAccount, ok: bool, msg: str):
        r = self._row_for_account(acc)
        if r >= 0:
            it = self.table.item(r, 4)
            if it:
                it.setText(msg)
                it.setForeground(QtGui.QColor("#E8E8E8" if ok else "#888888"))

    def _on_request_browser_login(self, acc: RobloxAccount, reason: str):
        self.btn_skip.setEnabled(True)
        browser_dlg = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            runner=self.runner,
            target_account=acc,
            auto_login=True,
            parent=self,
        )
        self._current_browser_dlg = browser_dlg

        def _on_browser_closed():
            self._current_browser_dlg = None
            self.btn_skip.setEnabled(False)
            if self.worker:
                self.worker.notify_browser_finished()

        browser_dlg.account_authenticated.connect(lambda saved_acc: _on_browser_closed())
        browser_dlg.finished.connect(lambda result: _on_browser_closed())
        browser_dlg.show()
        browser_dlg.raise_()
        browser_dlg.activateWindow()

    def _on_row_double_clicked(self, row: int, col: int):
        if 0 <= row < len(self.accounts):
            self._solve_account_in_browser(self.accounts[row])

    def _on_solve_selected_clicked(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.accounts):
            self._solve_account_in_browser(self.accounts[row])
        else:
            QMessageBox.information(self, "Verify in Browser", "Please click an account row in the table first.")

    def _solve_account_in_browser(self, acc: RobloxAccount):
        self._open_webengine_login(acc)

    def _open_webengine_login(self, acc: RobloxAccount):
        dlg = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            runner=self.runner,
            target_account=acc,
            auto_login=True,
            parent=self,
        )
        def _on_saved(saved):
            r = self._row_for_account(acc)
            if r >= 0:
                it = self.table.item(r, 4)
                if it:
                    it.setText("Success (Web verified)")
                    it.setForeground(QtGui.QColor("#E8E8E8"))
            self.status_lbl.setText(f"Successfully authenticated @{acc.username} in embedded browser!")
            self._select_next_account()
        dlg.account_authenticated.connect(_on_saved)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_skip_clicked(self):
        if self._current_browser_dlg:
            self._current_browser_dlg.close()
        if self.worker:
            self.worker.skip_current_account()

    def _on_batch_complete(self, success_cnt: int, fail_cnt: int, web_verified_cnt: int):
        self.progress_bar.setValue(len(self.accounts))
        self.btn_start.setEnabled(True)
        self.delay_spin.setEnabled(True)
        self.cb_jitter.setEnabled(True)
        self.cb_auto_browser.setEnabled(True)
        self.btn_skip.setEnabled(False)
        self.btn_close.setText("Close")

        direct_cnt = max(0, success_cnt - web_verified_cnt)
        self.status_lbl.setText(
            f"Batch complete: {success_cnt} succeeded ({direct_cnt} direct API, {web_verified_cnt} web verified), {fail_cnt} failed."
        )

        breakdown = [f"• Succeeded: {success_cnt}"]
        if web_verified_cnt > 0:
            breakdown.append(f"  - Direct API logins: {direct_cnt}")
            breakdown.append(f"  - Web browser verifications: {web_verified_cnt}")
        breakdown.append(f"• Failed: {fail_cnt}")

        msg_body = (
            f"Completed sequential auto log in across {len(self.accounts)} accounts:\n\n"
            + "\n".join(breakdown)
            + "\n\nAll captured session cookies have been updated in your local encrypted vault."
        )

        QMessageBox.information(
            self,
            "Batch Auto Log In Complete",
            msg_body
        )
        self.accept()

    def _get_selected_account(self) -> Optional[RobloxAccount]:
        row = self.table.currentRow()
        if 0 <= row < len(self.accounts):
            return self.accounts[row]
        return None

    def _update_live_2fa(self):
        acc = self._get_selected_account()
        if not acc:
            if hasattr(self, "lbl_selected_acc"):
                self.lbl_selected_acc.setText("No selection")
                self.btn_copy_user.setEnabled(False)
                self.btn_copy_pass.setEnabled(False)
                self.btn_copy_2fa.setEnabled(False)
                self.btn_copy_2fa.setText("2FA: --- ---")
            return

        if hasattr(self, "lbl_selected_acc"):
            self.lbl_selected_acc.setText(f"@{acc.username}")
            self.btn_copy_user.setEnabled(bool(acc.username))
            self.btn_copy_pass.setEnabled(bool(acc.password))

            if acc.totp_secret:
                from core.totp import generate_totp, get_totp_remaining_seconds
                try:
                    code = generate_totp(acc.totp_secret)
                    rem = get_totp_remaining_seconds()
                    self.btn_copy_2fa.setEnabled(True)
                    code_fmt = f"{code[:3]} {code[3:]}"
                    self.btn_copy_2fa.setText(f"2FA: {code_fmt} ({rem}s)")
                    self.btn_copy_2fa.setStyleSheet(
                        "QPushButton { background-color: #161616; border: 1px solid #505050; color: #FFFFFF; font-weight: bold; border-radius: 0px; } "
                        "QPushButton:hover { background-color: #E8E8E8; border-color: #E8E8E8; color: #000000; }"
                    )
                except Exception:
                    self.btn_copy_2fa.setEnabled(False)
                    self.btn_copy_2fa.setText("2FA: Invalid Key")
            else:
                self.btn_copy_2fa.setEnabled(False)
                self.btn_copy_2fa.setText("2FA: None")
                self.btn_copy_2fa.setStyleSheet(
                    "QPushButton { background-color: #080808; border: 1px solid #1A1A1A; color: #505050; border-radius: 0px; }"
                )

    def _copy_selected_username(self):
        acc = self._get_selected_account()
        if acc and acc.username:
            QtWidgets.QApplication.clipboard().setText(acc.username)
            self.status_lbl.setText(f"Copied username @{acc.username} to clipboard.")

    def _copy_selected_password(self):
        acc = self._get_selected_account()
        if acc and acc.password:
            QtWidgets.QApplication.clipboard().setText(acc.password)
            self.status_lbl.setText(f"Copied password for @{acc.username} to clipboard.")

    def _copy_selected_2fa(self):
        acc = self._get_selected_account()
        if acc and acc.totp_secret:
            from core.totp import generate_totp, get_totp_remaining_seconds
            try:
                code = generate_totp(acc.totp_secret)
                rem = get_totp_remaining_seconds()
                QtWidgets.QApplication.clipboard().setText(code)
                self.status_lbl.setText(f"Copied 2FA code {code[:3]} {code[3:]} for @{acc.username} (valid for {rem}s).")
            except Exception as e:
                self.status_lbl.setText(f"Failed to generate 2FA code: {e}")

    def _select_next_account(self):
        curr = self.table.currentRow()
        next_row = curr + 1 if curr + 1 < self.table.rowCount() else 0
        self.table.selectRow(next_row)
        self._update_live_2fa()

    def _on_table_context_menu(self, pos: QtCore.QPoint):
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        if not (0 <= row < len(self.accounts)):
            return
        acc = self.accounts[row]
        menu = QtWidgets.QMenu(self)

        act_user = menu.addAction(f"Copy Username (@{acc.username})")
        act_user.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(acc.username))

        if acc.password:
            act_pass = menu.addAction("Copy Password")
            act_pass.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(acc.password))

        if acc.totp_secret:
            from core.totp import generate_totp, get_totp_remaining_seconds
            try:
                code = generate_totp(acc.totp_secret)
                rem = get_totp_remaining_seconds()
                act_2fa = menu.addAction(f"Copy 2FA Code ({code[:3]} {code[3:]} - {rem}s)")
                act_2fa.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(code))
            except Exception:
                pass

        menu.addSeparator()
        act_web = menu.addAction("Verify in Browser...")
        act_web.triggered.connect(lambda: self._open_webengine_login(acc))

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _row_for_account(self, acc: RobloxAccount) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 1)
            if it and it.text() == f"@{acc.username}":
                return r
        return -1

    def _on_close_clicked(self):
        if hasattr(self, "_totp_timer") and self._totp_timer.isActive():
            self._totp_timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        if self._current_browser_dlg:
            self._current_browser_dlg.close()
        self.reject()

    def closeEvent(self, event):
        if hasattr(self, "_totp_timer") and self._totp_timer.isActive():
            self._totp_timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1500)
        if self._current_browser_dlg:
            self._current_browser_dlg.close()
        super().closeEvent(event)






