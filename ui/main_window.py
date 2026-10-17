"""
Main window for AERAM using the authentic RAM layout and clean dark aesthetics.
Supports Account Grouping, Dynamic Selection Counter, Sequential/Staggered Multi-Join,
Mode Toggling (High / Low), and Fast utilitarian management.
"""

import os
import sys
import time
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone
from PyQt6 import QtWidgets, QtCore, QtGui

from core.models import RobloxAccount, FavoriteGame
from core.storage import StorageManager
from core.roblox_api import RobloxAPI
from core.instance_runner import InstanceRunner
from core.auth_state import AccountAuthState
from ui.dialogs import (
    AddAccountDialog, ImportRAMDialog, MultiLaunchDialog, EditAccountDialog,
    SettingsDialog, GroupManagerDialog, WebLoginDialog, UnblockEveryoneDialog,
    FastFlagsDialog, BatchSignOutDialog, GoogleAuthImportDialog,
    DiagnosticsDialog, UpdateDialog, ChangePasswordDialog, BatchPasswordChangeDialog,
    BatchUnblockDialog, BatchLogoutDialog, BatchFriendsDialog, BatchPrivacyDialog,
    Live2FAHubDialog, BatchAutoLoginDialog
)
from ui.server_finder_dialog import ServerFinderDialog
from ui.borderless_dialog import BorderlessInputDialog, BorderlessMessageBox as QMessageBox
from ui.theme import DARK_THEME_QSS

log = logging.getLogger("MainWindow")

AUTH_FAIL_MSG = "Please right-click the account and select 'Edit Account' to paste a fresh .ROBLOSECURITY cookie."

_LED_ICON_CACHE: Dict[str, QtGui.QIcon] = {}


def get_led_icon(color_hex: str, size: int = 12) -> QtGui.QIcon:
    """Generates and caches an anti-aliased phosphor LED micro-indicator dot with subtle glow halo."""
    key = f"{color_hex}_{size}"
    if key not in _LED_ICON_CACHE:
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Phosphor outer halo (soft glow)
        halo = QtGui.QColor(color_hex)
        halo.setAlpha(45)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(halo))
        painter.drawEllipse(1, 1, size - 2, size - 2)

        # Solid core dot
        core = QtGui.QColor(color_hex)
        painter.setBrush(QtGui.QBrush(core))
        core_offset = 3
        core_size = max(2, size - 6)
        painter.drawEllipse(core_offset, core_offset, core_size, core_size)
        painter.end()

        _LED_ICON_CACHE[key] = QtGui.QIcon(pixmap)
    return _LED_ICON_CACHE[key]


def _format_last_online(iso_str: Optional[str]) -> str:
    """Formats an ISO-8601 timestamp from Roblox Presence API into human relative time."""
    if not iso_str:
        return ""
    try:
        clean_str = iso_str.rstrip("Z").split(".")[0]
        dt = datetime.fromisoformat(clean_str).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = max(0.0, (now - dt).total_seconds())
        if diff < 60:
            return "Just now"
        elif diff < 3600:
            return f"{int(diff // 60)}m ago"
        elif diff < 86400:
            return f"{int(diff // 3600)}h ago"
        else:
            return f"{int(diff // 86400)}d ago"
    except Exception:
        return ""


class PresenceMonitorWorker(QtCore.QThread):
    """
    Background worker that continuously monitors Roblox online/in-game web presence
    for all stored accounts (matching RAM presence watcher behavior) in efficient batches.
    """
    presences_updated = QtCore.pyqtSignal(list)  # List[Dict[str, Any]]

    def __init__(self, api: RobloxAPI, accounts_getter, interval_sec: int = 12, parent=None):
        super().__init__(parent)
        self.api = api
        self.accounts_getter = accounts_getter
        self.interval_sec = max(5, int(interval_sec))
        self.stopped = False
        self._force_refresh = False

    def trigger_immediate_refresh(self):
        self._force_refresh = True

    def run(self):
        while not self.stopped:
            accounts: List[RobloxAccount] = self.accounts_getter()
            user_ids = [a.user_id for a in accounts if a.user_id and a.user_id > 0]
            caller_cookie = next((a.cookie for a in accounts if a.cookie), None)

            if user_ids:
                try:
                    presences = self.api.get_users_presences(user_ids, cookie=caller_cookie)
                    if presences and not self.stopped:
                        self.presences_updated.emit(presences)
                except Exception as e:
                    log.debug(f"[Presence] Error querying presence batch: {e}")

            # Sleep in 200ms increments so thread can wake on immediate trigger or stop quickly
            self._force_refresh = False
            for _ in range(int(self.interval_sec * 5)):
                if self.stopped or self._force_refresh:
                    break
                time.sleep(0.2)

    def stop(self):
        self.stopped = True


class AccountSyncWorker(QtCore.QThread):
    """Background worker that enriches profile metadata and avatar thumbnails without freezing GUI."""
    account_synced = QtCore.pyqtSignal(str, str, str, int)  # (account_id, display_name, avatar_url, robux)
    all_finished = QtCore.pyqtSignal(int)  # updated count

    def __init__(self, storage, api, accounts, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.api = api
        self.accounts = accounts
        self.stopped = False

    def run(self):
        updated = 0
        for acc in self.accounts:
            if self.stopped:
                break
            if not acc.cookie:
                continue
            try:
                data = self.api.enrich_account(acc.cookie)
                if data:
                    self.storage.update_metadata_preserve_alias(
                        acc.account_id, data["display_name"], data["avatar_url"], data["robux_balance"]
                    )
                    self.account_synced.emit(
                        acc.account_id, data["display_name"], data["avatar_url"], data["robux_balance"]
                    )
                    updated += 1
            except Exception as e:
                log.debug(f"Failed to sync metadata for {acc.username}: {e}")
            time.sleep(0.08)  # 80ms spacing to prevent rate limits

        self.all_finished.emit(updated)

    def stop(self):
        self.stopped = True


class CookieValidationWorker(QtCore.QThread):
    """Background worker that gently verifies all account cookies and syncs Robux balances."""
    account_checked = QtCore.pyqtSignal(str, bool, int, str)  # (account_id, is_valid, robux, status_reason)
    all_finished = QtCore.pyqtSignal(int, int)  # (valid_count, expired_count)

    def __init__(self, storage, api, accounts, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.api = api
        self.accounts = accounts
        self.stopped = False

    def run(self):
        valid_cnt = 0
        expired_cnt = 0
        for acc in self.accounts:
            if self.stopped:
                break
            if not acc.cookie:
                continue
            is_valid, user_data, reason = self.api.validate_cookie(acc.cookie)
            robux = -1
            if is_valid:
                valid_cnt += 1
                try:
                    robux = self.api.get_robux_balance(acc.user_id, acc.cookie)
                except Exception:
                    pass
            elif reason == "EXPIRED":
                expired_cnt += 1

            self.account_checked.emit(acc.account_id, is_valid, robux, reason)
            time.sleep(0.08)  # 80ms spacing to prevent rate limits

        self.all_finished.emit(valid_cnt, expired_cnt)

    def stop(self):
        self.stopped = True


class MultiLaunchWorker(QtCore.QThread):
    """
    Background worker that launches accounts sequentially with a configurable
    stagger delay, providing live second-by-second countdown and responsive cancellation.
    """
    progress = QtCore.pyqtSignal(int, int, str, str)   # (current_idx, total, username, status_msg)
    countdown_tick = QtCore.pyqtSignal(int, str)       # (seconds_left, next_username)
    account_launched = QtCore.pyqtSignal(object)       # (account)
    finished_queue = QtCore.pyqtSignal(int, list)      # (launched_count, failed_list)

    def __init__(
        self,
        runner: Any,
        accounts: List[RobloxAccount],
        place_id: Optional[int] = None,
        job_id: Optional[str] = None,
        follow_user_id: Optional[int] = None,
        stagger_delay: float = 15.0,
    ):
        super().__init__()
        self.runner = runner
        self.accounts = list(accounts)
        self.place_id = place_id
        self.job_id = job_id
        self.follow_user_id = follow_user_id
        self.stagger_delay = max(1.0, float(stagger_delay))
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        launched = 0
        failed = []
        total = len(self.accounts)

        for idx, acc in enumerate(self.accounts):
            if self._cancelled:
                break

            can_launch, reason = self.runner.auth_state_manager.can_launch(acc.account_id) if hasattr(self.runner, "auth_state_manager") else (True, "")
            if not can_launch:
                failed.append((acc.username, reason))
                self.progress.emit(idx + 1, total, acc.username, f"Skipped: {reason}")
                continue

            self.progress.emit(idx + 1, total, acc.username, f"Launching @{acc.username} ({idx + 1}/{total})...")

            try:
                self.runner.launch_instance(
                    account=acc,
                    place_id=self.place_id,
                    job_id=self.job_id,
                    follow_user_id=self.follow_user_id,
                )
                launched += 1
                self.account_launched.emit(acc)
            except Exception as e:
                failed.append((acc.username, str(e)))
                self.progress.emit(idx + 1, total, acc.username, f"Error: {e}")

            # If more accounts remain, perform staggered countdown
            if idx < total - 1 and not self._cancelled:
                next_acc = self.accounts[idx + 1]
                remaining_time = int(self.stagger_delay)
                while remaining_time > 0 and not self._cancelled:
                    self.countdown_tick.emit(remaining_time, next_acc.username)
                    self.msleep(1000)
                    remaining_time -= 1

        self.finished_queue.emit(launched, failed)



class ReorderableTableWidget(QtWidgets.QTableWidget):
    """QTableWidget with row drag-and-drop reordering and keyboard interaction support."""
    rows_reordered = QtCore.pyqtSignal(list, int)  # (source_rows, target_row)
    enter_pressed = QtCore.pyqtSignal()
    delete_pressed = QtCore.pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragDrop)

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        key = event.key()
        if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.enter_pressed.emit()
            event.accept()
            return
        elif key == QtCore.Qt.Key.Key_Delete:
            self.delete_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.source() == self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if event.source() == self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        if not indexes:
            return
        drag = QtGui.QDrag(self)
        mime = self.model().mimeData(indexes)
        drag.setMimeData(mime)
        # Use CopyAction so Qt's startDrag never executes destructive clearOrRemove()
        drag.exec(QtCore.Qt.DropAction.CopyAction)

    def dropEvent(self, event: QtGui.QDropEvent):
        if event.source() == self:
            selected_rows = sorted(set(idx.row() for idx in self.selectedIndexes()))
            if not selected_rows:
                event.ignore()
                return

            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            drop_index = self.indexAt(pos)
            target_row = drop_index.row() if drop_index.isValid() else self.rowCount()

            indicator = self.dropIndicatorPosition()
            if indicator == QtWidgets.QAbstractItemView.DropIndicatorPosition.BelowItem:
                target_row += 1
            elif indicator == QtWidgets.QAbstractItemView.DropIndicatorPosition.OnItem:
                # If moving downwards and dropping directly onto an item, drop after it
                if selected_rows and selected_rows[0] < target_row:
                    target_row += 1

            # Accept drop and tell Qt to ignore default deletion
            event.accept()
            event.setDropAction(QtCore.Qt.DropAction.IgnoreAction)

            # Defer emission to next event loop iteration so drag finishes cleanly before table re-renders
            QtCore.QTimer.singleShot(0, lambda rows=selected_rows, tgt=target_row: self.rows_reordered.emit(rows, tgt))
        else:
            event.ignore()


class DraggableDeckFrame(QtWidgets.QFrame):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.win = window
        self._dragging = False
        self._drag_start = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.win.toggle_maximize()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.globalPosition().toPoint() - self.win.pos()
            if self.win.windowHandle():
                try:
                    self.win.windowHandle().startSystemMove()
                except Exception:
                    pass

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.win.move(event.globalPosition().toPoint() - self._drag_start)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)


class AntiAfkWorker(QtCore.QThread):
    pulsed = QtCore.pyqtSignal(int)

    def __init__(self, interval_sec: int = 840):
        super().__init__()
        self.interval_sec = interval_sec
        self.running = True

    def run(self):
        import time
        from core.anti_afk import AntiAfkEngine
        engine = AntiAfkEngine(interval_seconds=self.interval_sec)
        while self.running:
            for _ in range(self.interval_sec):
                if not self.running:
                    return
                time.sleep(1)
            if self.running:
                count = engine.pulse_all_instances()
                self.pulsed.emit(count)

    def stop(self):
        self.running = False
        self.wait(1200)


