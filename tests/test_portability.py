"""
Unit tests for AERAM Linux Portability, Mocktail Discovery, Compositor Integration,
Dynamic CPU Topology, and Diagnostics Engine.
"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.instance_runner import compute_cpu_sets
from core.mocktail_manager import MocktailManager, MocktailInstallationInfo
from core.compositor import (
    CompositorType,
    detect_compositor_type,
    get_compositor,
    HyprlandCompositor,
    KWinWaylandCompositor,
    X11Compositor,
    BaseCompositor,
)
from core.diagnostics import DiagnosticsRunner, DiagnosticReport


class TestPortabilityAndTopology(unittest.TestCase):

    def test_compute_cpu_sets_various_topologies(self):
        """Dynamic CPU allocation splits available cores proportionally across host sizes."""
        # 1-core fallback
        main_1, alt_1 = compute_cpu_sets(1)
        self.assertEqual(main_1, {0})
        self.assertEqual(alt_1, {0})

        # 2-core machine
        main_2, alt_2 = compute_cpu_sets(2)
        self.assertEqual(main_2, {0, 1})
        self.assertEqual(alt_2, {0, 1})

        # 4-core machine
        main_4, alt_4 = compute_cpu_sets(4)
        self.assertEqual(main_4, {0, 1, 2, 3})
        self.assertEqual(alt_4, {2, 3})

        # 12 logical CPUs with unknown topology: numeric split
        unknown = lambda cpu: None
        main_12, alt_12 = compute_cpu_sets(12, core_of=unknown)
        self.assertEqual(main_12, {0, 1, 2, 3, 4, 5, 6, 7})
        self.assertEqual(alt_12, {8, 9, 10, 11})

        # 16 logical CPUs with unknown topology
        main_16, alt_16 = compute_cpu_sets(16, core_of=unknown)
        self.assertEqual(len(main_16) + len(alt_16), 16)
        self.assertTrue(len(alt_16) >= 2)

    def test_compute_cpu_sets_never_shares_a_physical_core(self):
        """6 cores / 12 threads numbered like Linux on Ryzen: cpuN and cpuN+6 are SMT siblings."""
        smt = lambda cpu: (0, cpu % 6)
        main, alts = compute_cpu_sets(12, core_of=smt)
        self.assertEqual(main, {0, 1, 2, 3, 6, 7, 8, 9})
        self.assertEqual(alts, {4, 5, 10, 11})
        main_cores = {smt(c) for c in main}
        alt_cores = {smt(c) for c in alts}
        self.assertFalse(main_cores & alt_cores, "alts must not run on SMT siblings of the main's cores")

        # 8 cores / 16 threads
        smt16 = lambda cpu: (0, cpu % 8)
        main16, alts16 = compute_cpu_sets(16, core_of=smt16)
        self.assertFalse({smt16(c) for c in main16} & {smt16(c) for c in alts16})
        self.assertEqual(len(main16) + len(alts16), 16)


class TestMocktailManager(unittest.TestCase):

    def test_mocktail_missing_detection(self):
        """When no mocktail executable exists, report clean missing status without crashing."""
        with patch.object(MocktailManager, "_resolve_executable", return_value=None):
            mgr = MocktailManager()
            info = mgr.detect()
            self.assertFalse(info.is_installed)
            self.assertEqual(info.install_type, "missing")
            self.assertTrue(any("executable not found" in issue for issue in info.issues))

    def test_mocktail_custom_path_resolution(self):
        """Custom executable path is respected and inspected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_bin = Path(tmp_dir) / "custom_mocktail"
            fake_bin.write_text("#!/bin/sh\nexit 0\n")
            fake_bin.chmod(0o755)

            mgr = MocktailManager(custom_bin_path=fake_bin)
            info = mgr.detect()
            self.assertTrue(info.is_installed)
            self.assertEqual(info.bin_path, fake_bin)
            self.assertEqual(info.install_type, "custom")

    def test_mocktail_version_parsing_from_current_json(self):
        """Detects version name and payload hash from Mocktail's current.json manifest."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bin_path = tmp_path / "mocktail"
            bin_path.write_text("#!/bin/sh\nexit 0\n")
            bin_path.chmod(0o755)

            current_json = tmp_path / "current.json"
            current_json.write_text(json.dumps({
                "version": "version-a1b2c3d4",
                "payload": "2.736.1408",
            }))

            mgr = MocktailManager(custom_bin_path=bin_path)
            info = mgr.detect()
            self.assertTrue(info.is_installed)
            self.assertEqual(info.version_name, "version-a1b2c3d4")
            self.assertEqual(info.payload_id, "2.736.1408")


class TestCompositorIntegration(unittest.TestCase):

    def test_detect_compositor_hyprland(self):
        """Hyprland is correctly identified via environment signatures."""
        with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "hypr_test_1234"}, clear=True):
            comp_type = detect_compositor_type()
            self.assertEqual(comp_type, CompositorType.HYPRLAND)
            comp = get_compositor()
            self.assertIsInstance(comp, HyprlandCompositor)

    def test_detect_compositor_kde_wayland(self):
        """KDE Plasma Wayland is detected via desktop/session variables."""
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland"}, clear=True):
            comp_type = detect_compositor_type()
            self.assertEqual(comp_type, CompositorType.KDE_WAYLAND)
            comp = get_compositor()
            self.assertIsInstance(comp, KWinWaylandCompositor)

    def test_detect_compositor_x11(self):
        """X11 session is identified and returns X11Compositor."""
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, clear=True):
            comp_type = detect_compositor_type()
            self.assertEqual(comp_type, CompositorType.X11)
            comp = get_compositor()
            self.assertIsInstance(comp, X11Compositor)

    def test_hyprland_cycle_mocktail_windows(self):
        """Hyprland window cycling accurately targets mocktail clients and restores active window."""
        comp = HyprlandCompositor()
        comp.hyprctl = "/usr/bin/hyprctl"

        mock_active = json.dumps({"address": "0x12345678", "pid": 1111})
        mock_clients = json.dumps([
            {"address": "0x12345678", "pid": 1111, "class": "kitty"},
            {"address": "0xabcdef01", "pid": 2222, "class": "Mocktail"},
            {"address": "0xabcdef02", "pid": 3333, "class": "Mocktail"},
        ])

        with patch.object(comp, "_run_hyprctl") as mock_run:
            mock_run.side_effect = [
                mock_active,     # activewindow -j
                mock_clients,    # clients -j
                None,            # dispatch focuswindow 0xabcdef01
                None,            # dispatch focuswindow 0xabcdef02
                None,            # dispatch focuswindow restore 0x12345678
            ]

            pulsed = comp.cycle_mocktail_windows([2222, 3333])
            self.assertEqual(pulsed, 2)
            # Verify original active window was restored
            mock_run.assert_any_call(["dispatch", "focuswindow", "address:0x12345678"])


class TestDiagnosticsEngine(unittest.TestCase):

    def test_diagnostics_evaluation(self):
        """Diagnostics runner evaluates all system components and formats clean text."""
        runner = DiagnosticsRunner()
        report = runner.run_all_checks()
        self.assertIsInstance(report, DiagnosticReport)
        self.assertGreater(len(report.checks), 5)
        text = report.to_text()
        self.assertIn("AERAM System Diagnostics", text)
        self.assertIn("Dependencies", text)
        self.assertIn("Mocktail", text)
        self.assertIn("System", text)
        self.assertIn("Compositor", text)

    def test_diagnostics_auto_repair(self):
        """Auto-repair creates runtime directories and sets up pulse file safely."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = DiagnosticsRunner(project_root=Path(tmp_dir))
            messages = runner.auto_repair()
            self.assertIsInstance(messages, list)
            self.assertTrue(len(messages) > 0)


