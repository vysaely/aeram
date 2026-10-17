# AERAM

A multi-account Roblox manager for Linux.

AERAM uses Mocktail to run multiple Roblox instances with per-account isolation. It brings account storage, graphics settings, authentication tools, session management, and server browsing into one application.

**Features**
- **Multi-instance management** — Run multiple Roblox accounts on Wayland and X11, with separate configurations and caches.
- **Encrypted account vault** — Store credentials and `.ROBLOSECURITY` session tokens locally, encrypted with libsodium.
- **Graphics and FastFlags** — Configure performance settings with presets or custom options.
- **Two-factor authentication** — Generate TOTP codes and import supported authenticator setups.
- **Session management** — Monitor connections, handle supported reconnect workflows, and manage background instances.
- **Server browsing** — Browse public servers and join experiences using Place IDs or Job IDs.

AERAM is an independent project focused on making multi-account management on Linux easier to configure and maintain.

Vibecoded.

## Installation

### Arch Linux / CachyOS / Manjaro

```bash
sudo pacman -S --needed python python-pyqt6 python-pyqt6-webengine python-pynacl python-evdev base-devel
paru -S --needed mocktail-bin   # or: yay -S mocktail-bin
```

### Setup

```bash
git clone https://github.com/vysaely/aeram.git
cd aeram
./install.sh
```

## Usage

```bash
aeram         # Launch GUI
aeram --help  # CLI commands
```

## Development

```bash
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```

`tests/conftest.py` redirects HOME, XDG directories and PATH to a throwaway sandbox, so the suite never touches a real vault or Mocktail install.

## License

[GNU General Public License v3.0](LICENSE)
