# AERAM — Linux Functional Parity Report & Regression Audit

**Document Version:** 1.0.0  
**Repository:** `https://github.com/vysaely/aeram.git`  
**Evaluation Baselines:**
1. **Original Linux Baseline:** Commit `f43ffc2` (Tag `v0.1.0-linux`, Milestone `v2.0-perfected-runtime`)
2. **Current Linux State:** Commit `93cd072` (`origin/main`, tested on Linux 6.x host)
3. **Windows 11 Target:** Commit `93cd072` (Win32 architecture and packaging)

---

## 1. Executive Parity Matrix

The following matrix compares actual observable capabilities across the original Linux-first implementation, the current Linux state under the ported codebase, and the native Windows 11 target.

| Capability | Original Linux (`f43ffc2`) | Current Linux (`origin/main`) | Windows 11 (`origin/main`) | Recommendation |
|---|---|---|---|---|
| **Account Management** | **Verified Functional** (SQLite WAL, CRUD, group ordering) | **Verified Functional** (Preserved + added auth state badges) | **Verified Functional** (Shared DB schema) | **KEEP** current improved state. |
| **Session Persistence** | **Verified Functional** (Encrypted `.ROBLOSECURITY`, `0600` files) | **Verified Functional** (Added expiration isolation, no auto-delete) | **Verified Functional** (DPAPI vault, but shared disk cookie file) | **KEEP** current improved state. |
| **Multi-Instance Launching** | **Verified Functional** (Uncapped 50+ instances, reflink/symlink) | **Verified Functional** (Identical Mocktail launch path) | **Verified with Caveats** (Named mutex pre-emption; disk contention) | **PRESERVE** Linux container runner. |
| **Account Isolation** | **Verified Perfect** (Private mounts, private cookies, XDG dirs) | **Verified Perfect** (Unchanged on Linux) | **Degraded** (Single shared `%LOCALAPPDATA%` cookie DB) | **MAINTAIN** Linux as architectural gold standard. |
| **FPS Control** | **Verified Functional** (330 FPS / Unlimited 10000 ceiling) | **Verified Functional** (Unchanged on Linux) | **Verified Functional** (XML `GlobalBasicSettings_13.xml` parser) | **KEEP** Linux env/ClientAppSettings mechanism. |
| **Process Supervision** | **Verified Functional** (`/proc` UID scan, cgroups `MemoryHigh`) | **Verified Functional** (UID `/proc` scan + launch locks) | **Verified Functional** (`Toolhelp32` + Win32 `OpenProcess`) | **KEEP** Linux cgroups & UID scan. |
| **Browser Login** | **Not Present** (Manual cookie entry & RAM JSON import only) | **REGRESSED / DEFECTIVE** (`PyQt6-WebEngine` missing; AttributeError in fallback) | **Functional** (Tested in Windows PyInstaller bundle) | **ADAPT** with strict optional gating or external helper. |
| **Encrypted Storage** | **Verified Functional** (libsodium Argon2id + XSalsa20, 0600) | **Verified Functional** (Added PyNaCl fallback) | **Verified Functional** (DPAPI key storage + libsodium) | **KEEP** Linux libsodium/PyNaCl backend. |
| **Import / Export** | **Verified Functional** (RAM JSON import, plaintext cookie list) | **Verified Functional** (Added `AERAM_VAULT_EXPORT_V1` encrypted archive) | **Verified Functional** (Cross-platform export/import) | **KEEP** encrypted migration engine. |
| **Packaging & CI** | **Verified Functional** (`install.sh`, .desktop launcher) | **Verified Functional** (Linux packaging unchanged) | **Verified Functional** (PyInstaller spec, Inno Setup, GitHub Actions) | **DISCARD / ARCHIVE** Windows packaging. |

---

## 2. Linux Test Suite Execution & Regression Audit

### 2.1 Original Linux Baseline (`f43ffc2`)
- **Execution Command:** `python3 -m unittest discover -s tests -v`
- **Total Tests Discovered:** 31
- **Passed:** 31 (100%)
- **Failed / Errors:** 0
- **Execution Duration:** 2.074s
- **Observations:** Clean execution of all CLI, GUI table ordering, RAM importer, instance runner, self-heal, storage, and vault cryptographic tests.

### 2.2 Current Linux Working Tree (`origin/main` / `93cd072`)
- **Execution Command:** `python3 -m unittest discover -s tests -v`
- **Total Tests Discovered:** 41
- **Passed:** 37
- **Failed / Errors:** 3
- **Skipped:** 1 (`test_packaged_build.TestPackagedBuild` - skipped cleanly on non-win32)
- **Execution Duration:** 2.252s

#### Root Cause Analysis of the 3 Test Failures on Linux:

#### Defect 1: `test_auth_lifecycle` (ImportError: No module named 'pytest')
- **File:** `tests/test_auth_lifecycle.py:6`
- **Failure:**
  ```
  ImportError: Failed to import test module: test_auth_lifecycle
  Traceback (most recent call last):
    File ".../tests/test_auth_lifecycle.py", line 6, in <module>
      import pytest
  ModuleNotFoundError: No module named 'pytest'
  ```
