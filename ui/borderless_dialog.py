"""
Achromatic Minimalist Borderless Dialog Base Class.
Provides seamless frameless window behavior, custom draggable title bar,
high-contrast monochrome styling, and native window dragging/resizing.
"""

from typing import Optional
from PyQt6 import QtWidgets, QtCore
from ui.theme import DARK_THEME_QSS


class BorderlessDialog(QtWidgets.QDialog):
    """
    Base dialog for all secondary windows in AERAM.
    Enforces a borderless (FramelessWindowHint) presentation with a custom
    dark title bar, dragging, resizing, and keyboard handling (Esc to close).
    """

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        title: str = "AERAM",
        can_maximize: bool = False,
        resizable: bool = True,
    ):
        super().__init__(parent)
        self._can_maximize = can_maximize
        self._resizable = resizable
        self._dragging = False
        self._drag_start_pos = None

        # Seamless frameless dialog (non-modal so the main interface remains fully interactive)
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Dialog)
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.setMouseTracking(True)
        self.setStyleSheet(DARK_THEME_QSS)

        # Root layout for QDialog (zero margins so container covers entire window)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Central container frame with hairline border (matches MainWindow ramCentral)
        self.container = QtWidgets.QFrame(self)
        self.container.setObjectName("ramCentral")
        root.addWidget(self.container)

        container_layout = QtWidgets.QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # ─── Custom Top Header Bar ───
        self.top_header_frame = QtWidgets.QFrame(self.container)
        self.top_header_frame.setObjectName("top_header_frame")
        self.top_header_frame.setFixedHeight(34)

        header_layout = QtWidgets.QHBoxLayout(self.top_header_frame)
        header_layout.setContentsMargins(12, 0, 8, 0)
        header_layout.setSpacing(8)
        header_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.lbl_dialog_title = QtWidgets.QLabel(self._format_title(title))
        self.lbl_dialog_title.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.lbl_dialog_title.setStyleSheet("color: #E8E8E8; font-weight: bold; font-size: 10px; letter-spacing: 1.5px;")
        header_layout.addWidget(self.lbl_dialog_title)

        header_layout.addStretch()

        if self._can_maximize:
            self.btn_dialog_max = QtWidgets.QPushButton("□")
            self.btn_dialog_max.setToolTip("Maximize / Restore")
            self.btn_dialog_max.setFixedSize(24, 22)
            self.btn_dialog_max.setStyleSheet(
                "QPushButton { border: 1px solid #353535; font-size: 10px; padding: 0px; background-color: #000000; color: #A8A8A8; } "
                "QPushButton:hover { background-color: #E8E8E8; color: #000000; border-color: #E8E8E8; }"
            )
            self.btn_dialog_max.clicked.connect(self.toggle_maximize)
            header_layout.addWidget(self.btn_dialog_max)

        self.btn_dialog_close = QtWidgets.QPushButton("✕")
        self.btn_dialog_close.setToolTip("Close (Esc)")
        self.btn_dialog_close.setFixedSize(24, 22)
        self.btn_dialog_close.setStyleSheet(
            "QPushButton { border: 1px solid #353535; font-size: 10px; padding: 0px; background-color: #000000; color: #A8A8A8; } "
            "QPushButton:hover { background-color: #E8E8E8; color: #000000; border-color: #E8E8E8; }"
        )
        self.btn_dialog_close.clicked.connect(self.reject)
        header_layout.addWidget(self.btn_dialog_close)

        container_layout.addWidget(self.top_header_frame)

        # ─── Content Widget & Layout for Subclasses ───
        self.content_widget = QtWidgets.QWidget(self.container)
        self.body_layout = QtWidgets.QVBoxLayout(self.content_widget)
        self.body_layout.setContentsMargins(16, 14, 16, 14)
        self.body_layout.setSpacing(10)
        container_layout.addWidget(self.content_widget, stretch=1)

    def exec(self) -> int:
        """
        Executes the dialog without blocking interaction with the main interface.
        Keeps the window modality as NonModal so the user can interact freely with
        the main window and other popup windows simultaneously.
        """
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.show()
        self.raise_()
        self.activateWindow()

        loop = QtCore.QEventLoop()

        def _on_finish(*args):
            if loop.isRunning():
                loop.quit()

        self.finished.connect(_on_finish)
        self.destroyed.connect(_on_finish)
        loop.exec()

        try:
            self.finished.disconnect(_on_finish)
        except Exception:
            pass
        try:
            self.destroyed.disconnect(_on_finish)
        except Exception:
            pass

        return self.result()

    exec_ = exec

    def _format_title(self, raw_title: str) -> str:
        clean = raw_title.replace(" - AERAM", "").replace(" — AERAM", "").strip()
        return f"// {clean.upper()}"

    def setWindowTitle(self, title: str) -> None:
        super().setWindowTitle(title)
        if hasattr(self, "lbl_dialog_title") and self.lbl_dialog_title:
            self.lbl_dialog_title.setText(self._format_title(title))

    def toggle_maximize(self):
        if not self._can_maximize:
            return
        if self.isMaximized():
            self.showNormal()
            if hasattr(self, "btn_dialog_max"):
                self.btn_dialog_max.setText("□")
        else:
            self.showMaximized()
            if hasattr(self, "btn_dialog_max"):
                self.btn_dialog_max.setText("[+]")

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and event.position().y() <= 34:
            if self._can_maximize:
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

            if self._resizable and (is_left or is_right or is_top or is_bottom):
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

            if pos.y() <= 34:
                if self.windowHandle():
                    try:
                        self.windowHandle().startSystemMove()
                        return
                    except Exception:
                        pass
                self._dragging = True
                self._drag_start_pos = event.globalPosition().toPoint() - self.pos()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_dragging", False):
            self.move(event.globalPosition().toPoint() - self._drag_start_pos)
            return

        if not self._resizable:
            super().mouseMoveEvent(event)
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

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)


