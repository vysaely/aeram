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

**Requirements:** Linux x86_64 (Wayland or X11), Python 3.10+, and Mocktail installed as a **native package**
(AUR `mocktail-bin`, or the `.deb` / `.rpm` from [Mocktail releases](https://github.com/komaruworld/mocktail/releases)).
The Mocktail **Flatpak and AppImage do not work** with AERAM: running several accounts at once needs a separate
copy of the Mocktail program per account, which their sandboxes don't allow.

### Arch Linux / CachyOS / Manjaro

```bash
sudo pacman -S --needed python python-pyqt6 python-pyqt6-webengine python-pynacl python-evdev base-devel sdl3 mesa
paru -S --needed mocktail-bin   # or: yay -S mocktail-bin
```

### Debian / Ubuntu

```bash
sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine python3-nacl python3-evdev build-essential libsdl3-dev libgles-dev
# Mocktail: download mocktail_<version>_amd64.deb from the releases page, then:
sudo apt install ./mocktail_*_amd64.deb
```

### Fedora

```bash
sudo dnf install python3-pyqt6 python3-pyqt6-webengine python3-pynacl python3-evdev gcc make SDL3-devel mesa-libGLES-devel
# Mocktail: download mocktail-<version>.x86_64.rpm from the releases page, then:
sudo dnf install ./mocktail-*.x86_64.rpm
```

The build packages (`base-devel`/`build-essential`/`gcc`, SDL3 and OpenGL ES headers) are only needed for AERAM's
optional native helper (chat overlay fix, in-game Anti-AFK). Without them AERAM still works.

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

## Tips

- **Lowest input latency:** Settings → Client Engine & FastFlags → Graphics Backend → **Vulkan**. Main accounts
  then run on Mocktail's Vulkan renderer; Roblox's in-game graphics slider controls quality. Alts always use OpenGL.
- **Dark games:** keep Shadows & Lighting on **Full**; Disabled/Simple freeze Roblox's lighting.

## Development

```bash
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```

`tests/conftest.py` redirects HOME, XDG directories and PATH to a throwaway sandbox, so the suite never touches a real vault or Mocktail install.

## License

[GNU General Public License v3.0](LICENSE)
