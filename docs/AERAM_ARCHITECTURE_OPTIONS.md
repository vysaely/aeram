# AERAM — Future Architecture Strategy & Options Analysis

**Document Version:** 1.0.0  
**Repository:** `https://github.com/vysaely/aeram.git`  
**Decision Context:** The owner is re-evaluating long-term Windows support and requires an evidence-based comparison of three architectural pathways to guide repository governance.

---

## 1. The Core Strategic Dilemma

AERAM was originally engineered from first principles as a high-performance Linux application leveraging Linux-specific kernel capabilities:
- Reflink container cloning (`cp --reflink=auto`)
- POSIX memory cgroups (`systemd-run -pMemoryHigh`)
- CPU affinity core pinning (`os.sched_setaffinity`)
- Realtime priority limits (`RLIMIT_RTPRIO`)
- Sandboxed private UNIX domain sockets & runtime directories (`XDG_RUNTIME_DIR`)
- Isolated per-instance cookie files consumed directly by Mocktail

The Windows port required working around Windows OS limitations:
- Global shared files in `%LOCALAPPDATA%\Roblox\LocalStorage\RobloxCookies.dat`
- Single-instance Named Mutex / Event collision hacks (`CreateMutexW` on `ROBLOX_singletonEvent`)
- Launch ticket minting via `auth.roblox.com` with Arkose CAPTCHA exposure
- XML string parsing of `GlobalBasicSettings_13.xml`
- Win32 API ctypes plumbing for process termination and memory statistics

Maintaining both platforms under a single unified core creates continuous architectural tension, test fragility, and maintenance drag.

---

## 2. Comprehensive Comparison of Architectural Options

### Option A — Linux-Only (Purge Windows Support After Salvaging Improvements)
In this model, all Windows-specific code (`core/platform/windows*.py`, DPAPI, PyInstaller specs, Inno Setup, Windows CI) is archived to a permanent Git tag/branch and removed from `main`. Valuable cross-platform innovations (`core/auth_state.py`, `core/migration.py`, improved API diagnostics, achromatic UI branding) are retained in a streamlined, Linux-native core.

### Option B — Linux-First, Windows Maintained Separately (Modular Subsystem)
In this model, Linux is the primary, authoritative development target. Windows support is preserved but completely decoupled into a standalone backend provider or submodule (`aeram-windows` or `plugins/windows/`). Linux development proceeds without Windows dependencies or ctypes constraints. Windows code is only updated when contributor resources permit.

### Option C — Fully Cross-Platform (Equal Dual-Platform Maintenance)
In this model, the repository continues to treat Linux and Windows 11 as co-equal targets. A completed, symmetric `AbstractPlatformProvider` abstraction is maintained. Every PR and feature must be tested and verified on both platforms simultaneously.

---

## 3. Detailed Comparative Evaluation

| Evaluation Criteria | Option A: Linux-Only | Option B: Linux-First (Separated) | Option C: Fully Cross-Platform |
|---|---|---|---|
| **Codebase Complexity** | **Very Low** (Single clean path, direct Linux primitives) | **Moderate** (Clean boundary, optional imports) | **High** (Two divergent execution paradigms, abstract interfaces) |
| **Ongoing Maintenance Effort** | **Minimal** (1 developer can easily maintain 100% of code) | **Low to Moderate** (Windows only touched when needed) | **Extreme** (Every Roblox update or Windows patch requires dual testing) |
| **System Reliability** | **Maximum** (Zero ctypes hacks, deterministic cgroups) | **High on Linux**, Moderate on Windows | **Moderate** (Windows anti-tamper updates frequently break mutex tricks) |
| **Account Isolation Security** | **Maximum** (100% filesystem & process isolation) | **Maximum on Linux**, Degraded on Windows | **Uneven** (Linux is secure; Windows suffers from shared cookie DB) |
| **Performance Overhead** | **Near-Zero** (No abstraction layer indirection) | **Near-Zero on Linux** | **Low to Moderate** (Dynamic platform dispatch, XML parsing) |
| **Test & CI Burden** | **Very Low** (Fast native Linux test suite, 0 skips) | **Low** (Linux CI runs fast; Windows CI optional) | **High** (Dual CI runners, Windows runner costs, PyInstaller builds) |
| **Risk of Linux Regressions** | **Zero** (Windows changes cannot pollute Linux) | **Near-Zero** (Strictly bounded) | **High** (Windows-specific fixes frequently break Linux test discovery) |
| **Long-Term Development Cost** | **Lowest** | **Manageable** | **High** |

---

## 4. Deep-Dive Tradeoff Analysis

### 4.1 Maintenance & Anti-Tamper Realities (The Byfron Factor)
On Windows, Roblox is protected by Byfron (Hyperion anti-cheat). While AERAM's current mutex pre-emption technique does not inject code or mutate process memory, Roblox regularly updates its startup sequence, command-line parsing, and synchronization object names. Maintaining Windows support requires constant vigilance against Byfron detection heuristics and silent client launch failures.

On Linux, Mocktail / Sober runs native Android ARM64/x86_64 translation layers with unencumbered multi-instance sandboxing. Linux multi-instance operation does not rely on mutex collision tricks; it relies standard POSIX directory separation.

### 4.2 Account Isolation Discrepancy
As documented in `docs/windows-port/SESSION_ISOLATION.md`, running 50+ concurrent accounts on Windows is inherently compromised by Roblox's hardcoded `%LOCALAPPDATA%\Roblox\LocalStorage\RobloxCookies.dat` path. When 50 instances start on Windows, SQLite file locks and cookie overwrite races are unavoidable without virtualization or separate Windows user accounts.

On Linux, 50+ accounts run in dedicated subdirectories with dedicated tmpfs sockets and independent cgroups without a single shared byte of mutable state.

### 4.3 Test Suite & Environment Fragility
The current codebase on `origin/main` demonstrates the exact failure mode of Option C:
- Windows developers added `import winreg` and `ctypes.windll` at top-level in tests, breaking Linux test discovery.
- Windows developers added `PyQt6-WebEngine` without verifying its availability on Linux.
- Windows developers wrote tests in `pytest` while the Linux core relies on standard `unittest`.

Maintaining Option C requires a disciplined dual-platform CI setup that blocks PRs whenever either platform breaks.

---

## 5. Architectural Recommendation

### Primary Recommendation: **Option A (Linux-Only) with Option B Staging**

AERAM should return to a **pure Linux-first architecture (Option A)**.

#### Rationale:
1. **Product Identity:** AERAM's unique value proposition is ultra-dense, 50+ instance farming with 330 FPS graphics tuning on Linux workstations. On Windows, dozens of generic account managers (e.g. ic3w0lf22 RAM) already exist. On Linux, AERAM is the undisputed premier solution.
2. **Superior Technical Foundation:** The Linux container sandbox is structurally superior to anything achievable on Windows without kernel drivers.
3. **Salvage Value Secured:** The Windows port was not wasted effort. It yielded four major architectural upgrades that immediately benefit Linux:
   - The 9-state `AccountAuthState` lifecycle engine.
   - The `AERAM_VAULT_EXPORT_V1` encrypted migration engine.
   - Detailed HTTP 401/403/429 status parsing in `RobloxAPI`.
   - The achromatic `ae` ligature branding assets.
4. **Preservation without Drag:** Windows support should be permanently tagged (`archive/v0.2.0-windows-port`) and isolated. If a dedicated Windows maintainer emerges in the future, it can be re-introduced as a pluggable provider (Option B). For the primary maintainer, shedding Windows maintenance will cut development friction in half.
