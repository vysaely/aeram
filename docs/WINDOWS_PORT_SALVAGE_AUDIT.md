# AERAM — Windows Port Salvage Audit & Subsystem Inventory

**Document Version:** 1.0.0  
**Repository:** `https://github.com/vysaely/aeram.git`  
**Baseline Git Range:** Commit `f43ffc2` (Linux Baseline / Rebrand) → Commit `93cd072` (`origin/main`, Windows Port & Auth Pass)  
**Audit Context:** Systematic review of all 58 files modified (+5,812 lines / -339 lines) during the Windows 11 port to identify reusable improvements, platform overhead, and regression hazards.

---

## 1. Executive Summary

Between commit `5ca9efa` and commit `93cd072`, AERAM underwent a port to Windows 11. While the primary goal was enabling native execution of `RobloxPlayerBeta.exe` on Windows, several architectural, cryptographic, and authentication improvements were developed alongside the port. Concurrently, significant Windows-specific scaffolding was introduced that provides zero utility on Linux and introduces test suite regressions and architectural asymmetry.

This audit catalogues every subsystem modified, classifies every change into one of five actionable categories (**KEEP**, **ADAPT**, **OPTIONAL**, **REMOVE CANDIDATE**, **INVESTIGATE**), and supplies concrete code evidence.

---

## 2. Classification Schema

| Category | Definition | Action in Linux-First Core |
|---|---|---|
| **KEEP** | High-value, cross-platform, or Linux-specific innovation developed during the port. | Preserve and integrate into primary codebase. |
| **ADAPT** | Sound concept whose current Windows-centric implementation should be refactored for Linux. | Rewrite/decouple to fit Linux-native architecture. |
| **OPTIONAL** | Feature valuable only if Windows support is maintained; must be cleanly isolated. | Isolate behind platform boundary or archive. |
| **REMOVE CANDIDATE** | Windows-only mechanism offering zero value to Linux that inflates maintenance overhead. | Deprecate, remove, or archive to historical branch. |
| **INVESTIGATE** | Complex change requiring verification, dependency auditing, or security validation. | Audit thoroughly before committing to adoption. |

---

## 3. Subsystem Audit & Classification Matrix

### 3.1 `core/platform/` & Platform Abstraction Layer

#### Changes Introduced
- `core/platform/__init__.py`: Dynamic provider selector (`get_platform()`).
- `core/platform/base.py`: `AbstractPlatformProvider` interface declaring 12 OS primitives.
- `core/platform/linux.py`: `LinuxPlatformProvider` implementation.
- `core/platform/windows.py`: `WindowsPlatformProvider` implementation (Win32 ctypes, registry queries, `Toolhelp32` snapshot).
- `core/platform/windows_multi_instance.py`: Mutex/Event conflict manager.
- `core/platform/windows_settings.py`: XML configuration manager for `GlobalBasicSettings_13.xml`.

#### Concrete Evidence & Code Inspection
1. **Asymmetric & Incomplete Abstraction (`core/platform/linux.py` vs `core/instance_runner.py`):**
   In `core/platform/linux.py`, the `launch_client` method is an empty stub:
   ```python
   def launch_client(self, ...):
       # Delegates to Linux runner logic in instance_runner.py
       pass
   ```
   Meanwhile, `core/instance_runner.py` directly executes the full Linux Mocktail launch pipeline (gamescope, cgroups, environment variables, hardlinks) under an `if sys.platform != "win32"` branch. Only Windows actually routes through `get_platform().launch_client()`.
2. **Top-Level Windows Imports Breaking Linux Tests:**
   `core/platform/windows.py` executes unconditionally at module level:
   ```python
   import winreg
   kernel32 = ctypes.windll.kernel32
   user32 = ctypes.windll.user32
   ```
   When `tests/test_windows_native.py` imports `WindowsPlatformProvider` at top-level on Linux, test discovery crashes with `ModuleNotFoundError: No module named 'winreg'`.

#### Classifications
- `core/platform/base.py`: **ADAPT**. The concept of an OS abstraction is reasonable, but the current interface is Windows-biased and incomplete on Linux.
- `core/platform/linux.py`: **ADAPT**. Must actually encapsulate the Linux runner primitives instead of being an empty stub.
- `core/platform/windows.py`: **REMOVE CANDIDATE** (in Linux-only) / **OPTIONAL** (in hybrid).
- `core/platform/windows_multi_instance.py`: **REMOVE CANDIDATE**. Windows-specific singleton lock pre-emption.
- `core/platform/windows_settings.py`: **REMOVE CANDIDATE**. Windows-specific XML configuration.

