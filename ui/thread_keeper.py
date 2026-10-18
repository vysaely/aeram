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
