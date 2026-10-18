"""
Process supervisor for launching isolated Roblox instances via native Mocktail runtime.
Manages isolated execution, decoupled locks, direct-vulkan acceleration,
cookie authentication, instance lifecycle, and concurrent multi-launching.
"""

import os
import signal
import subprocess
import time
import logging
import re
import shutil
import json
import atexit
try:
    import resource
except ImportError:
    resource = None

from typing import Dict, Optional, List, Callable, Any, Tuple
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .models import RobloxAccount, RunningInstance
from .storage import StorageManager, DEFAULT_ALT_FPS
from .auth_state import AccountAuthState, AccountAuthStateManager

log = logging.getLogger("InstanceRunner")


def extract_private_server_link_code(link: str) -> Optional[str]:
    """Extracts the link code from a Roblox private server link."""
    if not link:
        return None
    # Check for direct link code
    if re.match(r'^[a-zA-Z0-9_\-]+$', link) and len(link) > 15:
        return link

    # Check for url params: privateServerLinkCode=xxx or linkCode=xxx
    try:
        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        if "privateServerLinkCode" in query:
            return query["privateServerLinkCode"][0]
        if "linkCode" in query:
            return query["linkCode"][0]
    except Exception:
        pass
    return None


def _physical_core_groups(total_cpus: int, core_of: Optional[Callable[[int], Optional[tuple]]] = None) -> List[set[int]]:
    """Groups logical CPUs that share a physical core, ordered by their lowest CPU number."""
    def sysfs_core(cpu: int) -> Optional[tuple]:
        base = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            return (int((base / "physical_package_id").read_text()), int((base / "core_id").read_text()))
        except (OSError, ValueError):
            return None

    lookup = core_of or sysfs_core
    groups: Dict[tuple, set[int]] = {}
    for cpu in range(total_cpus):
        key = lookup(cpu)
        if key is None:
            return []  # topology unknown: callers fall back to numeric splitting
        groups.setdefault(key, set()).add(cpu)
    return sorted(groups.values(), key=min)