class BorderlessInputDialog(BorderlessDialog):
    """
    Drop-in replacement for QInputDialog that is fully borderless, frameless,
    and matches AERAM's achromatic pitch-black theme.
    """

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        title: str = "AERAM",
        label: str = "",
        text: str = "",
        is_password: bool = False,
        is_int: bool = False,
        int_value: int = 0,
        int_min: int = 1,
        int_max: int = 9999,
    ):
        super().__init__(parent, title=title, can_maximize=False, resizable=False)
        self.setFixedWidth(460)

        self.lbl_prompt = QtWidgets.QLabel(label)
        self.lbl_prompt.setWordWrap(True)
        self.lbl_prompt.setStyleSheet("color: #dcdcdc; font-size: 11px; line-height: 1.4;")
        self.body_layout.addWidget(self.lbl_prompt)

        self._is_int = is_int
        if is_int:
            self.spin_box = QtWidgets.QSpinBox()
            self.spin_box.setRange(int_min, int_max)
            self.spin_box.setValue(int_value)
            self.spin_box.setFixedHeight(30)
            self.body_layout.addWidget(self.spin_box)
            self.input_field = self.spin_box
        else:
            self.line_edit = QtWidgets.QLineEdit()
            self.line_edit.setText(text)
            self.line_edit.setFixedHeight(30)
            if is_password:
                self.line_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            self.line_edit.returnPressed.connect(self.accept)
            self.body_layout.addWidget(self.line_edit)
            self.input_field = self.line_edit

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)

        self.btn_ok = QtWidgets.QPushButton("OK")
        self.btn_ok.setObjectName("primaryBtn")
        self.btn_ok.setFixedHeight(28)
        self.btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_ok)

        self.body_layout.addLayout(btn_row)
        QtCore.QTimer.singleShot(0, self.input_field.setFocus)

    @classmethod
    def getText(
        cls,
        parent: Optional[QtWidgets.QWidget],
        title: str,
        label: str,
        echo: QtWidgets.QLineEdit.EchoMode = QtWidgets.QLineEdit.EchoMode.Normal,
        text: str = "",
    ) -> tuple[str, bool]:
        dlg = cls(
            parent=parent,
            title=title,
            label=label,
            text=text,
            is_password=(echo == QtWidgets.QLineEdit.EchoMode.Password),
        )
        accepted = (dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted)
        return (dlg.line_edit.text(), accepted)

    @classmethod
    def getInt(
        cls,
        parent: Optional[QtWidgets.QWidget],
        title: str,
        label: str,
        value: int = 0,
        minValue: int = 1,
        maxValue: int = 9999,
        step: int = 1,
    ) -> tuple[int, bool]:
        dlg = cls(
            parent=parent,
            title=title,
            label=label,
            is_int=True,
            int_value=value,
            int_min=minValue,
            int_max=maxValue,
        )
        accepted = (dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted)
        return (dlg.spin_box.value(), accepted)


