# ARCHITECTURE DECISION DOCUMENT (ADR)
## AERAM (Roblox Account Manager for Linux)

**Status**: IMPLEMENTED & AUDITED  
**Date**: October 17, 2026  
**Auditor & Architect**: Principal Systems & Security Engineer  

---

## 1. Context & Objectives

The goal is to deliver a production-quality, Linux-native reimplementation of Roblox Account Manager (RAM) for Arch Linux / CachyOS running Hyprland / KDE Plasma / Wayland, supporting the Mocktail runtime.

Key Requirements:
1. **Concurrent Multi-Roblox Instances**: Launch multiple independent Roblox accounts at the exact same time without singleton mutex collisions, session overwrites, or crashes via inode hardlink decoupling.
2. **Account & Credential Security**: Primary authentication relies strictly on encrypted `.ROBLOSECURITY` session tokens in an authenticated vault with `0600` permissions. Optional password retention is encrypted via libsodium SecretBox for quick relogging, with per-account and global purge capabilities.
3. **Compatibility with Original RAM**: Import Windows RAM `AccountData.json` (plaintext and encrypted Argon2+libsodium formats), cookie lists, and CSV exports.
4. **Wayland & Linux Native Desktop Integration**: Native `.desktop` entry, application icon, Wayland-friendly dark theme, first-class CLI, and fast startup with negligible idle CPU/RAM overhead.
5. **No Web-Based Shell Bloat**: Reject heavy Electron/Node runtime overheads; deliver a lean, responsive desktop application.

---

## 2. Evaluation of Implementation Strategies

| Criterion | Strategy A: Harden Kerneluxx Python/Qt | Strategy B: Pure Native Rust + GUI (Iced/Slint/Egui) | Strategy C: Clean Modular Native Python 3 + PyQt6 / Rust Core |
| :--- | :--- | :--- | :--- |
| **Security** | Low (architected around passwords & clipboard) | High (memory-safe, static typing, strong crypto) | High (zero passwords, authenticated encryption, strict 0600) |
| **Multi-Instance Support** | Broken (ineffective `--env=XDG_DATA_HOME`) | Requires custom flatpak process orchestration | Fully supported via containerized `$HOME` sandboxes |
| **Performance (Idle RAM/CPU)** | Low (~45 MB RAM, 0% CPU) | Extremely low (<15 MB RAM, 0% CPU) | Low (~48 MB RAM, 0% CPU) |
| **Wayland / Hyprland Integration** | Fair (PySide6) | Variable (Wayland protocol support varies in Rust GUI toolkits) | Excellent (PyQt6 uses Qt Wayland QPA with fractional scaling & native decorations) |
| **Development Velocity & Maintainability** | Poor (requires rewriting core assumptions) | Moderate (heavy compile times, Rust GUI ecosystem churn) | Superior (PyQt6 already installed on host system in `/usr/lib/python3.14`) |
| **Packaging & Usability** | Python virtualenv / Pip | Single static ELF binary | Single launcher script / PKGBUILD / `.desktop` launcher |

### Selected Strategy: Strategy C (Clean Linux-Native Architecture with PyQt6 + Direct Process Supervision)

**Tradeoff Rationale**:
- Strategy A is fundamentally flawed: its codebase was designed around copying user passwords to the clipboard, has a committed vault in git, and cannot launch multiple instances.
- Strategy B (Pure Rust GUI) has high engineering merits for memory safety, but desktop GUI toolkits in Rust (Iced, Slint, egui) suffer from incomplete Wayland input method (IME), fractional scaling quirks on Hyprland, and non-native desktop integration compared to Qt 6.
- The host system already has **PyQt6 installed natively** (`/usr/lib/python3.14/site-packages/PyQt6`), **Python 3.14**, and **Flatpak 1.16**.
- By building a clean, decoupled architecture with strict domain boundaries, we get instant Wayland fidelity, zero Electron bloat, 0% idle CPU overhead, and robust multi-instance process supervision.

---

## 3. Subsystem Decomposition & Ownership Boundaries

```
+---------------------------------------------------------------------------------+
|                                 USER INTERFACES                                 |
|      +-----------------------------------+   +----------------------------+     |
|      |        PyQt6 Graphical UI         |   |    CLI Interface           |     |
|      | (Cards, Badges, Modals, Hotkeys)  |   | (List, Launch, Stop, Info) |     |
|      +-----------------+-----------------+   +--------------+-------------+     |
+------------------------|------------------------------------|-------------------+
                         +-----------------+------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------+
|                                APPLICATION CORE                                 |
|                                                                                 |
|   +-----------------------+   +----------------------+   +------------------+   |
|   |   Account Manager     |   |   Instance Runner    |   |  Roblox Client   |   |
|   | (CRUD, Groups, Filter)|   | (Flatpak Supervision)|   | (API / Metadata) |   |
|   +-----------+-----------+   +----------+-----------+   +--------+---------+   |
|               |                          |                        |             |
|               v                          v                        v             |
|   +-----------------------+   +----------------------+   +------------------+   |
|   |   Encrypted Vault     |   |  Sandbox Filesystem  |   |  RAM Importer    |   |
|   |  (AES-GCM / 0600 db)  |   | (Asset deduplication)|   | (Windows RAM/Enc)|   |
|   +-----------------------+   +----------------------+   +------------------+   |
+---------------------------------------------------------------------------------+
```

### 1. Application State & Account Metadata
- Central repository for account records, groups, tags, and usage statistics.
- Storage backend: SQLite (`manager.db`) with `WAL` mode and atomic transactional updates.
- Stored attributes: `account_id`, `username`, `display_name`, `user_id`, `avatar_url`, `robux_balance`, `group`, `notes`, `last_played`.

