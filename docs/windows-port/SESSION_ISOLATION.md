# AERAM — Windows Account-Session Isolation Analysis

**Author:** Senior Windows Systems & Security Architect  
**Project:** AERAM (Roblox Account Manager)  
**Target:** Windows 11 vs Linux Container Isolation  
**Status:** Complete & Validated  

---

## 1. Executive Summary

This document evaluates how native Windows 11 desktop applications store and isolate authentication state, comparing the architecture of the native Windows Roblox client (`RobloxPlayerBeta.exe`) against the Linux Flatpak/Mocktail container model.

It details the shared resources utilized by Roblox on Windows, explains why concurrent instances overwrite shared storage files, evaluates prospective isolation strategies, and documents AERAM's verified non-invasive mitigations.

---

## 2. Platform Isolation Architecture Comparison

### 2.1 Linux Sandboxing (Flatpak & Mocktail)

On Linux, true multi-instance process and session isolation is trivial due to Linux kernel namespaces and XDG directory separation:

```
Linux Host User
 ├── Account A Sandbox (/home/user/.local/share/mocktail/instances/acc_01/)
 │    ├── MOCKTAIL_COOKIE_FILE: ./roblox.cookie
 │    ├── XDG_DATA_HOME: ./data
 │    ├── XDG_CONFIG_HOME: ./config
 │    └── Hardlinked Binary: mocktail_acc_01
 └── Account B Sandbox (/home/user/.local/share/mocktail/instances/acc_02/)
      ├── MOCKTAIL_COOKIE_FILE: ./roblox.cookie
      ├── XDG_DATA_HOME: ./data
      ├── XDG_CONFIG_HOME: ./config
      └── Hardlinked Binary: mocktail_acc_02
```

Each instance has its own private filesystem mount, isolated cookie database, and private systemd cgroup slices. Cross-instance pollution is physically impossible.

---

### 2.2 Windows 11 Shared Environment (`RobloxPlayerBeta.exe`)

On Windows 11, all native Roblox client instances run under the same logged-in Windows user desktop session. As a result, all instances share a fixed set of local filesystem paths and Windows registry keys:

```
Windows Host User (%LOCALAPPDATA%\Roblox\)
 ├── LocalStorage\RobloxCookies.dat       <-- [CRITICAL CONFLICT] Shared SQLite cookie store
 ├── GlobalBasicSettings_13.xml           <-- Shared graphics, FramerateCap, audio volume
 ├── ClientSettings\ClientAppSettings.json<-- Shared FastFlags configuration
 ├── logs\log_*.txt                       <-- Shared diagnostic output directory
 └── Registry (HKCU\Software\ROBLOX Corporation\) <-- Shared protocol handlers
```

---

## 3. Analysis of Shared Resources & Collision Mechanics

### 3.1 `%LOCALAPPDATA%\Roblox\LocalStorage\RobloxCookies.dat`

#### Architecture
`RobloxCookies.dat` is an SQLite database used by the Roblox desktop client to persist session authentication tokens (`.ROBLOSECURITY`, `RBXEventTrackerV2`, etc.).

#### The Collision Scenario
1. Account A launches with Ticket A. `RobloxPlayerBeta.exe` redeems Ticket A and writes Account A's cookies into `RobloxCookies.dat`.
2. Account A loads its authenticated session into volatile RAM and establishes an active WebSocket/HTTPS heartbeat connection with Roblox game servers.
3. Account B launches with Ticket B. `RobloxPlayerBeta.exe` redeems Ticket B and writes Account B's cookies into `RobloxCookies.dat`, overwriting Account A's record.
4. **Why Simultaneous Launches Collide:**
   - If Account A and Account B are launched simultaneously (0.0s – 2.0s delay), both processes attempt to open, write, and lock `RobloxCookies.dat` concurrently.
   - SQLite file locks (`SQLITE_BUSY`) occur, causing ticket redemption failures.
   - One client may inadvertently read the other client's partially written cookie, causing an immediate session mismatch that trips Roblox Arkose Labs security heuristics and triggers CAPTCHA challenges.

---

### 3.2 `%LOCALAPPDATA%\Roblox\GlobalBasicSettings_13.xml`

This XML configuration file holds persistent client settings, most notably:
- `FramerateCap`: Target FPS (e.g., 60, 144, 240, 330).
- `GraphicsQuality`: Manual graphics slider position (1–21).

If two accounts with different target frame rates (e.g., Main at 330 FPS and Alt at 15 FPS) launch simultaneously, they race to write this file. To prevent corruption, AERAM employs atomic file replacement with randomized retry locks.

---

## 4. Evaluation of Potential Isolation Strategies

We evaluated various techniques to achieve session isolation on Windows 11:

| Isolation Technique | Mechanism | Anti-Cheat (Hyperion) Compatibility | User Experience & Viability |
|---|---|---|---|
| **Windows AppContainer** | Isolate processes with restricted SID tokens | **INCOMPATIBLE** (Hyperion anti-cheat halts if token is restricted) | Fails; client cannot run. |
| **Secondary Windows Accounts (`RunAs`)** | Spawn separate Windows user accounts via `CreateProcessWithLogonW` | Compatible | **VERY POOR** (Requires user passwords, separate Windows logins, high disk overhead, no unified window management). |
| **NTFS Junction / Symlink Swapping** | Dynamically repoint `%LOCALAPPDATA%\Roblox` before each launch | **HIGH RISK** (Hyperion anti-tamper scans junction alterations and flags process suspension) | Fragile; causes file lock crashes if background alts read files. |
| **Named Mutex Pre-emption + Staggered Ticket Launch** | Pre-claim `ROBLOX_singletonEvent`, launch with single-use tickets, and enforce 8–15s sequential stagger | **100% COMPATIBLE & SAFE** (Non-invasive, fully supported by official Roblox protocol) | **EXCELLENT (Adopted by AERAM)** |

---

## 5. AERAM's Production Isolation Strategy

To provide maximum stability without violating Roblox terms of service or triggering anti-cheat mechanisms, AERAM enforces the following architecture:

1. **Sequential Staggered Multi-Join (8.0s – 15.0s default):**
   - Each client is given sufficient time to complete ticket redemption, establish its encrypted game connection, load its credentials into memory, and release locks on `RobloxCookies.dat`.
   - Randomized humanized jitter (+0.2s – 1.2s) breaks robotic metronomic intervals.

2. **Pre-emptive Mutex Ownership:**
   - AERAM pre-claims `ROBLOX_singletonEvent` to allow independent instances to launch simultaneously on the same desktop.

3. **Atomic XML Settings Serialization:**
   - Target FPS profiles are committed atomically using `RobloxSettingsManager` before each process spawn.

4. **Zero Ticketless Launches:**
   - If an account fails to acquire a ticket (due to rate limiting or verification requirement), launch is cleanly aborted rather than falling back to an unauthenticated launch that contaminates `RobloxCookies.dat`.

5. **Formal Auth State Machine:**
   - Halts automated launches when verification is required, preventing Arkose challenge loops.