class BorderlessMessageBox(BorderlessDialog):
    """
    Drop-in replacement for QMessageBox that is fully borderless, frameless,
    and matches AERAM's achromatic pitch-black theme.
    """
    StandardButton = QtWidgets.QMessageBox.StandardButton

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        title: str = "AERAM",
        text: str = "",
        buttons: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.StandardButton.Ok,
        default_button: Optional[QtWidgets.QMessageBox.StandardButton] = None,
    ):
        super().__init__(parent, title=title, can_maximize=False, resizable=False)
        self.setFixedWidth(440)
        self._buttons = buttons
        self._result_button = QtWidgets.QMessageBox.StandardButton.NoButton

        self.lbl_text = QtWidgets.QLabel(text)
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_text.setStyleSheet("color: #e0e0e0; font-size: 11px; line-height: 1.4;")
        self.body_layout.addWidget(self.lbl_text)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        btn_flags = int(buttons)

        if btn_flags & int(QtWidgets.QMessageBox.StandardButton.No):
            self.btn_no = QtWidgets.QPushButton("No")
            self.btn_no.setFixedHeight(28)
            self.btn_no.clicked.connect(lambda: self._choose(QtWidgets.QMessageBox.StandardButton.No))
            btn_row.addWidget(self.btn_no)

        if btn_flags & int(QtWidgets.QMessageBox.StandardButton.Cancel):
            self.btn_cancel = QtWidgets.QPushButton("Cancel")
            self.btn_cancel.setFixedHeight(28)
            self.btn_cancel.clicked.connect(lambda: self._choose(QtWidgets.QMessageBox.StandardButton.Cancel))
            btn_row.addWidget(self.btn_cancel)

        if btn_flags & int(QtWidgets.QMessageBox.StandardButton.Yes):
            self.btn_yes = QtWidgets.QPushButton("Yes")
            self.btn_yes.setObjectName("primaryBtn")
            self.btn_yes.setFixedHeight(28)
            self.btn_yes.clicked.connect(lambda: self._choose(QtWidgets.QMessageBox.StandardButton.Yes))
            btn_row.addWidget(self.btn_yes)

        if btn_flags & int(QtWidgets.QMessageBox.StandardButton.Ok):
            self.btn_ok = QtWidgets.QPushButton("OK")
            self.btn_ok.setObjectName("primaryBtn")
            self.btn_ok.setFixedHeight(28)
            self.btn_ok.clicked.connect(lambda: self._choose(QtWidgets.QMessageBox.StandardButton.Ok))
            btn_row.addWidget(self.btn_ok)

        self.body_layout.addLayout(btn_row)

    def _choose(self, btn: QtWidgets.QMessageBox.StandardButton):
        self._result_button = btn
        self.accept()

    def reject(self):
        btn_flags = int(self._buttons)
        if btn_flags & int(QtWidgets.QMessageBox.StandardButton.Cancel):
            self._result_button = QtWidgets.QMessageBox.StandardButton.Cancel
        elif btn_flags & int(QtWidgets.QMessageBox.StandardButton.No):
            self._result_button = QtWidgets.QMessageBox.StandardButton.No
        else:
            self._result_button = QtWidgets.QMessageBox.StandardButton.Ok
        super().reject()

    @classmethod
    def information(cls, parent: Optional[QtWidgets.QWidget], title: str, text: str) -> QtWidgets.QMessageBox.StandardButton:
        dlg = cls(parent, title=title, text=text, buttons=QtWidgets.QMessageBox.StandardButton.Ok)
        dlg.exec()
        return QtWidgets.QMessageBox.StandardButton.Ok

    @classmethod
    def warning(
        cls,
        parent: Optional[QtWidgets.QWidget],
        title: str,
        text: str,
        buttons: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.StandardButton.Ok,
        default_button: Optional[QtWidgets.QMessageBox.StandardButton] = None,
    ) -> QtWidgets.QMessageBox.StandardButton:
        dlg = cls(parent, title=title, text=text, buttons=buttons, default_button=default_button)
        dlg.exec()
        return dlg._result_button or QtWidgets.QMessageBox.StandardButton.Ok

    @classmethod
    def critical(cls, parent: Optional[QtWidgets.QWidget], title: str, text: str) -> QtWidgets.QMessageBox.StandardButton:
        dlg = cls(parent, title=title, text=text, buttons=QtWidgets.QMessageBox.StandardButton.Ok)
        dlg.exec()
        return QtWidgets.QMessageBox.StandardButton.Ok

    @classmethod
    def question(
        cls,
        parent: Optional[QtWidgets.QWidget],
        title: str,
        text: str,
        buttons: QtWidgets.QMessageBox.StandardButton = (QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No),
        default_button: Optional[QtWidgets.QMessageBox.StandardButton] = None,
    ) -> QtWidgets.QMessageBox.StandardButton:
        dlg = cls(parent, title=title, text=text, buttons=buttons, default_button=default_button)
        dlg.exec()
        return dlg._result_button or QtWidgets.QMessageBox.StandardButton.No
