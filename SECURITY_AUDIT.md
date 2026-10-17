# SECURITY AUDIT: AERAM (Roblox Account Manager for Linux)

**Target Repository**: `https://github.com/vysaely/aeram`  
**Audited Commit**: `f4c5201` (branch: `main`)  
**Audited Date**: October 17, 2026  
**Auditor**: Application Security & Linux Systems Review  

---

## 1. Executive Summary

A comprehensive source-level and runtime security audit was conducted on **AERAM** (`vysaely/aeram`).  
AERAM is a Linux-native desktop application built with Python 3, PyQt6, and native C companions, designed to isolate and run concurrent Roblox sessions via the Mocktail runtime on Wayland and X11 environments.

### Overall Assessment
AERAM implements strong systems-level isolation and cryptographic hygiene:
1. **Encrypted Vault**: Credentials and `.ROBLOSECURITY` session tokens are stored locally in SQLite encrypted with `libsodium` SecretBox (`XSalsa20-Poly1305`) and strict `0600` / `0700` filesystem permissions.
2. **Zero Telemetry**: The codebase contains zero external trackers, analytics, or third-party webhooks. Network traffic is restricted strictly to official Roblox APIs (`*.roblox.com`) and GitHub release queries.
3. **True Session Decoupling**: Each account executes with independent inode-hardlinks (`mocktail_<account_id>`), dedicated `XDG_DATA_HOME` / `XDG_RUNTIME_DIR` namespaces, and isolated `MOCKTAIL_COOKIE_FILE` paths, preventing session cross-talk and lock collisions.
4. **Hardened Anti-AFK**: Utilizes non-focus-stealing in-process event injection via `SIGUSR1` and an owner-only (`0600`) `/dev/shm` pulse file, monitored by `evdev` hardware idle sensors to prevent typing disruption.

---

## 2. Threat Model & Cryptographic Architecture

### Key Management & Storage
- **Master Key**: Generated using `libsodium.crypto_secretbox_keygen()` (256-bit random key) and stored at `~/.local/share/ram-linux/vault.key` (or `~/.local/share/aeram/vault.key`).
- **File Permissions**: The key file and database are created and explicitly enforced with `0600` POSIX permissions (`-rw-------`). Directories are enforced with `0700` (`drwx------`).
- **Cipher**: `crypto_secretbox_easy` (XSalsa20 stream cipher with Poly1305 MAC) providing authenticated encryption against tampering and offline inspection.

### Credential Handling & Passwords
- **Tokens**: `.ROBLOSECURITY` session tokens are stored encrypted and injected into per-instance runtime files at launch with `0600` permissions.
- **Passwords**: Account passwords, if supplied by the user for convenience (e.g. quick relogging, automated password changes, or manual clipboard copy), are encrypted with `libsodium` SecretBox before persistence. Plaintext passwords are never stored in the database.
- **Recommendations**:
  - Keep password saving optional per account.
  - Never allow silent plaintext fallbacks if the cryptographic provider is unavailable.

---

## 3. Subprocess & Process Supervision Security

1. **Child Process Launching**: Mocktail instances are invoked via `subprocess.Popen` using explicit argument lists (never `shell=True`).
2. **Resource Throttling**: Background alt accounts are wrapped with `systemd-run --user --scope` applying `MemoryHigh` and `CPUWeight=1` to prevent denial-of-service / memory thrashing of the primary user session.
3. **Hardlink Binary Inode Decoupling**: Hardlinks are placed in user-private `~/.local/lib/mocktail/` to decouple Mocktail's `/proc/self/exe` single-instance FNV-1a hash mutex without granting elevated privileges.

---

## 4. Audit Findings & Remediations

| Finding | Severity | Status | Remediation |
|---|---|---|---|
| **AERAM-01**: Root audit doc referenced legacy project | High | Resolved | Replaced with this scoped AERAM security audit. |
| **AERAM-02**: CLI flags without subcommands opened GUI | High | Resolved | Enforced strict CLI argument validation and error routing. |
| **AERAM-03**: Hardcoded font payload path in runner | Medium | Resolved | Dynamic active payload resolution via Mocktail discovery. |
| **AERAM-04**: Updater unverified build exit codes | Medium | Resolved | Added build returncode checks. Note: the git path is a fast-forward pull; the standalone-archive path merges files in place and is not atomic (see AERAM-15). |
| **AERAM-05**: Password retention in encrypted vault | Medium | Addressed | Retained as optional encrypted feature; documented model. |
| **AERAM-06**: Monolithic UI module sizes | Low | Tracked | Planned modularization in upcoming non-breaking releases. |

---

## 5. Hardening Pass (2026-10-17, branch `audit/hardening-20261017`)

| Finding | Severity | Status | Remediation |
|---|---|---|---|
| **AERAM-07**: `core/instance_runner.py` annotated with undefined `Tuple`; on Python 3.10–3.13 (supported per `pyproject.toml`) importing the module raises `NameError`, so neither GUI nor CLI starts. Masked on 3.14 by deferred annotations. | High | Resolved | Import added; regression test resolves all annotations. Verified import on CPython 3.12 and 3.13. |
| **AERAM-08**: `terminate_instance` sent SIGTERM/SIGKILL to `getpgid(pid)` without checking the PID still belonged to Mocktail. A recycled PID, or an adopted instance sharing a group with a shell or AERAM itself, could get unrelated processes killed. | High | Resolved | Signals only go to PIDs whose cmdline is Mocktail and owned by the user; the group is signalled only when the instance leads it and it is not AERAM's own group. |
| **AERAM-09**: Process scan attributed processes by substring (`"acc_1" in "mocktail_acc_10"`), so stopping one account could terminate another's client. | Medium | Resolved | Exact `mocktail_<account_id>` match in both scanners. |
| **AERAM-10**: Per-account hardlinked binaries were never refreshed after Mocktail replaced its binary (new inode), so accounts kept launching the stale client. | Medium | Resolved | Re-provision when the link no longer mirrors the source; staged and renamed atomically. |
| **AERAM-11**: `/dev/shm/aeram_afk_pulse` and `/dev/shm/roblox_typing_active` were forced world-writable (`0666`), letting any local user inject Anti-AFK input or flip the typing flag. | Medium | Resolved | Created `0600` with `O_NOFOLLOW`; native helper ignores pulse files not owned by the user. Paths unchanged for external tool compatibility. |
| **AERAM-12**: Invalid CLI flag combinations were rejected only after opening the vault and running instance startup cleanup. The test suite hit this against the developer's live vault. | Medium | Resolved | Validation runs before any state is touched; `tests/conftest.py` isolates HOME/XDG/PATH/D-Bus for every test. |
| **AERAM-13**: Concurrent first-run key generation (GUI and CLI together) could overwrite `vault.key` after data was encrypted under the first key; key was not fsynced. | Low | Resolved | Key published with `link()` (adopts an existing key), fsynced file and directory. |
| **AERAM-14**: Batch 2FA workers could block forever if `stop()` landed between the stop check and `Event.clear()`. | Low | Resolved | Stop flag re-checked after clearing. |
| **AERAM-15**: Standalone-archive updates copy files over the install in place; an interruption leaves a mixed tree. Updates trust GitHub `main` without signature verification. | Low | Open | Accepted for now; git installs are unaffected. |

---

## 6. Verification Limits

This audit evaluated commit `f4c5201` running on Linux x86_64 with Mocktail 2.736.x. It does not certify third-party codebases, closed-source Mocktail binaries, or upstream Roblox API service alterations.