### 2. Credential Vault & Cryptography
- **Primary Authentication**: `.ROBLOSECURITY` session cookies stored with authenticated encryption (`libsodium` SecretBox `XSalsa20-Poly1305` or Argon2id) in SQLite (`manager.db`) with restrictive POSIX `0600` permissions.
- **Optional Password Storage**: Passwords, if provided by the user for fast relogging or batch password changes, are encrypted in SQLite using `libsodium` SecretBox. Users can delete stored passwords at any time (`clear_account_password` or `purge_all_passwords`) while keeping session cookies active. Plaintext fallback is strictly prohibited.
- **Two-Factor Authentication**: TOTP secret keys are encrypted in SQLite using `libsodium` SecretBox.
- **Keyfile Protection**: Protected keyfile (`vault.key`) with strict `0600` permissions. Corrupted keyfiles trigger aborts rather than destructive overwrites.

### 3. Client Adapters & Multi-Instance Process Supervision
- **Native Mocktail Isolation Architecture**:
  - Each account profile is assigned an isolated home directory:
    `~/.local/share/ram-linux/instances/<account_id>/`
  - Mocktail is executed directly with hardware-accelerated Vulkan (`--graphics direct-vulkan` on RADV / AMDGPU).
  - **Decoupled FNV-1a Lock Resolution**:
    Mocktail calculates its runtime lock (`/run/user/<uid>/mocktail.<hash>.instance.lock`) by computing an FNV-1a hash over `/proc/self/exe`. To allow concurrent execution of arbitrarily many instances without lock collisions, `ram-linux` creates a dedicated zero-byte hardlink `~/.local/lib/mocktail/mocktail_<account_id>` adjacent to all companion shared libraries.
  - **XDG Environment Isolation**:
    Per-account isolated trees for `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and `XDG_STATE_HOME`.
  - **Decoupled XDG_RUNTIME_DIR**:
    Unix domain sockets (`wayland-0`, `pulse`, `pipewire-0`, `bus`) are symlinked into a private per-instance runtime directory in `/run/user/<uid>/ram-linux/<account_id>`.
  - **Asset Deduplication**:
    Static payloads (`payloads/`) are symlinked from master shared storage (`~/.local/share/mocktail/payloads/`), eliminating multi-gigabyte re-downloads.
  - **Authentication**:
    Cookie is passed via `MOCKTAIL_COOKIE_FILE` with mode `0600` containing `.ROBLOSECURITY=<cookie>`.
  - **Process Supervision**: Uses `os.kill(pid, 0)` and process group tracking (`setsid`) to detect instance termination, crash recovery, and prevent duplicate launches.

### 4. Game & Server Discovery
- Supports:
  - Place ID: Direct game launching (`roblox://experiences/start?placeId=<id>`)
  - Job ID: Server instance joining (`&gameInstanceId=<job_id>`)
  - Private Server: Link or link code joining (`&linkCode=<code>`)
- API integration: Queries Roblox public endpoints for place details, server player counts, universe ID, and thumbnail assets.

### 5. Windows RAM Importer Subsystem
- Supports importing:
  - Original RAM `AccountData.json` (unencrypted / plaintext JSON).
  - Original RAM encrypted files (`RAMHeader` + Argon2 + libsodium secretbox).
  - Raw cookie lists (one `.ROBLOSECURITY` token per line).
  - CSV exports (`Username,Cookie,Group`).
  - Auto-enrichment: Fetches username, display name, user ID, and avatar headshot from Roblox API upon import.

### 6. Two-Step Verification (2FA / RFC 6238 TOTP) & Security Hardening
- Pure Python standard library implementation of RFC 6238 TOTP (HMAC-SHA1) with zero external dependencies.
- Google Authenticator QR migration parsing (multi-PNG screenshot accumulation, Protobuf wire decoding, Base32 normalization).
- Two-factor secret key storage encrypted inside local SQLite vault (`manager.db`) with Argon2id + XSalsa20-Poly1305.
- Cross-platform encrypted `.aeram` archive export and import preserving all TOTP keys and passwords.
- Multi-format 2FA export: standard `otpauth://` URIs, JSON, CSV, and username:key pairs.
- Automated 2FA challenge resolution during batch sign-out from other sessions.

### 7. Performance, Idle Efficiency & Storage Architecture
- **Single-Transaction Batch Persistence**: `save_accounts_batch()` aggregates arbitrary account additions and metadata updates into a single SQLite commit, slashing disk I/O and latency.
- **State-Snapshot Table Polling**: O(1) state tuple comparison `(running, pid, auth_state, pres_type, pres_place_id, pres_game_id)` prevents unnecessary table cell repaints when accounts are idle.
- **Debounced Filesystem Caching**: `mains.txt` role queries are throttled with a 3-second debounce, eliminating up to 35 filesystem `stat` syscalls per second.
- **Mocktail Runtime Check Throttling**: Reduces Mocktail capability scanning from 10s down to 60s once runtime presence is confirmed.
- **Clean Teardown Protocol**: Explicit `closeEvent` and dialog `reject` hooks cleanly terminate all child QTimers, QThreads, and detached Anti-AFK subprocesses to eliminate orphaned background tasks.

### 8. Desktop & System Integration
- Standard Freedesktop `.desktop` file installed to `~/.local/share/applications/aeram.desktop`.
- High-resolution SVG application icon installed to `~/.local/share/icons/hicolor/scalable/apps/aeram.svg`.
- CLI binary link: `~/.local/bin/aeram`.
