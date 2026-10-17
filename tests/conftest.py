"""
Test-session isolation.

Several code paths resolve real user locations at call time (Path.home(),
XDG_RUNTIME_DIR, the Mocktail install found on PATH). Without isolation the
suite hardlinks binaries into the developer's real ~/.local/lib/mocktail and
opens the live ~/.local/share/aeram vault. Point every such location at a
throwaway directory before any project module is imported.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_real_home = os.path.realpath(os.path.expanduser("~"))
_sandbox = tempfile.mkdtemp(prefix="aeram-test-home-")
atexit.register(shutil.rmtree, _sandbox, ignore_errors=True)

os.environ["HOME"] = _sandbox
for _var, _sub in (
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_CACHE_HOME", ".cache"),
    ("XDG_STATE_HOME", ".local/state"),
    ("XDG_RUNTIME_DIR", "run"),
):
    _path = Path(_sandbox) / _sub
    _path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ[_var] = str(_path)

# Drop PATH entries inside the real home so a user-local Mocktail is never detected.
os.environ["PATH"] = os.pathsep.join(
    p for p in os.environ.get("PATH", "").split(os.pathsep)
    if p and not os.path.realpath(p).startswith(_real_home + os.sep)
)

# Never talk to the developer's desktop session bus or display.
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
