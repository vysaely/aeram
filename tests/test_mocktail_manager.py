import os
import shutil
import unittest
from pathlib import Path

from core.mocktail_manager import AERAM_PATCH_MARKER_NAME, MocktailManager, binary_has_latency_patch


class TestLatencyPatchDetection(unittest.TestCase):
    def setUp(self):
        self.local = Path.home() / ".local"
        self.bin = self.local / "bin" / "mocktail"
        self.lib = self.local / "lib" / "mocktail"
        self.bin.parent.mkdir(parents=True, exist_ok=True)
        self.lib.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.bin.unlink(missing_ok=True)
        shutil.rmtree(self.lib, ignore_errors=True)

    def _write_bin(self, payload: bytes):
        self.bin.write_bytes(b"\x7fELF" + payload)
        os.chmod(self.bin, 0o755)

    def test_signature_detects_patched_binary(self):
        self._write_bin(b"\0stock mocktail\0")
        self.assertFalse(binary_has_latency_patch(self.bin))
        self._write_bin(b"\0getenv MOCKTAIL_HOST_CURSOR\0 and more")
        self.assertTrue(binary_has_latency_patch(self.bin))
        self.assertFalse(binary_has_latency_patch(self.bin.with_name("missing")))

    def test_marker_makes_aeram_build_win_over_path(self):
        self._write_bin(b"\0MOCKTAIL_HOST_CURSOR\0")
        mgr = MocktailManager()
        self.assertEqual(mgr._resolve_executable(), self.bin)  # found via known paths either way

        (self.lib / AERAM_PATCH_MARKER_NAME).write_text("551d2a1 abc\n")
        old_path = os.environ["PATH"]
        system_dir = Path.home() / "sysbin"
        system_dir.mkdir(exist_ok=True)
        system_bin = system_dir / "mocktail"
        system_bin.write_bytes(b"\x7fELF")
        os.chmod(system_bin, 0o755)
        os.environ["PATH"] = f"{system_dir}{os.pathsep}{old_path}"
        try:
            self.assertEqual(mgr._resolve_executable(), self.bin)
            (self.lib / AERAM_PATCH_MARKER_NAME).unlink()
            self.assertEqual(mgr._resolve_executable(), system_bin)
        finally:
            os.environ["PATH"] = old_path
            shutil.rmtree(system_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
