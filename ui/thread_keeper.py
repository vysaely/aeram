"""
Keeps finishing QThreads alive and frees closed windows.

Qt aborts the whole process when a QThread object is destroyed while its thread
is still running, which happens when a window holding a busy worker is deleted
or a worker reference is replaced mid-request. Threads handed to retire_thread()
are detached from their owner and kept referenced until they finish.
"""

from PyQt6 import QtCore

_retired = set()


def retire_thread(thread) -> None:
    """Lets a (possibly running) QThread finish on its own, then releases it."""
    if thread is None:
        return
    try:
        if not thread.isRunning():
            return
        thread.setParent(None)
    except RuntimeError:  # C++ object already gone
        return
    _retired.add(thread)
    # Release on the next loop pass, not inside the thread's own finished signal.
    thread.finished.connect(lambda t=thread: QtCore.QTimer.singleShot(0, lambda: _retired.discard(t)))


def dispose_window(window) -> None:
    """Deletes a closed window; its running workers are retired instead of destroyed."""
    try:
        threads = set(window.findChildren(QtCore.QThread))
        threads.update(v for v in vars(window).values() if isinstance(v, QtCore.QThread))
        for thread in threads:
            retire_thread(thread)
        # A QWebEnginePage must go before the profile that owns its storage.
        for name in ("page", "view"):
            child = getattr(window, name, None)
            if isinstance(child, QtCore.QObject):
                child.deleteLater()
        window.deleteLater()
    except RuntimeError:
        pass


class _CallWorker(QtCore.QThread):
    result = QtCore.pyqtSignal(object)

    def __init__(self, fn, parent):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            value = self._fn()
        except Exception as e:  # handed to the callback like a normal result
            value = e
        self.result.emit(value)


def run_in_thread(owner: QtCore.QObject, fn, on_done) -> QtCore.QThread:
    """Runs fn() off the GUI thread and calls on_done(result) on it.

    on_done should be a method of owner: if owner is deleted first, Qt drops the
    connection and the result is discarded. An exception is passed as the result.
    """
    worker = _CallWorker(fn, owner)
    worker.result.connect(on_done)
    worker.finished.connect(worker.deleteLater)
    worker.start()
    return worker