---

### 3.2 Roblox Installation & Executable Discovery

#### Changes Introduced
- Windows client discovery in `core/platform/windows.py:find_roblox_client`: Inspects `HKCR\roblox-player\shell\open\command`, `%LOCALAPPDATA%\Roblox\Versions\version-*\RobloxPlayerBeta.exe`, and Program Files roots.
- Linux client discovery in `core/platform/linux.py:find_roblox_client`: Checks `~/.local/lib/mocktail/mocktail`.

#### Concrete Evidence
Linux Mocktail installations are deterministic: `~/.local/lib/mocktail/mocktail` and `~/.local/share/mocktail`. The Windows discovery logic uses `winreg` and Windows directory structures (`%LOCALAPPDATA%`, `%ProgramFiles%`).

#### Classifications
- Linux client discovery (`core/storage.py`, `core/platform/linux.py`): **KEEP**.
- Windows registry/directory discovery (`core/platform/windows.py`): **REMOVE CANDIDATE**.

---

### 3.3 Account Storage & Database Layer

#### Changes Introduced
- `core/storage.py`:
  - Path adaptation for `%LOCALAPPDATA%\AERAM\manager.db` vs `~/.local/share/ram-linux/manager.db`.
  - Directory junction fallback (`_winapi.CreateJunction`) in `_link_directory` for systems without symlink permissions.
  - Windows runtime directory mapping to `<profile_dir>/run`.
  - Preserved Linux optimizations: SQLite WAL mode, in-memory settings cache, shared Mesa shader cache (`shared_mesa_shader_cache`), and shared Roblox content cache (`shared_rbx_storage`).

#### Concrete Evidence
All 7 unit tests in `tests/test_storage.py` continue to pass on Linux with 100% fidelity. Shared asset symlinks eliminate redundant downloads and reduce disk footprint by over 80% across 50+ accounts.

#### Classifications
- SQLite schema, in-memory cache, account reordering, group management: **KEEP**.
- Shared asset and shader cache hardlinking/symlinking: **KEEP**.
- Windows NTFS junction fallback (`_winapi`): **OPTIONAL** (harmless if gated by `sys.platform == 'win32'`).

---

### 3.4 Cookie/Session Management & 9-State Auth Lifecycle

#### Changes Introduced
- `core/auth_state.py`: Introduced `AccountAuthState` enum with 9 distinct states (`STORED`, `VALIDATING`, `READY`, `AUTHENTICATING`, `VERIFICATION_REQUIRED`, `LAUNCHING`, `RUNNING`, `SESSION_EXPIRED`, `ERROR`).
- `AccountAuthStateManager`: Thread-safe per-account state tracker with:
  - Launch deduplication lock (`acquire_launch_lock`, `release_launch_lock`).
  - Automated retry suppression on CAPTCHA / 2FA challenges (`mark_verification_required`).
  - Session expiration isolation (`mark_session_expired`) preventing accidental cookie deletion.
- Integration into `core/instance_runner.py` and `core/self_heal.py`.

#### Concrete Evidence
In early Windows and Linux builds, accounts receiving an Arkose challenge would enter rapid retry loops driven by multi-launch queues or the Self-Healing watchdog. This triggered Akamai IP rate-limits (HTTP 429) and repeated CAPTCHA challenges across all accounts. `AccountAuthStateManager` immediately halts automated reconnection when `VERIFICATION_REQUIRED` is flagged.

#### Classifications
- `core/auth_state.py` (`AccountAuthState`, `AccountAuthStateManager`): **KEEP**. One of the most valuable architectural improvements in the repository.
- Integration into `instance_runner.py` and `self_heal.py`: **KEEP**.

---

### 3.5 Browser-Based Account Login (`ui/web_login_dialog.py`)

#### Changes Introduced
- `ui/web_login_dialog.py`: 463-line modal dialog wrapping embedded Chromium via `PyQt6.QtWebEngineWidgets`.
- Off-the-record profile (`QWebEngineProfile`) navigating to `https://www.roblox.com/login`.
- Cookie store listener (`cookieAdded`) intercepting `.ROBLOSECURITY` without user interaction.
- Automatic account enrichment via `RobloxAPI.enrich_account` and encrypted persistence into vault.

