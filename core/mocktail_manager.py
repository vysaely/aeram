"""
Dedicated Mocktail Runtime Integration Layer for AERAM.
Responsible for detecting Mocktail installations, resolving executable paths,
identifying versions, validating configurations, checking companion helpers,
and providing safe, idempotent setup/rollback workflows.
"""

import os
import shutil
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

log = logging.getLogger("MocktailManager")



FLATPAK_APP_ID = "space.bigrat.mocktail"
RELEASES_URL = "https://github.com/komaruworld/mocktail/releases"


def _distro_family(os_release: Path = Path("/etc/os-release")) -> str:
    """Returns 'arch', 'debian', 'fedora' or 'other' from /etc/os-release."""
    try:
        fields = {}
        for line in os_release.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value.strip().strip('"').lower()
        ids = {fields.get("ID", "")} | set(fields.get("ID_LIKE", "").split())
    except OSError:
        return "other"
    for family, members in (
        ("arch", {"arch", "archlinux"}),
        ("debian", {"debian", "ubuntu"}),
        ("fedora", {"fedora", "rhel"}),
    ):
        if ids & members:
            return family
    return "other"

@dataclass
class MocktailInstallationInfo:
    """Represents detected Mocktail runtime state and capabilities."""
    is_installed: bool
    bin_path: Optional[Path] = None
    real_bin_path: Optional[Path] = None
    install_type: str = "missing"  # "aur_system", "user_local", "path", "missing"
    version_name: Optional[str] = None
    payload_id: Optional[str] = None
    lib_dir: Optional[Path] = None
    data_dir: Optional[Path] = None
    config_dir: Optional[Path] = None
    helper_path: Optional[Path] = None
    has_helper: bool = False
    config_valid: bool = False
    vulkan_available: bool = False
    opengl_available: bool = True
    issues: List[str] = None

    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class MocktailManager:
    """Manages Mocktail discovery, configuration validation, and diagnostic repair."""

    KNOWN_SEARCH_PATHS = [
        Path.home() / ".local" / "bin" / "mocktail",
        Path.home() / ".local" / "lib" / "mocktail" / "mocktail",
        Path("/usr/bin/mocktail"),
        Path("/usr/local/bin/mocktail"),
        Path("/opt/mocktail/bin/mocktail"),
    ]

    def __init__(self, custom_bin_path: Optional[Path] = None):
        self.custom_bin_path = custom_bin_path

    def detect(self) -> MocktailInstallationInfo:
        """Inspects the host system and constructs a complete MocktailInstallationInfo profile."""
        resolved_bin = self._resolve_executable()
        if not resolved_bin or not resolved_bin.exists():
            return MocktailInstallationInfo(
                is_installed=False,
                issues=["Mocktail executable not found in PATH or standard directories (~/.local, /usr/bin)."]
            )

        # Resolve real underlying ELF binary, bypassing shell wrapper scripts
        real_bin = self._resolve_real_elf(resolved_bin)
        install_type = self._classify_install(resolved_bin, real_bin)

        # Locate companion directories
        lib_dir = self._resolve_lib_dir(resolved_bin, real_bin)
        data_dir = self._resolve_data_dir(real_bin, resolved_bin)
        config_dir = self._resolve_config_dir()

        # Check payload and version
        version_name, payload_id = self._inspect_payload(data_dir)

        # Check companion native helper
        helper_path = self._find_helper(lib_dir)
        has_helper = helper_path is not None and helper_path.exists()

        # Check config validity
        config_valid, config_issues = self._validate_config(config_dir)

        # Check graphics drivers
        vulkan_avail = self._check_vulkan()

        issues = []
        if not version_name:
            issues.append("No active Roblox payload activated in Mocktail (current.json missing or incomplete).")
        if not has_helper:
            issues.append("Native input helper (libmocktail_mouse_helper.so) is missing or uncompiled.")
        issues.extend(config_issues)

        return MocktailInstallationInfo(
            is_installed=True,
            bin_path=resolved_bin,
            real_bin_path=real_bin,
            install_type=install_type,
            version_name=version_name,
            payload_id=payload_id,
            lib_dir=lib_dir,
            data_dir=data_dir,
            config_dir=config_dir,
            helper_path=helper_path,
            has_helper=has_helper,
            config_valid=config_valid,
            vulkan_available=vulkan_avail,
            opengl_available=True,
            issues=issues,
        )

    def _resolve_executable(self) -> Optional[Path]:
        """Resolves the mocktail executable path with strict fallback priority."""
        if self.custom_bin_path and Path(self.custom_bin_path).exists():
            return Path(self.custom_bin_path)

        # Check PATH first
        in_path = shutil.which("mocktail")
        if in_path:
            p = Path(in_path)
            if p.exists() and os.access(p, os.X_OK):
                return p

        # Check known candidate paths
        for cand in self.KNOWN_SEARCH_PATHS:
            if cand.exists() and os.access(cand, os.X_OK):
                return cand

        return None

    def _resolve_real_elf(self, bin_path: Path) -> Path:
        """Resolves the real ELF executable if bin_path is a shell wrapper script or symlink."""
        resolved = bin_path.resolve()
        try:
            if resolved.exists() and resolved.is_file():
                with open(resolved, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        return resolved

                # If it's a wrapper script, parse for executed binary
                text = resolved.read_text(encoding="utf-8", errors="ignore")
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("exec "):
                        parts = line.split()
                        if len(parts) >= 2:
                            cand = Path(parts[1])
                            if cand.exists() and cand.is_file():
                                with open(cand, "rb") as cf:
                                    if cf.read(4) == b"\x7fELF":
                                        return cand.resolve()
        except Exception:
            pass

        # If a custom binary was explicitly specified and not in standard host locations,
        # don't bleed host system binaries into custom/test environments.
        if self.custom_bin_path and not str(bin_path).startswith(("/usr", str(Path.home() / ".local"))):
            return resolved

        # Check known candidate ELF binaries
        candidates = [
            resolved.parent / "mocktail",
            Path.home() / ".local" / "lib" / "mocktail" / "mocktail",
            Path("/usr/lib/mocktail/mocktail"),
            Path("/usr/local/lib/mocktail/mocktail"),
            Path("/opt/mocktail/lib/mocktail"),
            Path("/opt/mocktail/bin/mocktail"),
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file() and cand != resolved:
                    with open(cand, "rb") as f:
                        if f.read(4) == b"\x7fELF":
                            return cand.resolve()
            except Exception:
                pass

        return resolved

    def _classify_install(self, bin_path: Path, real_bin_path: Path) -> str:
        """Determines how Mocktail was installed."""
        str_path = str(real_bin_path)
        if str_path.startswith("/usr/"):
            return "aur_system"
        if str(bin_path).startswith(str(Path.home() / ".local")):
            return "user_local"
        return "custom"

    def _resolve_lib_dir(self, bin_path: Path, real_bin_path: Path) -> Path:
        """Finds the directory containing Mocktail's companion shared libraries."""
        local_candidates = [
            real_bin_path.parent,
            bin_path.parent,
            bin_path.parent / "lib",
            bin_path.parent.parent / "lib" / "mocktail",
        ]
        for cand in local_candidates:
            if (cand / "libmocktail_audio_sdl.so").exists() or (cand / "libmocktail_mouse_helper.so").exists():
                return cand

        # If a custom binary was explicitly specified and not in standard host locations,
        # isolate it to its own hierarchy so test fixtures and custom runtimes don't bleed.
        if self.custom_bin_path and not str(bin_path).startswith(("/usr", str(Path.home() / ".local"))):
            return real_bin_path.parent

        system_candidates = [
            Path.home() / ".local" / "lib" / "mocktail",
            Path("/usr/lib/mocktail"),
            Path("/usr/local/lib/mocktail"),
            Path("/opt/mocktail/lib"),
            Path.home() / ".local" / "lib",
            Path("/usr/lib"),
            Path("/usr/local/lib"),
        ]
        # Prioritize directory that actually contains Mocktail's core audio shared library
        for cand in system_candidates:
            if (cand / "libmocktail_audio_sdl.so").exists():
                return cand

        # Second pass: check for companion mouse helper
        for cand in system_candidates:
            if (cand / "libmocktail_mouse_helper.so").exists():
                return cand

        # Fallback to standard directories if they exist
        for cand in [Path.home() / ".local" / "lib" / "mocktail", Path("/usr/lib/mocktail")]:
            if cand.exists():
                return cand

        return real_bin_path.parent

    def _resolve_data_dir(self, bin_path: Optional[Path] = None, orig_bin: Optional[Path] = None) -> Path:
        if orig_bin and (orig_bin.parent / "current.json").exists():
            return orig_bin.parent
        if bin_path and (bin_path.parent / "current.json").exists():
            return bin_path.parent
        xdg_data = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
        return base / "mocktail"

    def _resolve_config_dir(self) -> Path:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_config) if xdg_config else Path.home() / ".config"
        return base / "mocktail"

    def _inspect_payload(self, data_dir: Path) -> Tuple[Optional[str], Optional[str]]:
        """Reads version and payload ID from Mocktail's current.json."""
        current_file = data_dir / "current.json"
        if not current_file.exists():
            return None, None
        try:
            data = json.loads(current_file.read_text(encoding="utf-8"))
            ver = data.get("version_name") or data.get("version")
            payload = data.get("payload_id") or data.get("payload")
            return ver, payload
        except Exception:
            return None, None

    def _find_helper(self, lib_dir: Path) -> Optional[Path]:
        """Locates the companion libmocktail_mouse_helper.so."""
        candidates = [
            lib_dir / "libmocktail_mouse_helper.so",
            Path.home() / ".local" / "lib" / "mocktail" / "libmocktail_mouse_helper.so",
            Path.home() / ".local" / "lib" / "libmocktail_mouse_helper.so",
            Path("/usr/lib/mocktail/libmocktail_mouse_helper.so"),
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _validate_config(self, config_dir: Path) -> Tuple[bool, List[str]]:
        """Inspects config.yaml and validates critical settings without modifying the file."""
        config_file = config_dir / "config.yaml"
        if not config_file.exists():
            return False, ["Mocktail configuration (~/.config/mocktail/config.yaml) has not been initialized."]

        issues = []
        try:
            content = config_file.read_text(encoding="utf-8")
            if "version:" not in content:
                issues.append("config.yaml is missing version field.")
            if "backend:" not in content:
                issues.append("config.yaml is missing graphics backend declaration.")
        except Exception as e:
            issues.append(f"Could not read config.yaml: {e}")

        return (len(issues) == 0, issues)

    def _check_vulkan(self) -> bool:
        """Determines if a functional Vulkan driver is available."""
        return shutil.which("vulkaninfo") is not None or Path("/usr/share/vulkan/icd.d").exists()

    def backup_config(self) -> Optional[Path]:
        """Creates a timestamped backup of ~/.config/mocktail/config.yaml."""
        config_dir = self._resolve_config_dir()
        config_file = config_dir / "config.yaml"
        if not config_file.exists():
            return None
        import time
        backup_file = config_dir / f"config.yaml.bak_{int(time.time())}"
        try:
            shutil.copy2(config_file, backup_file)
            log.info(f"Created config backup at {backup_file}")
            return backup_file
        except Exception as e:
            log.error(f"Failed to create config backup: {e}")
            return None

    @staticmethod
    def sandboxed_install_present() -> bool:
        """True when Mocktail is installed only in a form AERAM cannot multi-instance (Flatpak)."""
        for root in (Path("/var/lib/flatpak/app"), Path.home() / ".local" / "share" / "flatpak" / "app"):
            if (root / FLATPAK_APP_ID).exists():
                return True
        return False

    def get_installation_instructions(self) -> str:
        """Provides user-facing, distro-specific guidance to install Mocktail natively."""
        distro = _distro_family()
        if distro == "arch":
            how = (
                "Install the prebuilt package from the AUR:\n\n"
                "  paru -S mocktail-bin\n"
                "  # or: yay -S mocktail-bin"
            )
        elif distro == "debian":
            how = (
                f"Download the .deb from {RELEASES_URL} and install it:\n\n"
                "  sudo apt install ./mocktail_*_amd64.deb"
            )
        elif distro == "fedora":
            how = (
                f"Download the .rpm from {RELEASES_URL} and install it:\n\n"
                "  sudo dnf install ./mocktail-*.x86_64.rpm"
            )
        else:
            how = f"Install the .deb, .rpm or Arch package from {RELEASES_URL}."

        note = ""
        if self.sandboxed_install_present():
            note = (
                "Mocktail is installed as a Flatpak. AERAM cannot use it: running several accounts at once "
                "needs a separate copy of the Mocktail program per account, which the Flatpak sandbox "
                "does not allow. The AppImage has the same limitation. Install the native package too:\n\n"
            )
        return f"{note}{how}\n\nUpstream project: https://github.com/komaruworld/mocktail"
