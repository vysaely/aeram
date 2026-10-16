# AERAM — Windows 11 Development Handoff & Architecture Blueprint

**Document Version**: 1.0.0  
**Target Platform**: Windows 11 (x86_64)  
**Source Platform**: Arch Linux / CachyOS (Linux 6.x)  
**Milestone Release**: `v0.1.0-linux`  
**Repository**: `https://github.com/vysaely/aeram`  

---

## 1. Project Overview

**AERAM (Roblox Account Manager)** is a high-performance, security-hardened, utilitarian multi-instance Roblox account manager. It features:
- **Dark Minimalist Interface**: Ultra-dense, pitch-black monochrome aesthetic (`#0c0c0c` / `#161616` / `#222222`), zero emojis, clear status indicators.
- **Simultaneous Multi-Client Running**: Concurrently running 50+ independent Roblox accounts with independent graphics profiles (e.g. 1 High-fidelity main at 330 FPS alongside 51 background farming alts at 6 FPS).
- **Group Management & Stable Custom Sorting**: Full drag-and-drop custom ordering of accounts, custom groups, and group-order sorting that sorts group blocks while preserving custom relative ordering within groups.
- **Zero-Password Security**: Strictly operates using encrypted `.ROBLOSECURITY` session cookies with native authenticated cryptography (libsodium XSalsa20-Poly1305 + Argon2id KDF). Passwords are never collected, stored, or transmitted.
- **Self-Healing Watchdog Engine**: Automatically detects disconnected or crashed background alts and rejoins them directly without hijacking the main account's game session.
- **Anti-AFK Engine**: Hardware-level periodic pulse with active play immunity, ensuring background farming accounts are never disconnected by Roblox's 20-minute idle watchdog.

---

## 2. Current Architecture & Codebase Map

AERAM is structured into cleanly decoupled layers:

```
aeram/
├── core/
│   ├── models.py            # Strongly-typed dataclasses (RobloxAccount, FavoritePlace, RunningInstance)
│   ├── storage.py           # SQLite WAL persistence, in-memory settings cache, profile sandboxes
│   ├── vault.py             # Authenticated libsodium encryption (XSalsa20-Poly1305, Argon2id)
│   ├── roblox_api.py        # Roblox HTTP API (presence, place/universe resolution, thumbnail, robux)
│   ├── instance_runner.py   # Multi-instance supervisor, sandbox bootstrapping, cgroup limits
│   ├── self_heal.py         # Watchdog monitoring error logs (Error 277) and auto-reconnecting alts
│   └── anti_afk.py          # Evdev/uinput pulse with active play immunity and KWin script focus
├── ui/
│   ├── main_window.py       # Core PyQt6 window, in-place table updates, multi-selection, menu actions
│   ├── dialogs.py           # Account utilities, RAM JSON importer, multi-launch dialog, settings
│   └── theme.py             # Dark utilitarian QSS stylesheet (pitch-black minimalist theme)
├── tests/                   # 31 unit & regression tests (mocking storage, crypto, runner, UI)
├── native/                  # Optional native C helpers (Wayland mouse passthrough)
├── cli.py                   # Headless CLI entrypoint
├── ram-linux                # Unified executable launcher (--gui, CLI flags)
├── requirements.txt         # Production dependencies
└── pyproject.toml           # Standard packaging metadata
```

### Shared Logic (Portable across OS)
- `core/models.py`: 100% platform-agnostic data models.
- `core/storage.py`: SQLite schema, in-memory cache, and account CRUD are platform-agnostic. Filesystem paths use `pathlib.Path`.
- `core/roblox_api.py`: Platform-agnostic `urllib.request` client for Roblox Web APIs.
- `ui/*`: PyQt6 UI code is cross-platform.
- Windows RAM Importer (`core/importer.py` logic inside `dialogs.py` / `storage.py`): Parses standard Windows RAM `AccountData.json`.