class AuthValidatorWorker(QtCore.QThread):
    """
    Background worker that validates account session cookies asynchronously
    without blocking the Qt main GUI thread.
    Emits account_checked(account_id, is_valid, status_reason).
    """
    account_checked = QtCore.pyqtSignal(str, bool, str)
    finished_all = QtCore.pyqtSignal(int, int)  # valid_count, expired_count

    def __init__(self, accounts: List[Tuple[str, str]], api: RobloxAPI):
        super().__init__()
        self.accounts = accounts  # List of (account_id, cookie)
        self.api = api
        self.running = True

    def run(self):
        valid_cnt = 0
        expired_cnt = 0
        for acc_id, cookie in self.accounts:
            if not self.running:
                break
            if not cookie:
                self.account_checked.emit(acc_id, False, "EMPTY_COOKIE")
                continue
            try:
                is_valid, user_data, status_reason = self.api.validate_cookie(cookie)
                if is_valid:
                    valid_cnt += 1
                elif status_reason == "EXPIRED":
                    expired_cnt += 1
                self.account_checked.emit(acc_id, is_valid, status_reason)
            except Exception as e:
                self.account_checked.emit(acc_id, False, f"ERROR_{e}")
        self.finished_all.emit(valid_cnt, expired_cnt)

    def stop(self):
        self.running = False
        self.wait(1000)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, storage: StorageManager, api: RobloxAPI, runner: InstanceRunner):
        super().__init__()
        self.storage = storage
        self.api = api
        self.runner = runner
        self.anti_afk_worker: Optional[AntiAfkWorker] = None
        self._auth_validator_worker: Optional[AuthValidatorWorker] = None
        from core.self_heal import SelfHealEngine
        self.self_heal_engine = SelfHealEngine(self.runner, self.storage, api=self.api)
        self._heal_tick_counter = 0

        self.accounts: List[RobloxAccount] = []
        self.visible_accounts: List[RobloxAccount] = []
        self.account_map: Dict[str, RobloxAccount] = {}
        self.current_account: Optional[RobloxAccount] = None
        self._presence_map: Dict[int, Dict[str, Any]] = {}
        self._presence_worker: Optional[PresenceMonitorWorker] = None
        self._row_status_snapshot: Dict[str, Tuple[Any, ...]] = {}
        self.selected_group: str = "All Groups"
        self._sort_column: Optional[int] = None
        self._sort_order: Optional[QtCore.Qt.SortOrder] = None
        self.base_headers = ["Username", "Alias", "Group", "Mode", "Description"]

        self.setWindowTitle("AERAM")
        self.setMinimumSize(740, 415)
        self.resize(920, 450)

        # Seamless Frameless Window
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Window)
        self.setMouseTracking(True)
        self._dragging = False
        self._drag_start_pos = None

        self.setStyleSheet(DARK_THEME_QSS)

        self._init_ui()
        is_startup_check = self.storage.get_setting("check_cookies_on_startup", "true").lower() == "true"
        self._load_accounts(validate_auth=is_startup_check)

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self._poll_instances)
        self.poll_timer.start(1500)
        self._start_presence_monitor()

    # ------------------------------------------------------------------ layout

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.btn_win_max.setText("□")
        else:
            self.showMaximized()
            self.btn_win_max.setText("[+]")

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and event.position().y() <= 38:
            self.toggle_maximize()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            pos = event.position()
            w = self.width()
            h = self.height()
            margin = 6

            is_left = pos.x() <= margin
            is_right = pos.x() >= w - margin
            is_top = pos.y() <= margin
            is_bottom = pos.y() >= h - margin

            if is_left or is_right or is_top or is_bottom:
                edges = QtCore.Qt.Edge(0)
                if is_left: edges |= QtCore.Qt.Edge.LeftEdge
                if is_right: edges |= QtCore.Qt.Edge.RightEdge
                if is_top: edges |= QtCore.Qt.Edge.TopEdge
                if is_bottom: edges |= QtCore.Qt.Edge.BottomEdge
                win_handle = self.windowHandle()
                if win_handle:
                    try:
                        win_handle.startSystemResize(edges)
                    except Exception:
                        pass
                return

            if pos.y() <= 38:
                if self.windowHandle():
                    try:
                        self.windowHandle().startSystemMove()
                        return
                    except Exception:
                        pass
                self._dragging = True
                self._drag_start_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if getattr(self, "_dragging", False):
            self.move(event.globalPosition().toPoint() - self._drag_start_pos)
            return

        pos = event.position()
        w = self.width()
        h = self.height()
        margin = 6

        is_left = pos.x() <= margin
        is_right = pos.x() >= w - margin
        is_top = pos.y() <= margin
        is_bottom = pos.y() >= h - margin

        if (is_left and is_top) or (is_right and is_bottom):
            self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        elif (is_right and is_top) or (is_left and is_bottom):
            self.setCursor(QtCore.Qt.CursorShape.SizeBDiagCursor)
        elif is_left or is_right:
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        elif is_top or is_bottom:
            self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def _init_ui(self):
        container = QtWidgets.QWidget()
        container.setObjectName("ramCentral")
        self.setCentralWidget(container)

        container_layout = QtWidgets.QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # ─── Seamless Top Header & Navigation Bar ───
        top_header_frame = DraggableDeckFrame(self)
        top_header_frame.setObjectName("top_header_frame")
        top_header_frame.setFixedHeight(34)
        top_header_layout = QtWidgets.QHBoxLayout(top_header_frame)
        top_header_layout.setContentsMargins(12, 0, 8, 0)
        top_header_layout.setSpacing(8)
        top_header_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)

        lbl_brand = QtWidgets.QLabel("// AERAM <span style='color: #707070;'>· ROBLOX ACCOUNT MANAGER</span>")
        lbl_brand.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lbl_brand.setStyleSheet("color: #E8E8E8; font-weight: bold; font-size: 10px; letter-spacing: 1.5px;")
        lbl_brand.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)
        top_header_layout.addWidget(lbl_brand)

        top_header_layout.addStretch()

        # Window Controls (Min, Max, Close)
        win_box = QtWidgets.QHBoxLayout()
        win_box.setSpacing(2)

        self.btn_win_min = QtWidgets.QPushButton("−")
        self.btn_win_min.setToolTip("Minimize")
        self.btn_win_min.setFixedSize(24, 22)
        self.btn_win_min.setStyleSheet(
            "QPushButton { border: 1px solid #353535; font-size: 11px; padding: 0px; background-color: #000000; color: #A8A8A8; } "
            "QPushButton:hover { background-color: #E8E8E8; color: #000000; border-color: #E8E8E8; }"
        )
        self.btn_win_min.clicked.connect(self.showMinimized)
        win_box.addWidget(self.btn_win_min)

        self.btn_win_max = QtWidgets.QPushButton("□")
        self.btn_win_max.setToolTip("Maximize / Restore")
        self.btn_win_max.setFixedSize(24, 22)
        self.btn_win_max.setStyleSheet(
            "QPushButton { border: 1px solid #353535; font-size: 10px; padding: 0px; background-color: #000000; color: #A8A8A8; } "
            "QPushButton:hover { background-color: #E8E8E8; color: #000000; border-color: #E8E8E8; }"
        )
        self.btn_win_max.clicked.connect(self.toggle_maximize)
        win_box.addWidget(self.btn_win_max)

        self.btn_win_close = QtWidgets.QPushButton("✕")
        self.btn_win_close.setToolTip("Close")
        self.btn_win_close.setFixedSize(24, 22)
        self.btn_win_close.setStyleSheet(
            "QPushButton { border: 1px solid #353535; font-size: 10px; padding: 0px; background-color: #000000; color: #A8A8A8; } "
            "QPushButton:hover { background-color: #E8E8E8; color: #000000; border-color: #E8E8E8; }"
        )
        self.btn_win_close.clicked.connect(self.close)
        win_box.addWidget(self.btn_win_close)

        top_header_layout.addLayout(win_box)
        container_layout.addWidget(top_header_frame)

        # ─── Main Content Workspace ───
        content_widget = QtWidgets.QWidget()
        container_layout.addWidget(content_widget, stretch=1)

        root = QtWidgets.QHBoxLayout(content_widget)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        # ---- Left: Group filter bar + accounts table + Add Account / Remove
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)

        # Group Selector & Selection Counter Bar
        group_bar = QtWidgets.QHBoxLayout()
        group_bar.setSpacing(6)
        group_bar.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(110)
        self.group_combo.setFixedHeight(28)
        self.group_combo.currentTextChanged.connect(self._on_group_filter_changed)
        self.group_combo.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.group_combo.customContextMenuRequested.connect(self._open_group_combo_menu)
        group_bar.addWidget(self.group_combo, stretch=1)

        self.btn_manage_groups = QtWidgets.QPushButton("Manage Groups")
        self.btn_manage_groups.setToolTip("Manage & reorder groups (drag & drop, rename, delete)")
        self.btn_manage_groups.setFixedHeight(28)
        self.btn_manage_groups.clicked.connect(self._open_group_manager)
        group_bar.addWidget(self.btn_manage_groups)

        self.selection_badge = QtWidgets.QLabel("0 selected")
        self.selection_badge.setFixedHeight(28)
        self.selection_badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.selection_badge.setStyleSheet(
            "color: #707070; font-size: 10px; background-color: #000000; "
            "border: 1px solid #353535; padding: 0px 8px; border-radius: 0px; font-weight: bold;"
        )
        group_bar.addWidget(self.selection_badge)

        left.addLayout(group_bar)

        self.table = ReorderableTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(self.base_headers)
        self.table.setIconSize(QtCore.QSize(12, 12))
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hdr = self.table.horizontalHeader()
        hdr.setSectionsClickable(True)
        hdr.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        hdr.sectionClicked.connect(self._on_header_section_clicked)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 80)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(lambda r, c: self._join_server_clicked())
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._open_context_menu)
        self.table.rows_reordered.connect(self._on_table_rows_reordered)
        self.table.enter_pressed.connect(self._join_server_clicked)
        self.table.delete_pressed.connect(self._remove_selected)
        left.addWidget(self.table, stretch=1)

        left_bar = QtWidgets.QHBoxLayout()
        left_bar.setSpacing(6)
        left_bar.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.btn_add = QtWidgets.QPushButton("Add Account")
        self.btn_add.setFixedHeight(28)
        self.btn_add.setProperty("hasMenu", True)
        self.btn_add.clicked.connect(lambda: self._open_web_login(force_engine="internal"))
        add_menu = QtWidgets.QMenu(self.btn_add)
        add_menu.addAction("Log In via Web Browser...", lambda: self._open_web_login(force_engine="internal"))
        add_menu.addSeparator()
        add_menu.addAction("Add via Cookie Token...", self._open_add_account)
        add_menu.addSeparator()
        add_menu.addAction("Import Windows RAM / .aeram File... (Ctrl+I)", self._open_import_ram)
        self.btn_add.setMenu(add_menu)
        left_bar.addWidget(self.btn_add)

        self.btn_open_browser = QtWidgets.QPushButton("Open Browser")
        self.btn_open_browser.setFixedHeight(28)
        self.btn_open_browser.setProperty("hasMenu", True)
        self.btn_open_browser.setToolTip("Open selected account session in embedded browser")
        self.btn_open_browser.clicked.connect(lambda: self._open_account_in_browser(destination="home", force_engine="internal"))
        browser_menu = QtWidgets.QMenu(self.btn_open_browser)
        browser_menu.addAction("Home", lambda: self._open_account_in_browser(destination="home", force_engine="internal"))
        browser_menu.addAction("Profile", lambda: self._open_account_in_browser(destination="profile", force_engine="internal"))
        browser_menu.addAction("Avatar Editor", lambda: self._open_account_in_browser(destination="avatar", force_engine="internal"))
        browser_menu.addAction("Inventory", lambda: self._open_account_in_browser(destination="inventory", force_engine="internal"))
        browser_menu.addSeparator()
        browser_menu.addAction("Account Settings", lambda: self._open_account_in_browser(destination="settings", force_engine="internal"))
        browser_menu.addAction("Security && Sessions", lambda: self._open_account_in_browser(destination="security", force_engine="internal"))
        browser_menu.addAction("Transactions", lambda: self._open_account_in_browser(destination="transactions", force_engine="internal"))
        browser_menu.addSeparator()
        browser_menu.addAction("Custom URL...", lambda: self._open_account_in_browser(destination="custom", force_engine="internal"))
        self.btn_open_browser.setMenu(browser_menu)
        left_bar.addWidget(self.btn_open_browser)
        left_bar.addStretch()
        left.addLayout(left_bar)

        root.addLayout(left, stretch=64)

        # ---- Right: Functional Modular Decks (Scrollable to prevent collision on smaller displays)
        right_scroll = QtWidgets.QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        right_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        right_widget = QtWidgets.QWidget()
        right_widget.setObjectName("rightDeckWidget")
        right_widget.setStyleSheet("QWidget#rightDeckWidget { background: transparent; }")
        right = QtWidgets.QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 4, 0)
        right.setSpacing(4)

        # ── 1. Target Experience & Launching ──
        sec_target = QtWidgets.QLabel("// TARGET EXPERIENCE & LAUNCH")
        sec_target.setStyleSheet("color: #707070; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        right.addWidget(sec_target)

        ids = QtWidgets.QHBoxLayout()
        ids.setSpacing(6)
        self.place_id_input = QtWidgets.QLineEdit()
        self.place_id_input.setPlaceholderText("Place ID")
        self.place_id_input.setMinimumWidth(60)
        self.place_id_input.setFixedHeight(28)
        self.place_id_input.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(r"\d*")))
        saved_place = self.storage.get_setting("SavedPlaceId", "")
        if saved_place:
            self.place_id_input.setText(saved_place)
        self.place_id_input.textChanged.connect(
            lambda text: self.storage.set_setting("SavedPlaceId", text.strip())
        )
        ids.addWidget(self.place_id_input, stretch=1)

        self.job_id_input = QtWidgets.QLineEdit()
        self.job_id_input.setPlaceholderText("Job ID")
        self.job_id_input.setMinimumWidth(60)
        self.job_id_input.setFixedHeight(28)
        saved_job = self.storage.get_setting("SavedJobId", "")
        if saved_job:
            self.job_id_input.setText(saved_job)
        self.job_id_input.textChanged.connect(
            lambda text: self.storage.set_setting("SavedJobId", text.strip())
        )
        ids.addWidget(self.job_id_input, stretch=1)

        self.btn_bookmarks = QtWidgets.QPushButton("Bookmarks")
        self.btn_bookmarks.setObjectName("btn_bookmarks")
        self.btn_bookmarks.setToolTip("Saved Place & Server Bookmarks")
        self.btn_bookmarks.setFixedSize(76, 28)
        self.btn_bookmarks.clicked.connect(self._open_bookmarks_menu)
        ids.addWidget(self.btn_bookmarks)
        right.addLayout(ids)

        self.btn_join_server = QtWidgets.QPushButton("Join Server")
        self.btn_join_server.setObjectName("primaryBtn")
        self.btn_join_server.setFixedHeight(28)
        self.btn_join_server.clicked.connect(self._join_server_clicked)
        right.addWidget(self.btn_join_server)

        # Follow launch row
        follow_row = QtWidgets.QHBoxLayout()
        follow_row.setSpacing(6)
        follow_row.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.username_input = QtWidgets.QLineEdit()
        self.username_input.setPlaceholderText("Username to follow...")
        self.username_input.setMinimumWidth(80)
        self.username_input.setFixedHeight(28)
        saved_follow = self.storage.get_setting("SavedFollowUser", "")
        if saved_follow:
            self.username_input.setText(saved_follow)
        self.username_input.textChanged.connect(
            lambda text: self.storage.set_setting("SavedFollowUser", text.strip())
        )
        follow_row.addWidget(self.username_input, stretch=1)
        self.btn_follow = QtWidgets.QPushButton("Follow")
        self.btn_follow.setFixedSize(76, 28)
        self.btn_follow.setToolTip("Follow player into their active Roblox game")
        self.btn_follow.clicked.connect(self._follow_clicked)
        follow_row.addWidget(self.btn_follow)
        right.addLayout(follow_row)

        # ── 2. Active Account Details ──
        sec_acc = QtWidgets.QLabel("// ACTIVE ACCOUNT")
        sec_acc.setStyleSheet("color: #707070; font-size: 9px; font-weight: 700; letter-spacing: 0.8px; margin-top: 2px;")
        right.addWidget(sec_acc)

        alias_row = QtWidgets.QHBoxLayout()
        alias_row.setSpacing(6)
        self.alias_input = QtWidgets.QLineEdit()
        self.alias_input.setPlaceholderText("Alias...")
        self.alias_input.setMinimumWidth(80)
        self.alias_input.setFixedHeight(28)
        self.alias_input.returnPressed.connect(self._set_alias_clicked)
        alias_row.addWidget(self.alias_input, stretch=1)

        self.btn_replicate_alias = QtWidgets.QPushButton("Sync")
        self.btn_replicate_alias.setObjectName("btn_replicate_alias")
        self.btn_replicate_alias.setToolTip("Replicate Roblox Name to Alias (Right-click for options)")
        self.btn_replicate_alias.setFixedSize(50, 28)
        self.btn_replicate_alias.clicked.connect(self._replicate_alias_clicked)
        self.btn_replicate_alias.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_replicate_alias.customContextMenuRequested.connect(self._open_replicate_menu)
        alias_row.addWidget(self.btn_replicate_alias)

        self.btn_set_alias = QtWidgets.QPushButton("Set Alias")
        self.btn_set_alias.setFixedSize(76, 28)
        self.btn_set_alias.clicked.connect(self._set_alias_clicked)
        alias_row.addWidget(self.btn_set_alias)
        right.addLayout(alias_row)

        desc_box = QtWidgets.QVBoxLayout()
        desc_box.setSpacing(4)
        self.description_input = QtWidgets.QPlainTextEdit()
        self.description_input.setPlaceholderText("Notes & Description...")
        self.description_input.setMinimumHeight(88)
        self.description_input.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.description_input.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        desc_box.addWidget(self.description_input, stretch=1)

        desc_btn_row = QtWidgets.QHBoxLayout()
        desc_btn_row.setSpacing(6)
        desc_btn_row.addStretch()
        self.btn_set_desc = QtWidgets.QPushButton("Set Description")
        self.btn_set_desc.setFixedSize(132, 26)
        self.btn_set_desc.clicked.connect(self._set_description_clicked)
        desc_btn_row.addWidget(self.btn_set_desc)
        desc_box.addLayout(desc_btn_row)
        right.addLayout(desc_box, stretch=1)

        # ── 3. Controls & Utilities (Functional 2x2 Grid) ──
        sec_ctrl = QtWidgets.QLabel("// CONTROLS & UTILITIES")
        sec_ctrl.setStyleSheet("color: #707070; font-size: 9px; font-weight: 700; letter-spacing: 0.8px; margin-top: 2px;")
        right.addWidget(sec_ctrl)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # Row 0: Account Management & Server Finder
        self.btn_acc_tools = QtWidgets.QPushButton("Account Tools")
        self.btn_acc_tools.setMinimumWidth(90)
        self.btn_acc_tools.setFixedHeight(28)
        self.btn_acc_tools.setProperty("hasMenu", True)
        self.btn_acc_tools.setToolTip("Instances, Anti-AFK, Self-Healing, session checks, and maintenance")
        tools_menu = QtWidgets.QMenu(self.btn_acc_tools)
        tools_menu.aboutToShow.connect(self._update_acc_tools_menu)
        self.btn_acc_tools.setMenu(tools_menu)
        self._update_acc_tools_menu()
        self.btn_acc_control = self.btn_acc_tools
        self.btn_acc_utils = self.btn_acc_tools
        grid.addWidget(self.btn_acc_tools, 0, 0)

        self.btn_server_finder = QtWidgets.QPushButton("Server Monitor")
        self.btn_server_finder.setMinimumWidth(90)
        self.btn_server_finder.setFixedHeight(28)
        self.btn_server_finder.setToolTip("Monitor live servers, track populations/pings, and inspect sub-places (Ctrl+S)")
        self.btn_server_finder.clicked.connect(self._open_server_finder)
        grid.addWidget(self.btn_server_finder, 0, 1)

        # Row 1: Client Settings & System Diagnostics
        self.btn_settings = QtWidgets.QPushButton("Settings")
        self.btn_settings.setMinimumWidth(90)
        self.btn_settings.setFixedHeight(28)
        self.btn_settings.setToolTip("Configure application preferences, framerates, memory limits, and launch options")
        self.btn_settings.clicked.connect(self._open_settings)
        grid.addWidget(self.btn_settings, 1, 0)

        self.btn_diagnostics = QtWidgets.QPushButton("System Debug")
        self.btn_diagnostics.setMinimumWidth(90)
        self.btn_diagnostics.setFixedHeight(28)
        self.btn_diagnostics.setToolTip("Inspect system diagnostics, Mocktail status, graphics drivers, and kernel cgroups (Ctrl+D)")
        self.btn_diagnostics.clicked.connect(self._open_diagnostics)
        grid.addWidget(self.btn_diagnostics, 1, 1)

        right.addLayout(grid)
        right_scroll.setWidget(right_widget)
        root.addWidget(right_scroll, stretch=36)

        # Shortcuts
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+N"), self, self._open_add_account)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+I"), self, self._open_import_ram)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+S"), self, self._open_server_finder)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+M"), self, self._open_server_finder)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+D"), self, self._open_diagnostics)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+U"), self, self._open_updater)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+T"), self, self._open_live_2fa_hub)
        QtGui.QShortcut(QtGui.QKeySequence("F5"), self, self._refresh_accounts)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Up"), self, self._move_selected_rows_up)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Down"), self, self._move_selected_rows_down)

        self.btn_cancel_queue = QtWidgets.QPushButton("Cancel Queue")
        self.btn_cancel_queue.setStyleSheet("background-color: #000000; color: #E8E8E8; border: 1px solid #606060; padding: 2px 8px; font-size: 10px; font-weight: bold; border-radius: 0px;")
        self.btn_cancel_queue.clicked.connect(self._cancel_launch_queue)
        self.btn_cancel_queue.setVisible(False)
        self.statusBar().addPermanentWidget(self.btn_cancel_queue)

        self._status_metric_index = 0
        self.count_label = QtWidgets.QLabel()
        self.count_label.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.count_label.mousePressEvent = lambda e: self._on_metrics_clicked(e)
        self.statusBar().addPermanentWidget(self.count_label)
        self._update_status_bar()

        # Dynamic cycling timer: rotates the active metric every 4.5 seconds
        self._metric_cycle_timer = QtCore.QTimer(self)
        self._metric_cycle_timer.timeout.connect(self._cycle_status_metric)
        self._metric_cycle_timer.start(4500)

    # ------------------------------------------------------------------ data & filtering

    def _update_group_combo_items(self):
        prev = self.selected_group
        self.group_combo.blockSignals(True)
        self.group_combo.clear()

        # Count per group
        from collections import Counter
        counts = Counter(a.group for a in self.accounts)

        self.group_combo.addItem(f"All Groups ({len(self.accounts)})")
        all_groups = self.storage.list_groups()
        for g in all_groups:
            self.group_combo.addItem(f"{g} ({counts.get(g, 0)})")

        # Restore selection
        idx_to_select = 0
        if prev and prev != "All Groups":
            for i in range(1, self.group_combo.count()):
                if self.group_combo.itemText(i).startswith(f"{prev} ("):
                    idx_to_select = i
                    break
        self.group_combo.setCurrentIndex(idx_to_select)
        self.group_combo.blockSignals(False)

    def _on_group_filter_changed(self, text: str):
        if not text:
            return
        if text.startswith("All Groups"):
            self.selected_group = "All Groups"
        else:
            self.selected_group = text.split(" (")[0].strip()
        self._populate_table()

    def _load_accounts(self, validate_auth: bool = True):
        self.accounts = self.storage.list_accounts()
        self.account_map = {a.account_id: a for a in self.accounts}
        self._update_group_combo_items()
        self._populate_table()
        if validate_auth:
            self._start_background_auth_validation()

    def _start_background_auth_validation(self, specific_accounts: Optional[List[RobloxAccount]] = None):
        """
        Launches non-blocking background worker to validate session cookies.
        Updates AccountAuthStateManager and GUI seamlessly without freezing UI.
        """
        targets = specific_accounts if specific_accounts is not None else self.accounts
        if not targets:
            return

        # Prepare payload: (account_id, cookie)
        # Avoid checking accounts that are actively RUNNING
        acc_payload = []
        for acc in targets:
            if not self.runner.is_instance_running(acc.account_id):
                acc_payload.append((acc.account_id, acc.cookie))

        if not acc_payload:
            return

        # Stop existing background worker if active
        if getattr(self, "_auth_validator_worker", None) and self._auth_validator_worker.isRunning():
            self._auth_validator_worker.stop()

        worker = AuthValidatorWorker(acc_payload, self.api)
        worker.account_checked.connect(self._on_background_auth_checked)
        self._auth_validator_worker = worker
        worker.start()

    def _on_background_auth_checked(self, account_id: str, is_valid: bool, status_reason: str):
        if not hasattr(self.runner, "auth_state_manager"):
            return
        current_state = self.runner.auth_state_manager.get_state(account_id)
        # Never override actively running instances or manual captcha challenges
        if current_state in (AccountAuthState.RUNNING, AccountAuthState.AUTHENTICATING, AccountAuthState.LAUNCHING, AccountAuthState.VERIFICATION_REQUIRED):
            return

        if is_valid:
            if current_state != AccountAuthState.READY:
                self.runner.auth_state_manager.mark_ready(account_id)
                self._poll_instances()
        elif status_reason == "EXPIRED":
            if current_state != AccountAuthState.SESSION_EXPIRED:
                self.runner.auth_state_manager.mark_session_expired(account_id)
                self._poll_instances()
        elif status_reason == "EMPTY_COOKIE":
            if current_state != AccountAuthState.SESSION_EXPIRED:
                self.runner.auth_state_manager.mark_session_expired(account_id, reason="No cookie provided")
                self._poll_instances()

    def _start_presence_monitor(self):
        if getattr(self, "_presence_worker", None) and self._presence_worker.isRunning():
            return
        self._presence_worker = PresenceMonitorWorker(
            api=self.api,
            accounts_getter=lambda: list(self.accounts),
            interval_sec=12,
            parent=self,
        )
        self._presence_worker.presences_updated.connect(self._on_presences_updated)
        self._presence_worker.start()

    def _on_presences_updated(self, presences: List[Dict[str, Any]]):
        changed = False
        for p in presences:
            uid = p.get("userId")
            if uid:
                old = self._presence_map.get(uid)
                if (
                    not old
                    or old.get("userPresenceType") != p.get("userPresenceType")
                    or old.get("lastLocation") != p.get("lastLocation")
                    or old.get("gameId") != p.get("gameId")
                ):
                    changed = True
                self._presence_map[uid] = p
        if changed:
            self._poll_instances()

    def _refresh_presence(self):
        if getattr(self, "_presence_worker", None):
            self._presence_worker.trigger_immediate_refresh()
            self.statusBar().showMessage("Refreshed web presence check", 2500)

    def _quick_join_presence(self, place_id: int, job_id: Optional[str] = None):
        if place_id:
            self.place_id_input.setText(str(place_id))
        if job_id:
            self.job_id_input.setText(str(job_id))
        self._join_server_clicked()

    def _populate_table(self):
        if self.selected_group == "All Groups":
            self.visible_accounts = list(self.accounts)
        else:
            self.visible_accounts = [a for a in self.accounts if a.group == self.selected_group]

        self._row_status_snapshot.clear()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.visible_accounts))
        for row, acc in enumerate(self.visible_accounts):
            self._render_row(row, acc)
        self.table.blockSignals(False)

        if self.visible_accounts:
            target = self.current_account.account_id if self.current_account else None
            row = 0
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if item and item.data(QtCore.Qt.ItemDataRole.UserRole) == target:
                    row = r
                    break
            self.table.selectRow(row)
            self._select_account(self.visible_accounts[row])
        else:
            self.current_account = None
            self.username_input.clear()
            self.alias_input.clear()
            self.description_input.clear()

        self._on_selection_changed()
        self._update_status_bar()

    def _render_row(self, row: int, acc: RobloxAccount):
        running = self.runner.is_instance_running(acc.account_id)
        inst = self.runner.get_running_instance(acc.account_id) if running else None
        auth_info = self.runner.auth_state_manager.get_state_info(acc.account_id) if hasattr(self.runner, "auth_state_manager") else None
        auth_state = auth_info.state if auth_info else AccountAuthState.STORED

        # Live Web Presence from Roblox Presence API (matching RAM)
        pres = self._presence_map.get(acc.user_id) if (hasattr(self, "_presence_map") and acc.user_id) else None
        pres_type = pres.get("userPresenceType", 0) if pres else 0
        pres_location = (pres.get("lastLocation") or "").strip() if pres else ""
        pres_place_id = pres.get("placeId") if pres else None
        pres_game_id = pres.get("gameId") if pres else None
        pres_last_online = pres.get("lastOnline") if pres else None
        pid = inst.pid if inst else None
        self._row_status_snapshot[acc.account_id] = (running, pid, auth_state, pres_type, pres_place_id, pres_game_id)

        text = acc.username
        target_bold = False
        target_color0 = QtGui.QColor("#E8E8E8")
        status_led_color = "#505050"
        tip0 = "Ready • Offline"

        if running:
            target_bold = True
            target_color0 = QtGui.QColor("#FFFFFF")
            status_led_color = "#E8E8E8"
            pid_str = f"PID {inst.pid}" if (inst and inst.pid) else "Running"
            loc_str = f" • {pres_location}" if pres_location else (f" • Place {inst.place_id}" if (inst and inst.place_id) else "")
            tip0 = f"In-Game (Local) • {pid_str}{loc_str} • {acc.username}"
            if pres_game_id:
                tip0 += f"\nServer Job ID: {pres_game_id}"
        elif auth_state == AccountAuthState.VERIFICATION_REQUIRED:
            status_led_color = "#A8A8A8"
            ch_name = (auth_info.challenge_type or 'Arkose CAPTCHA') if auth_info else 'Arkose CAPTCHA'
            tip0 = f"Security verification required ({ch_name}). Right-click -> Account Utilities -> Verify in Browser."
        elif auth_state == AccountAuthState.SESSION_EXPIRED:
            status_led_color = "#707070"
            tip0 = "Session cookie expired (HTTP 401). Right-click -> Account Utilities -> Edit Account to update .ROBLOSECURITY."
        elif auth_state == AccountAuthState.AUTHENTICATING:
            target_bold = True
            status_led_color = "#A8A8A8"
            tip0 = "Authenticating session ticket..."
        elif auth_state == AccountAuthState.LAUNCHING:
            target_bold = True
            status_led_color = "#A8A8A8"
            tip0 = "Launching Roblox client..."
        elif auth_state == AccountAuthState.ERROR:
            status_led_color = "#707070"
            tip0 = f"Error: {auth_info.error_message if auth_info else 'Unknown'}"
        elif pres_type == 2:  # In-Game on Roblox Web/App
            target_bold = True
            target_color0 = QtGui.QColor("#FFFFFF")
            status_led_color = "#E8E8E8"
            game_name = pres_location or "Roblox Experience"
            place_str = f" (Place: {pres_place_id})" if pres_place_id else ""
            tip0 = f"In-Game • {game_name}{place_str} • {acc.username}"
            if pres_game_id:
                tip0 += f"\nServer Job ID: {pres_game_id}"
        elif pres_type == 1:  # Online on Web / App
            status_led_color = "#A8A8A8"
            tip0 = f"Online • Active on Roblox (Website / App) • {acc.username}"
        elif pres_type == 3:  # In Studio
            status_led_color = "#A8A8A8"
            studio_name = pres_location or "Roblox Studio"
            tip0 = f"In Studio • {studio_name} • {acc.username}"
        else:
            status_led_color = "#505050"
            last_seen = _format_last_online(pres_last_online)
            tip0 = f"Offline • Last Online: {last_seen}" if last_seen else "Ready • Offline"

        led_icon = get_led_icon(status_led_color)

        # Col 0: Username
        u = self.table.item(row, 0)
        if not u:
            u = QtWidgets.QTableWidgetItem(text)
            u.setData(QtCore.Qt.ItemDataRole.UserRole, acc.account_id)
            u.setData(QtCore.Qt.ItemDataRole.UserRole + 1, status_led_color)
            u.setIcon(led_icon)
            if target_bold:
                f = u.font()
                f.setBold(True)
                u.setFont(f)
            u.setForeground(QtGui.QBrush(target_color0))
            if tip0:
                u.setToolTip(tip0)
            self.table.setItem(row, 0, u)
        else:
            if u.text() != text:
                u.setText(text)
            if u.data(QtCore.Qt.ItemDataRole.UserRole) != acc.account_id:
                u.setData(QtCore.Qt.ItemDataRole.UserRole, acc.account_id)
            if u.data(QtCore.Qt.ItemDataRole.UserRole + 1) != status_led_color:
                u.setIcon(led_icon)
                u.setData(QtCore.Qt.ItemDataRole.UserRole + 1, status_led_color)
            f0 = u.font()
            if f0.bold() != target_bold:
                f0.setBold(target_bold)
                u.setFont(f0)
            if u.foreground().color() != target_color0:
                u.setForeground(QtGui.QBrush(target_color0))
            if u.toolTip() != tip0:
                u.setToolTip(tip0)


        # Col 1: Display Name
        disp = acc.display_name or ""
        item1 = self.table.item(row, 1)
        if not item1:
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(disp))
        elif item1.text() != disp:
            item1.setText(disp)

        # Col 2: Group
        grp = acc.group or "Default"
        item2 = self.table.item(row, 2)
        if not item2:
            item2 = QtWidgets.QTableWidgetItem(grp)
            item2.setForeground(QtGui.QBrush(QtGui.QColor("#A8A8A8")))
            self.table.setItem(row, 2, item2)
        elif item2.text() != grp:
            item2.setText(grp)

        # Col 3: Mode / FPS
        is_main = self.storage.is_main_account(acc)
        target_fps = self.storage.get_account_target_fps(acc)
        fps_label = "Unlimited" if (target_fps >= 1000 or target_fps <= 0) else f"{target_fps} FPS"
        tag = "H" if is_main else "L"
        mode_text = f"{tag} • {fps_label}"
        tip_text = f"{'High (H)' if is_main else 'Low (L)'} — {fps_label}"

        if is_main:
            target_color3 = QtGui.QColor("#FFFFFF")
        else:
            target_color3 = QtGui.QColor("#FFFFFF") if running else QtGui.QColor("#A8A8A8")

        item3 = self.table.item(row, 3)
        if not item3:
            item3 = QtWidgets.QTableWidgetItem(mode_text)
            item3.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            item3.setToolTip(tip_text)
            item3.setForeground(QtGui.QBrush(target_color3))
            if running:
                f = item3.font()
                f.setBold(True)
                item3.setFont(f)
            self.table.setItem(row, 3, item3)
        else:
            if item3.text() != mode_text:
                item3.setText(mode_text)
            if item3.toolTip() != tip_text:
                item3.setToolTip(tip_text)
            f3 = item3.font()
            if f3.bold() != target_bold:
                f3.setBold(target_bold)
                item3.setFont(f3)
            if item3.foreground().color() != target_color3:
                item3.setForeground(QtGui.QBrush(target_color3))

        # Col 4: Notes
        notes_text = (acc.notes or "").replace("\n", " ")
        item4 = self.table.item(row, 4)
        if not item4:
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(notes_text))
        elif item4.text() != notes_text:
            item4.setText(notes_text)

    def _poll_instances(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                aid = item.data(QtCore.Qt.ItemDataRole.UserRole)
                acc = self.account_map.get(aid)
                if acc:
                    running = self.runner.is_instance_running(acc.account_id)
                    inst = self.runner.get_running_instance(acc.account_id) if running else None
                    auth_info = self.runner.auth_state_manager.get_state_info(acc.account_id) if hasattr(self.runner, "auth_state_manager") else None
                    auth_state = auth_info.state if auth_info else AccountAuthState.STORED
                    pres = self._presence_map.get(acc.user_id) if (hasattr(self, "_presence_map") and acc.user_id) else None
                    pres_type = pres.get("userPresenceType", 0) if pres else 0
                    pres_place_id = pres.get("placeId") if pres else None
                    pres_game_id = pres.get("gameId") if pres else None
                    pid = inst.pid if inst else None

                    snapshot = (running, pid, auth_state, pres_type, pres_place_id, pres_game_id)
                    if snapshot != self._row_status_snapshot.get(acc.account_id):
                        self._render_row(r, acc)
        self.table.blockSignals(False)
        self._update_status_bar()

        # Check self-healing watchdog every ~12 seconds
        self._heal_tick_counter += 1
        if self._heal_tick_counter >= 8:
            self._heal_tick_counter = 0
            if getattr(self, "self_heal_engine", None) and self.self_heal_engine.running:
                try:
                    healed = self.self_heal_engine.check_and_heal()
                    if healed > 0:
                        self.statusBar().showMessage(f"Self-Heal: Reconnected {healed} account(s)", 5000)
                except Exception:
                    pass

    def _update_status_bar(self):
        now = time.time()
        mocktail_check_interval = 60.0 if getattr(self, "_mocktail_detected_ok", False) else 10.0
        if not hasattr(self, "_cached_mocktail_status") or (now - getattr(self, "_last_mocktail_check", 0) > mocktail_check_interval):
            try:
                from core.mocktail_manager import MocktailManager
                info = MocktailManager().detect()
                if info.is_installed:
                    self._cached_mocktail_status = f"Mocktail: OK ({info.version_name or 'Ready'})"
                    self._mocktail_detected_ok = True
                else:
                    self._cached_mocktail_status = "Mocktail: NOT FOUND"
                    self._mocktail_detected_ok = False
            except Exception:
                is_mock = bool(shutil.which("mocktail"))
                self._cached_mocktail_status = "Mocktail: OK" if is_mock else "Mocktail: NOT FOUND"
                self._mocktail_detected_ok = is_mock
            self._last_mocktail_check = now
        runtime = self._cached_mocktail_status
        stats = self.runner.get_memory_stats()
        avail_gb = stats["mem_avail_mb"] / 1024.0
        swap_gb = stats["swap_free_mb"] / 1024.0

        v_used, v_tot = 0.0, 0.0
        if not hasattr(self, "_vram_path"):
            self._vram_path = None
            self._has_nvidia_smi = (shutil.which("nvidia-smi") is not None)
            self._last_nvidia_check = 0.0
            self._cached_nvidia_vram = (0.0, 0.0)
            try:
                for card in sorted(Path("/sys/class/drm").glob("card*/device/mem_info_vram_used")):
                    total_p = card.parent / "mem_info_vram_total"
                    if total_p.exists():
                        self._vram_path = (card, total_p)
                        break
            except Exception:
                pass

        if self._vram_path:
            try:
                v_used = int(self._vram_path[0].read_text()) / (1024**3)
                v_tot = int(self._vram_path[1].read_text()) / (1024**3)
            except Exception:
                pass
        elif getattr(self, "_has_nvidia_smi", False):
            # Universal fallback for Nvidia proprietary drivers (cached every 4s)
            now_t = time.time()
            if now_t - getattr(self, "_last_nvidia_check", 0.0) > 4.0:
                self._last_nvidia_check = now_t
                try:
                    res = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=1.0
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        first_line = res.stdout.strip().split("\n")[0]
                        parts = first_line.split(",")
                        if len(parts) >= 2:
                            self._cached_nvidia_vram = (float(parts[0].strip()) / 1024.0, float(parts[1].strip()) / 1024.0)
                except Exception:
                    pass
            v_used, v_tot = getattr(self, "_cached_nvidia_vram", (0.0, 0.0))

        total_cnt = len(self.accounts)
        active_cnt = len(self.runner.active_instances)

        # Dynamic cycling metrics (rotates every 4.5s on timer, or on event update/click)
        metric_mode = getattr(self, "_status_metric_index", 0) % 2

        if metric_mode == 0:
            # Slide 0: RAM, Virtual RAM (Swap), and VRAM
            if v_tot > 0:
                visible_text = f"RAM: {avail_gb:.1f}G  •  Swap: {swap_gb:.1f}G  •  VRAM: {v_used:.1f}/{v_tot:.0f}G  •  {active_cnt}/{total_cnt} Active"
            else:
                visible_text = f"RAM: {avail_gb:.1f}G  •  Swap: {swap_gb:.1f}G  •  {active_cnt}/{total_cnt} Active"
            self.count_label.setStyleSheet("color: #888888; font-size: 10px; padding-right: 6px;")
        else:
            # Slide 1: Mocktail runtime & instances
            if "NOT FOUND" in runtime:
                visible_text = f"[Mocktail Missing]  •  {active_cnt}/{total_cnt} Active"
                self.count_label.setStyleSheet("color: #FFFFFF; font-size: 10px; font-weight: bold; padding-right: 6px;")
            else:
                visible_text = f"{runtime}  •  {active_cnt}/{total_cnt} Active"
                self.count_label.setStyleSheet("color: #A8A8A8; font-size: 10px; padding-right: 6px;")

        self.count_label.setText(visible_text)

        tip_lines = [
            f"Runtime: {runtime}",
            f"Physical RAM: {avail_gb:.1f} GB Free",
            f"Virtual Memory (Swap): {swap_gb:.1f} GB Free",
        ]
        if v_tot > 0:
            tip_lines.append(f"VRAM: {v_used:.1f} / {v_tot:.0f} GB")
        tip_lines.append(f"Accounts: {total_cnt} Total ({active_cnt} Active/Running)")
        tip_lines.append("")
        tip_lines.append("(Click to cycle displayed metrics)")
        self.count_label.setToolTip("\n".join(tip_lines))

        now = time.time()
        if avail_gb < 1.5 and (now - getattr(self, "_last_low_ram_notif", 0)) > 180:
            self._last_low_ram_notif = now
            if shutil.which("notify-send"):
                import threading
                msg = f"Low physical RAM ({avail_gb:.1f} GB free). Alts paging to swap."
                threading.Thread(
                    target=lambda: subprocess.run(
                        ["notify-send", "-a", "AERAM", "-u", "normal", "AERAM Memory", msg],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    ),
                    daemon=True,
                ).start()

    def _cycle_status_metric(self):
        self._status_metric_index = (getattr(self, "_status_metric_index", 0) + 1) % 3
        self._update_status_bar()

    def _on_metrics_clicked(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._cycle_status_metric()
            if hasattr(self, "_metric_cycle_timer"):
                self._metric_cycle_timer.start(4500)

    # ------------------------------------------------------------------ selection / editing

    def _selected_accounts(self) -> List[RobloxAccount]:
        out = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            acc = self.account_map.get(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else None
            if acc:
                out.append(acc)
        return out

    def _on_selection_changed(self):
        sel = self._selected_accounts()
        sel_count = len(sel)

        if sel_count > 0:
            self._select_account(sel[0])
            self.selection_badge.setText(f"{sel_count} selected")
            self.selection_badge.setStyleSheet(
                "color: #000000; font-size: 10px; background-color: #E8E8E8; "
                "border: 1px solid #E8E8E8; padding: 2px 8px; border-radius: 0px; font-weight: bold;"
            )
            if sel_count > 1:
                self.btn_join_server.setText(f"Join Server ({sel_count} Accounts)")
            else:
                self.btn_join_server.setText("Join Server")
        else:
            self.selection_badge.setText("0 selected")
            self.selection_badge.setStyleSheet(
                "color: #707070; font-size: 10px; background-color: #000000; "
                "border: 1px solid #353535; padding: 2px 8px; border-radius: 0px; font-weight: bold;"
            )
            self.btn_join_server.setText("Join Server")

        self._update_status_bar()

    def _select_account(self, acc: RobloxAccount):
        self.current_account = acc
        self.alias_input.setText(acc.display_name or "")
        self.description_input.setPlainText(acc.notes or "")
        if acc.cookie:
            from core.browser_helper import sync_mocktail_auth_cookie
            sync_mocktail_auth_cookie(acc.cookie)
            try:
                self.storage.init_profile_sandbox(acc)
            except Exception:
                pass

    def _set_alias_clicked(self):
        if not self.current_account:
            return
        alias = self.alias_input.text().strip()
        self.current_account.display_name = alias
        self.storage.update_alias(self.current_account.account_id, alias)
        self._load_accounts()
        self.statusBar().showMessage(f"Updated alias for @{self.current_account.username}", 3000)

    def _replicate_alias_clicked(self):
        if not self.current_account:
            self.statusBar().showMessage("No account selected to replicate", 3000)
            return
        targets = self._selected_accounts()
        if len(targets) > 1:
            self._replicate_alias_for_targets()
            return
        source = getattr(self.current_account, "web_display_name", "") or self.current_account.username
        self._apply_replicate_alias(source)

    def _apply_replicate_alias(self, name: str):
        if not self.current_account:
            return
        self.alias_input.setText(name)
        self.current_account.display_name = name
        self.storage.update_alias(self.current_account.account_id, name)
        self._load_accounts()
        self.statusBar().showMessage(f"Replicated '{name}' to alias for @{self.current_account.username}", 3000)

    def _open_replicate_menu(self, pos: QtCore.QPoint = None):
        if not self.current_account:
            return
        menu = QtWidgets.QMenu(self)
        acc = self.current_account
        u_name = acc.username
        w_name = getattr(acc, "web_display_name", "")

        act_u = menu.addAction(f"Replicate Username (@{u_name})")
        act_u.triggered.connect(lambda: self._apply_replicate_alias(u_name))

        if w_name and w_name.lower() != u_name.lower():
            act_w = menu.addAction(f"Replicate Web Display Name ({w_name})")
            act_w.triggered.connect(lambda: self._apply_replicate_alias(w_name))

        targets = self._selected_accounts()
        if len(targets) > 1:
            menu.addSeparator()
            act_all = menu.addAction(f"Replicate Usernames to Aliases ({len(targets)} accounts)")
            act_all.triggered.connect(self._replicate_alias_for_targets)

        if pos is not None:
            menu.exec(self.btn_replicate_alias.mapToGlobal(pos))
        else:
            menu.exec(self.btn_replicate_alias.mapToGlobal(QtCore.QPoint(0, self.btn_replicate_alias.height() + 2)))

    def _replicate_alias_for_targets(self):
        targets = self._selected_accounts()
        if not targets:
            return
        for a in targets:
            new_alias = a.username
            a.display_name = new_alias
            self.storage.update_alias(a.account_id, new_alias)
        if self.current_account and self.current_account in targets:
            self.alias_input.setText(self.current_account.display_name)
        self._load_accounts()
        self.statusBar().showMessage(f"Replicated username to alias for {len(targets)} account(s)", 4000)

    def _clear_alias_for_current(self):
        if not self.current_account:
            return
        self.alias_input.clear()
        self.current_account.display_name = ""
        self.storage.update_alias(self.current_account.account_id, "")
        self._load_accounts()
        self.statusBar().showMessage(f"Cleared alias for @{self.current_account.username}", 3000)

    def _prompt_custom_alias(self):
        if not self.current_account:
            return
        curr = self.current_account.display_name or ""
        new_val, ok = BorderlessInputDialog.getText(
            self, "Set Custom Alias", f"Enter custom alias for @{self.current_account.username}:",
            text=curr
        )
        if ok:
            self._apply_replicate_alias(new_val.strip())

    def _set_description_clicked(self):
        if not self.current_account:
            return
        desc = self.description_input.toPlainText().strip()
        self.current_account.notes = desc
        self.storage.update_notes(self.current_account.account_id, desc)
        self._load_accounts()

    # ------------------------------------------------------------------ bookmarks

    def _open_bookmarks_menu(self):
        menu = QtWidgets.QMenu(self)

        act_add = menu.addAction("+ Bookmark Current Place / Job...")
        act_add.triggered.connect(self._bookmark_current_place)

        menu.addSeparator()

        favorites = self.storage.list_favorites()
        if favorites:
            for fav in favorites:
                label = f"{fav.name}  [{fav.place_id}]"
                act_item = menu.addAction(label)
                def _make_loader(p_id=fav.place_id, job=fav.notes):
                    return lambda: self._apply_bookmark(p_id, job)
                act_item.triggered.connect(_make_loader())

            menu.addSeparator()
            del_menu = menu.addMenu("Delete Bookmark")
            for fav in favorites:
                def _make_deleter(p_id=fav.place_id, name=fav.name):
                    return lambda: self._delete_bookmark(p_id, name)
                del_act = del_menu.addAction(f"Delete '{fav.name}'")
                del_act.triggered.connect(_make_deleter())
        else:
            empty_act = menu.addAction("(No bookmarks saved yet)")
            empty_act.setEnabled(False)

        menu.addSeparator()
        act_clear = menu.addAction("Clear Place && Job Fields")
        act_clear.triggered.connect(self._clear_place_and_job)

        menu.exec(self.btn_bookmarks.mapToGlobal(QtCore.QPoint(0, self.btn_bookmarks.height() + 2)))

    def _apply_bookmark(self, place_id: int, job_id: str):
        self.place_id_input.setText(str(place_id))
        self.job_id_input.setText(job_id or "")
        self.statusBar().showMessage(f"Loaded bookmark: Place {place_id}", 3500)

    def _delete_bookmark(self, place_id: int, name: str):
        self.storage.delete_favorite(place_id)
        self.statusBar().showMessage(f"Deleted bookmark '{name}'", 3500)

    def _clear_place_and_job(self):
        self.place_id_input.clear()
        self.job_id_input.clear()

    def _bookmark_current_place(self):
        place_text = self.place_id_input.text().strip()
        if not place_text.isdigit():
            QMessageBox.warning(self, "Invalid Place ID", "Please enter a valid numeric Place ID to bookmark.")
            return
        place_id = int(place_text)
        job_id = self.job_id_input.text().strip()
        name, ok = BorderlessInputDialog.getText(
            self, "New Bookmark", "Enter a name for this bookmark:",
            text=f"Place {place_id}"
        )
        if ok and name.strip():
            fav = FavoriteGame(
                place_id=place_id,
                name=name.strip(),
                notes=job_id,
                created_at=time.time()
            )
            self.storage.save_favorite(fav)
            self.statusBar().showMessage(f"Saved bookmark '{name.strip()}'", 3500)

    # ------------------------------------------------------------------ launching

    def _check_mocktail_readiness(self) -> bool:
        """Verifies Mocktail installation and Roblox client payload readiness before launching."""
        try:
            from core.mocktail_manager import MocktailManager
            info = MocktailManager().detect()
            if not info.is_installed:
                QMessageBox.critical(
                    self,
                    "Mocktail Not Found",
                    "Mocktail Roblox client runtime was not found on your system.\n\n"
                    "AERAM is an account manager; it uses Mocktail to run Roblox on Linux.\n\n"
                    + MocktailManager().get_installation_instructions()
                )
                return False

            if not info.version_name:
                QMessageBox.warning(
                    self,
                    "Roblox Payload Missing",
                    "Mocktail is installed, but the Roblox client payload (APK) has not been downloaded yet.\n\n"
                    "To finish setup:\n"
                    "1. Open a terminal and run:\n"
                    "     mocktail\n"
                    "2. Wait for it to finish downloading the official Roblox client payload.\n"
                    "3. Return to AERAM and click Join Server again!"
                )
            # Proactively ensure companion libraries are mirrored before launch
            if hasattr(self, "storage") and hasattr(self.storage, "ensure_mocktail_companion_libraries"):
                self.storage.ensure_mocktail_companion_libraries()
        except Exception:
            pass
        return True

    def _join_server_clicked(self):
        if not self._check_mocktail_readiness():
            return

        targets = self._selected_accounts()
        if not targets:
            if self.current_account:
                targets = [self.current_account]
            else:
                QMessageBox.information(self, "Join Server", "Please select at least one account in the table.")
                return

        place_text = self.place_id_input.text().strip()
        place_id = int(place_text) if place_text.isdigit() else None
        job_id = (self.job_id_input.text().strip() or None) if place_id else None

        self._start_staggered_queue(targets=targets, place_id=place_id, job_id=job_id)

    def _follow_clicked(self):
        if not self._check_mocktail_readiness():
            return

        target_name = self.username_input.text().strip()
        if not target_name:
            QMessageBox.information(self, "Follow", "Please enter a username to follow.")
            return

        targets = self._selected_accounts()
        if not targets:
            if self.current_account:
                targets = [self.current_account]
            else:
                QMessageBox.information(self, "Follow", "Select an account to launch from first.")
                return

        user_id = self.api.resolve_username_to_user_id(target_name)
        if not user_id:
            QMessageBox.warning(self, "Follow Failed", f"Could not find user '{target_name}' on Roblox.")
            return

        caller_cookie = targets[0].cookie if targets else None
        presence = self.api.get_user_presence(user_id, cookie=caller_cookie)

        target_place_id = None
        in_game = False

        if presence:
            if presence.get("userPresenceType") == 2:
                in_game = True
                # In universe subplaces (e.g. Rogue Lineage Gaia), follow requests must route
                # via rootPlaceId (Lobby) so Roblox matchmaker can issue the signed follow teleport ticket.
                target_place_id = presence.get("rootPlaceId") or presence.get("placeId")

        if not in_game:
            place_text = self.place_id_input.text().strip()
            if place_text.isdigit():
                target_place_id = int(place_text)

            confirm = QMessageBox.question(
                self,
                "Warning",
                f"The user '{target_name}' is not in game or has their joins off.\n\nDo you want to attempt to join anyways?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        self._start_staggered_queue(targets=targets, place_id=target_place_id, follow_user_id=user_id)

    def _start_staggered_queue(
        self,
        targets: List[RobloxAccount],
        place_id: Optional[int] = None,
        job_id: Optional[str] = None,
        follow_user_id: Optional[int] = None,
    ):
        if not targets:
            return
        if len(targets) == 1:
            if follow_user_id:
                try:
                    self.runner.launch_instance(targets[0], place_id=place_id, follow_user_id=follow_user_id)
                except Exception as e:
                    QMessageBox.critical(self, "Launch Error", str(e))
            else:
                self._launch(targets[0], place_id, job_id)
            self._poll_instances()
            return

        if hasattr(self, "_launch_worker") and self._launch_worker and self._launch_worker.isRunning():
            QMessageBox.warning(self, "Queue Active", "A launch queue is already running. Please cancel or wait for it to finish.")
            return

        stagger = float(self.storage.get_setting("stagger_delay", "15.0"))
        self._launch_worker = MultiLaunchWorker(
            runner=self.runner,
            accounts=targets,
            place_id=place_id,
            job_id=job_id,
            follow_user_id=follow_user_id,
            stagger_delay=stagger,
        )
        self.btn_cancel_queue.setVisible(True)

        self._launch_worker.progress.connect(self._on_queue_progress)
        self._launch_worker.countdown_tick.connect(self._on_queue_countdown)
        self._launch_worker.account_launched.connect(lambda acc: self._poll_instances())
        self._launch_worker.finished_queue.connect(self._on_queue_finished)
        self._launch_worker.start()

    def _on_queue_progress(self, current: int, total: int, username: str, msg: str):
        self.statusBar().showMessage(f"[{current}/{total}] {msg}", 0)

    def _on_queue_countdown(self, seconds_left: int, next_username: str):
        self.statusBar().showMessage(f"Staggering @{next_username} ({seconds_left}s)...", 0)

    def _on_queue_finished(self, launched_count: int, failed_list: list):
        self.btn_cancel_queue.setVisible(False)
        self._poll_instances()
        if failed_list:
            fail_str = "\n".join([f"• @{u}: {r}" for u, r in failed_list[:5]])
            self.statusBar().showMessage(f"Queue finished: {launched_count} launched, {len(failed_list)} failed", 6000)
            QMessageBox.warning(
                self,
                "Queue Completed with Notices",
                f"Launched {launched_count} accounts.\n\nNotices / Skipped ({len(failed_list)}):\n{fail_str}",
            )
        else:
            self.statusBar().showMessage(f"Launched {launched_count} account(s)", 5000)

    def _cancel_launch_queue(self):
        if hasattr(self, "_launch_worker") and self._launch_worker and self._launch_worker.isRunning():
            self._launch_worker.cancel()
            self.statusBar().showMessage("Cancelling queue...", 3000)
            self.btn_cancel_queue.setVisible(False)

    def _open_modeless_window(self, key: str, create_fn, on_finished=None, on_accepted=None):
        """
        Opens or activates a modeless secondary window so user interaction
        with the main interface is never blocked.
        """
        if not hasattr(self, "_active_modeless_windows"):
            self._active_modeless_windows = {}

        existing = self._active_modeless_windows.get(key)
        if existing is not None:
            try:
                if not existing.isHidden():
                    existing.raise_()
                    existing.activateWindow()
                    return existing
            except RuntimeError:
                self._active_modeless_windows.pop(key, None)

        dlg = create_fn()
        dlg.setModal(False)
        dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self._active_modeless_windows[key] = dlg

        if on_accepted:
            dlg.accepted.connect(on_accepted)

        def _cleanup(*args):
            self._active_modeless_windows.pop(key, None)
            if on_finished:
                on_finished()

        dlg.finished.connect(_cleanup)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def _open_server_finder(self):
        place_text = self.place_id_input.text().strip()
        initial_pid = int(place_text) if place_text.isdigit() else None
        targets = self._selected_accounts()
        dlg = self._open_modeless_window(
            "server_finder",
            lambda: ServerFinderDialog(
                storage=self.storage,
                api=self.api,
                initial_place_id=initial_pid,
                selected_accounts=targets,
                parent=self,
            ),
        )
        try:
            dlg.server_selected.disconnect(self._on_server_finder_selected)
        except Exception:
            pass
        dlg.server_selected.connect(self._on_server_finder_selected)
        return dlg

    def _on_server_finder_selected(self, place_id: int, job_id: str, auto_join: bool = True):
        self.place_id_input.setText(str(place_id))
        self.job_id_input.setText(job_id)
        if job_id:
            self.statusBar().showMessage(f"Selected: Place {place_id} • Job {job_id[:8]}", 4000)
        else:
            self.statusBar().showMessage(f"Selected: Place {place_id}", 4000)

        if auto_join:
            targets = self._selected_accounts()
            if not targets and self.current_account:
                targets = [self.current_account]
            if targets:
                self._start_staggered_queue(targets=targets, place_id=place_id, job_id=job_id or None)

    def _launch(self, acc: RobloxAccount, place_id: Optional[int] = None, job_id: Optional[str] = None):
        try:
            self.runner.launch_instance(acc, place_id=place_id, job_id=job_id)
            self._poll_instances()
            err = self.runner.get_instance_error(acc.account_id)
            if err:
                msg = f"Account {acc.username} exited immediately:\n\n{err}"
                if any(x in err.lower() for x in ("401", "cookie", "authentication", "unauthorized", "login")):
                    msg += f"\n\n{AUTH_FAIL_MSG}"
                QMessageBox.warning(self, "Launch Failed", msg)
        except Exception as e:
            err_msg = str(e)
            if "verification required" in err_msg.lower() or "captcha" in err_msg.lower():
                reply = QMessageBox.warning(
                    self,
                    "Roblox Verification Required",
                    f"{err_msg}\n\nWould you like to open Roblox in your browser now to complete verification?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._open_browser_verification()
            else:
                QMessageBox.critical(self, "Launch Error", err_msg)
            self._poll_instances()


    def _open_multi_launch(self):
        if not self._check_mocktail_readiness():
            return

        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(self, "Multi-Launch", "Select the accounts to launch in the list first.")
            return
        default_stagger = float(self.storage.get_setting("stagger_delay", "3.0"))
        dlg = MultiLaunchDialog(targets, self.storage.list_favorites(), self, default_delay=default_stagger)
        if dlg.exec():
            try:
                def _progress(idx, total, acc, warning=None):
                    if warning:
                        self.statusBar().showMessage(f"Warning: {warning}", 8000)
                        if shutil.which("notify-send"):
                            try:
                                subprocess.Popen(["notify-send", "-a", "AERAM", "-u", "normal", "AERAM Memory", warning])
                            except Exception:
                                pass
                    else:
                        self.statusBar().showMessage(f"Launching @{acc.username} ({idx + 1}/{total})...", 5000)
                    QtWidgets.QApplication.processEvents()

                self.runner.launch_multi(
                    accounts=targets,
                    place_id=dlg.place_id,
                    job_id=dlg.job_id,
                    stagger_delay=dlg.stagger_delay,
                    on_progress=_progress,
                )
                self._poll_instances()
            except Exception as e:
                QMessageBox.critical(self, "Multi-Launch Error", str(e))

    def _kill_selected(self):
        for acc in self._selected_accounts():
            if getattr(self, "self_heal_engine", None):
                self.self_heal_engine.unregister_account(acc.account_id)
            self.runner.terminate_instance(acc.account_id)
        self._poll_instances()

    def _stop_all_instances(self):
        if getattr(self, "self_heal_engine", None):
            self.self_heal_engine.monitored_account_ids.clear()
            self.self_heal_engine.reconnect_cooldowns.clear()
        count = self.runner.terminate_all_instances()
        self._poll_instances()
        self.statusBar().showMessage(f"Terminated {count} instance(s)", 3000)

    # ------------------------------------------------------------------ context menu / dialogs

    def _open_context_menu(self, pos: QtCore.QPoint):
        if not self.table.itemAt(pos) or not self.current_account:
            return
        targets = self._selected_accounts()
        target_count = len(targets)
        menu = QtWidgets.QMenu(self.table)

        # ── 1. Dynamic Contextual In-Game Quick Join ──
        if target_count == 1 and self.current_account and self.current_account.user_id:
            curr_pres = self._presence_map.get(self.current_account.user_id) if hasattr(self, "_presence_map") else None
            if curr_pres and curr_pres.get("userPresenceType") == 2:
                pres_loc = curr_pres.get("lastLocation") or "Game"
                pres_place = curr_pres.get("placeId") or curr_pres.get("rootPlaceId")
                pres_job = curr_pres.get("gameId")
                if pres_place:
                    menu.addAction(
                        f"Join @{self.current_account.username}'s Game ({pres_loc})",
                        lambda: self._quick_join_presence(pres_place, pres_job),
                    )
                if pres_job:
                    menu.addAction(
                        "Copy Server Job ID",
                        lambda: QtWidgets.QApplication.clipboard().setText(str(pres_job)),
                    )
                menu.addSeparator()

        # Pending Verification alert if applicable
        auth_info = self.runner.auth_state_manager.get_state_info(self.current_account.account_id) if hasattr(self.runner, "auth_state_manager") else None
        if auth_info and auth_info.state == AccountAuthState.VERIFICATION_REQUIRED:
            menu.addAction("Solve Verification in Browser...", lambda: self._open_browser_verification(force_engine="internal"))
            menu.addSeparator()

        # ── 2. Unified Copy & Credentials Submenu ──
        copy_menu = menu.addMenu("Copy")
        copy_menu.addAction("Copy Username", self._copy_selected_username)
        copy_menu.addAction("Copy Password", self._copy_selected_password)
        if target_count == 1 and self.current_account and self.current_account.totp_secret:
            from core.totp import generate_totp, get_totp_remaining_seconds
            try:
                c = generate_totp(self.current_account.totp_secret)
                rem_s = get_totp_remaining_seconds()
                copy_menu.addAction(f"Copy 2FA Code ({c[:3]} {c[3:]} - {rem_s}s)", self._copy_current_2fa_code)
            except Exception:
                copy_menu.addAction("Copy 2FA Code", self._copy_current_2fa_code)
        copy_menu.addSeparator()
        copy_menu.addAction("Copy User ID", self._copy_selected_user_id)
        copy_menu.addAction("Copy Session Cookie (.ROBLOSECURITY)", self._copy_selected_cookie)

        menu.addSeparator()
        move_to_menu = menu.addMenu("Move to Group")
        all_groups = self.storage.list_groups()
        for g in all_groups:
            act = move_to_menu.addAction(g)
            act.triggered.connect(lambda checked, group_name=g: self._move_selected_to_group(group_name))
        move_to_menu.addSeparator()
        act_new_grp = move_to_menu.addAction("+ New Group...")
        act_new_grp.triggered.connect(self._create_and_move_to_new_group)

        # ── 3. Performance & Graphics ──
        profile_menu = menu.addMenu("Profile Mode")
        act_set_main = profile_menu.addAction("Set as High (H)")
        act_set_alt = profile_menu.addAction("Set as Low (L)")
        act_set_main.triggered.connect(lambda: self._set_selected_profile_mode("main"))
        act_set_alt.triggered.connect(lambda: self._set_selected_profile_mode("alt"))

        fps_menu = menu.addMenu("Target Framerate (FPS)")
        act_unlimited = fps_menu.addAction("Unlimited")
        act_unlimited.triggered.connect(lambda: self._set_selected_fps(0))
        fps_menu.addSeparator()
        for fps_val, label in [
            (330, "330 FPS"),
            (240, "240 FPS"),
            (165, "165 FPS"),
            (144, "144 FPS"),
            (120, "120 FPS"),
            (60,  "60 FPS"),
            (6,   "6 FPS"),
        ]:
            act_fps = fps_menu.addAction(label)
            act_fps.triggered.connect(lambda checked, f=fps_val: self._set_selected_fps(f))
        fps_menu.addSeparator()
        act_custom_fps = fps_menu.addAction("Custom FPS...")
        act_custom_fps.triggered.connect(self._prompt_custom_fps)

        menu.addSeparator()
        remove_label = f"Remove Account ({target_count} Accounts) (Del)" if target_count > 1 else "Remove Account (Del)"
        menu.addAction(remove_label, self._remove_selected)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _move_selected_to_group(self, group_name: str):
        targets = self._selected_accounts()
        if not targets:
            return
        acc_ids = [a.account_id for a in targets]
        self.storage.update_accounts_group(acc_ids, group_name)
        for a in targets:
            a.group = group_name
        self._load_accounts()
        self.statusBar().showMessage(f"Moved {len(targets)} account(s) to '{group_name}'", 4000)

    def _create_and_move_to_new_group(self):
        targets = self._selected_accounts()
        if not targets:
            return
        new_group, ok = BorderlessInputDialog.getText(
            self, "New Group", "Enter new group name:"
        )
        if ok and new_group.strip():
            self._move_selected_to_group(new_group.strip())

    def _on_table_rows_reordered(self, source_rows: List[int], target_row: int):
        if not source_rows or not self.visible_accounts:
            return
        moved_accounts = [self.visible_accounts[r] for r in source_rows if r < len(self.visible_accounts)]
        if not moved_accounts:
            return

        moved_ids = {a.account_id for a in moved_accounts}
        remaining = [a for a in self.visible_accounts if a.account_id not in moved_ids]

        adj_target = target_row - sum(1 for r in source_rows if r < target_row)
        adj_target = max(0, min(len(remaining), adj_target))

        for i, acc in enumerate(moved_accounts):
            remaining.insert(adj_target + i, acc)

        self.visible_accounts = remaining

        # Reset column sort indicators when manually reordering
        self._sort_column = None
        self._sort_order = None
        self.table.setHorizontalHeaderLabels(self.base_headers)

        # Update order in self.accounts
        if self.selected_group != "All Groups":
            new_acc_list = []
            vis_iter = iter(self.visible_accounts)
            for a in self.accounts:
                if a.group == self.selected_group:
                    new_acc_list.append(next(vis_iter))
                else:
                    new_acc_list.append(a)
            self.accounts = new_acc_list
        else:
            self.accounts = list(self.visible_accounts)

        # Persist custom sort order to storage
        self.storage.reorder_accounts([a.account_id for a in self.accounts])

        # Re-render table while preserving scroll and selection on moved items
        v_scroll = self.table.verticalScrollBar().value()
        h_scroll = self.table.horizontalScrollBar().value()
        self._populate_table()
        self.table.blockSignals(True)
        self.table.clearSelection()
        selection_model = self.table.selectionModel()
        for r, acc in enumerate(self.visible_accounts):
            if acc.account_id in moved_ids:
                selection_model.select(
                    self.table.model().index(r, 0),
                    QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows
                )
        self.table.verticalScrollBar().setValue(v_scroll)
        self.table.horizontalScrollBar().setValue(h_scroll)
        self.table.blockSignals(False)
        self._on_selection_changed()
        self.statusBar().showMessage(f"Reordered {len(moved_accounts)} account(s)", 2500)

    def _on_header_section_clicked(self, logical_index: int):
        if self._sort_column == logical_index:
            self._sort_order = (
                QtCore.Qt.SortOrder.DescendingOrder
                if self._sort_order == QtCore.Qt.SortOrder.AscendingOrder
                else QtCore.Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column = logical_index
            self._sort_order = QtCore.Qt.SortOrder.AscendingOrder

        reverse = (self._sort_order == QtCore.Qt.SortOrder.DescendingOrder)

        if logical_index == 0:  # Username
            self.accounts.sort(key=lambda a: (a.username or "").lower(), reverse=reverse)
        elif logical_index == 1:  # Alias
            self.accounts.sort(key=lambda a: (getattr(a, 'display_name', '') or a.username or "").lower(), reverse=reverse)
        elif logical_index == 2:  # Group (Sorts groups only, strictly preserving custom account order inside each group)
            groups = self.storage.list_groups()
            group_priority = {g.lower(): i for i, g in enumerate(groups)}
            all_groups = []
            seen = set()
            for a in self.accounts:
                grp = (a.group or "Default")
                if grp.lower() not in seen:
                    seen.add(grp.lower())
                    all_groups.append(grp)

            # Sort groups by custom priority or name
            all_groups.sort(key=lambda g: (group_priority.get(g.lower(), 9999), g.lower()), reverse=reverse)
            grp_rank = {g.lower(): idx for idx, g in enumerate(all_groups)}

            # Stable sort on group rank only: preserves 100% of custom account order within each group
            self.accounts.sort(key=lambda a: grp_rank.get((a.group or "Default").lower(), 9999))
        elif logical_index == 3:  # Mode (High vs Low / FPS)
            self.accounts.sort(
                key=lambda a: (
                    1 if self.storage.is_main_account(a) else 0,
                    self.storage.get_account_target_fps(a),
                ),
                reverse=reverse,
            )
        elif logical_index == 4:  # Description
            self.accounts.sort(key=lambda a: (a.notes or "").lower(), reverse=reverse)

        # Persist the new order in storage
        self.storage.reorder_accounts([a.account_id for a in self.accounts])
        self._populate_table()

        # Update header labels with sort arrow
        hdr_labels = list(self.base_headers)
        arrow = " ▼" if reverse else " ▲"
        if 0 <= logical_index < len(hdr_labels):
            hdr_labels[logical_index] += arrow
        self.table.setHorizontalHeaderLabels(hdr_labels)

        col_name = self.base_headers[logical_index] if 0 <= logical_index < len(self.base_headers) else "Column"
        direction = "descending" if reverse else "ascending"
        self.statusBar().showMessage(f"Sorted by {col_name} ({direction})", 3000)

    def _move_selected_rows_up(self):
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows or 0 in selected_rows:
            return
        target_row = selected_rows[0] - 1
        self._on_table_rows_reordered(selected_rows, target_row)

    def _move_selected_rows_down(self):
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows or (len(self.visible_accounts) - 1) in selected_rows:
            return
        target_row = selected_rows[-1] + 2
        self._on_table_rows_reordered(selected_rows, target_row)

    def _move_selected_rows_to_top(self):
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows or 0 in selected_rows:
            return
        self._on_table_rows_reordered(selected_rows, 0)

    def _move_selected_rows_to_bottom(self):
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows or (len(self.visible_accounts) - 1) in selected_rows:
            return
        self._on_table_rows_reordered(selected_rows, len(self.visible_accounts))

    def _sort_table_by_group_order(self):
        groups = self.storage.list_groups()
        group_priority = {g: i for i, g in enumerate(groups)}

        # Stable sort preserving relative order within the same group
        self.accounts.sort(key=lambda a: group_priority.get(a.group, 9999))
        self.storage.reorder_accounts([a.account_id for a in self.accounts])
        self._sort_column = None
        self._sort_order = None
        self.table.setHorizontalHeaderLabels(self.base_headers)
        self._populate_table()
        self.statusBar().showMessage("Sorted by group order", 3000)

    def _open_group_manager(self):
        self._open_modeless_window(
            "group_manager",
            lambda: GroupManagerDialog(self.storage, self),
            on_accepted=lambda: (
                self._update_group_combo_items(),
                self.statusBar().showMessage("Updated group order", 3000),
            ),
        )

    def _open_group_combo_menu(self, pos: QtCore.QPoint):
        menu = QtWidgets.QMenu(self.group_combo)
        menu.addAction("Manage Groups...", self._open_group_manager)
        menu.addSeparator()
        if self.selected_group != "All Groups":
            menu.addAction(f"Move '{self.selected_group}' Up", self._move_current_group_up)
            menu.addAction(f"Move '{self.selected_group}' Down", self._move_current_group_down)
            menu.addSeparator()
        menu.addAction("+ New Group...", self._create_and_move_to_new_group)
        menu.exec(self.group_combo.mapToGlobal(pos))

    def _move_current_group_up(self):
        if self.selected_group == "All Groups":
            return
        groups = self.storage.list_groups()
        if self.selected_group in groups:
            idx = groups.index(self.selected_group)
            if idx > 0:
                groups[idx], groups[idx - 1] = groups[idx - 1], groups[idx]
                self.storage.set_group_order(groups)
                self._update_group_combo_items()
                self.statusBar().showMessage(f"Moved group up: {self.selected_group}", 3000)

    def _move_current_group_down(self):
        if self.selected_group == "All Groups":
            return
        groups = self.storage.list_groups()
        if self.selected_group in groups:
            idx = groups.index(self.selected_group)
            if idx < len(groups) - 1:
                groups[idx], groups[idx + 1] = groups[idx + 1], groups[idx]
                self.storage.set_group_order(groups)
                self._update_group_combo_items()
                self.statusBar().showMessage(f"Moved group down: {self.selected_group}", 3000)

    def _set_selected_profile_mode(self, mode: str):
        targets = self._selected_accounts()
        if not targets:
            return
        alt_fps = self.storage.get_setting("alt_fps", "6")
        main_fps = self.storage.get_setting("main_target_fps", "330")
        label = f"High ({main_fps} FPS)" if mode == "main" else f"Low ({alt_fps} FPS)"
        for acc in targets:
            self.storage.set_account_profile_mode(acc.account_id, mode)
            self.storage.init_profile_sandbox(acc)
        self._load_accounts()
        self.statusBar().showMessage(f"Set mode: {label} ({len(targets)} accounts)", 4000)

    def _set_selected_fps(self, fps: int):
        targets = self._selected_accounts()
        if not targets:
            return
        for acc in targets:
            self.storage.set_account_target_fps(acc.account_id, fps)
            self.storage.init_profile_sandbox(acc)
        self._load_accounts()
        msg = "Unlimited" if (fps >= 1000 or fps <= 0) else f"{fps} FPS"
        self.statusBar().showMessage(f"Set FPS: {msg} ({len(targets)} accounts)", 4000)

    def _prompt_custom_fps(self):
        targets = self._selected_accounts()
        if not targets:
            return
        fps, ok = BorderlessInputDialog.getInt(
            self,
            "Custom Framerate",
            "Enter target FPS (1 - 10000):",
            330,
            1,
            10000,
            10,
        )
        if ok:
            self._set_selected_fps(fps)

    def _open_web_login(self, *args, target_account: Optional[RobloxAccount] = None, auto_login: bool = False, force_engine: Optional[str] = None, **kwargs):
        if args and isinstance(args[0], RobloxAccount):
            target_account = args[0]
        selected_grp = getattr(self, "selected_group", "All Groups")
        group = selected_grp if selected_grp and selected_grp not in ("All Groups", "All") else "Default"

        # Modeless dialog so user can still interact with AERAM main window
        dialog = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            runner=self.runner,
            target_account=target_account,
            default_group=group,
            mode="login",
            parent=self,
            auto_login=auto_login,
        )
        dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal)

        def on_auth(acc):
            if target_account and hasattr(self.runner, "auth_state_manager"):
                self.runner.auth_state_manager.reset_verification_status(target_account.account_id)
            self._load_accounts()
            self.statusBar().showMessage(
                f"Saved @{acc.username} to vault",
                5000,
            )

        dialog.account_authenticated.connect(on_auth)
        dialog.finished.connect(lambda result: self._load_accounts())

        # Keep reference to prevent garbage collection
        if not hasattr(self, "_active_web_dialogs"):
            self._active_web_dialogs = []
        self._active_web_dialogs.append(dialog)
        dialog.finished.connect(lambda: self._active_web_dialogs.remove(dialog) if dialog in self._active_web_dialogs else None)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_add_account(self):
        self._open_modeless_window(
            "add_account",
            lambda: AddAccountDialog(self.storage, self.api, self),
            on_accepted=self._load_accounts,
        )

    def _open_import_ram(self):
        self._open_modeless_window(
            "import_ram",
            lambda: ImportRAMDialog(self.storage, self.api, self),
            on_accepted=self._load_accounts,
        )

    def _export_encrypted_archive(self):
        accounts = self.storage.list_accounts()
        if not accounts:
            QMessageBox.information(self, "Export Accounts", "There are no accounts in the vault to export.")
            return

        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Encrypted AERAM Vault",
            str(Path.home() / "aeram_backup.aeram"),
            "AERAM Encrypted Vault (*.aeram);;All Files (*)",
        )
        if not file_path:
            return

        password, ok = BorderlessInputDialog.getText(
            self,
            "Master Encryption Password",
            "Enter a password to encrypt this export archive with Argon2id + XSalsa20-Poly1305:\n"
            "(You will need this password to import onto another Linux machine)",
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not ok or not password.strip():
            QMessageBox.warning(self, "Export Cancelled", "A master password is required to encrypt the export archive.")
            return

        try:
            from core.migration import export_accounts_encrypted
            export_accounts_encrypted(accounts, password.strip(), Path(file_path))
            QMessageBox.information(
                self,
                "Export Complete",
                f"Successfully exported {len(accounts)} accounts into encrypted archive:\n{file_path}\n\n"
                "This file is safely encrypted with libsodium.",
            )
            self.statusBar().showMessage(f"Exported {len(accounts)} account(s) to {Path(file_path).name}", 5000)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export accounts:\n{e}")

    def _edit_current_account(self):
        acc = self.current_account
        if not acc:
            targets = self._selected_accounts()
            if targets:
                acc = targets[0]
        if not acc:
            QMessageBox.information(self, "Edit Account", "Please select an account from the table first.")
            return

        def _make_dlg():
            dlg = EditAccountDialog(acc, self)
            def _on_edited():
                self.storage.save_account(acc)
                if hasattr(dlg, "new_cookie") and dlg.new_cookie and hasattr(self.runner, "auth_state_manager"):
                    self.runner.auth_state_manager.mark_ready(acc.account_id)
                self._load_accounts()
            dlg.accepted.connect(_on_edited)
            return dlg

        self._open_modeless_window(f"edit_account_{acc.account_id}", _make_dlg)

    def _open_account_in_browser(
        self,
        destination: str = "home",
        target_account: Optional[RobloxAccount] = None,
        force_engine: Optional[str] = None,
        force_external: Optional[bool] = None,
    ):
        """
        Opens the selected account's session directly in the embedded Chromium
        WebEngine dialog with the account's session cookie injected.
        """
        acc = target_account or self.current_account
        if not acc:
            targets = self._selected_accounts()
            if targets:
                acc = targets[0]

        if not acc:
            QMessageBox.information(
                self,
                "Open in Browser",
                "Please select a Roblox account from the table first.",
            )
            return

        if not acc.cookie:
            ret = QMessageBox.question(
                self,
                "No Session Cookie",
                f"Account @{acc.username} does not have a session cookie (.ROBLOSECURITY) stored.\n\nWould you like to open the browser to log in?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ret == QMessageBox.StandardButton.Yes:
                destination = "login"
            else:
                return

        # Determine target URL
        dest_map = {
            "home": "https://www.roblox.com/home",
            "login": "https://www.roblox.com/login",
            "profile": f"https://www.roblox.com/users/{acc.user_id}/profile" if acc.user_id else "https://www.roblox.com/my/profile",
            "avatar": "https://www.roblox.com/my/avatar",
            "inventory": f"https://www.roblox.com/users/{acc.user_id}/inventory" if acc.user_id else "https://www.roblox.com/users/inventory",
            "settings": "https://www.roblox.com/my/account",
            "security": "https://www.roblox.com/my/account#!/security",
            "transactions": "https://www.roblox.com/transactions",
        }

        if destination == "custom":
            custom_url, ok = BorderlessInputDialog.getText(
                self,
                "Open Custom URL",
                f"Enter URL to open as @{acc.username}:",
                QtWidgets.QLineEdit.EchoMode.Normal,
                "https://www.roblox.com/home",
            )
            if not ok or not custom_url.strip():
                return
            target_url = custom_url.strip()
            if not target_url.startswith("http://") and not target_url.startswith("https://"):
                target_url = "https://" + target_url
        else:
            target_url = dest_map.get(destination, "https://www.roblox.com/home")

        dialog = WebLoginDialog(
            storage=self.storage,
            api=self.api,
            runner=self.runner,
            target_account=acc,
            mode="browser",
            initial_url=target_url,
            parent=self,
        )
        dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _auto_login_account(self, account: Optional[RobloxAccount] = None):
        """
        Performs automated end-to-end login for the target account.
        Tries direct API login with 2FA first. If Roblox requests a visual captcha,
        seamlessly launches WebLoginDialog in auto-login mode with credentials and
        TOTP automated injection active.
        """
        acc = account or self.current_account
        if not acc:
            targets = self._selected_accounts()
            if targets:
                acc = targets[0]
        if not acc:
            QMessageBox.information(self, "Auto Log In", "Please select an account from the table first.")
            return

        if not acc.password:
            pwd, ok = BorderlessInputDialog.getText(
                self,
                "Password Required",
                f"Account @{acc.username} does not have a saved password.\nEnter password for automated login:",
                QtWidgets.QLineEdit.EchoMode.Password,
            )
            if not ok or not pwd.strip():
                return
            acc.password = pwd.strip()
            self.storage.save_account(acc)

        self.statusBar().showMessage(f"Auto logging in @{acc.username} (API)...", 4000)
        QtWidgets.QApplication.processEvents()

        ok, cookie, user_data, msg = self.api.login_with_credentials(
            acc.username, acc.password, acc.totp_secret
        )

        if ok and cookie:
            acc.cookie = cookie
            if user_data:
                acc.user_id = user_data.get("id", acc.user_id)
                acc.display_name = user_data.get("displayName", acc.display_name)
            self.storage.save_account(acc)
            if hasattr(self.runner, "auth_state_manager"):
                self.runner.auth_state_manager.mark_ready(acc.account_id)
            self._load_accounts()
            self.statusBar().showMessage(f"Auto Log In successful for @{acc.username}! Session updated.", 6000)
            return

        # If API login required captcha or encountered a browser challenge, seamlessly launch in-browser auto login
        is_challenge = (
            msg == "CAPTCHA_REQUIRED"
            or "challenge" in msg.lower()
            or "captcha" in msg.lower()
            or "verification" in msg.lower()
        )
        reason = "Roblox requires visual verification (Captcha)" if is_challenge else msg
        self.statusBar().showMessage(f"Launching in-browser auto login ({reason})...", 5000)
        self._open_web_login(target_account=acc, auto_login=True)

    def _open_browser_verification(self, *args, force_engine: Optional[str] = None):
        acc = self.current_account
        if not acc:
            targets = self._selected_accounts()
            if targets:
                acc = targets[0]
        if not acc:
            QMessageBox.information(self, "Re-authenticate", "Please select an account from the table first.")
            return
        dest = "home" if acc.cookie else "login"
        self._open_account_in_browser(destination=dest, target_account=acc, force_engine=force_engine)

    def _reset_current_verification(self):
        targets = self._selected_accounts()
        if not targets:
            return
        count = len(targets)
        for acc in targets:
            if hasattr(self.runner, "auth_state_manager"):
                self.runner.auth_state_manager.reset_verification_status(acc.account_id)
        self._populate_table()
        if count == 1:
            self.statusBar().showMessage(f"Reset status: @{targets[0].username}", 4000)
        else:
            self.statusBar().showMessage(f"Reset status: {count} accounts", 4000)

    def _validate_current_account_session(self):
        """Validates session cookie for selected account(s) one-by-one."""
        targets = self._selected_accounts()
        if not targets:
            return

        count = len(targets)
        valid_count = 0
        expired_count = 0
        error_count = 0

        for idx, acc in enumerate(targets, 1):
            if count > 1:
                self.statusBar().showMessage(f"Validating [{idx}/{count}]: @{acc.username}", 0)
            else:
                self.statusBar().showMessage(f"Validating @{acc.username}...", 3000)
            QtWidgets.QApplication.processEvents()

            is_valid, user_data, status_reason = self.api.validate_cookie(acc.cookie)
            if is_valid and user_data:
                valid_count += 1
                if hasattr(self.runner, "auth_state_manager"):
                    self.runner.auth_state_manager.mark_ready(acc.account_id)
            elif status_reason == "EXPIRED":
                expired_count += 1
                if hasattr(self.runner, "auth_state_manager"):
                    self.runner.auth_state_manager.mark_session_expired(acc.account_id)
            else:
                error_count += 1

        self._populate_table()

        if count == 1:
            acc = targets[0]
            if valid_count == 1:
                self.statusBar().showMessage(f"Session valid: @{acc.username}", 5000)
                QMessageBox.information(
                    self,
                    "Session Valid",
                    f"Authentication session for @{acc.username} is valid and active.\n\n"
                    f"User ID: {acc.user_id}\n"
                    f"Display Name: {acc.display_name or acc.username}",
                )
            elif expired_count == 1:
                self.statusBar().showMessage(f"Session expired: @{acc.username}", 5000)
                QMessageBox.warning(
                    self,
                    "Session Expired",
                    f"The session for @{acc.username} has expired (HTTP 401 Unauthorized).\n\n"
                    f"Please re-authenticate via browser or update the .ROBLOSECURITY cookie.",
                )
            else:
                self.statusBar().showMessage(f"Validation failed: @{acc.username}", 5000)
                QMessageBox.information(
                    self,
                    "Validation Status",
                    f"Could not verify session for @{acc.username} at this time.",
                )
        else:
            self.statusBar().showMessage(
                f"Validated: {valid_count} active, {expired_count} expired", 6000
            )
            QMessageBox.information(
                self,
                "Session Validation Summary",
                f"Validated {count} account(s):\n\n"
                f"• Active & Valid: {valid_count}\n"
                f"• Expired (HTTP 401): {expired_count}\n"
                f"• Network / Other Errors: {error_count}",
            )

    def _open_unblock_everyone(self):
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(
                self, "Unblock Everyone", "Please select one or more accounts from the table first."
            )
            return

        valid_targets = [a for a in targets if a.cookie]
        if not valid_targets:
            QMessageBox.warning(
                self,
                "Unblock Everyone",
                "None of the selected accounts have an active session cookie (.ROBLOSECURITY).",
            )
            return

        if len(valid_targets) == 1:
            from ui.dialogs import UnblockEveryoneDialog
            create_dlg = lambda: UnblockEveryoneDialog(valid_targets[0], self.api, self)
        else:
            from ui.dialogs import BatchUnblockDialog
            create_dlg = lambda: BatchUnblockDialog(valid_targets, self.api, self)

        self._open_modeless_window("unblock_everyone", create_dlg)

    def _open_change_password(self):
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(
                self, "Change Password", "Please select one or more accounts from the table first."
            )
            return

        valid_targets = [a for a in targets if a.cookie]
        if not valid_targets:
            QMessageBox.warning(
                self,
                "Change Password",
                "None of the selected accounts have an active session cookie (.ROBLOSECURITY).\n"
                "A valid session is required to authorize the password update.",
            )
            return

        if len(valid_targets) == 1:
            from ui.dialogs import ChangePasswordDialog
            create_dlg = lambda: ChangePasswordDialog(valid_targets[0], self.api, self.storage, self)
        else:
            from ui.dialogs import BatchPasswordChangeDialog
            create_dlg = lambda: BatchPasswordChangeDialog(valid_targets, self.api, self.storage, self)

        self._open_modeless_window(
            "change_password",
            create_dlg,
            on_accepted=self._load_accounts,
        )

    def _open_batch_auto_login(self, targets: Optional[List[RobloxAccount]] = None):
        if targets is None:
            targets = self._selected_accounts()
        if not targets:
            targets = self.accounts
        if not targets:
            QMessageBox.information(
                self, "Batch Auto Log In", "Please select one or more accounts from the table first."
            )
            return

        def _on_done():
            self._load_accounts()
            if self.current_account:
                self._select_account(self.current_account)

        self._open_modeless_window(
            "batch_auto_login",
            lambda: BatchAutoLoginDialog(targets, self.api, self.storage, self.runner, self),
            on_accepted=_on_done,
        )

    def _open_batch_logout(self):
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(
                self, "Complete Logout", "Please select one or more accounts from the table first."
            )
            return

        valid_targets = [a for a in targets if a.cookie]
        if not valid_targets:
            QMessageBox.warning(
                self,
                "Complete Logout",
                "None of the selected accounts have an active session cookie (.ROBLOSECURITY).\n"
                "Session cookies are already cleared or missing.",
            )
            return

        def _on_logout():
            self._load_accounts()
            if self.current_account:
                self._select_account(self.current_account)

        self._open_modeless_window(
            "batch_logout",
            lambda: BatchLogoutDialog(valid_targets, self.api, self.storage, self),
            on_accepted=_on_logout,
        )

    def _open_batch_friends(self):
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(
                self, "Friends Manager", "Please select one or more accounts from the table first."
            )
            return

        valid_targets = [a for a in targets if a.cookie]
        if not valid_targets:
            QMessageBox.warning(
                self,
                "Friends Manager",
                "None of the selected accounts have an active session cookie (.ROBLOSECURITY).\n"
                "A valid session is required to interact with friends.",
            )
            return

        self._open_modeless_window(
            "batch_friends",
            lambda: BatchFriendsDialog(valid_targets, self.api, self),
        )

    def _open_batch_privacy(self):
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(
                self, "Join Privacy Presets", "Please select one or more accounts from the table first."
            )
            return

        valid_targets = [a for a in targets if a.cookie]
        if not valid_targets:
            QMessageBox.warning(
                self,
                "Join Privacy Presets",
                "None of the selected accounts have an active session cookie (.ROBLOSECURITY).\n"
                "A valid session is required to update privacy settings.",
            )
            return

        def _on_privacy_updated():
            self._load_accounts()
            if self.current_account:
                self._select_account(self.current_account)

        self._open_modeless_window(
            "batch_privacy",
            lambda: BatchPrivacyDialog(valid_targets, self.api, self.storage, self),
            on_accepted=_on_privacy_updated,
        )

    def _apply_join_privacy_for_current(self):
        self._open_batch_privacy()

    def _sync_selected_account_robux(self):
        if not self.current_account:
            self.statusBar().showMessage("No account selected to sync Robux", 3000)
            return
        self._sync_robux_for_targets([self.current_account])

    def _sync_robux_for_targets(self, targets=None):
        if targets is None:
            targets = self._selected_accounts()
            if not targets:
                targets = list(self.accounts)

        valid_targets = [a for a in targets if a.cookie and a.user_id]
        if not valid_targets:
            self.statusBar().showMessage("No accounts with active session cookies to sync Robux", 4000)
            return

        self.statusBar().showMessage(f"Syncing Robux for {len(valid_targets)} account(s)...", 5000)

        import threading
        def _worker():
            updated_count = 0
            for acc in valid_targets:
                try:
                    res = self.api.get_currency_summary(acc.user_id, acc.cookie)
                    if res.get("success"):
                        robux = res.get("robux", 0)
                        pending = res.get("pending_robux", 0)
                        acc.robux_balance = robux
                        acc.pending_robux = pending
                        self.storage.update_currency(acc.account_id, robux, pending)
                        updated_count += 1
                except Exception as e:
                    log.debug(f"Failed to sync currency for {acc.username}: {e}")
                time.sleep(0.35)

            def _on_done():
                self._load_accounts()
                if self.current_account:
                    self._select_account(self.current_account)
                self.statusBar().showMessage(f"Synced Robux for {updated_count} account(s)", 4000)
            QtCore.QMetaObject.invokeMethod(self, _on_done, QtCore.Qt.ConnectionType.QueuedConnection)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_cookie_check(self, show_prompt: bool = False):
        """
        Validates session cookies for all accounts in the background.
        Syncs Robux balances and auth state without blocking the GUI thread.
        """
        if getattr(self, "_cookie_validator_worker", None) and self._cookie_validator_worker.isRunning():
            if show_prompt:
                self.statusBar().showMessage("Verification already in progress", 3000)
            return

        if not self.accounts:
            return

        self.statusBar().showMessage(f"Verifying {len(self.accounts)} session(s)...", 4000)
        worker = CookieValidationWorker(self.storage, self.api, list(self.accounts), self)
        worker.account_checked.connect(self._on_account_cookie_checked)
        worker.all_finished.connect(self._on_cookie_check_finished)
        self._cookie_validator_worker = worker
        worker.start()

    def _on_account_cookie_checked(self, account_id: str, is_valid: bool, robux: int, status_reason: str):
        acc = self.account_map.get(account_id)
        if not acc:
            return

        # Update robux balance if fetched successfully
        if robux >= 0 and robux != acc.robux_balance:
            acc.robux_balance = robux
            try:
                self.storage.update_metadata(acc.account_id, acc.display_name, acc.avatar_url, robux)
            except Exception as e:
                log.debug(f"Failed to update metadata for {acc.username}: {e}")

        # Update auth state
        if hasattr(self.runner, "auth_state_manager"):
            current_state = self.runner.auth_state_manager.get_state(account_id)
            if current_state not in (
                AccountAuthState.RUNNING,
                AccountAuthState.AUTHENTICATING,
                AccountAuthState.LAUNCHING,
                AccountAuthState.VERIFICATION_REQUIRED,
            ):
                if is_valid:
                    if current_state != AccountAuthState.READY:
                        self.runner.auth_state_manager.mark_ready(account_id)
                elif status_reason == "EXPIRED":
                    if current_state != AccountAuthState.SESSION_EXPIRED:
                        self.runner.auth_state_manager.mark_session_expired(account_id)
                elif status_reason == "EMPTY_COOKIE":
                    if current_state != AccountAuthState.SESSION_EXPIRED:
                        self.runner.auth_state_manager.mark_session_expired(account_id, reason="No cookie provided")

        self._poll_instances()

    def _on_cookie_check_finished(self, valid_count: int, expired_count: int):
        total = len(self.accounts)
        if expired_count > 0:
            self.statusBar().showMessage(
                f"Verified: {valid_count}/{total} active ({expired_count} expired)", 6000
            )
        else:
            self.statusBar().showMessage(
                f"Verified: all {valid_count} accounts active", 5000
            )

    def _log_out_of_other_sessions(self):
        """
        Signs out of all other active sessions for selected account(s) one-by-one.
        Invalidates all other logged-in browsers and devices while updating
        AERAM with the newly rotated session token so each account remains logged in.
        """
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(
                self,
                "Log Out of Other Sessions",
                "Please select one or more accounts from the table.",
            )
            return

        valid_targets = [a for a in targets if a.cookie]
        if not valid_targets:
            QMessageBox.warning(
                self,
                "Log Out of Other Sessions",
                "None of the selected accounts have an active session cookie (.ROBLOSECURITY).",
            )
            return

        count = len(valid_targets)
        account_names = ", ".join([f"@{a.username}" for a in valid_targets[:5]])
        if count > 5:
            account_names += f" and {count - 5} more"

        self._open_modeless_window(
            "batch_sign_out",
            lambda: BatchSignOutDialog(
                valid_targets,
                self.api,
                self.storage,
                runner=self.runner,
                parent=self,
            ),
            on_finished=self._load_accounts,
        )

    def _open_live_2fa_hub(self):
        all_accounts = self.storage.list_accounts()
        return self._open_modeless_window(
            "live_2fa_hub",
            lambda: Live2FAHubDialog(all_accounts, self.storage, parent=self),
            on_finished=self._load_accounts,
        )

    def _action_import_google_auth(self):
        all_accounts = self.storage.list_accounts()
        def _on_imported():
            self._load_accounts()
            self.statusBar().showMessage("Updated 2FA secret keys from Authenticator.", 4000)

        self._open_modeless_window(
            "google_auth_import",
            lambda: GoogleAuthImportDialog(all_accounts, self.storage, parent=self),
            on_accepted=_on_imported,
        )

    def _action_export_2fa_keys(self):
        from ui.dialogs import TwoFactorExportDialog
        all_accounts = self.storage.list_accounts()
        self._open_modeless_window(
            "two_factor_export",
            lambda: TwoFactorExportDialog(all_accounts, parent=self),
        )

    def _action_edit_2fa_for_current(self):
        if not self.current_account:
            return
        acc = self.current_account
        self._open_modeless_window(
            f"edit_2fa_{acc.account_id}",
            lambda: EditAccountDialog(acc, parent=self),
            on_accepted=lambda: (self.storage.save_account(acc), self._load_accounts()),
        )

    def _copy_current_2fa_code(self):
        if not self.current_account or not self.current_account.totp_secret:
            return
        try:
            from core.totp import generate_totp, get_totp_remaining_seconds
            code = generate_totp(self.current_account.totp_secret)
            QtWidgets.QApplication.clipboard().setText(code)
            rem = get_totp_remaining_seconds()
            self.statusBar().showMessage(f"Copied 2FA code for @{self.current_account.username}: {code} (expires in {rem}s)", 4500)
        except Exception as e:
            self.statusBar().showMessage(f"Failed to generate 2FA code: {e}", 4000)

    def _copy_selected_username(self):
        targets = self._selected_accounts()
        if not targets:
            return
        lines = [acc.username for acc in targets]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(targets)} username(s)", 3000)

    def _copy_selected_password(self):
        targets = self._selected_accounts()
        if not targets:
            return
        lines = [acc.password for acc in targets]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        empty_count = sum(1 for p in lines if not p)
        if empty_count > 0:
            self.statusBar().showMessage(f"Copied {len(targets)} password(s) ({empty_count} blank)", 4000)
        else:
            self.statusBar().showMessage(f"Copied {len(targets)} password(s)", 3000)

    def _copy_selected_user_pass(self):
        targets = self._selected_accounts()
        if not targets:
            return
        lines = [f"{acc.username}:{acc.password}" for acc in targets]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(targets)} combo(s)", 3000)

    def _copy_selected_user_id(self):
        targets = self._selected_accounts()
        if not targets:
            return
        lines = [str(acc.user_id) for acc in targets]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(targets)} User ID(s)", 3000)

    def _copy_selected_cookie(self):
        targets = self._selected_accounts()
        if not targets:
            return
        lines = [acc.cookie for acc in targets]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(targets)} cookie(s)", 3000)

    def _remove_selected(self):
        targets = self._selected_accounts()
        if not targets:
            QMessageBox.information(self, "Remove Account", "Please select one or more accounts from the table first.")
            return
        names = ", ".join(a.username for a in targets[:5]) + (" ..." if len(targets) > 5 else "")
        reply = QMessageBox.question(
            self, "Remove", f"Remove {len(targets)} account(s)?\n\n{names}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for acc in targets:
            self.runner.terminate_instance(acc.account_id)
            self.storage.delete_account(acc.account_id)
        self.current_account = None
        self._load_accounts()

    def _refresh_accounts(self):
        self._refresh_presence()
        if getattr(self, "_account_sync_worker", None) and self._account_sync_worker.isRunning():
            self.statusBar().showMessage("Sync already in progress", 3000)
            return

        if not self.accounts:
            self.statusBar().showMessage("No accounts to sync", 3000)
            return

        self.statusBar().showMessage(f"Syncing {len(self.accounts)} account(s)...", 4000)
        worker = AccountSyncWorker(self.storage, self.api, list(self.accounts), self)
        worker.account_synced.connect(self._on_account_synced)
        worker.all_finished.connect(self._on_account_sync_finished)
        self._account_sync_worker = worker
        worker.start()

    def _on_account_synced(self, account_id: str, display_name: str, avatar_url: str, robux: int):
        acc = self.account_map.get(account_id)
        if not acc:
            return
        acc.web_display_name = display_name
        # Preserve user's custom alias: only populate display_name if unset/blank
        if not (acc.display_name or "").strip():
            acc.display_name = display_name
            if self.current_account and self.current_account.account_id == account_id:
                self.alias_input.setText(display_name)
        acc.avatar_url = avatar_url
        if robux >= 0:
            acc.robux_balance = robux
        self._poll_instances()

    def _on_account_sync_finished(self, updated_count: int):
        self.statusBar().showMessage(f"Synced {updated_count} account(s)", 5000)

    def _open_settings(self):
        return self._open_modeless_window("settings", lambda: SettingsDialog(self.storage, self))

    def _open_diagnostics(self):
        return self._open_modeless_window("diagnostics", lambda: DiagnosticsDialog(self))

    def _open_updater(self):
        return self._open_modeless_window("updater", lambda: UpdateDialog(self))

    def _is_anti_afk_running(self) -> bool:
        if getattr(self, "anti_afk_worker", None) and self.anti_afk_worker.isRunning():
            return True
        proc = getattr(self, "_anti_afk_proc", None)
        if proc is not None:
            if proc.poll() is None:
                return True
            else:
                self._anti_afk_proc = None

        now = time.monotonic()
        if (now - getattr(self, "_last_anti_afk_check", 0)) > 10.0:
            self._last_anti_afk_check = now
            found = False
            if hasattr(os, "getuid"):
                try:
                    my_uid = os.getuid()
                    with os.scandir("/proc") as it:
                        for entry in it:
                            if not entry.name.isdigit():
                                continue
                            try:
                                if entry.stat().st_uid != my_uid:
                                    continue
                                with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                                    if b"core/anti_afk.py" in f.read():
                                        found = True
                                        break
                            except Exception:
                                continue
                except Exception:
                    pass
            self._cached_anti_afk_running = found

        return getattr(self, "_cached_anti_afk_running", False)

    def _update_acc_tools_menu(self):
        menu = getattr(self.btn_acc_tools, "menu", lambda: None)()
        if not menu:
            return
        menu.clear()

        targets = self._selected_accounts()
        target_cnt = len(targets)
        target_suffix = f" (@{targets[0].username})" if target_cnt == 1 else (f" ({target_cnt} Accounts)" if target_cnt > 1 else "")

        # ── 1. Instance Operations ──
        kill_label = f"Kill Selected Instance{target_suffix}" if target_cnt > 0 else "Kill Selected Instance"
        menu.addAction(kill_label, self._kill_selected)
        menu.addAction("Kill All Running Instances", self._stop_all_instances)
        menu.addSeparator()

        # ── 2. Automation & Watchdogs ──
        is_afk = self._is_anti_afk_running()
        afk_label = "Anti-AFK: ON  [Click to Disable]" if is_afk else "Anti-AFK: OFF  [Click to Enable]"
        menu.addAction(afk_label, self._toggle_anti_afk)
        menu.addAction("Force Anti-AFK Pulse Now", self._force_anti_afk_now)

        is_heal = bool(getattr(self, "self_heal_engine", None) and self.self_heal_engine.running)
        heal_label = "Self-Healing: ON  [Click to Disable]" if is_heal else "Self-Healing: OFF  [Click to Enable]"
        menu.addAction(heal_label, self._toggle_self_heal)
        menu.addAction("Simulate Alt Disconnect (Test Self-Heal)...", self._test_self_heal_simulation)
        menu.addSeparator()

        # ── 3. Profile & Session Verification ──
        menu.addAction("Refresh Web Presence (Online/In-Game)", self._refresh_presence)
        menu.addAction("Verify Session Cookies (All)", lambda: self._start_cookie_check(show_prompt=True))
        if target_cnt > 0:
            if target_cnt == 1:
                menu.addAction(f"Auto Log In (@{targets[0].username})", lambda: self._auto_login_account(targets[0]))
            else:
                menu.addAction(f"Auto Log In ({target_cnt} Accounts)...", lambda: self._open_batch_auto_login(targets))
            menu.addAction(f"Validate Session{target_suffix}", self._validate_current_account_session)
            menu.addAction(f"Re-authenticate in Browser{target_suffix}...", lambda: self._open_browser_verification(force_engine="internal"))
            menu.addAction(f"Reset Status to Ready{target_suffix}", self._reset_current_verification)
        menu.addSeparator()

        # ── 4. Maintenance, Security & Social ──
        utils_menu = menu.addMenu(f"Account Utilities{target_suffix}")

        sec_menu = utils_menu.addMenu("Security & Privacy")
        sec_menu.addAction("Join Privacy Presets (Ghost / Friends / Public)...", self._open_batch_privacy)
        sec_menu.addAction("Change Password...", self._open_change_password)
        sec_menu.addAction("Log Out of Other Sessions...", self._log_out_of_other_sessions)
        sec_menu.addAction("Complete Logout (Revoke Session)...", self._open_batch_logout)

        social_menu = utils_menu.addMenu("Social Management")
        social_menu.addAction("Friends Manager (Decline / Accept / Wipe)...", self._open_batch_friends)
        social_menu.addAction("Clear Block List (Unblock Everyone)...", self._open_unblock_everyone)

        # ── 5. Two-Factor Authentication (2FA) ──
        two_fa_menu = menu.addMenu(f"Two-Step Verification (2FA){target_suffix}")
        two_fa_menu.addAction("Live Authenticator Hub (Mobile Login)... (Ctrl+T)", self._open_live_2fa_hub)
        two_fa_menu.addSeparator()
        two_fa_menu.addAction("Import Authenticator QR Codes...", self._action_import_google_auth)
        two_fa_menu.addAction("Export 2FA Keys...", self._action_export_2fa_keys)
        if target_cnt == 1 and targets[0]:
            two_fa_menu.addAction("Edit 2FA Secret Key...", self._action_edit_2fa_for_current)

        menu.addSeparator()

        # ── 6. Vault Backup ──
        menu.addAction("Export Encrypted Archive (.aeram)...", self._export_encrypted_archive)

    def _open_fastflags_dialog(self):
        self._open_modeless_window("fastflags", lambda: FastFlagsDialog(self.storage, self))

    def _test_self_heal_simulation(self):
        running = getattr(self.runner, "active_instances", {})
        if not running:
            QMessageBox.information(
                self, "Self-Heal Test",
                "No Roblox instances are currently running.\n\n"
                "To test Self-Heal in real-time:\n"
                "1. Launch at least one alt account.\n"
                "2. Click 'Simulate Alt Disconnect'.\n"
                "3. Watch Self-Heal detect Error 277, terminate the stuck window, and auto-rejoin!"
            )
            return

        targets = [aid for aid in running.keys() if not self.storage.is_main_account(aid)]
        if not targets:
            targets = list(running.keys())

        target_aid = targets[0]
        acc = next((a for a in self.accounts if a.account_id == target_aid), None)
        target_name = acc.username if acc else target_aid

        reply = QMessageBox.question(
            self, "Simulate Alt Disconnect",
            f"Simulate a server disconnect (Error 277) on @{target_name}?\n\n"
            "This will append a disconnect error to its instance log so the Self-Heal engine "
            "immediately kicks in, detects the error, and automatically reconnects it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            if not getattr(self.self_heal_engine, "running", False):
                self.self_heal_engine.enable()
            self.self_heal_engine.simulate_disconnect(target_aid, error_code=277)
            self.statusBar().showMessage(f"Injected disconnect error on @{target_name}. Self-Heal triggered...", 5000)
            QtCore.QTimer.singleShot(1000, lambda: self.self_heal_engine.check_and_heal())

    def _force_anti_afk_now(self):
        self.statusBar().showMessage("Pulsing Anti-AFK...", 5000)
        anti_script = str(Path(__file__).parent.parent / "core" / "anti_afk.py")
        cmd = [sys.executable, anti_script, "--once"]

        def _worker():
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0:
                    QtCore.QMetaObject.invokeMethod(
                        self,
                        lambda: self.statusBar().showMessage("Anti-AFK pulse complete", 5000),
                        QtCore.Qt.ConnectionType.QueuedConnection,
                    )
            except Exception:
                pass

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _toggle_anti_afk(self):
        if self._is_anti_afk_running():
            if getattr(self, "anti_afk_worker", None) and self.anti_afk_worker.isRunning():
                self.anti_afk_worker.stop()
                self.anti_afk_worker = None
            if getattr(self, "_anti_afk_proc", None):
                try:
                    self._anti_afk_proc.terminate()
                except Exception:
                    pass
                self._anti_afk_proc = None
            try:
                subprocess.run(["pkill", "-f", "core/anti_afk.py"], capture_output=True)
            except Exception:
                pass
            self._cached_anti_afk_running = False
            self.statusBar().showMessage("Anti-AFK disabled", 4000)
        else:
            anti_script = str(Path(__file__).parent.parent / "core" / "anti_afk.py")
            cmd = [sys.executable, anti_script, "--interval", "900", "--idle-threshold", "0"]
            self._anti_afk_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self._cached_anti_afk_running = True
            self.statusBar().showMessage("Anti-AFK enabled", 5000)

    def _toggle_self_heal(self):
        if not getattr(self, "self_heal_engine", None):
            return
        if self.self_heal_engine.running:
            self.self_heal_engine.disable()
            self.statusBar().showMessage("Self-Healing disabled", 4000)
        else:
            self.self_heal_engine.enable()
            self.statusBar().showMessage("Self-Healing enabled", 5000)
            try:
                healed = self.self_heal_engine.check_and_heal()
                if healed > 0:
                    self.statusBar().showMessage(f"Self-Heal: Reconnected {healed} account(s)", 5000)
            except Exception:
                pass

    def closeEvent(self, event):
        if hasattr(self, "poll_timer") and self.poll_timer.isActive():
            self.poll_timer.stop()
        if hasattr(self, "_metric_cycle_timer") and self._metric_cycle_timer.isActive():
            self._metric_cycle_timer.stop()
        if getattr(self, "_anti_afk_proc", None):
            try:
                self._anti_afk_proc.terminate()
                self._anti_afk_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._anti_afk_proc.kill()
                except Exception:
                    pass
            self._anti_afk_proc = None
        if getattr(self, "_auth_validator_worker", None):
            self._auth_validator_worker.stop()
            self._auth_validator_worker.wait(1000)
            self._auth_validator_worker = None
        if getattr(self, "_cookie_validator_worker", None):
            self._cookie_validator_worker.stop()
            self._cookie_validator_worker.wait(1000)
            self._cookie_validator_worker = None
        if getattr(self, "anti_afk_worker", None):
            self.anti_afk_worker.stop()
            self.anti_afk_worker.wait(1000)
            self.anti_afk_worker = None
        if getattr(self, "_presence_worker", None):
            self._presence_worker.stop()
            self._presence_worker.wait(1000)
            self._presence_worker = None
        if getattr(self, "self_heal_engine", None):
            self.self_heal_engine.disable()
        super().closeEvent(event)