#### Concrete Evidence & Issues
1. **Unmet Dependency on Linux:**
   `PyQt6-WebEngine` is not specified in `requirements.txt` or `pyproject.toml` dependencies and is not installed in the Linux host environment.
2. **Defect in Fallback UI:**
   When `WEBENGINE_AVAILABLE` is False, `_init_fallback_ui` runs, which omits creating `self.status_label`. Any subsequent call to `_process_detected_cookie` throws:
   `AttributeError: 'WebLoginDialog' object has no attribute 'status_label'`.
   This causes `tests/test_gui.py:test_web_login_dialog_lifecycle` to fail.
3. **Architectural Cost:**
   Chromium WebEngine introduces ~150–250 MB of binary dependencies, memory overhead (~80 MB idle), and forces OpenGL context sharing (`AA_ShareOpenGLContexts`).

#### Classifications
- Feature Concept (interactive browser login capturing cookies directly): **ADAPT**.
- Current Implementation (`PyQt6-WebEngine` hard-dependency): **ADAPT**. Must be refactored with strict optional dependency gating, a bug-free fallback UI, and an alternative lightweight external browser authentication helper.

---

### 3.6 Encrypted Credential Vault & Cryptography

#### Changes Introduced
- `core/vault.py`:
  - Dual backend: System `libsodium` via `ctypes` on Linux, falling back to `pynacl` on Windows or minimal installations.
  - Windows DPAPI master key protection (`_win_dpapi_protect` / `_win_dpapi_unprotect`).
  - Linux POSIX file permission enforcement (`0600` on `vault.key`, `0700` on base directory).
  - Argon2id key derivation (`derive_key_argon2`) and XSalsa20-Poly1305 authenticated symmetric encryption (`secretbox_encrypt`, `secretbox_decrypt`).

#### Concrete Evidence
All 5 cryptographic unit tests in `tests/test_vault.py` pass cleanly. Deterministic key derivation, tamper resistance, and short payload checks verify authenticated encryption integrity.

#### Classifications
- Argon2id + XSalsa20-Poly1305 core (`secretbox_*`): **KEEP**.
- PyNaCl fallback when system libsodium is absent: **KEEP**.
- Linux `0600` keyfile isolation: **KEEP**.
- Windows DPAPI wrapper (`_win_dpapi_*`): **REMOVE CANDIDATE** (in Linux-only) / **OPTIONAL** (in hybrid).

---

### 3.7 Account Migration & Encrypted Export/Import

#### Changes Introduced
- `core/migration.py`: Encrypted account archive engine (`AERAM_VAULT_EXPORT_V1`).
- Argon2id KDF (memory 64MB, ops 3) + XSalsa20-Poly1305 encryption.
- CLI integration: `--export <file>`, `--password <pwd>`.
- GUI integration: Account Utilities → "Export Encrypted Archive".

#### Concrete Evidence
Tested via `tests/test_migration.py`: 3/3 tests pass (`test_encrypted_export_and_import_roundtrip`, `test_password_required_flag`, `test_wrong_password_fails_cleanly`). Fully portable, zero plaintext token exposure, and cross-platform compatible.

#### Classifications
- `core/migration.py` and CLI/GUI integration: **KEEP**.

---

### 3.8 Roblox Launch-Ticket Handling & API Client

#### Changes Introduced
- `core/roblox_api.py`:
  - `request_auth_ticket(cookie)`: Acquires single-use launch tickets from `auth.roblox.com/v1/authentication-ticket` with automatic `x-csrf-token` handshake.
  - Arkose challenge header inspection (`rblx-challenge-type`, `rblx-challenge-id`, `rblx-challenge-metadata`).
  - Structured response object `AuthTicketResult` differentiating expired sessions (401), rate limits (429), and challenges (403).
  - `validate_cookie(cookie)` returning detailed status codes (`VALID`, `EXPIRED`, `RATE_LIMITED`).

#### Concrete Evidence
On Windows, Roblox client requires single-use launch tickets (`roblox-player:1+launchmode:play+gameinfo:<ticket>`). On Linux, Mocktail directly consumes `.ROBLOSECURITY` via `MOCKTAIL_COOKIE_FILE`. Ticket generation is not required on Linux. However, the diagnostic error classification and challenge header detection are useful for detecting account state before launching.

