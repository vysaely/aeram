# AERAM — Windows Release Verification Checklist

This checklist must be executed prior to publishing any production release of AERAM on Windows.

---

## Pre-Build Verification

- [ ] **Clean Working Tree**: Verify `git status` has no uncommitted changes or untracked sensitive files.
- [ ] **Unit Tests**: Run `python -m unittest discover -s tests -v` and verify 100% pass rate (47/47 tests).
- [ ] **Code Hygiene**: Run `flake8` to ensure no syntax errors or undefined symbols.
- [ ] **Zero Credential Leaks**: Verify `.gitignore` excludes `manager.db`, `vault.key`, `*.cookie`, `*.aeram`, `dist/`, `build/`.
- [ ] **Version Synchronization**: Verify version numbers in `pyproject.toml`, `version_info.txt`, `installer.iss`, and `docs/windows-port/PROGRESS.md` match.

---

## Build Pipeline Execution

- [ ] Run `python scripts/build_windows.py`.
- [ ] Confirm `dist/AERAM/AERAM.exe` and `dist/AERAM/AERAM-CLI.exe` exist.
- [ ] Confirm `dist/AERAM-Portable.exe` exists and is under 50 MB.
- [ ] Confirm `dist/AERAM-Setup.exe` exists and is under 35 MB.
- [ ] Check SHA-256 hashes of all generated artifacts.

---

## Packaged Binary Testing

- [ ] **Automated Frozen Suite**: Run `python -m unittest tests/test_packaged_build.py -v`.
- [ ] **GUI Startup (No Console)**:
  - Double-click `dist/AERAM/AERAM.exe` directly from Windows Explorer.
  - Verify that the window appears with the dark minimalist theme.
  - Verify that **no console window appears** in the taskbar or desktop.
  - Verify taskbar icon renders `assets/aeram.ico` and groups correctly under `AERAM`.
- [ ] **CLI Execution**:
  - Run `dist\AERAM\AERAM-CLI.exe --list`.
  - Verify terminal output prints cleanly and exits with code 0.
- [ ] **Portable Executable**:
  - Move `dist/AERAM-Portable.exe` to a temporary directory.
  - Run `.\AERAM-Portable.exe --list`.
  - Verify execution succeeds without requiring local dependencies.

---

## Installer Testing

- [ ] **Clean Per-User Install**:
  - Run `dist/AERAM-Setup.exe`.
  - Verify that no UAC elevation prompt is requested.
  - Verify default destination is `%LOCALAPPDATA%\Programs\AERAM`.
  - Complete installation and check that Start Menu shortcuts (`AERAM`, `AERAM CLI`) are created.
- [ ] **Launch via Start Menu**:
  - Click the Start Menu shortcut and verify AERAM launches cleanly.
- [ ] **Upgrade Install**:
  - Run `dist/AERAM-Setup.exe` again over the existing install.
  - Verify files overwrite cleanly without error prompts.
- [ ] **Uninstallation & Data Preservation**:
  - Go to Windows Settings > Installed apps > AERAM > Uninstall.
  - Verify application directory `%LOCALAPPDATA%\Programs\AERAM` is completely removed.
  - **Verify `%LOCALAPPDATA%\AERAM` (databases, vault keys, user settings) is intact and NOT deleted**.

---

## Publication Procedure

- [ ] Generate release notes detailing improvements, fixes, and compatibility status.
- [ ] Tag the git commit with the release version (`git tag -a v1.0.0 -m "Release v1.0.0"`).
- [ ] Push tags to remote (`git push origin --tags`).
- [ ] Upload `AERAM-Setup.exe` and `AERAM-Portable.exe` with their SHA-256 checksums to GitHub Releases.
