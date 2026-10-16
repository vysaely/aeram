# AERAM — Windows Functional Parity Audit & Root Cause Analysis

**Document Version:** 1.0.0  
**Target Platform:** Windows 11 (Build 26100+ / 64-bit)  
**Audited Client Version:** Roblox `0.742.0.7421053` (Hyperion / Byfron 64-bit protected)  
**Repository State:** `C:\Dev\aeram` (Git `main`)

---

## 1. Executive Summary

This audit assesses the functional parity between AERAM's original Linux architecture and its native Windows 11 implementation. Two critical functional regressions were identified during initial testing:

1. **Simultaneous Multi-Instance Roblox Execution:** Under Windows 11, launching a second Roblox account terminated immediately or transferred focus to the existing window.
2. **FPS Profiles Ineffective on Windows Client:** Setting account-specific FPS limits (e.g. 330 FPS for main, 15 FPS for alts) produced zero observable effect on the running official Windows Roblox client.

This document details the root-cause analysis, empirical findings on the Windows client runtime, the remediation architecture, and a definitive capability matrix.

---

## 2. Regression 1: Simultaneous Multi-Instance Roblox Support

### 2.1 Expected Behavior
On Linux, AERAM achieves 100% concurrent multi-instance isolation via the native Mocktail runtime. Each account receives a distinct `XDG_RUNTIME_DIR`, sandboxed configuration roots, independent hardlinked binaries, and separate lock spaces. Users expect Windows 11 to similarly support concurrent instances across multiple accounts.

### 2.2 Previous State & Root Cause
In the initial Windows port, `core/platform/windows.py` defined `handle_multi_instance_lock()` as a no-op placeholder returning `True`:
```python
def handle_multi_instance_lock(self) -> bool:
    # Under modern 64-bit Hyperion, mutating process handles from unprivileged space
    # is strictly guarded. We report this capability status cleanly without crashing.
    return True
```
Because no synchronization mechanism was manipulated, Roblox executed its standard single-instance guard.

### 2.3 Runtime Synchronization Object Analysis
Empirical inspection of running Roblox processes (PID `2616` on Windows 11) confirmed the following:
* **Object Name:** `\Sessions\1\BaseNamedObjects\ROBLOX_singletonEvent`.
* **Kernel Object Type:** `Event` (created via `CreateEventW`), **not** a Mutex.
* **Roblox Behavior:** When `RobloxPlayerBeta.exe` starts, it calls `CreateEventW(NULL, TRUE, FALSE, L"ROBLOX_singletonEvent")`.
  * If the call returns a handle and `GetLastError() == ERROR_ALREADY_EXISTS` (183), Roblox signals the primary instance, brings its window to focus, and terminates the secondary process.
  * If no mitigation is in place, only one instance can execute per user session.

### 2.4 Remediation: Non-Invasive Singleton Pre-Emption
AERAM implements `WindowsMultiInstanceManager` in `core/platform/windows_multi_instance.py`:
1. **Pre-emptive Object Holding:** Before Roblox instances start, AERAM claims `ROBLOX_singletonEvent` as an owned **Mutex** (`CreateMutexW`).
2. **Kernel Type Mismatch:** When subsequent Roblox instances call `CreateEventW("ROBLOX_singletonEvent")`, Windows encounters an existing object of type *Mutant* rather than *Event*. Windows fails the call with `ERROR_INVALID_HANDLE` (6) rather than returning `ERROR_ALREADY_EXISTS` (183).
3. **Execution Continuation:** Roblox does not evaluate `ERROR_INVALID_HANDLE` as an existing instance collision, allowing multiple independent instances to run simultaneously without triggering Byfron memory or handle hooks.

---

## 3. Regression 2: Windows FPS Profile Control

### 3.1 Expected Behavior
AERAM allows setting custom frame rate caps per account (e.g. 144/240/330 FPS for active main, 15/30/60 FPS for background alts to conserve CPU/GPU power).

### 3.2 Previous State & Root Cause
The initial port wrote `DFIntTaskSchedulerTargetFps` to `ClientAppSettings.json`:
```python
data["DFIntTaskSchedulerTargetFps"] = int(target_fps)
s_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
```
* **Obsolescence:** In modern 64-bit Roblox (version `0.742+`), FastFlags in `ClientAppSettings.json` are gated by an internal FastFlag allowlist and ignored on vanilla production client builds unless using third-party bootstrappers.
* **Official Setting Location:** Roblox official client persists frame rate limits in:
  `%LOCALAPPDATA%\Roblox\GlobalBasicSettings_13.xml`
  under the tag `<int name="FramerateCap">240</int>`.