class TestDesktopIntegration(unittest.TestCase):

    def test_set_process_name(self):
        """_set_process_name runs safely and sets comm when supported."""
        from core.entrypoint import _set_process_name
        # Should not raise any exceptions
        _set_process_name("aeram")
        if sys.platform == "linux":
            try:
                with open("/proc/self/comm", "r", encoding="utf-8") as f:
                    comm = f.read().strip()
                self.assertEqual(comm, "aeram")
            except (OSError, IOError):
                pass

    def test_desktop_file_syntax_and_metadata(self):
        """Ensure packaging/aeram.desktop complies with FreeDesktop specs and AERAM requirements."""
        desktop_file = Path(__file__).parent.parent / "packaging" / "aeram.desktop"
        self.assertTrue(desktop_file.exists(), "packaging/aeram.desktop must exist")
        content = desktop_file.read_text(encoding="utf-8")

        self.assertIn("Type=Application", content)
        self.assertIn("Name=AERAM", content)
        self.assertIn("Icon=aeram", content)
        self.assertIn("StartupWMClass=aeram", content)
        self.assertIn("StartupNotify=true", content)
        self.assertIn("Categories=Game;", content)
        self.assertIn("Actions=StopAll;ListAccounts;Diagnostics;", content)


class TestAnnotationsResolve(unittest.TestCase):
    """Python < 3.14 evaluates annotations at import time; an undefined name there breaks startup."""

    def test_core_annotations_resolve(self):
        import inspect
        import typing
        import core.instance_runner as runner_mod
        for name, obj in vars(runner_mod).items():
            if inspect.isfunction(obj) and obj.__module__ == runner_mod.__name__:
                with self.subTest(function=name):
                    typing.get_type_hints(obj)


