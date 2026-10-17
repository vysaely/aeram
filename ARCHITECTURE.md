# AERAM architecture

AERAM is a PyQt6 desktop app (plus a CLI) that stores Roblox accounts and runs one
[Mocktail](https://github.com/komaruworld/mocktail) instance per account.

## Layout

| Path | What it does |
| --- | --- |
| `core/storage.py` | SQLite (`manager.db`, WAL) for accounts, groups, settings and favorites; builds per-account sandboxes |
| `core/vault.py` | libsodium SecretBox (XSalsa20-Poly1305) encryption of cookies, passwords and TOTP secrets |
| `core/instance_runner.py` | Launches, tracks and stops Mocktail instances |
| `core/roblox_api.py` | Roblox web API calls (profiles, presence, friends, privacy, sign-out, login) |
| `core/mocktail_manager.py` | Finds the Mocktail install and explains how to fix a missing one |
| `core/anti_afk.py` | Anti-AFK subprocess (pulse file plus compositor fallback) |
| `core/ram_importer.py`, `core/migration.py` | Import from Windows RAM (`AccountData.json`, encrypted or not) and `.aeram` archives |
| `core/paths.py` | Data directory: `~/.local/share/aeram`, or the pre-rename `~/.local/share/ram-linux` when that is where the accounts are |
| `ui/` | Main window and dialogs |
| `native/libmocktail_mouse_helper.c` | `LD_PRELOAD` helper inside each game: chat overlay, mouse fixes, in-process anti-AFK, 240 FPS clamp removal |
| `native/mocktail/*.patch` | Optional patches on upstream Mocktail (`scripts/build_mocktail.sh`) |

## Storage

- `manager.db` and `vault.key` are owner-only (`0600`). The key is a random 256-bit key with no master
  password, so the encryption protects a copied database, not a readable home directory.
- `mains.txt` lists the main accounts; every other account is an alt (low graphics, muted, low FPS).

## Running several instances

Mocktail takes a lock named after a hash of `/proc/self/exe`, so two copies of the same binary cannot run
together. Each account gets its own hardlink `~/.local/lib/mocktail/mocktail_<account_id>` next to
Mocktail's libraries, which gives it its own lock.

Each instance also gets:

- a profile under `<data dir>/instances/<account_id>/`, with the shared Mocktail payloads symlinked in
  so nothing is downloaded twice;
- its own `XDG_RUNTIME_DIR` at `/run/user/<uid>/aeram/<account_id>`, with the Wayland, PipeWire, Pulse and
  D-Bus sockets symlinked in;
- the session cookie through `MOCKTAIL_COOKIE_FILE` (a `0600` file, removed when the instance exits).

Instances start in their own session (`setsid`), so AERAM can stop the whole process group and find
running games again after a restart.