- **Analysis:** The test author wrote `test_auth_lifecycle.py` targeting `pytest` instead of Python standard library `unittest.TestCase`. Because AERAM uses `unittest discover`, this breaks test suite discovery on clean developer environments without external test runners installed.
- **Remediation:** Refactor `test_auth_lifecycle.py` into standard `unittest.TestCase` without pytest dependencies.

#### Defect 2: `test_web_login_dialog_lifecycle` (AttributeError: 'status_label')
- **File:** `tests/test_gui.py:205` / `ui/web_login_dialog.py:360`
- **Failure:**
  ```
  Traceback (most recent call last):
    File ".../tests/test_gui.py", line 205, in test_web_login_dialog_lifecycle
      dlg._process_detected_cookie(fake_cookie)
    File ".../ui/web_login_dialog.py", line 360, in _process_detected_cookie
      self.status_label.setStyleSheet(...)
  AttributeError: 'WebLoginDialog' object has no attribute 'status_label'
  ```
- **Analysis:** On Linux without `PyQt6-WebEngine`, `WebLoginDialog` renders `_init_fallback_ui`. However, `_init_fallback_ui` fails to instantiate `self.status_label`. When any cookie processing or status update executes, an unhandled `AttributeError` crashes the application.
- **Remediation:** Ensure `self.status_label` is instantiated unconditionally in `_init_ui` prior to engine branching.

#### Defect 3: `test_windows_native` (ModuleNotFoundError: No module named 'winreg')
- **File:** `tests/test_windows_native.py:18`
- **Failure:**
  ```
  ImportError: Failed to import test module: test_windows_native
  Traceback (most recent call last):
    File ".../tests/test_windows_native.py", line 18, in <module>
      from core.platform.windows import WindowsPlatformProvider
    File ".../core/platform/windows.py", line 11, in <module>
      import winreg
  ModuleNotFoundError: No module named 'winreg'
  ```
- **Analysis:** `test_windows_native.py` contains `setUpClass` skipping non-win32 platforms, but imports `core.platform.windows` at top-level. `core/platform/windows.py` imports `winreg` and calls `ctypes.windll`, which fail immediately on Linux during test collection.
- **Remediation:** Move Windows-only imports inside `setUpClass` or gate top-level imports behind `if sys.platform == "win32":`.

---

## 3. Structural & Architectural Regressions

### 3.1 Asymmetric Platform Abstraction
The creation of `core/platform/` was intended to provide a symmetric abstraction between Linux and Windows. In reality:
- `core/platform/linux.py` is an incomplete stub: `launch_client` contains only `pass`.
- `core/instance_runner.py` retained 300+ lines of hardcoded Linux Mocktail logic under `if sys.platform != "win32"`.
- Windows launch logic routes through `get_platform().launch_client()`, while Linux launch logic bypasses the platform provider entirely.
- Result: An abstraction layer that adds indirection without delivering separation of concerns.

### 3.2 Dependency Footprint Expansion
The addition of `PyQt6-WebEngine` introduces:
- A massive binary footprint (~150MB+ download, ~350MB uncompressed).
- OpenGL context sharing constraints (`AA_ShareOpenGLContexts`) set in `core/entrypoint.py`, which can interfere with native Wayland/OpenGL hardware surfaces on Linux.
- Fragility on Linux distributions where Qt6 WebEngine is packaged separately from PyQt6 (`python-pyqt6-webengine`).

---

## 4. Linux Verification Status Summary

| Area | Verified Passing | Regressed / Defective | Unverified |
|---|---|---|---|
| Core SQLite Storage & Migrations | Yes (7/7 tests) | None | None |
| Cryptographic Vault (libsodium / PyNaCl) | Yes (5/5 tests) | None | None |
| Encrypted Export/Import (`.aeram`) | Yes (3/3 tests) | None | None |
| Mocktail Instance Runner & Cgroups | Yes (7/7 tests) | None | None |
| Self-Healing Watchdog Engine | Yes (5/5 tests) | None | None |
| Headless CLI & RAM JSON Importer | Yes (5/5 tests) | None | None |
| GUI Core Reordering & Account Table | Yes (2/2 tests) | None | None |
| Auth Lifecycle State Machine | Functional in core | Blocked in test (`pytest` import) | None |
| Browser Web Login Dialog | None | Defective (`AttributeError`) | None |
| Windows Platform & Settings | N/A (Windows) | Blocked on Linux (`winreg`) | Verified on Win32 |

---

## 5. Required Linux Remediation Gates

Before AERAM can be certified as fully stable in a Linux-first architecture, the following remediation gates must be satisfied:

1. **Gate 1 — Test Discovery Cleared:**
   Standard `python3 -m unittest discover -s tests -v` must execute with 0 errors and 0 unhandled import exceptions on Linux.
2. **Gate 2 — Test Portability Restored:**
   `test_auth_lifecycle.py` must run under standard `unittest` without requiring `pytest`.
3. **Gate 3 — Web Login Gated & Fixed:**
   `WebLoginDialog` must function without `AttributeError` in headless or fallback environments, and tests must pass without `PyQt6-WebEngine`.
4. **Gate 4 — Architectural Symmetry:**
   Either eliminate `core/platform/` and restore clean, direct Linux execution in `instance_runner.py`, or fully populate `LinuxPlatformProvider` so that all Linux launch code is cleanly encapsulated.