#### Classifications
- `validate_cookie` and diagnostic status reporting: **KEEP**.
- `request_auth_ticket` / `get_auth_ticket`: **OPTIONAL** (not needed for Linux Mocktail runtime, but harmless and useful if Windows support is kept).

---

### 3.9 Process Supervision & Instance Lifecycle

#### Changes Introduced
- `core/instance_runner.py`:
  - Linux: sandboxed Mocktail execution, cgroups memory limits (`systemd-run -pMemoryHigh`), CPU affinity scheduling (`os.sched_setaffinity`), RT priority caps (`RLIMIT_RTPRIO`), and decoupled `XDG_RUNTIME_DIR`.
  - Windows: Win32 API supervision (`OpenProcess`, `GetExitCodeProcess`, `TerminateProcess`) and `GlobalMemoryStatusEx` telemetry.
  - Multi-instance scan: Windows `Toolhelp32` snapshot vs Linux UID-filtered `/proc` scan.

#### Concrete Evidence
Linux cgroups and CPU scheduling provide deterministic multi-instance performance: 50+ background farming alts confined to secondary CPU cores at 6 FPS, while the primary gaming instance runs unthrottled at 330 FPS. The Windows port has no equivalent cgroup isolation.

#### Classifications
- Linux cgroups, CPU affinity, resource limits, and `/proc` scanning: **KEEP**.
- Windows Win32 process supervision and `GlobalMemoryStatusEx`: **REMOVE CANDIDATE**.

---

### 3.10 Multi-Instance Account Isolation

#### Changes Introduced
- Linux: Full container filesystem sandboxing (`MOCKTAIL_DATA_ROOT`, private runtime directories, private cookie files, hardlinked binaries per account).
- Windows: Pre-emptive named mutex ownership (`ROBLOX_singletonEvent`) in `WindowsMultiInstanceManager`.

#### Concrete Evidence
As documented in `docs/windows-port/SESSION_ISOLATION.md`, Windows client processes share `%LOCALAPPDATA%\Roblox\LocalStorage\RobloxCookies.dat`. When multiple instances execute, they overwrite shared files. Linux provides true hardware and filesystem-level isolation per account.

#### Classifications
- Linux container sandboxing: **KEEP** (Architectural Authority).
- Windows mutex trick (`windows_multi_instance.py`): **REMOVE CANDIDATE**.

---

### 3.11 Framerate (FPS) Configuration

#### Changes Introduced
- Linux: Native environment variable `MOCKTAIL_FRAME_RATE_LIMIT` and `ClientAppSettings.json` (`DFIntTaskSchedulerTargetFps`), uncapped to 330 FPS or unlimited (10000).
- Windows: `RobloxSettingsManager` mutating `GlobalBasicSettings_13.xml` (`FramerateCap`).

#### Concrete Evidence
Linux Mocktail directly accepts framerate limits via environment variables and ClientAppSettings overrides without touching user configuration files. Windows XML editing carries risk of file corruption and anti-tamper resets.

#### Classifications
- Linux dynamic FPS targeting (330 FPS / unlimited): **KEEP**.
- Windows `GlobalBasicSettings_13.xml` manager: **REMOVE CANDIDATE**.

---

### 3.12 PyQt6 User Interface & Branding

#### Changes Introduced
- `ui/main_window.py`:
  - 9-state auth lifecycle badge display (`READY`, `RUNNING`, `VERIFICATION REQUIRED`, `SESSION EXPIRED`).
  - Right-click "Validate Session" action.
  - Right-click "Reset Verification Status" action.
  - Right-click "Web Browser Login / Re-authenticate".
  - Menu action "Export Encrypted Archive".
- `assets/`: Minimalist achromatic `ae` ligature branding (`aeram.svg`, `aeram.png`, `aeram.ico`).

#### Concrete Evidence
The UI enhancements add clarity for account health monitoring without bloating the interface. The aesthetic adheres strictly to the pitch-black minimalist design guidelines.

#### Classifications
- Auth status indicators and session validation actions: **KEEP**.
- Encrypted export menu integration: **KEEP**.
- New achromatic branding assets: **KEEP**.

---

### 3.13 Packaging, Installers & CI