### Platform-Specific Logic (Requires Windows Backend)
- `core/instance_runner.py`:
  - *Linux*: Launches sandboxed Mocktail binaries with `XDG_RUNTIME_DIR` / `MOCKTAIL_RUNTIME_ROOT` and `systemd-run` memory cgroups.
  - *Windows*: Must launch official `RobloxPlayerBeta.exe` using standard Windows multi-instance mutex handling (closing the `ROBLOX_singletonMutex` or using `CreateProcessW` / job objects).
- `core/vault.py`:
  - *Linux*: Loads `libsodium.so` via `ctypes.CDLL`.
  - *Windows*: Can load `libsodium.dll` via `ctypes`, or integrate Windows DPAPI (`CryptProtectData` / `CryptUnprotectData`) for native OS-protected master key storage.
- `core/anti_afk.py`:
  - *Linux*: Uses `/dev/uinput` and KDE KWin DBus scripting.
  - *Windows*: Will use native Win32 `SendInput` or `keybd_event` without requiring third-party drivers.

---

## 3. Linux Performance Baseline & Regression Targets

Any cross-platform refactoring must **never regress** the Linux implementation. The Linux baseline has been benchmarked on 52 accounts:

| Metric | Measured Baseline Target |
| :--- | :--- |
| **GUI Main-Thread Polling Latency** | **1.04 ms (p50)**, **1.88 ms (p95)** across 52 accounts |
| **Main-Thread Blocking Subprocesses** | **0 calls** during regular UI timer ticks |
| **Settings Database Queries** | **0 queries/tick** (served entirely from in-memory cache) |
| **QTableWidgetItem Allocation Churn** | **0 allocations/tick** (all rows updated in-place) |
| **Process Discovery Latency** | **1.97 ms** via UID-filtered `os.scandir` |
| **Parent File Descriptor Leaks** | **0 open handle leaks** on process launches |
| **Test Suite Pass Rate** | **31 / 31 tests passing (100%)** |

---

## 4. Windows Port Objectives & Technical Strategy

### Goal
Produce a single, unified codebase that detects the host operating system (`sys.platform == 'win32'` vs `'linux'`) and provides native multi-instance Roblox management on both platforms with 100% feature parity.

### Target Architecture
Introduce a clean platform abstraction layer:
```
core/
├── platform/
│   ├── base.py              # AbstractPlatformProvider interface
│   ├── linux.py             # LinuxProvider (Mocktail, gamescope, cgroups, uinput)
│   └── windows.py           # WindowsProvider (RobloxPlayerBeta, mutex bypass, DPAPI, SendInput)
```

### Windows Multi-Instance Technical Mechanism
1. **Roblox Singleton Mutex**:
   - Official Roblox on Windows enforces a single client instance using a named Win32 Mutex: `ROBLOX_singletonMutex` or `ROBLOX_singletonEvent`.
   - On Windows, multi-instance managers duplicate the handle or close the mutex handle via `NtQuerySystemInformation` / `DuplicateHandle` / `CloseHandle`, allowing subsequent instances to start.
2. **Launch Protocol**:
   - Roblox on Windows accepts standard URI protocols: `roblox-player:1+launchmode:play+gameinfo:<auth_ticket>+placelauncherurl:...`
   - Alternatively, launch `RobloxPlayerBeta.exe` with arguments: `--app -t <auth_ticket> -j <join_script_url>`.
   - Authentication tickets are generated via `POST https://auth.roblox.com/v1/authentication-ticket` using the account's `.ROBLOSECURITY` cookie and `x-csrf-token`.
3. **Window Management & Anti-AFK**:
   - Use Win32 API `EnumWindows` / `GetWindowThreadProcessId` to track Roblox client windows.
   - Use `SendInput` targeting minimized or background windows (or temporary `SetForegroundWindow` + `SendInput` + restore previous window).

---

## 5. Security Requirements for Windows Development

