#!/usr/bin/env bash
# ==============================================================================
# AERAM: Installer & Linux Desktop Integration Script
# Authentic Roblox Account Manager for Linux (Mocktail Runtime)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${HOME}/.local"

# Parse CLI arguments
UNINSTALL=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --uninstall|-u)
            UNINSTALL=true
            shift
            ;;
        --prefix|-p)
            PREFIX="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --prefix <path>   Installation prefix (default: ~/.local)"
            echo "  --uninstall, -u   Remove AERAM desktop entries and launcher shortcuts"
            echo "  --help, -h        Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

BIN_DIR="${PREFIX}/bin"
DESKTOP_DIR="${PREFIX}/share/applications"
ICON_DIR="${PREFIX}/share/icons/hicolor/scalable/apps"
ICON_512_DIR="${PREFIX}/share/icons/hicolor/512x512/apps"

# Handle uninstallation
if [ "$UNINSTALL" = true ]; then
    echo "=========================================================="
    echo "  AERAM: Uninstalling Desktop Integration"
    echo "=========================================================="
    echo ""
    echo "Removing CLI launcher shortcuts..."
    rm -f "${BIN_DIR}/aeram" "${BIN_DIR}/aem" "${BIN_DIR}/ram-linux"
    
    echo "Removing desktop entry and icons..."
    rm -f "${DESKTOP_DIR}/aeram.desktop" "${DESKTOP_DIR}/ram-linux.desktop" "${DESKTOP_DIR}/roblox-account-manager.desktop"
    rm -f "${ICON_DIR}/aeram.svg" "${ICON_512_DIR}/aeram.png"
    
    if command -v update-desktop-database &> /dev/null; then
        update-desktop-database "${DESKTOP_DIR}" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache &> /dev/null; then
        gtk-update-icon-cache -q -t -f "${PREFIX}/share/icons/hicolor" 2>/dev/null || true
    fi
    
    echo "Done. AERAM desktop integration removed."
    echo "Note: Account vaults in ~/.local/share/aeram were preserved."
    exit 0
fi

echo "=========================================================="
echo "  AERAM: Linux Installer & System Integration"
echo "  Target: Arch Linux / Hyprland / KDE Plasma / Generic Wayland"
echo "=========================================================="
echo ""

# 1. Check Python version
echo "[1/5] Checking Python runtime..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 is required but not installed."
    echo "  Arch Linux: sudo pacman -S python"
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  * Detected Python ${PY_VER}"
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    echo "ERROR: Python 3.10 or newer is required."
    exit 1
fi

# 2. Check Dependencies
echo "[2/5] Checking required Python dependencies..."
MISSING_DEPS=0

if ! python3 -c "import PyQt6" &> /dev/null; then
    echo "  [✗] PyQt6 is missing."
    MISSING_DEPS=1
else
    PYQT_VER=$(python3 -c "from PyQt6.QtCore import PYQT_VERSION_STR; print(PYQT_VERSION_STR)")
    echo "  [✓] PyQt6 ${PYQT_VER} installed"
fi

if ! python3 -c "import PyQt6.QtWebEngineWidgets" &> /dev/null; then
    echo "  [✗] PyQt6-WebEngine is missing. Required for embedded Roblox browser login."
    MISSING_DEPS=1
else
    echo "  [✓] PyQt6-WebEngine installed (in-app browser login enabled)"
fi

if python3 -c "import nacl" &> /dev/null; then
    echo "  [✓] PyNaCl security library installed"
elif python3 -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}'); from core.vault import Vault" &> /dev/null; then
    echo "  [✓] Native libsodium security provider installed (ctypes)"
else
    echo "  [✗] Neither PyNaCl nor native libsodium is available. Required for encrypted credentials vault."
    MISSING_DEPS=1
fi

if ! python3 -c "import evdev" &> /dev/null; then
    echo "  [✗] evdev is missing. Required for hardware idle sensing."
    MISSING_DEPS=1
else
    echo "  [✓] evdev installed (hardware idle monitor enabled)"
fi

if [ $MISSING_DEPS -ne 0 ]; then
    echo ""
    echo "ERROR: Missing required dependencies. Install them via your package manager:"
    echo "  Arch Linux / CachyOS: sudo pacman -S --needed python-pyqt6 python-pyqt6-webengine python-pynacl python-evdev"
    echo "  Debian / Ubuntu:      sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine python3-nacl python3-evdev"
    echo "  Fedora:               sudo dnf install python3-pyqt6 python3-pyqt6-webengine python3-pynacl python3-evdev"
    echo "  Pip fallback:         pip install PyQt6 PyQt6-WebEngine pynacl evdev"
    exit 1
fi

