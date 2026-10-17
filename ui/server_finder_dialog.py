"""
Server Monitor & Inspector Dialog for AERAM.
Allows monitoring live public servers, tracking player counts, ping, FPS,
inspecting sub-place instances (e.g. Rogue Lineage Gaia / Khei), copying Job IDs,
and auto-refreshing live server population statistics.
"""

import re
import time
import logging
from typing import Optional, List, Dict, Any

from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

from core.models import RobloxAccount
from core.storage import StorageManager
from core.roblox_api import RobloxAPI
from ui.borderless_dialog import BorderlessDialog, BorderlessMessageBox as QMessageBox

log = logging.getLogger("ServerMonitor")


def strip_emojis(text: str) -> str:
    """Remove emoji and decorative pictograph characters for a clean text UI."""
    if not text:
        return ""
    emoji_pattern = re.compile(
        '['
        '\U0001F600-\U0001F64F'  # emoticons
        '\U0001F300-\U0001F5FF'  # symbols & pictographs
        '\U0001F680-\U0001F6FF'  # transport & map symbols
        '\U0001F1E0-\U0001F1FF'  # flags
        '\U00002702-\U000027B0'
        '\U000024C2-\U0001F251'
        '\U0001F900-\U0001F9FF'  # supplemental symbols
        '\U0001FA70-\U0001FAFF'  # extended pictographs
        ']+',
        flags=re.UNICODE
    )
    cleaned = emoji_pattern.sub('', text)
    cleaned = re.sub(r'\[\s*\]', '', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()


class ServerFetchWorker(QtCore.QThread):
    """Background worker to fetch game servers and sub-places without freezing the GUI."""
    servers_loaded = pyqtSignal(list, str, object)   # (servers_list, place_name, root_place_id)
    sub_places_loaded = pyqtSignal(list)     # list of (name, place_id)
    error_occurred = pyqtSignal(str)

    def __init__(self, api: RobloxAPI, place_id: int, cookie: Optional[str] = None, sort_order: str = "Asc", limit: int = 100):
        super().__init__()
        self.api = api
        self.place_id = place_id
        self.cookie = cookie
        self.sort_order = sort_order
        self.limit = limit

    def run(self):
        try:
            place_name = f"Place {self.place_id}"
            root_place_id = None
            details = self.api.get_place_details(self.place_id, cookie=self.cookie)
            if details:
                place_name = strip_emojis(details.get("name", place_name))
                universe_id = details.get("universeId")
                root_place_id = details.get("universeRootPlaceId")
                if universe_id:
                    places = self.api.get_universe_places(universe_id, cookie=self.cookie)
                    if places and len(places) > 1:
                        sub_places = [(strip_emojis(p.get("name", f"Place {p.get('id')}")), p.get("id")) for p in places if p.get("id")]
                        self.sub_places_loaded.emit(sub_places)

            result = self.api.get_public_servers(
                self.place_id,
                cookie=self.cookie,
                sort_order=self.sort_order,
                limit=self.limit,
            )
            servers = result.get("data", [])
            self.servers_loaded.emit(servers, place_name, root_place_id)
        except Exception as e:
            log.error(f"Failed to fetch servers for place {self.place_id}: {e}")
            self.error_occurred.emit(str(e))


class ServerFinderDialog(BorderlessDialog):
    """
    Clean, informative Server Monitor & Inspector dialog.
    Monitors live public servers, tracking latency, player populations, and Job IDs.
    """

    server_selected = pyqtSignal('qint64', str, bool)  # (place_id, job_id, auto_join)

    def __init__(
        self,
        storage: StorageManager,
        api: RobloxAPI,
        initial_place_id: Optional[int] = None,
        selected_accounts: Optional[List[RobloxAccount]] = None,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent, title="Server Monitor", can_maximize=True, resizable=True)
        self.storage = storage
        self.api = api
        self.selected_accounts = selected_accounts or []
        self.current_place_id = initial_place_id or 3016661674
        self.root_place_id: Optional[int] = None
        self.raw_servers: List[Dict[str, Any]] = []
        self._worker: Optional[ServerFetchWorker] = None

        # Auto-refresh timer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(30000)  # 30 seconds
        self._refresh_timer.timeout.connect(self._fetch_servers)

        self.setWindowTitle("Server Monitor - AERAM")
        self.resize(800, 560)
        self.setMinimumSize(680, 460)
        self._init_ui()

        # Initial fetch
        self._fetch_servers()

    def _init_ui(self):
        layout = self.body_layout
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # ─── Top Control Bar: Place Input & Sub-Places (Audyrare / Melodyae Monospace Layout) ───
        lbl_row = QtWidgets.QHBoxLayout()
        lbl_row.setSpacing(6)
        lbl_p = QtWidgets.QLabel("PLACE ID")
        lbl_p.setFixedWidth(140)
        lbl_p.setStyleSheet("color: #555555; font-weight: bold; font-size: 10px; letter-spacing: 1px;")
        lbl_row.addWidget(lbl_p)
        lbl_w = QtWidgets.QLabel("WORLD / SUB-PLACE")
        lbl_w.setStyleSheet("color: #555555; font-weight: bold; font-size: 10px; letter-spacing: 1px;")
        lbl_row.addWidget(lbl_w, 1)
        layout.addLayout(lbl_row)

        input_row = QtWidgets.QHBoxLayout()
        input_row.setSpacing(6)
        self.place_input = QtWidgets.QLineEdit()
        self.place_input.setText(str(self.current_place_id))
        self.place_input.setFixedWidth(140)
        self.place_input.returnPressed.connect(self._on_search_clicked)
        input_row.addWidget(self.place_input)

        self.sub_place_combo = QtWidgets.QComboBox()
        self.sub_place_combo.currentIndexChanged.connect(self._on_sub_place_changed)
        input_row.addWidget(self.sub_place_combo, stretch=1)

        self.btn_fetch = QtWidgets.QPushButton("Inspect")
        self.btn_fetch.setObjectName("primaryBtn")
        self.btn_fetch.setFixedWidth(84)
        self.btn_fetch.clicked.connect(self._on_search_clicked)
        input_row.addWidget(self.btn_fetch)
        layout.addLayout(input_row)

        # ─── Live Statistics Status Bar (Modern TUI Frame) ───
        stats_frame = QtWidgets.QFrame()
        stats_frame.setStyleSheet("background-color: #000000; border: 1px solid #353535; padding: 2px 6px; border-radius: 0px;")
        stats_layout = QtWidgets.QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(4, 2, 4, 2)
        stats_layout.setSpacing(12)

        self.stat_total_servers = QtWidgets.QLabel("Active: --")
        self.stat_total_servers.setStyleSheet("color: #E8E8E8; font-size: 10px;")
        stats_layout.addWidget(self.stat_total_servers)

        self.stat_min_players = QtWidgets.QLabel("Lowest Pop: --")
        self.stat_min_players.setStyleSheet("color: #FFFFFF; font-weight: bold; font-size: 10px;")
        stats_layout.addWidget(self.stat_min_players)

        self.stat_best_ping = QtWidgets.QLabel("Latency: --")
        self.stat_best_ping.setStyleSheet("color: #E8E8E8; font-size: 10px;")
        stats_layout.addWidget(self.stat_best_ping)

        stats_layout.addStretch()

        self.stat_last_updated = QtWidgets.QLabel("Updated: --")
        self.stat_last_updated.setStyleSheet("color: #707070; font-size: 10px;")
        stats_layout.addWidget(self.stat_last_updated)

        layout.addWidget(stats_frame)

        # ─── Filter & Auto-Refresh Bar ───
        filter_bar = QtWidgets.QHBoxLayout()
        filter_bar.setSpacing(8)

        lbl_sort = QtWidgets.QLabel("SORT:")
        lbl_sort.setStyleSheet("color: #555555; font-weight: bold; font-size: 10px; letter-spacing: 1px;")
        filter_bar.addWidget(lbl_sort)

        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.addItem("Fewest Players (Solo / Low Pop)", "asc_players")
        self.sort_combo.addItem("Lowest Ping (Best Latency)", "asc_ping")
        self.sort_combo.addItem("Most Players (Crowded)", "desc_players")
        self.sort_combo.currentIndexChanged.connect(self._apply_filters_and_render)
        filter_bar.addWidget(self.sort_combo)

        lbl_max = QtWidgets.QLabel("MAX:")
        lbl_max.setStyleSheet("color: #555555; font-weight: bold; font-size: 10px; letter-spacing: 1px;")
        filter_bar.addWidget(lbl_max)

        self.max_players_combo = QtWidgets.QComboBox()
        self.max_players_combo.addItem("All Servers", 999)
        self.max_players_combo.addItem("≤ 3 Players (Solo Farming)", 3)
        self.max_players_combo.addItem("≤ 6 Players (Small)", 6)
        self.max_players_combo.addItem("≤ 12 Players (Medium)", 12)
        self.max_players_combo.addItem("≤ 18 Players", 18)
        self.max_players_combo.currentIndexChanged.connect(self._apply_filters_and_render)
        filter_bar.addWidget(self.max_players_combo)

        filter_bar.addStretch()

        self.chk_auto_refresh = QtWidgets.QCheckBox("Auto-Refresh (30s)")
        self.chk_auto_refresh.toggled.connect(self._toggle_auto_refresh)
        filter_bar.addWidget(self.chk_auto_refresh)

        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._fetch_servers)
        filter_bar.addWidget(self.btn_refresh)

        layout.addLayout(filter_bar)

        # ─── Servers Table (Sharp Monospace Terminal Grid) ───
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Players", "Max", "Ping", "FPS", "Job ID"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 95)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 80)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.doubleClicked.connect(lambda: self._on_choose_server(auto_join=True))
        layout.addWidget(self.table, stretch=1)

        # ─── Bottom Actions Bar (Clean Text, No Emojis) ───
        bot_bar = QtWidgets.QHBoxLayout()
        bot_bar.setSpacing(10)

        self.status_label = QtWidgets.QLabel("Ready. Select a server row to inspect or copy.")
        self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
        bot_bar.addWidget(self.status_label, stretch=1)

        self.btn_copy_job = QtWidgets.QPushButton("Copy Job ID")
        self.btn_copy_job.clicked.connect(self._copy_selected_job_id)
        bot_bar.addWidget(self.btn_copy_job)

        self.btn_copy_teleport = QtWidgets.QPushButton("Copy Script")
        self.btn_copy_teleport.setToolTip("Copies TeleportService Luau script for this server instance")
        self.btn_copy_teleport.clicked.connect(self._copy_teleport_script)
        bot_bar.addWidget(self.btn_copy_teleport)

        self.btn_select = QtWidgets.QPushButton("Select Server")
        self.btn_select.setToolTip("Set Place ID and Job ID in AERAM without launching immediately")
        self.btn_select.clicked.connect(lambda: self._on_choose_server(auto_join=False))
        bot_bar.addWidget(self.btn_select)

        self.btn_join = QtWidgets.QPushButton("Launch Server")
        self.btn_join.setObjectName("primaryBtn")
        self.btn_join.setToolTip("Launch selected server (or route via Lobby if sub-place)")
        self.btn_join.clicked.connect(lambda: self._on_choose_server(auto_join=True))
        bot_bar.addWidget(self.btn_join)

        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        bot_bar.addWidget(self.btn_close)

        layout.addLayout(bot_bar)

    def closeEvent(self, event):
        self._refresh_timer.stop()
        super().closeEvent(event)

    def _toggle_auto_refresh(self, enabled: bool):
        if enabled:
            self._refresh_timer.start()
            self.status_label.setText("Auto-refresh active (updates every 30 seconds).")
        else:
            self._refresh_timer.stop()
            self.status_label.setText("Auto-refresh stopped.")

    def _get_active_cookie(self) -> Optional[str]:
        if self.selected_accounts and self.selected_accounts[0].cookie:
            return self.selected_accounts[0].cookie
        accounts = self.storage.list_accounts()
        if accounts and accounts[0].cookie:
            return accounts[0].cookie
        return None

    def _on_sub_place_changed(self, index: int):
        data = self.sub_place_combo.currentData()
        if data and data != self.current_place_id:
            self.place_input.setText(str(data))
            self.current_place_id = int(data)
            self._fetch_servers(skip_subplaces=True)

    def _on_search_clicked(self):
        text = self.place_input.text().strip()
        if not text.isdigit():
            QMessageBox.warning(self, "Invalid Place ID", "Please enter a valid numeric Place ID.")
            return
        self.current_place_id = int(text)
        self._fetch_servers()

    def _fetch_servers(self, skip_subplaces: bool = False):
        self.status_label.setText(f"Monitoring live servers for Place {self.current_place_id}...")
        self.btn_fetch.setEnabled(False)
        self.btn_refresh.setEnabled(False)

        cookie = self._get_active_cookie()
        self._worker = ServerFetchWorker(
            api=self.api,
            place_id=self.current_place_id,
            cookie=cookie,
            sort_order="Asc",
            limit=100,
        )
        self._worker.servers_loaded.connect(self._on_servers_loaded)
        if not skip_subplaces:
            self._worker.sub_places_loaded.connect(self._on_sub_places_loaded)
        self._worker.error_occurred.connect(self._on_fetch_error)
        self._worker.finished.connect(lambda: [
            self.btn_fetch.setEnabled(True),
            self.btn_refresh.setEnabled(True)
        ])
        self._worker.start()

    def _on_sub_places_loaded(self, sub_places: List[tuple]):
        self.sub_place_combo.blockSignals(True)
        self.sub_place_combo.clear()
        found_current = False
        for name, pid in sub_places:
            clean_name = strip_emojis(name)
            self.sub_place_combo.addItem(f"{clean_name} [{pid}]", pid)
            if pid == self.current_place_id:
                self.sub_place_combo.setCurrentIndex(self.sub_place_combo.count() - 1)
                found_current = True
        if not found_current:
            self.sub_place_combo.insertItem(0, f"Current Place [{self.current_place_id}]", self.current_place_id)
            self.sub_place_combo.setCurrentIndex(0)
        self.sub_place_combo.blockSignals(False)

    def _on_servers_loaded(self, servers: List[Dict[str, Any]], place_name: str, root_place_id: Optional[int] = None):
        self.raw_servers = servers
        self.root_place_id = root_place_id
        self.stat_last_updated.setText(f"Updated: {time.strftime('%H:%M:%S')}")
        self._apply_filters_and_render()

    def _on_fetch_error(self, err_msg: str):
        self.status_label.setStyleSheet("color: #f48771; font-size: 11px;")
        self.status_label.setText(f"Error fetching servers: {err_msg}")

    def _apply_filters_and_render(self):
        if not self.raw_servers:
            self.table.setRowCount(0)
            self.stat_total_servers.setText("Active Servers: 0")
            self.stat_min_players.setText("Lowest Pop: --")
            self.stat_best_ping.setText("Best Latency: --")
            self.status_label.setText("No public servers currently found for this place.")
            return

        # Update stats banner
        total_count = len(self.raw_servers)
        player_counts = [s.get("playing", 0) for s in self.raw_servers]
        min_players = min(player_counts) if player_counts else 0
        valid_pings = [s.get("ping") for s in self.raw_servers if s.get("ping") is not None]
        best_ping = min(valid_pings) if valid_pings else None

        self.stat_total_servers.setText(f"Active Servers: {total_count}")
        self.stat_min_players.setText(f"Lowest Pop: {min_players} player{'s' if min_players != 1 else ''}")
        self.stat_best_ping.setText(f"Best Latency: {best_ping} ms" if best_ping is not None else "Best Latency: --")

        sort_mode = self.sort_combo.currentData()
        max_limit = self.max_players_combo.currentData() or 999

        # Filter
        filtered = [s for s in self.raw_servers if s.get("playing", 0) <= max_limit]

        # Sort
        if sort_mode == "asc_players":
            filtered.sort(key=lambda s: (s.get("playing", 0), s.get("ping") or 999))
        elif sort_mode == "asc_ping":
            filtered.sort(key=lambda s: (s.get("ping") or 999, s.get("playing", 0)))
        elif sort_mode == "desc_players":
            filtered.sort(key=lambda s: s.get("playing", 0), reverse=True)

        self.table.setRowCount(len(filtered))
        for row, s in enumerate(filtered):
            playing = s.get("playing", 0)
            max_pl = s.get("maxPlayers", 0)
            ping = s.get("ping")
            fps = s.get("fps", 0)
            job_id = s.get("id", "")

            # Players column
            item_pl = QtWidgets.QTableWidgetItem(f"{playing} / {max_pl}")
            item_pl.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if playing <= 3:
                item_pl.setForeground(QtGui.QColor("#FFFFFF"))
                item_pl.setToolTip("Quiet / low-population server")
            elif playing <= 8:
                item_pl.setForeground(QtGui.QColor("#E8E8E8"))
            else:
                item_pl.setForeground(QtGui.QColor("#A8A8A8"))
            self.table.setItem(row, 0, item_pl)

            # Max Players
            item_max = QtWidgets.QTableWidgetItem(f"{max_pl}")
            item_max.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_max.setForeground(QtGui.QColor("#707070"))
            self.table.setItem(row, 1, item_max)

            # Ping
            ping_str = f"{ping} ms" if ping is not None else "--"
            item_png = QtWidgets.QTableWidgetItem(ping_str)
            item_png.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if ping is not None:
                if ping < 80:
                    item_png.setForeground(QtGui.QColor("#FFFFFF"))
                elif ping < 160:
                    item_png.setForeground(QtGui.QColor("#E8E8E8"))
                else:
                    item_png.setForeground(QtGui.QColor("#707070"))
            self.table.setItem(row, 2, item_png)

            # FPS
            fps_str = f"{fps:.1f}" if fps else "--"
            item_fps = QtWidgets.QTableWidgetItem(fps_str)
            item_fps.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_fps.setForeground(QtGui.QColor("#A8A8A8"))
            self.table.setItem(row, 3, item_fps)

            # Job ID
            item_id = QtWidgets.QTableWidgetItem(job_id)
            item_id.setData(Qt.ItemDataRole.UserRole, s)
            self.table.setItem(row, 4, item_id)

        count = len(filtered)
        self.status_label.setStyleSheet("color: #E8E8E8; font-size: 11px;")
        self.status_label.setText(f"Showing {count} live servers. Select a server to inspect or copy Job ID.")

    def _get_selected_server(self) -> Optional[Dict[str, Any]]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 4)
        if not item:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_table_selection_changed(self):
        server = self._get_selected_server()
        if server:
            job_id = server.get("id", "")
            players = server.get("playing", 0)
            ping = server.get("ping")
            ping_text = f"{ping}ms" if ping is not None else "n/a"
            self.status_label.setStyleSheet("color: #cccccc; font-size: 11px;")
            self.status_label.setText(f"Selected: {players} players, {ping_text} ping | Job ID: {job_id}")

    def _copy_selected_job_id(self):
        server = self._get_selected_server()
        if not server:
            self.status_label.setText("Please select a server row in the table first.")
            return
        job_id = server.get("id", "")
        QtWidgets.QApplication.clipboard().setText(job_id)
        self.status_label.setText(f"Copied Job ID: {job_id}")

    def _copy_teleport_script(self):
        server = self._get_selected_server()
        if not server:
            self.status_label.setText("Please select a server row in the table first.")
            return
        job_id = server.get("id", "")
        code = f'game:GetService("TeleportService"):TeleportToPlaceInstance({self.current_place_id}, "{job_id}", game.Players.LocalPlayer)'
        QtWidgets.QApplication.clipboard().setText(code)
        self.status_label.setText(f"Copied Luau Teleport script for Job ID {job_id[:8]}...")

    def _on_choose_server(self, auto_join: bool = True):
        server = self._get_selected_server()
        if not server:
            self.status_label.setText("Please select a server row in the table first.")
            return

        job_id = server.get("id", "")
        target_place = self.current_place_id
        target_job = job_id
        if self.root_place_id and self.root_place_id != self.current_place_id:
            # Sub-place direct external joins trigger Roblox status 12 ('Cannot join this non-root place due to join restrictions')
            # which hangs the mobile client on an infinite loading screen.
            # We automatically route via universe root place (Lobby) to prevent client freeze.
            target_place = self.root_place_id
            target_job = ""
            QtWidgets.QApplication.clipboard().setText(job_id)

        self.server_selected.emit(target_place, target_job, auto_join)
        self.accept()


# Alias for backward compatibility
ServerMonitorDialog = ServerFinderDialog