1. **Zero Password Policy**: Never prompt for or store account passwords.
2. **Encrypted Cookie Storage**:
   - Use libsodium (`libsodium.dll`) or Windows DPAPI (`CryptProtectData`) to encrypt cookies at rest.
   - Database file (`manager.db`) must be stored in `%LOCALAPPDATA%\AERAM\manager.db`.
3. **Never Commit Live Data**:
   - Keep `.gitignore` strictly enforced.
   - Never commit `manager.db`, `vault.key`, `.ROBLOSECURITY` tokens, or Windows RAM `AccountData.json` exports.
4. **Untrusted Code Reference Policy**:
   - Do not copy third-party binary blobs, uninspected DLLs, or obfuscated drivers.
   - Write clean, auditable Python code using standard Win32 APIs (`ctypes.windll`).

---

## 6. Recommended 10-Phase Implementation Roadmap

When resuming this work on Windows 11, execute the port in these verified steps:

1. **Phase 1: Environment & Baseline Verification**
   - Clone repo on Windows 11.
   - Create Python 3.11/3.12/3.13 venv.
   - Run `python -m unittest discover -s tests -v` to verify existing platform-independent tests pass.
2. **Phase 2: Platform Abstraction Extraction**
   - Define `AbstractPlatformProvider` in `core/platform/base.py`.
   - Move Linux-specific Mocktail runner code into `core/platform/linux.py`.
3. **Phase 3: Windows Storage & Path Provider**
   - Map storage paths to `%LOCALAPPDATA%\AERAM` on Windows while preserving `~/.local/share/ram-linux` on Linux.
4. **Phase 4: Windows Cryptographic Vault**
   - Provide Windows DPAPI or bundled `libsodium.dll` backend for `Vault`.
5. **Phase 5: Roblox Player Discovery on Windows**
   - Detect `RobloxPlayerBeta.exe` from `%LOCALAPPDATA%\Roblox\Versions\version-*\` or Windows Registry.
6. **Phase 6: Multi-Instance Mutex Handling**
   - Implement Win32 mutex detection and handle release using `ctypes`.
7. **Phase 7: Windows Client Launch & Authentication Ticket Flow**
   - Generate authentication tickets via Roblox Auth API.
   - Launch `RobloxPlayerBeta.exe` with proper launch URI arguments.
8. **Phase 8: Windows Anti-AFK Engine**
   - Implement Win32 `SendInput` pulse with foreground-window restoration.
9. **Phase 9: Windows Self-Healing Watchdog**
   - Monitor Roblox client logs in `%LOCALAPPDATA%\Roblox\logs\` for disconnect error codes (e.g. 277).
10. **Phase 10: Dual-Platform Verification & Release**
    - Verify all tests pass on both Linux and Windows 11.
    - Tag release `v0.2.0-cross-platform`.

---

## 7. Windows Setup Commands (Copy & Paste)

```powershell
# 1. Clone repository
git clone https://github.com/vysaely/aeram.git
cd aeram

# 2. Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 4. Run test suite to verify baseline
python -m unittest discover -s tests -v
```

---

## 8. Ready-to-Copy Prompt for Future Windows AGY Session

Copy the prompt below into your first prompt when opening this project in AGY on Windows 11:

```markdown
Hello Antigravity. I am resuming development of AERAM (Roblox Account Manager) on Windows 11.

Please review `docs/WINDOWS_HANDOFF.md` and `README.md` first.

Our goals for this Windows session:
1. Verify the repository and existing test suite on Windows 11 (`python -m unittest discover -s tests -v`).
2. Implement the platform abstraction layer (`core/platform/`) to support both Linux and Windows.
3. Implement native Windows Roblox player detection, launch ticket flow, and multi-instance mutex handling.
4. Maintain all existing dark monochrome GUI aesthetics, custom group sorting, and high/low framerate profile modes.
5. Ensure zero regression to the verified Linux implementation.

Let's start by inspecting the current working tree and running the test suite.
```