def compute_cpu_sets(
    total_cpus: int,
    custom_alt_cpus: Optional[str] = None,
    core_of: Optional[Callable[[int], Optional[tuple]]] = None,
) -> Tuple[set[int], set[int]]:
    """
    Computes portable CPU affinity sets for main and background alt instances.
    Guarantees all cores are within range and dynamically scales across 2 to 128+ core systems.
    """
    if total_cpus <= 2:
        all_cpus = set(range(total_cpus))
        return all_cpus, all_cpus

    if custom_alt_cpus:
        try:
            custom_set = {int(x.strip()) for x in custom_alt_cpus.split(",") if x.strip().isdigit() and int(x.strip()) < total_cpus}
            if custom_set:
                main_set = set(range(total_cpus)) - custom_set
                return main_set or set(range(total_cpus)), custom_set
        except Exception:
            pass

    if total_cpus >= 8:
        # On 8+ core systems, give alts the upper ~35% of *physical* cores. Splitting logical CPUs by
        # number put alts on the SMT siblings of the main's cores (Linux numbers siblings as
        # cpuN and cpuN+cores), so a busy alt stole execution units from the main instance.
        cores = _physical_core_groups(total_cpus, core_of)
        if len(cores) >= 3:
            alt_core_count = max(1, int(len(cores) * 0.35))
            alt_set = set().union(*cores[-alt_core_count:])
            main_set = set(range(total_cpus)) - alt_set
            return main_set, alt_set
        alt_count = max(2, int(total_cpus * 0.35))
        alt_set = set(range(total_cpus - alt_count, total_cpus))
        main_set = set(range(0, total_cpus - alt_count))
    else:
        # On 4-7 core systems, allocate upper half to alts and all cores to main
        alt_set = set(range(total_cpus // 2, total_cpus))
        main_set = set(range(total_cpus))

    return main_set, alt_set


class InstanceRunner:
    def __init__(self, storage: StorageManager, api: Any = None):
        self.storage = storage
        self.api = api
        self.active_instances: Dict[str, RunningInstance] = {}
        self.last_errors: Dict[str, str] = {}
        self.auth_state_manager = AccountAuthStateManager()
        self.scan_running_instances()
        try:
            running_ids = set(self.active_instances.keys())
            stale_cleaned = self.storage.cleanup_stale_runtime_cookies(running_ids)
            if stale_cleaned > 0:
                log.info(f"Startup recovery: cleaned up {stale_cleaned} stale runtime cookie file(s)")
        except Exception as e:
            log.debug(f"Startup stale cookie cleanup note: {e}")
        try:
            atexit.register(self._atexit_cleanup)
        except Exception:
            pass

    def _atexit_cleanup(self) -> None:
        """Emergency process exit handler to ensure runtime cookies are unlinked on application shutdown."""
        for aid in list(self.active_instances.keys()):
            try:
                self.storage.cleanup_runtime_cookie(aid)
            except Exception:
                pass

    def scan_running_instances(self) -> None:
        """Discovers any existing Roblox instances already running on the system efficiently."""
        accounts = self.storage.list_accounts()
        if not accounts:
            return


        try:
            my_uid = os.getuid()
        except AttributeError:
            return

        candidates: Dict[str, Tuple[RobloxAccount, List[int]]] = {}
        try:
            with os.scandir("/proc") as it:
                for entry in it:
                    if not entry.name.isdigit():
                        continue
                    try:
                        if entry.stat().st_uid != my_uid:
                            continue
                        cmdline_file = os.path.join(entry.path, "cmdline")
                        with open(cmdline_file, "rb") as f:
                            raw = f.read()
                        if b"mocktail_acc_" not in raw:
                            continue

                        # Check status: ignore zombies and dead processes
                        status_file = os.path.join(entry.path, "status")
                        with open(status_file, "r", errors="ignore") as f:
                            status_text = f.read()
                        if "State:\tZ" in status_text or "State:\tX" in status_text:
                            continue

                        pid = int(entry.name)
                        content = raw.split(b"\x00")
                        exe = content[0].decode("utf-8", errors="ignore") if content else ""
                        fname = Path(exe).name

                        for acc in accounts:
                            if fname == f"mocktail_{acc.account_id}":
                                if acc.account_id not in self.active_instances:
                                    candidates.setdefault(acc.account_id, (acc, []))[1].append(pid)
                                break
                    except Exception:
                        continue
        except Exception as e:
            log.warning(f"Failed to scan running instances: {e}")

        # Mocktail forks a session-log writer with the same command line that ignores SIGTERM.
        # Adopt the game itself: the process-group leader of its setsid launch, else the oldest.
        for account_id, (acc, pids) in candidates.items():
            pid = next((p for p in sorted(pids) if self._leads_process_group(p)), min(pids))
            profile_dir = self.storage.get_profile_dir(acc)
            self.active_instances[account_id] = RunningInstance(
                account_id=account_id,
                username=acc.username,
                pid=pid,
                start_time=time.time(),
                log_path=str(profile_dir / "instance.log"),
            )

    @staticmethod
    def _leads_process_group(pid: int) -> bool:
        try:
            return os.getpgid(pid) == pid
        except OSError:
            return False

    def get_instance_error(self, account_id: str) -> Optional[str]:
        """Returns the most recent exit or launch error message for the given account."""
        return self.last_errors.get(account_id)

    def _handle_instance_exit(self, account_id: str, inst: RunningInstance) -> None:
        """Cleans up active instance tracking, transitions auth state, and inspects logs for early exit cause."""
        try:
            self.storage.cleanup_runtime_cookie(account_id)
        except Exception as e:
            log.debug(f"Error cleaning up runtime cookie for {account_id}: {e}")

        if account_id in self.active_instances:
            del self.active_instances[account_id]

        is_expired = False
        # Check instance log for fatal authentication failure or errors
        if inst.log_path and Path(inst.log_path).exists():
            try:
                log_text = Path(inst.log_path).read_text(errors="ignore")[-2048:]
                if "HTTP 401" in log_text or "authentication rejected" in log_text.lower():
                    self.last_errors[account_id] = "HTTP 401: Authentication Rejected (Cookie Expired)"
                    self.auth_state_manager.mark_session_expired(account_id, "HTTP 401: Authentication Rejected (Cookie Expired)")
                    is_expired = True
                    return
                if "[FATAL]" in log_text:
                    for line in reversed(log_text.splitlines()):
                        if "[FATAL]" in line:
                            self.last_errors[account_id] = line.strip()
                            self.auth_state_manager.set_state(account_id, AccountAuthState.ERROR, line.strip())
                            return
            except Exception:
                pass

        if not is_expired:
            current_st = self.auth_state_manager.get_state(account_id)
            if current_st == AccountAuthState.RUNNING:
                self.auth_state_manager.mark_ready(account_id)


    def is_instance_running(self, account_id: str) -> bool:
        """Checks if a given account's instance is currently alive and healthy."""
        if account_id not in self.active_instances:
            return False

        inst = self.active_instances[account_id]

        # 1. Non-blocking child reaping if direct child
        try:
            reaped_pid, status = os.waitpid(inst.pid, os.WNOHANG)
            if reaped_pid == inst.pid:
                self._handle_instance_exit(account_id, inst)
                return False
        except (ChildProcessError, OSError):
            pass

        # 2. Check proc status for zombie or dead state
        proc_status = Path(f"/proc/{inst.pid}/status")
        if not proc_status.exists():
            self._handle_instance_exit(account_id, inst)
            return False

        try:
            for line in proc_status.read_text().splitlines():
                if line.startswith("State:"):
                    state = line.split()[1]
                    if state in ("Z", "X"):  # Zombie or Dead
                        self._handle_instance_exit(account_id, inst)
                        return False
                    break
        except (ProcessLookupError, FileNotFoundError):
            self._handle_instance_exit(account_id, inst)
            return False
        except Exception:
            pass

        # 3. Check signal 0 for process liveness
        try:
            os.kill(inst.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            self._handle_instance_exit(account_id, inst)
            return False

    def get_running_instance(self, account_id: str) -> Optional[RunningInstance]:
        """Returns the RunningInstance if active, otherwise None."""
        if self.is_instance_running(account_id):
            return self.active_instances[account_id]
        return None

    def get_running_account_ids(self) -> List[str]:
        """Returns list of account IDs currently running."""
        return [aid for aid in list(self.active_instances.keys()) if self.is_instance_running(aid)]

    def is_account_running(self, account_id: str) -> bool:
        """Alias for is_instance_running."""
        return self.is_instance_running(account_id)

    def stop_instance(self, account_id: str) -> bool:
        """Alias for terminate_instance."""
        return self.terminate_instance(account_id)

    def launch_instance(
        self,
        account: RobloxAccount,
        place_id: Optional[int] = None,
        job_id: Optional[str] = None,
        private_server_link: Optional[str] = None,
        follow_user_id: Optional[int] = None,
    ) -> RunningInstance:
        """
        Launches an isolated Roblox instance for the given account.
        Enforces lifecycle states, suppresses retries on Arkose challenges,
        and prevents concurrent launching of the same account.
        """
        if self.is_instance_running(account.account_id):
            log.warning(f"Account {account.username} is already running with PID {self.active_instances[account.account_id].pid}")
            return self.active_instances[account.account_id]

        can_launch, reason = self.auth_state_manager.can_launch(account.account_id)
        if not can_launch:
            self.last_errors[account.account_id] = reason
            raise RuntimeError(reason)

        if not self.auth_state_manager.acquire_launch_lock(account.account_id):
            raise RuntimeError(f"Account {account.username} is already launching.")

        try:
            return self._launch_instance_internal(
                account=account,
                place_id=place_id,
                job_id=job_id,
                private_server_link=private_server_link,
                follow_user_id=follow_user_id,
            )
        finally:
            self.auth_state_manager.release_launch_lock(account.account_id)

    def _launch_instance_internal(
        self,
        account: RobloxAccount,
        place_id: Optional[int] = None,
        job_id: Optional[str] = None,
        private_server_link: Optional[str] = None,
        follow_user_id: Optional[int] = None,
    ) -> RunningInstance:
        self.last_errors.pop(account.account_id, None)
        self.auth_state_manager.set_state(account.account_id, AccountAuthState.AUTHENTICATING)


        profile_dir = self.storage.init_profile_sandbox(account)
        try:
            self.storage.write_runtime_cookie(account)
        except Exception as e:
            log.error(f"Failed to write runtime cookie for '{account.username}': {e}")
            raise RuntimeError(f"Failed to write runtime authentication: {e}")
        bin_path = self.storage.get_instance_binary(account)


        is_main = self.storage.is_main_account(account)
        is_alt = not is_main

        # The main account may opt into Mocktail's direct-vulkan backend (Settings > FastFlags >
        # Graphics Backend: Vulkan). Its unthrottled path presents with the immediate mode, which
        # avoids waiting on the compositor. Alts stay on OpenGL: the native helper's chat overlay,
        # in-process Anti-AFK and FPS unclamp all hook SDL_GL_SwapWindow.
        graphics_backend = "opengl"
        if is_main and self.storage.get_setting("fflag_graphics_api", "Auto") == "Vulkan":
            graphics_backend = "direct-vulkan"

        # Build native Mocktail execution command
        base_cmd = [str(bin_path), "--windowed", "--graphics", graphics_backend]

        env = os.environ.copy()

        # Build game launch URI if requested
        if follow_user_id:
            if place_id:
                uri = f"roblox://experiences/start?placeId={place_id}&userId={follow_user_id}"
            else:
                uri = f"roblox://experiences/start?userId={follow_user_id}"
            base_cmd.extend(["--launch-uri", uri])
        elif place_id:
            uri = f"roblox://experiences/start?placeId={place_id}"
            if job_id:
                uri += f"&gameInstanceId={job_id.strip()}"
            elif private_server_link:
                code = extract_private_server_link_code(private_server_link)
                if code:
                    uri += f"&linkCode={code}"
            base_cmd.extend(["--launch-uri", uri])

        cmd = base_cmd
        runtime_dir = self.storage.get_runtime_dir(account)

        # Decouple instance locks and state
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        env["MOCKTAIL_RUNTIME_ROOT"] = str(profile_dir)
        env["MOCKTAIL_DATA_ROOT"] = str(profile_dir / "data")
        env["MOCKTAIL_CONFIG_ROOT"] = str(profile_dir / "config")
        env["MOCKTAIL_CACHE_ROOT"] = str(profile_dir / "cache")
        env["MOCKTAIL_STATE_ROOT"] = str(profile_dir / "state")
        env["XDG_DATA_HOME"] = str(profile_dir / "data")
        env["XDG_CONFIG_HOME"] = str(profile_dir / "config")
        env["XDG_CACHE_HOME"] = str(profile_dir / "cache")
        env["XDG_STATE_HOME"] = str(profile_dir / "state")
        env["MOCKTAIL_COOKIE_FILE"] = str(profile_dir / "roblox.cookie")
        env["MOCKTAIL_DISABLE_FAILURE_DIALOG"] = "1"
        env["MOCKTAIL_GRAPHICS_BACKEND"] = graphics_backend
        # Always present a Windows PC with mouse and keyboard and no touchscreen.
        # Roblox only offers PC settings (camera sensitivity, shift lock, mouse
        # UI) when it sees a mouse, and a stray variable from the user's session
        # (or a legacy playability switch) must not flip it to the phone layout.
        for key in [k for k in env if k.startswith("MOCKTAIL_DEVICE_") or k == "MOCKTAIL_DESKTOP_PLAYABILITY"]:
            del env[key]
        env["MOCKTAIL_DEVICE_PROFILE"] = "pc-windows-11"
        env["MOCKTAIL_TOUCH_MODE"] = "0"
        env["MOCKTAIL_MOUSE_MODE"] = "1"
        env["MOCKTAIL_KEYBOARD_MODE"] = "1"

        # Detect Mocktail installation details
        from core.mocktail_manager import MocktailManager
        mock_info = MocktailManager().detect()

        # Dynamic font resolution from active Mocktail payload for crisp chat overlay
        font_path = None
        if mock_info.data_dir and mock_info.payload_id:
            candidate_font = mock_info.data_dir / "payloads" / mock_info.payload_id / "assets" / "fonts" / "SourceSansPro-Regular.ttf"
            if candidate_font.exists():
                font_path = candidate_font

        # Fallback to system fonts if payload font is unavailable
        if not font_path or not font_path.exists():
            for fallback_candidate in (
                Path("/usr/share/fonts/noto/NotoSans-Regular.ttf"),
                Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
                Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"),
            ):
                if fallback_candidate.exists():
                    font_path = fallback_candidate
                    break

        if font_path and font_path.exists():
            env["MOCKTAIL_TEXT_FONT"] = str(font_path)

        try:
            alt_fps = int(self.storage.get_setting("alt_fps", DEFAULT_ALT_FPS))
        except (ValueError, TypeError):
            alt_fps = int(DEFAULT_ALT_FPS)

        target_w = "1920" if is_main else "800"
        target_h = "1080" if is_main else "600"

        env["MOCKTAIL_WIN_WIDTH"] = target_w
        env["MOCKTAIL_WIN_HEIGHT"] = target_h
        env["MOCKTAIL_WIN_HIGH_DPI"] = "0"

        # Preload companion native helper to center cursor and handle in-process Anti-AFK
        mouse_helper = mock_info.helper_path or (Path.home() / ".local" / "lib" / "mocktail" / "libmocktail_mouse_helper.so")
        if mouse_helper and mouse_helper.exists():
            existing_preload = env.get("LD_PRELOAD", "")
            env["LD_PRELOAD"] = f"{mouse_helper}:{existing_preload}".rstrip(":") if existing_preload else str(mouse_helper)
            if mock_info.latency_patched:
                # Mocktail already raised libroblox's FPS clamps; skip the helper's one-off text scan
                env["AERAM_SKIP_FPS_UNCLAMP"] = "1"

        try:
            target_fps = int(self.storage.get_account_target_fps(account))
        except (ValueError, TypeError):
            target_fps = 330 if is_main else alt_fps
        try:
            main_gfx = int(self.storage.get_setting("graphics_quality_level", "7"))
        except (ValueError, TypeError):
            main_gfx = 7
        frm_scale_map = {1: 1, 2: 3, 3: 5, 4: 7, 5: 9, 6: 11, 7: 14, 8: 16, 9: 18, 10: 21}
        frm_quality = frm_scale_map.get(main_gfx, 14) if is_main else 1
        graphics_quality = "manual" if is_main else str(frm_quality)
        texture_quality = 3 if is_main else 1

        is_unlimited = (target_fps >= 1000 or target_fps <= 0)
        # Setting integer 10000 instructs Mocktail and libroblox to uncap TaskSchedulerTargetFps completely
        limit_fps = 10000 if is_unlimited else target_fps

        env["MOCKTAIL_GRAPHICS_QUALITY"] = graphics_quality
        # Custom shift-lock icon, served by AERAM's Mocktail build in place of Roblox's (survives updates)
        shiftlock_cursor = self.storage.get_setting("shiftlock_cursor_path", "")
        if isinstance(shiftlock_cursor, str) and shiftlock_cursor.strip() and os.path.isfile(shiftlock_cursor.strip()):
            shiftlock_cursor = shiftlock_cursor.strip()
            env["MOCKTAIL_SHIFTLOCK_CURSOR"] = shiftlock_cursor
        env["vblank_mode"] = "0"
        env["MOCKTAIL_FRAME_RATE_LIMIT"] = str(limit_fps)
        env["MOCKTAIL_VSYNC"] = "off"
        env["SDL_VIDEO_WAYLAND_ALLOW_TEARING"] = "1"
        env["MOCKTAIL_SWAPCHAIN_IMAGES"] = "3"

        # Completely mute audio and cap frame rate to prevent CPU starvation for alts
        if is_alt:
            env["SDL_AUDIODRIVER"] = "dummy"
            env["PULSE_SERVER"] = "/dev/null"
            env["ALSA_CARD"] = "Dummy"
            env["MOCKTAIL_F11_FULLSCREEN"] = "0"
            env["MOCKTAIL_FULLSCREEN_READINESS"] = "0"
            env["MOCKTAIL_MULTITHREADED_RENDERING"] = "1"  # Keep Luau script execution independent of rendering
            env["MOCKTAIL_PHYSICS_WORKER_MODE"] = "auto"
            # Rest the main thread between engine polls instead of spinning a core per alt
            env["MOCKTAIL_ENGINE_PUMP_REST"] = "sleep"
        else:
            env["MOCKTAIL_F11_FULLSCREEN"] = "1"
            env["MOCKTAIL_FULLSCREEN_READINESS"] = "1"
            env["MOCKTAIL_MULTITHREADED_RENDERING"] = "1"
            env["MOCKTAIL_PHYSICS_WORKER_MODE"] = "throughput"
            # Desktop hardware cursor instead of Roblox's frame-rendered arrow (AERAM's Mocktail build)
            if (self.storage.get_setting("main_host_cursor", "true") or "true").lower() != "false":
                env["MOCKTAIL_HOST_CURSOR"] = "1"

        # Enforce native window state prior to launch (fullscreen for main, windowed 800x600 for alts)
        window_state_json = json.dumps({
            "fullscreen": False if is_alt else True,
            "maximized": False,
            "schema_version": 1,
            "windowed": {
                "has_position": False,
                "height": int(target_h),
                "width": int(target_w)
            }
        })
        for ws in (
            profile_dir / "state" / "window-state.json",
            profile_dir / "state" / "mocktail" / "window-state.json",
        ):
            ws.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ws.write_text(window_state_json, encoding="utf-8")

        # Crisp desktop FFlags overrides (strictly compliant with Roblox official safety policy)
        fflags_dict = None
        if hasattr(self.storage, "get_effective_fflags"):
            try:
                res = self.storage.get_effective_fflags(is_alt=is_alt, texture_quality=texture_quality, frm_quality=frm_quality)
                if isinstance(res, dict):
                    fflags_dict = res
            except Exception:
                pass

        if fflags_dict is None:
            if is_alt:
                fflags_dict = {
                    "FStringRenderTextureBudgetByRam": "",
                    "DFFlagTextureQualityOverrideEnabled": True,
                    "DFIntTextureQualityOverride": 1,
                    "DFIntDebugFRMQualityLevelOverride": 1,
                    "FFlagDebugDisableDRS": True,
                    "FFlagAutomaticDRS": False,
                    "FFlagAutomaticDRSDisableHighDPIFramebufferHalving": True,
                    "DFFlagSkipHighResolutiontextureMipsOnLowMemoryDevices2": True,
                    "DFFlagEnableRequestAsyncCompression": False,
                    "DFIntCanHideGuiGroupId": 32380007
                }
            else:
                fflags_dict = {
                    "FStringRenderTextureBudgetByRam": "",
                    "DFFlagTextureQualityOverrideEnabled": True,
                    "DFIntTextureQualityOverride": texture_quality,
                    "DFIntDebugFRMQualityLevelOverride": frm_quality,
                    "FFlagDebugDisableDRS": True,
                    "FFlagAutomaticDRS": False,
                    "FFlagAutomaticDRSDisableHighDPIFramebufferHalving": True,
                    "FFlagDebugGraphicsPreferOpenGL": True,
                    "DFFlagSkipHighResolutiontextureMipsOnLowMemoryDevices2": False,
                    "DFFlagEnableRequestAsyncCompression": False,
                    "DFIntCanHideGuiGroupId": 32380007
                }

        if graphics_backend == "direct-vulkan":
            # Clean slate on Vulkan: the quality/lighting overrides above were tuned for the OpenGL
            # path, and with them the Vulkan renderer showed near-black scenes. Quality follows
            # Roblox's own in-game graphics slider instead.
            fflags_dict = {
                "FFlagDebugGraphicsPreferVulkan": True,
                "DFIntCanHideGuiGroupId": 32380007,
                "FFlagDebugDisableCrashReporting": True,
                "FStringRobloxAnalyticsURL": "",
            }

        challenge_flags = {
            "FFlagEnableExperienceGenericChallengeRenderingOnLoadingScript": True,
            "FFlagEnableExperienceGenericChallengeRenderingConnection": True,
            "FFlagEnableExperienceGenericChallengeRenderingConnection2": True,
            "FFlagEnableGenericChallengeResponseHandling": True,
            "FFlagLuaAppChallengeDisableForceWebViewVisible": False,
            "FFlagLuaAppChallengeTurnstileForceWebViewVisible": True,
            "FFlagHideChallengeWebViewsUntilDisplayed": False,
        }
        fflags_dict.update(challenge_flags)

        fflags_raw = json.dumps(fflags_dict)
        env["MOCKTAIL_CLIENT_SETTINGS_OVERRIDES_JSON"] = fflags_raw
        env["MOCKTAIL_FAST_FLAGS_JSON"] = fflags_raw

        # Host library path for companion libraries
        ld_paths = []
        if mock_info.lib_dir and mock_info.lib_dir.exists():
            ld_paths.append(str(mock_info.lib_dir))
        bin_dir = bin_path.parent
        if bin_dir.exists() and str(bin_dir) not in ld_paths:
            ld_paths.append(str(bin_dir))
        if "LD_LIBRARY_PATH" in env and env["LD_LIBRARY_PATH"]:
            for p in env["LD_LIBRARY_PATH"].split(":"):
                if p and p not in ld_paths:
                    ld_paths.append(p)
        if ld_paths:
            env["LD_LIBRARY_PATH"] = ":".join(ld_paths)

        # Manifest and metadata references
        if mock_info.data_dir:
            meta_dir = mock_info.data_dir / "metadata"
            if meta_dir.exists():
                env["MOCKTAIL_COMPATIBILITY_MANIFEST"] = str(meta_dir / "roblox_compatibility.json")
                env["MOCKTAIL_BOOTSTRAP_SOURCES_PATH"] = str(meta_dir / "roblox_bootstrap_sources.json")
                env["MOCKTAIL_HOST_ABI_PROFILE_FILE"] = str(meta_dir / "roblox_host_abi_reference.json")
                env["MOCKTAIL_UPDATE_SIGNING_TRUST_PATH"] = str(meta_dir / "roblox_signing_certificates.json")

        if not account.cookie:
            log.warning(f"Account '{account.username}' has no session cookie configured; launching in guest mode.")

        # If this is an alt account, force it into virtual memory via systemd cgroups MemoryHigh
        # and regulate CPU/IO weight so foreground apps are prioritized while keeping input and network responsive.
        # Dynamically compute CPU core sets based on host CPU count
        total_cpus = os.cpu_count() or 1
        custom_alt_cpus = self.storage.get_setting("custom_alt_cpus", None)
        main_cpu_set, alt_cpu_set = compute_cpu_sets(total_cpus, custom_alt_cpus)
        alt_cpus_arg = ",".join(str(c) for c in sorted(alt_cpu_set))

        # If this is an alt account, force it into virtual memory via systemd cgroups MemoryHigh
        # and regulate CPU/IO weight so foreground apps are prioritized while keeping input and network responsive.
        if is_alt and shutil.which("systemd-run"):
            try:
                alt_mem_high = int(self.storage.get_setting("alt_memory_high_mb", "900"))
            except (ValueError, TypeError):
                alt_mem_high = 900
            scope_cmd = [
                "systemd-run",
                "--user",
                "--scope",
                f"-pMemoryHigh={alt_mem_high}M",
                "-pCPUWeight=1",       # Lowest cgroup CPU weight (foreground apps get 99% of CPU slices)
                "-pIOWeight=1",        # Lowest disk IO weight
                f"-pAllowedCPUs={alt_cpus_arg}", # Confine alts to designated secondary cores
            ]
            for env_var in (
                "LD_LIBRARY_PATH",
                "XDG_RUNTIME_DIR",
                "MOCKTAIL_RUNTIME_ROOT",
                "MOCKTAIL_DATA_ROOT",
                "MOCKTAIL_CONFIG_ROOT",
                "MOCKTAIL_CACHE_ROOT",
                "MOCKTAIL_STATE_ROOT",
                "XDG_DATA_HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_STATE_HOME",
                "MOCKTAIL_COOKIE_FILE",
            ):
                if env_var in env and env[env_var]:
                    scope_cmd.append(f"-E{env_var}={env[env_var]}")
            scope_cmd.append("--")
            cmd = scope_cmd + cmd

        log_path = profile_dir / "instance.log"
        log.info(f"Launching account '{account.username}' with cmd: {' '.join(cmd)}")

        def _preexec_proc():
            os.setsid()  # Detach process as independent session leader
            if is_alt:
                try:
                    os.sched_setaffinity(0, alt_cpu_set)
                except Exception:
                    pass
                try:
                    # Hard-cap realtime priority to 0 so Mocktail/Roblox threads CANNOT
                    # elevate to SCHED_FIFO 99 (which starved KWin and the desktop).
                    resource.setrlimit(resource.RLIMIT_RTPRIO, (0, 0))
                except Exception:
                    pass
                try:
                    # Disallow negative nice values from ananicy or any process manager
                    resource.setrlimit(resource.RLIMIT_NICE, (0, 0))
                except Exception:
                    pass
                try:
                    # Background nice 15 preserves interactive responsiveness for clicks/network
                    # while yielding CPU to foreground applications.
                    os.nice(15)
                except Exception:
                    pass
            else:
                try:
                    os.sched_setaffinity(0, main_cpu_set)
                except Exception:
                    pass

        self.auth_state_manager.set_state(account.account_id, AccountAuthState.LAUNCHING)
        try:
            with open(log_path, "a", encoding="utf-8") as log_fd:
                proc = subprocess.Popen(
                    cmd,
                    env=env,
                    preexec_fn=_preexec_proc,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )

            inst = RunningInstance(
                account_id=account.account_id,
                username=account.username,
                pid=proc.pid,
                start_time=time.time(),
                place_id=place_id,
                job_id=job_id,
                log_path=str(log_path),
            )
            self.active_instances[account.account_id] = inst
            self.auth_state_manager.mark_running(account.account_id)
            self.storage.update_last_played(account.account_id, time.time())

            # Brief check if process crashed on immediate preflight
            time.sleep(0.15)
            if proc.poll() is not None:
                self._handle_instance_exit(account.account_id, inst)
                err = self.last_errors.get(account.account_id)
                if err:
                    log.warning(f"Account '{account.username}' exited immediately: {err}")

            return inst
        except Exception as e:
            try:
                self.storage.cleanup_runtime_cookie(account)
            except Exception:
                pass
            log.error(f"Failed to launch instance for '{account.username}': {e}")
            raise RuntimeError(f"Failed to launch Mocktail for {account.username}: {e}")

    def _instance_pgid(self, pid: int) -> Optional[int]:
        """The instance's process group when it leads its own (setsid launch), otherwise None."""
        # An adopted instance started elsewhere may share a group with a shell or with AERAM.
        try:
            candidate = os.getpgid(pid)
            if candidate == pid and candidate != os.getpgrp():
                return candidate
        except Exception:
            pass
        return None

    @staticmethod
    def _wait_for_exit(pid: int, timeout: float) -> bool:
        """Polls until pid has exited (reaping it when it is our child); True when it is gone."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return True
            except ChildProcessError:
                pass
            except OSError:
                return True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def terminate_instance(self, account_id: str, grace: float = 1.5) -> bool:
        """Terminates an active instance owned by this manager."""
        if not self.is_instance_running(account_id):
            return False
        inst = self.active_instances[account_id]
        pgid, signalled = self._send_terminate(account_id, inst)
        self._finish_termination(account_id, inst, pgid, signalled, grace)
        return True

    def _send_terminate(self, account_id: str, inst: RunningInstance) -> Tuple[Optional[int], bool]:
        """SIGTERMs the instance; returns (process group, whether it was signalled at all)."""
        log.info(f"Terminating instance {account_id} (PID {inst.pid})")
        if not self._is_mocktail_process(inst.pid):
            # The tracked PID exited and was recycled, or never was Mocktail: signalling it
            # (or its process group) could kill an unrelated program, so only drop the record.
            log.warning(f"PID {inst.pid} tracked for {account_id} is no longer a Mocktail process; not signalling it")
            return None, False
        pgid = self._instance_pgid(inst.pid)
        self._signal_instance(inst.pid, pgid, signal.SIGTERM)
        return pgid, True

    def _finish_termination(
        self, account_id: str, inst: RunningInstance, pgid: Optional[int], signalled: bool, grace: float
    ) -> None:
        if signalled:
            # Force kill only if the client is still alive after the grace period; the group
            # kill also takes down helpers (session-log tee, webview) that ignore SIGTERM.
            exited = self._wait_for_exit(inst.pid, grace)
            if pgid is not None or not exited:
                self._signal_instance(inst.pid, pgid, signal.SIGKILL)
                if not exited:
                    # Reap it, or a launched child lingers as a zombie until AERAM exits.
                    self._wait_for_exit(inst.pid, 0.5)

        # Clean up instance runtime lock directory
        try:
            account = self.storage.get_account(account_id)
            if account:
                run_dir = self.storage.get_runtime_dir(account)
                for lock_file in run_dir.glob("*.lock"):
                    try:
                        lock_file.unlink()
                    except Exception:
                        pass
        except Exception as e:
            log.debug(f"Error cleaning runtime directory for {account_id}: {e}")

        # Clean up runtime cookie
        try:
            self.storage.cleanup_runtime_cookie(account_id)
        except Exception as e:
            log.debug(f"Error cleaning runtime cookie for {account_id}: {e}")

        if account_id in self.active_instances:
            del self.active_instances[account_id]
        if hasattr(self, "auth_state_manager") and self.auth_state_manager.get_state(account_id) == AccountAuthState.RUNNING:
            self.auth_state_manager.mark_ready(account_id)

    @staticmethod
    def _is_mocktail_process(pid: int) -> bool:
        """True when /proc/<pid> is a live process of this user whose command line runs Mocktail."""
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                return False
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return b"mocktail" in f.read()
        except OSError:
            return False

    @staticmethod
    def _signal_instance(pid: int, pgid: Optional[int], sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except Exception:
            pass

    def terminate_all_instances(self) -> int:
        """Terminates all instances actively tracked by this manager."""
        # SIGTERM everything first so the instances shut down in parallel instead of
        # each one waiting out its own grace period in turn.
        pending = []
        for aid in list(self.active_instances.keys()):
            if self.is_instance_running(aid):
                inst = self.active_instances[aid]
                pending.append((aid, inst, *self._send_terminate(aid, inst)))
        deadline = time.monotonic() + 1.5
        for aid, inst, pgid, signalled in pending:
            self._finish_termination(aid, inst, pgid, signalled, max(0.0, deadline - time.monotonic()))
        return len(pending)

    @staticmethod
    def get_memory_stats() -> dict:
        stats = {
            "mem_avail_mb": 4096,
            "mem_total_mb": 32768,
            "swap_free_mb": 4096,
            "swap_total_mb": 32768,
        }

        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if not parts:
                        continue
                    key = parts[0].rstrip(":")
                    if key == "MemAvailable":
                        stats["mem_avail_mb"] = int(parts[1]) // 1024
                    elif key == "MemTotal":
                        stats["mem_total_mb"] = int(parts[1]) // 1024
                    elif key == "SwapFree":
                        stats["swap_free_mb"] = int(parts[1]) // 1024
                    elif key == "SwapTotal":
                        stats["swap_total_mb"] = int(parts[1]) // 1024
        except Exception:
            pass
        return stats

    def memory_launch_check(self, username: str) -> Tuple[bool, Optional[str]]:
        """
        Decides whether another instance may start given current RAM and swap.
        Returns (may_launch, warning); warning is None when memory is comfortable.
        """
        stats = self.get_memory_stats()
        avail_mb = stats["mem_avail_mb"]
        swap_free_mb = stats["swap_free_mb"]
        if avail_mb < 1500 and swap_free_mb > 1000:
            return True, f"System RAM low ({avail_mb} MB available). Virtual memory paging active (Swap free: {swap_free_mb} MB)."
        if (avail_mb + swap_free_mb) < 500:
            return False, f"Critical memory threshold reached ({avail_mb + swap_free_mb} MB total virtual memory left). Paused launch of {username} to protect stability."
        return True, None

    def launch_multi(
        self,
        accounts: List[RobloxAccount],
        place_id: Optional[int] = None,
        job_id: Optional[str] = None,
        private_server_link: Optional[str] = None,
        follow_user_id: Optional[int] = None,
        stagger_delay: float = 8.0,
        on_progress: Optional[Callable[..., None]] = None,
    ) -> List[RunningInstance]:
        """
        Launches multiple accounts sequentially with a recommended 8-15s delay
        to prevent overwhelming GPU resources and avoid triggering Roblox Arkose CAPTCHAs.
        Maintains process continuity, forcing alts into virtual memory
        and notifying the user rather than terminating or aborting.
        """
        import random
        launched = []
        for i, acc in enumerate(accounts):
            can_launch, reason = self.auth_state_manager.can_launch(acc.account_id)
            if not can_launch:
                log.warning(f"Skipping {acc.username} in multi-launch: {reason}")
                if on_progress:
                    try:
                        on_progress(i, len(accounts), acc, f"Skipped {acc.username}: {reason}")
                    except Exception:
                        pass
                continue

            may_launch, warn_msg = self.memory_launch_check(acc.username)
            if not may_launch:
                log.warning(warn_msg)
                if on_progress:
                    try:
                        on_progress(i, len(accounts), acc, warn_msg)
                    except Exception:
                        pass
                break
            if warn_msg:
                # RAM is low but swap is available: give the kernel a moment to page out
                log.info(warn_msg)
                if on_progress:
                    try:
                        on_progress(i, len(accounts), acc, warn_msg)
                    except Exception:
                        pass
                time.sleep(1.0)
            elif on_progress:
                try:
                    on_progress(i, len(accounts), acc, None)
                except Exception:
                    pass


            if i > 0 and stagger_delay > 0:
                # Add natural jitter (+0.2s - 1.2s) when delay is > 3s to break metronomic robotic timing
                jitter = random.uniform(0.2, 1.2) if stagger_delay >= 3.0 else 0.0
                time.sleep(stagger_delay + jitter)
            try:
                inst = self.launch_instance(
                    account=acc,
                    place_id=place_id,
                    job_id=job_id,
                    private_server_link=private_server_link,
                    follow_user_id=follow_user_id,
                )
                launched.append(inst)
            except Exception as e:
                log.error(f"Error launching {acc.username} in multi-launch: {e}")
        return launched

