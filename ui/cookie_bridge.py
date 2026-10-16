"""
Thread-Safe External Browser Cookie Bridge for AERAM.
Dispatches session cookies captured in background watcher threads safely to the Qt main thread
using Qt's queued signal/slot mechanism to avoid cross-thread GUI mutation segfaults.
"""

from typing import Callable, Optional
from PyQt6 import QtCore


class CookieSyncBridge(QtCore.QObject):
    """
    Qt QObject bridge that receives cookie updates from a background thread
    and dispatches them to a callback running on the main Qt GUI thread.
    """
    cookie_received = QtCore.pyqtSignal(str)

    def __init__(self, callback: Optional[Callable[[str], None]] = None, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        if callback:
            self.cookie_received.connect(callback)

    def emit_cookie(self, cookie: str):
        """Thread-safe slot emitter callable from any background thread."""
        self.cookie_received.emit(cookie)