### 3.3 Remediation: Robust XML Settings Manager
AERAM implements `RobloxSettingsManager` (`core/platform/windows_settings.py`):
1. **Atomic XML Update:** Parses and updates `<int name="FramerateCap">` in `GlobalBasicSettings_13.xml` while preserving all surrounding UserGameSettings, XML attributes, namespaces, and structure.
2. **Crash & Data Safety:** Creates an automatic safety backup (`.aeram.bak`) and writes atomically via temporary file replacement (`os.replace`).
3. **Dual-Layer Coverage:** Writes `FramerateCap` to `GlobalBasicSettings_13.xml` (primary for vanilla client) AND updates `ClientAppSettings.json` (secondary fallback for modified channels/bootstrappers).

### 3.4 Main vs Alt Shared Settings Architectural Limitation
Unlike Linux (where Mocktail isolates configuration trees per account), all Roblox processes in the current Windows user profile share `%LOCALAPPDATA%\Roblox\GlobalBasicSettings_13.xml`.
* **Launch-Time Target:** AERAM sets `FramerateCap` sequentially before launching each account. Each client reads the XML at startup.
* **Exit Flush Race Condition:** When a client exits, it serializes its in-memory settings back to `GlobalBasicSettings_13.xml`. If Main (240 FPS) exits after Alt (60 FPS), the file on disk reverts to 240.
* **Truthful Status:** Simultaneous per-instance FPS caps function at launch time, but cannot prevent disk overwrite on client exit without file-locking (which causes Roblox write failures).

---

## 4. Comprehensive Capability Matrix

| Feature / Subsystem | Linux Status | Windows 11 Status | Classification | Technical Mechanism / Architecture |
| :--- | :--- | :--- | :--- | :--- |
| **Account Vault & Encryption** | Native (AES-GCM / PyNaCl) | Native (DPAPI + libsodium) | `VERIFIED WORKING` | Win32 `CryptProtectData` master key isolation + SQLite storage |
| **Roblox Client Discovery** | Mocktail / Flatpak / Sober | Native Win32 Exe Discovery | `VERIFIED WORKING` | HKCR protocol handler + LocalAppData / ProgramFiles versions scan |
| **Process Supervision & Lifecycle** | `/proc` UID + cmdline | Win32 Toolhelp32 + OpenProcess | `VERIFIED WORKING` | `CreateToolhelp32Snapshot`, `OpenProcess`, `GetExitCodeProcess` |
| **Anti-AFK Keep-Alive** | evdev / uinput event injection | Win32 `mouse_event` (dx=0, dy=0) | `VERIFIED WORKING` | Zero-displacement hardware mouse event pulse, no focus theft |
| **Physical Idle Time Telemetry** | X11 / Wayland idle counters | Win32 `GetLastInputInfo` | `VERIFIED WORKING` | Sub-millisecond physical keyboard/mouse idle detection |
| **FPS Control (Vanilla Client)** | Mocktail environment variables | `GlobalBasicSettings_13.xml` | `VERIFIED WORKING` | Atomic XML editing of `<int name="FramerateCap">` |
| **Simultaneous Multi-Instance** | Mocktail sandbox isolation | Win32 Mutex Pre-Emption | `PARTIALLY WORKING` | Claims `ROBLOX_singletonEvent` before launch; shared cookie caveats |
| **Per-Instance Alt FPS Profiles** | Isolated MOCKTAIL env roots | Shared LocalAppData XML | `PARTIALLY WORKING` | Launch-time sequential cap applied; exit flush race condition exists |
| **Cross-Process Handle Duplication** | N/A (Linux unneeded) | Win32 `DuplicateHandle` close | `UNSUPPORTED` | Blocked by Byfron anti-cheat heuristics; risks ban/crash |
| **Separate Windows User Isolation** | N/A (Linux namespaces) | Multi-User Session Isolation | `NOT TESTED` | Requires secondary Windows user profile credentials |

---

## 5. Source Code Audit Trail

* `core/platform/windows.py`: Native Windows platform provider integrating settings and multi-instance managers.
* `core/platform/windows_settings.py`: Atomic, backup-preserving `GlobalBasicSettings_13.xml` manager.
* `core/platform/windows_multi_instance.py`: Non-invasive Win32 singleton synchronization supervisor.
* `core/instance_runner.py`: Cross-platform instance supervisor orchestrating launch tickets and process supervision.
* `ui/main_window.py`: Status bar and GUI reflecting active Windows client, multi-instance lock state, and FPS configuration.
