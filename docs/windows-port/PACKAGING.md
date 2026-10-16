# AERAM — Windows 11 Production Packaging Guide

This document describes the production packaging architecture, build pipeline, binary targets, and security properties for AERAM on Windows 11.

---

## 1. Packaging Architecture Overview

AERAM is distributed in three distinct formats to support every Windows deployment model without requiring users to install Python, PyQt6, or dependencies:

| Distribution Target | Path | Type | Target Use Case | Startup Latency |
| :--- | :--- | :--- | :--- | :--- |
| **Production Folder** | `dist/AERAM/` | Directory | Installed desktop app (source for Inno Setup) | **~0.17s** |
| **Windows Installer** | `dist/AERAM-Setup.exe` | Inno Setup (lzma2) | Standard user installation (Start Menu, Desktop, Apps) | N/A (installer) |
| **Standalone Portable** | `dist/AERAM-Portable.exe` | Single-File Executable | Flash drives, portable environments, quick evaluations | **~1.38s - 3.8s** |

---

## 2. One-Folder vs. One-File Analysis

A thorough architectural benchmark between One-Folder and One-File packaging on Windows 11 was conducted:

| Characteristic | One-Folder (`dist/AERAM/`) | One-File (`dist/AERAM-Portable.exe`) |
| :--- | :--- | :--- |
| **Startup Speed (p50)** | **0.17s - 0.18s** (Immediate) | **1.38s - 3.84s** (Decompression delay) |
| **Antivirus Reliability** | High trust; binaries are static on disk | Prone to heuristic false-positives due to `%TEMP%` extraction |
| **Temp Disk Usage** | 0 bytes temporary disk overhead | Unpacks ~100MB of Qt DLLs to `%TEMP%/_MEIxxxxxx` per run |
| **Failure Recovery** | No risk of stale orphaned files in `%TEMP%` | Process crashes can leave orphaned folders in `%TEMP%` |
| **Recommendation** | **Primary distribution target** (packaged via Inno Setup) | **Optional secondary download** for portable use |

---

## 3. Binary Suite Inside `dist/AERAM/`

Inside `dist/AERAM/`, two specialized binaries are compiled:
1. `AERAM.exe`:
   - Built with `--noconsole` (`runw.exe` bootloader).
   - Double-clicking from Windows Explorer or launching from the Start Menu displays **zero console window**.
   - If invoked from PowerShell / CMD with CLI flags (`--list`, `--launch`, `--status`), it dynamically attaches via Win32 `AttachConsole(-1)` or redirected standard pipe handles to print output directly to the terminal.
2. `AERAM-CLI.exe`:
   - Built with `--console` (`run.exe` bootloader).
   - Designed for headless scripts, PowerShell pipelines, scheduled automations, and troubleshooting.

---

## 4. Inno Setup Installer (`AERAM-Setup.exe`)

The Windows installer is defined in [`installer.iss`](file:///C:/Dev/aeram/installer.iss):
- **Per-User Installation**: Configured with `PrivilegesRequired=lowest`. Installs cleanly to `%LOCALAPPDATA%\Programs\AERAM` without requiring Administrator privileges or triggering UAC prompts.
- **Shortcuts**: Creates Start Menu entries (`AERAM` and `AERAM CLI`) and an optional Desktop shortcut.
- **Uninstaller**: Standard Windows registered uninstaller (`Settings > Installed apps`).
- **Data Protection Guarantee**: On uninstall or update, only `{app}` (`%LOCALAPPDATA%\Programs\AERAM`) is cleaned up. User account databases, encrypted vault keys, and settings located in `%LOCALAPPDATA%\AERAM` are **strictly preserved**.

---

## 5. Security & Vault Safety

- **Zero Credential Bundling**: No databases (`manager.db`), master keys (`vault.key`), or credentials are ever bundled into compiled binaries.
- **Isolated Storage**: User data is strictly stored in `%LOCALAPPDATA%\AERAM` via `WindowsPlatformProvider.get_default_base_dir()`. The application never writes state into its installation directory or current working directory.
- **Windows DPAPI**: Master encryption keys are wrapped using Win32 `CryptProtectData` with user-scope machine-bound encryption.
- **Anti-Cheat Integrity**: AERAM strictly avoids binary patching, DLL injection, kernel drivers, or defeating anti-cheat protections.

---

## 6. FastFlags & FPS Behavior Notice

AERAM provisions target framerates (`DFIntTaskSchedulerTargetFps`) into `%LOCALAPPDATA%\Roblox\ClientSettings\ClientAppSettings.json`.

> [!WARNING]
> Roblox's client engine enforces internal limits, graphics backend policies, and in-game framerate cap settings (up to 240 FPS). Depending on the active Roblox version and user hardware, `DFIntTaskSchedulerTargetFps` overrides may be clamped by the client. AERAM correctly provisions documented FastFlag settings without binary tampering, but does not claim anti-cheat circumvention or guaranteed uncap beyond client limits.

---

## 7. Reproducible Build Instructions

### Prerequisites
- Windows 11 (64-bit)
- Python 3.14 (or 3.12+)
- `uv` package manager
- Inno Setup 6 (`ISCC.exe`)

### Step 1: Environment Setup
```powershell
git clone https://github.com/vysaely/aeram.git
cd aeram

# Create virtualenv and install dependencies
uv venv .venv --python 3.14
.\.venv\Scripts\activate
uv pip install -e .
uv pip install pyinstaller
```

### Step 2: Run Automated Build
```powershell
.\.venv\Scripts\python scripts/build_windows.py
```

### Step 3: Output Artifacts
The artifacts will be produced in `dist/`:
- `dist/AERAM/` (Folder distribution)
- `dist/AERAM-Portable.exe` (Single-file portable executable)
- `dist/AERAM-Setup.exe` (Windows installer)