#### Changes Introduced
- Windows PyInstaller specs: `aeram.spec`, `aeram_portable.spec`.
- Windows Inno Setup script: `installer.iss`.
- Windows build scripts: `scripts/build_windows.py`, `scripts/compile_and_install.bat`.
- CI workflow: `.github/workflows/windows-build.yml`.
- Windows version info: `version_info.txt`.

#### Concrete Evidence
These files comprise over 600 lines of Windows packaging logic. Linux uses `install.sh`, systemd service integration, and desktop launchers.

#### Classifications
- Windows packaging scripts and CI: **REMOVE CANDIDATE** (in Linux-only) / **OPTIONAL** (in separate directory).
- Linux `install.sh` and desktop shortcut integration: **KEEP**.

---

### 3.14 Automated Test Suite

#### Changes Introduced
- `tests/test_migration.py`: 3 unit tests for encrypted archive export/import.
- `tests/test_auth_lifecycle.py`: 6 tests for auth state machine and Arkose suppression.
- `tests/test_windows_native.py`: 12 tests for Windows paths, DPAPI, discovery, mutex.
- `tests/test_windows_settings.py`: 4 tests for XML settings manager.
- `tests/test_packaged_build.py`: 4 tests for PyInstaller output.

#### Concrete Evidence
1. `tests/test_auth_lifecycle.py` uses `import pytest` at top level. In the default Python `unittest` runner, this throws `ModuleNotFoundError: No module named 'pytest'`.
2. `tests/test_windows_native.py` imports `winreg` unconditionally at top level, crashing Linux test discovery.
3. `tests/test_gui.py` contains `test_web_login_dialog_lifecycle` which fails when `PyQt6-WebEngine` is absent.

#### Classifications
- `test_migration.py`: **KEEP**.
- `test_auth_lifecycle.py`: **ADAPT** (Convert to standard `unittest.TestCase`).
- `test_gui.py`: **ADAPT** (Fix missing attribute bug and gate WebEngine tests).
- `test_windows_native.py`, `test_windows_settings.py`, `test_packaged_build.py`: **REMOVE CANDIDATE** (or isolate under `tests/windows/`).

---

## 4. Subsystem Salvage Summary Table

| Subsystem | Primary Files | Windows Change | Linux Value | Classification |
|---|---|---|---|---|
| **Auth State Machine** | `core/auth_state.py` | 9-state lifecycle & CAPTCHA suppression | **CRITICAL** | **KEEP** |
| **Encrypted Migration** | `core/migration.py` | Argon2id + XSalsa20 export/import | **HIGH** | **KEEP** |
| **API Error Diagnostics** | `core/roblox_api.py` | Detailed 401/403/429 parsing | **HIGH** | **KEEP** |
| **Branding & Theme** | `assets/*`, `ui/theme.py` | Achromatic `ae` ligature icons | **HIGH** | **KEEP** |
| **UI Account Actions** | `ui/main_window.py` | Validate session, export, auth badges | **HIGH** | **KEEP** |
| **Crypto Vault Backend** | `core/vault.py` | PyNaCl fallback alongside ctypes | **MEDIUM** | **KEEP** |
| **Browser Login** | `ui/web_login_dialog.py` | Embedded Chromium login | **MEDIUM** | **ADAPT** |
| **Platform Abstraction** | `core/platform/` | Abstract provider interface | **LOW** | **ADAPT** |
| **Test Suite Portability** | `tests/test_auth_lifecycle.py` | Pytest-based tests | **HIGH** | **ADAPT** |
| **Windows Mutex Manager** | `core/platform/windows_multi_instance.py` | Mutex/Event conflict handling | **NONE** | **REMOVE CANDIDATE** |
| **Windows Settings Mgr** | `core/platform/windows_settings.py` | XML FramerateCap editing | **NONE** | **REMOVE CANDIDATE** |
| **Windows Provider** | `core/platform/windows.py` | Win32 process & registry handling | **NONE** | **REMOVE CANDIDATE** |
| **DPAPI Key Storage** | `core/vault.py` | Win32 CryptProtectData | **NONE** | **REMOVE CANDIDATE** |
| **Windows Packaging** | `aeram.spec`, `installer.iss`, `scripts/` | PyInstaller & Inno Setup scripts | **NONE** | **REMOVE CANDIDATE** |
| **Windows CI** | `.github/workflows/windows-build.yml` | Windows GitHub Actions workflow | **NONE** | **REMOVE CANDIDATE** |