# 3. Mocktail Discovery
echo "[3/5] Inspecting Mocktail Roblox runtime..."
MOCKTAIL_INFO=$(python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from core.mocktail_manager import MocktailManager
mgr = MocktailManager()
info = mgr.detect()
if info.is_installed:
    print(f'INSTALLED|{info.bin_path}|{info.version_name or info.payload_id or \"unknown\"}')
else:
    print('MISSING')
" 2>/dev/null || echo "MISSING")

if [[ "${MOCKTAIL_INFO}" == INSTALLED* ]]; then
    IFS='|' read -r _ M_PATH M_VER <<< "${MOCKTAIL_INFO}"
    echo "  [✓] Mocktail detected: ${M_PATH} (Version: ${M_VER})"
else
    echo "  [!] Mocktail binary not found on PATH or standard locations."
    echo "      AERAM will manage accounts, but launching requires Mocktail."
    echo "      Install Mocktail on Arch Linux via AUR:"
    echo "        paru -S --needed mocktail-bin   # or yay -S mocktail-bin"
fi

# 4. Native companion helper compilation
echo "[4/5] Building native companion helper (libmocktail_mouse_helper.so)..."
if [ -f "${SCRIPT_DIR}/native/Makefile" ]; then
    if command -v make &> /dev/null && command -v gcc &> /dev/null; then
        if make -C "${SCRIPT_DIR}/native" > /dev/null 2>&1; then
            echo "  [✓] Compiled native/libmocktail_mouse_helper.so successfully."
            # Also copy to ~/.local/lib/mocktail
            mkdir -p "${HOME}/.local/lib/mocktail"
            cp "${SCRIPT_DIR}/native/libmocktail_mouse_helper.so" "${HOME}/.local/lib/mocktail/" 2>/dev/null || true
        else
            echo "  [!] Native helper compilation failed. In-process signal IPC fallback will be used."
        fi
    else
        echo "  [i] make or gcc not found. Skipping helper compilation."
        echo "      To enable low-level SDL3 event injection: sudo pacman -S base-devel && make -C native"
    fi
fi

# 5. Install CLI launchers and Desktop Entry
echo "[5/5] Installing desktop integration..."
chmod +x "${SCRIPT_DIR}/aeram" "${SCRIPT_DIR}/ram-linux"

mkdir -p "${BIN_DIR}"
ln -sf "${SCRIPT_DIR}/aeram" "${BIN_DIR}/aeram"
ln -sf "${SCRIPT_DIR}/aeram" "${BIN_DIR}/aem"
ln -sf "${SCRIPT_DIR}/aeram" "${BIN_DIR}/ram-linux"
echo "  * Symlinked ${BIN_DIR}/aeram -> ${SCRIPT_DIR}/aeram"

mkdir -p "${DESKTOP_DIR}"
mkdir -p "${ICON_DIR}"
mkdir -p "${ICON_512_DIR}"

# Install Scalable Vector Icon
if [ -f "${SCRIPT_DIR}/assets/aeram.svg" ]; then
    cp "${SCRIPT_DIR}/assets/aeram.svg" "${ICON_DIR}/aeram.svg"
elif [ -f "${SCRIPT_DIR}/assets/roblox-account-manager.svg" ]; then
    cp "${SCRIPT_DIR}/assets/roblox-account-manager.svg" "${ICON_DIR}/aeram.svg"
fi

# Install High-Resolution 512x512 Raster Icon
if [ -f "${SCRIPT_DIR}/assets/aeram.png" ]; then
    cp "${SCRIPT_DIR}/assets/aeram.png" "${ICON_512_DIR}/aeram.png"
fi

cat <<DESKTOPEOF > "${DESKTOP_DIR}/aeram.desktop"
[Desktop Entry]
Type=Application
Name=AERAM
GenericName=Roblox Account Manager
Comment=Manage and launch isolated Roblox accounts concurrently on Linux
Exec=${BIN_DIR}/aeram --gui %u
Icon=aeram
Terminal=false
Categories=Game;
Keywords=aeram;aem;roblox;ram;account;manager;mocktail;hyprland;kde;
StartupWMClass=aeram
StartupNotify=true
Actions=StopAll;ListAccounts;Diagnostics;

[Desktop Action StopAll]
Name=Stop All Running Instances
Exec=${BIN_DIR}/aeram --stop-all

[Desktop Action ListAccounts]
Name=List Accounts (Terminal)
Exec=${BIN_DIR}/aeram --list

[Desktop Action Diagnostics]
Name=Run System Diagnostics
Exec=${BIN_DIR}/aeram --diagnostics
DESKTOPEOF

chmod 644 "${DESKTOP_DIR}/aeram.desktop"
# Remove legacy desktop entries if present
rm -f "${DESKTOP_DIR}/ram-linux.desktop" "${DESKTOP_DIR}/roblox-account-manager.desktop"
echo "  * Installed ${DESKTOP_DIR}/aeram.desktop"

if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "${DESKTOP_DIR}" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache &> /dev/null; then
    gtk-update-icon-cache -q -t -f "${PREFIX}/share/icons/hicolor" 2>/dev/null || true
fi

echo ""
echo "=========================================================="
echo "  AERAM installed successfully!"
echo "  - Application Launcher: Search 'AERAM' or 'Roblox Account Manager'"
echo "  - Terminal Launch:      aeram"
echo "  - Diagnostic Report:    aeram --diagnostics"
echo "=========================================================="