class TestDefaultBaseDirSelection(unittest.TestCase):
    """An empty aeram/ directory must not hide accounts stored in the legacy ram-linux/ directory."""

    def _make_db(self, base, n_accounts):
        import sqlite3
        base.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(base / "manager.db")
        conn.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO accounts VALUES (?)", [(f"acc_{i}",) for i in range(n_accounts)])
        conn.commit()
        conn.close()

    def _base_dir(self, home):
        from core.paths import default_base_dir
        with patch("pathlib.Path.home", return_value=home):
            return default_base_dir()

    def test_empty_aeram_dir_does_not_hide_legacy_accounts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            share = home / ".local" / "share"
            self._make_db(share / "ram-linux", 3)
            self._make_db(share / "aeram", 0)  # created by a repair step / first launch
            self.assertEqual(self._base_dir(home), share / "ram-linux")

    def test_aeram_dir_with_accounts_wins(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            share = home / ".local" / "share"
            self._make_db(share / "ram-linux", 3)
            self._make_db(share / "aeram", 1)
            self.assertEqual(self._base_dir(home), share / "aeram")

    def test_fresh_system_uses_aeram_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(self._base_dir(home), home / ".local" / "share" / "aeram")


class TestMocktailInstallGuidance(unittest.TestCase):
    def _family(self, text):
        from core.mocktail_manager import _distro_family
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "os-release"
            f.write_text(text)
            return _distro_family(f)

    def test_distro_family_detection(self):
        self.assertEqual(self._family('ID=cachyos\nID_LIKE=arch\n'), "arch")
        self.assertEqual(self._family('ID=linuxmint\nID_LIKE="ubuntu debian"\n'), "debian")
        self.assertEqual(self._family('ID=fedora\n'), "fedora")
        self.assertEqual(self._family('ID=nixos\n'), "other")

    def test_flatpak_only_install_gets_explained(self):
        with patch.object(MocktailManager, "sandboxed_install_present", return_value=True), \
                patch("core.mocktail_manager._distro_family", return_value="debian"):
            text = MocktailManager().get_installation_instructions()
        self.assertIn("Flatpak", text)
        self.assertIn("apt install ./mocktail_", text)
        self.assertNotIn("vinegarhq/mocktail", text)


if __name__ == "__main__":
    unittest.main()
