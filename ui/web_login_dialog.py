"""
Embedded Chromium Web Login modal dialog for Roblox authentication.
Allows users to log in directly inside AERAM, solving 2FA and Arkose FunCAPTCHAs
with native browser fidelity and hardware acceleration, capturing .ROBLOSECURITY
cookies automatically without manual copy-pasting.
"""

import uuid
import json
import logging
import urllib.parse
import subprocess
from typing import Optional, Dict, Any

from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import QUrl, pyqtSignal, QTimer, QDateTime

from core.models import RobloxAccount
from core.storage import StorageManager, clean_cookie
from core.roblox_api import RobloxAPI
from ui.borderless_dialog import BorderlessDialog, BorderlessInputDialog, BorderlessMessageBox as QMessageBox

log = logging.getLogger("WebLoginDialog")


def parse_roblox_launch_url(url_str: str) -> Dict[str, str]:
    """Parses a roblox: or roblox-player: launch URI into normalized key-value fields."""
    res: Dict[str, str] = {}
    if not url_str:
        return res

    scheme_colon = url_str.find(":")
    if scheme_colon == -1:
        return res

    scheme = url_str[:scheme_colon].lower()
    payload = url_str[scheme_colon + 1:]

    if scheme == "roblox-player":
        parts = payload.split("+")
        for p in parts:
            if ":" in p:
                k, v = p.split(":", 1)
                res[k.lower()] = urllib.parse.unquote(v)
        if "placelauncherurl" in res:
            try:
                parsed = urllib.parse.urlparse(res["placelauncherurl"])
                params = urllib.parse.parse_qs(parsed.query)
                for pk, pv in params.items():
                    if pv:
                        res[pk.lower()] = pv[0]
            except Exception:
                pass
    elif scheme == "roblox":
        if "?" in payload:
            query = payload.split("?", 1)[1]
        elif payload.startswith("//"):
            query = payload[2:]
        else:
            query = payload
        try:
            params = urllib.parse.parse_qs(query)
            for pk, pv in params.items():
                if pv:
                    res[pk.lower()] = pv[0]
        except Exception:
            pass

    return res

# Verify WebEngine availability
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import (
        QWebEngineProfile,
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineScript,
    )
    from PyQt6.QtNetwork import QNetworkCookie

    WEBENGINE_AVAILABLE = True
    WEBENGINE_ERROR = ""

    class RobloxWebPage(QWebEnginePage):
        """Custom WebEnginePage that intercepts roblox: and roblox-player: protocol schemes."""

        def __init__(self, profile, parent=None, navigation_handler=None):
            super().__init__(profile, parent)
            self.navigation_handler = navigation_handler

        def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
            scheme = url.scheme().lower()
            if scheme in ("roblox", "roblox-player"):
                if self.navigation_handler:
                    self.navigation_handler(url.toString())
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

        def createWindow(self, window_type):
            # Return current page so popups/dialogs stay in the active view rather than being lost
            return self

    class FocusableWebEngineView(QWebEngineView):
        """
        Subclass of QWebEngineView that ensures StrongFocus and reliably claims
        keyboard and seat focus whenever clicked, preventing Wayland/X11 focus drops on inputs.
        """
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        def mousePressEvent(self, event: QtGui.QMouseEvent):
            self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
            super().mousePressEvent(event)

except Exception as _e:
    WEBENGINE_AVAILABLE = False
    WEBENGINE_ERROR = str(_e)
    log.warning(f"PyQt6-WebEngine is not available: {WEBENGINE_ERROR}")


CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

ROBLOX_LOGIN_URL = "https://www.roblox.com/login"
ROBLOX_HOME_URL = "https://www.roblox.com/home"

ROBLOX_2FA_DOM_FIX_JS = """
(function() {
    // Exempt Arkose Labs, FunCaptcha, Cloudflare, and captcha iframes from any DOM modifications
    try {
        var loc = (window.location ? (window.location.href || window.location.hostname || '') : '').toLowerCase();
        if (loc.indexOf('arkose') !== -1 || loc.indexOf('funcaptcha') !== -1 ||
            loc.indexOf('hcaptcha') !== -1 || loc.indexOf('recaptcha') !== -1 ||
            loc.indexOf('cloudflare') !== -1) {
            return;
        }
    } catch(e) {}

    if (window._aeram_fix_installed) return;
    window._aeram_fix_installed = true;

    // 1. Break Bootstrap / jQuery focus traps that yank focus away from 2FA
    try {
        if (window.$ && window.$.fn && window.$.fn.modal && window.$.fn.modal.Constructor) {
            window.$.fn.modal.Constructor.prototype.enforceFocus = function() {};
        }
        if (window.$ && typeof window.$(document).off === 'function') {
            window.$(document).off('focusin.bs.modal');
        }
    } catch(e) {}

    // 2. Stop modal focus traps from stealing focus when user enters 2FA code (without affecting captchas or buttons)
    document.addEventListener('focusin', function(e) {
        var t = e.target;
        if (t && t.tagName === 'INPUT' && t.matches && t.matches('input[placeholder*="6-digit" i], input[placeholder*="code" i], input[inputmode="numeric"]')) {
            e.stopImmediatePropagation();
        }
    }, true);

    // 3. Forward numeric keys directly to 2FA code input
    window.addEventListener('keydown', function(e) {
        var isDigit = (e.key >= '0' && e.key <= '9');
        if (isDigit || e.key === 'Backspace') {
            var active = document.activeElement;
            if (!active || (active.tagName !== 'INPUT' && active.tagName !== 'TEXTAREA')) {
                var codeInp = document.querySelector('input[placeholder*="6-digit" i], input[placeholder*="code" i], input[inputmode="numeric"]');
                if (codeInp && codeInp.offsetParent !== null) {
                    codeInp.focus();
                }
            }
        }
    }, true);

    // 4. Inject clean CSS rules that apply to present and future elements with ZERO DOM mutation loops
    function injectStyles() {
        if (document.getElementById('aeram-2fa-styles')) return;
        var style = document.createElement('style');
        style.id = 'aeram-2fa-styles';
        style.textContent = `
            .modal-backdrop, .MuiBackdrop-root, .MuiModal-backdrop, [class*="backdrop" i] {
                pointer-events: none !important;
            }
            .modal, [role="presentation"].MuiDialog-root, [role="presentation"].MuiModal-root {
                pointer-events: none !important;
            }
            .modal-dialog, .modal-content, .MuiPaper-root, .MuiDialog-container, [role="dialog"], [role="document"], .two-step-verification {
                pointer-events: auto !important;
            }
            .two-step-verification, [class*="two-step" i], [class*="verification" i], [class*="challenge" i] {
                z-index: 2147483647 !important;
                pointer-events: auto !important;
            }
            iframe[src*="arkose" i], iframe[src*="funcaptcha" i], [id*="arkose" i], [class*="arkose" i] {
                pointer-events: auto !important;
                z-index: 2147483647 !important;
            }
            input[placeholder*="6-digit" i], input[placeholder*="code" i], input[inputmode="numeric"] {
                cursor: text !important;
                pointer-events: auto !important;
            }
        `;
        (document.head || document.documentElement).appendChild(style);
    }

    if (document.head || document.documentElement) {
        injectStyles();
    } else {
        document.addEventListener('DOMContentLoaded', injectStyles);
    }

    // 5. Prevent WebAuthn / Passkey prompts from hanging with infinite spinners in WebEngine
    try {
        if (window.navigator && window.navigator.credentials) {
            window.navigator.credentials.create = function() {
                return Promise.reject(new DOMException("Passkey not supported. Please use password.", "NotAllowedError"));
            };
            window.navigator.credentials.get = function() {
                return Promise.reject(new DOMException("Passkey not supported. Please use password.", "NotAllowedError"));
            };
        }
    } catch(e) {}

    // 6. On "Protect your account" screen, ensure "Create new password" is fully visible and clickable without auto-clicking
    function checkProtectScreen() {
        var body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
        if (body.indexOf('Protect your account') !== -1 || body.indexOf('Add a passkey') !== -1 || body.indexOf('create a new password') !== -1) {
            var buttons = document.querySelectorAll('button, a, input[type="button"]');
            for (var i = 0; i < buttons.length; i++) {
                var btn = buttons[i];
                var txt = (btn.innerText || btn.textContent || btn.value || '').trim().toLowerCase();
                if (txt.indexOf('create new password') !== -1 || txt.indexOf('create a new password') !== -1 || txt.indexOf('new password') !== -1) {
                    btn.removeAttribute('disabled');
                    btn.style.pointerEvents = 'auto';
                    btn.style.zIndex = '2147483647';
                    btn.style.opacity = '1';
                }
            }
        }
    }
    setInterval(checkProtectScreen, 800);
})();
"""

ROBLOX_ENVIRONMENT_RICHNESS_JS = """
(function() {
    'use strict';
    // Completely exempt Arkose Labs, FunCaptcha, Cloudflare, and captcha iframes from any DOM or API tampering
    try {
        var loc = (window.location ? (window.location.href || window.location.hostname || '') : '').toLowerCase();
        if (loc.indexOf('arkose') !== -1 || loc.indexOf('funcaptcha') !== -1 ||
            loc.indexOf('hcaptcha') !== -1 || loc.indexOf('recaptcha') !== -1 ||
            loc.indexOf('cloudflare') !== -1) {
            return;
        }
    } catch(e) {}

    if (window._aeram_richness_installed) return;
    window._aeram_richness_installed = true;

    // 1. Suppress automation indicators (navigator.webdriver)
    try {
        if ('webdriver' in Navigator.prototype) {
            delete Navigator.prototype.webdriver;
        }
        if ('webdriver' in navigator) {
            delete navigator.webdriver;
        }
    } catch (e) {}

    // 2. Realistic Chrome runtime, app, csi, loadTimes
    try {
        if (!window.chrome) {
            window.chrome = {};
        }
        if (!window.chrome.runtime) {
            window.chrome.runtime = {
                connect: function() { return { disconnect: function() {}, onDisconnect: { addListener: function() {} }, onMessage: { addListener: function() {} }, postMessage: function() {} }; },
                sendMessage: function() {}
            };
        }
    } catch (e) {}

    // 3. Realistic Navigator Hardware & Platform properties matching native Linux desktop
    try {
        Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'Linux x86_64', configurable: true });
        Object.defineProperty(Navigator.prototype, 'vendor', { get: () => 'Google Inc.', configurable: true });
        Object.defineProperty(Navigator.prototype, 'deviceMemory', { get: () => 8, configurable: true });
        Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', { get: () => 8, configurable: true });
        Object.defineProperty(Navigator.prototype, 'languages', { get: () => ['en-US', 'en'], configurable: true });
        Object.defineProperty(Navigator.prototype, 'language', { get: () => 'en-US', configurable: true });
    } catch (e) {}

    // 4. Realistic Chrome PluginArray
    try {
        var dummyPlugins = [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
        ];
    } catch (e) {}

    // 5. Authentic GPU WebGL Vendor & Renderer
    try {
        var UNMASKED_VENDOR_WEBGL = 0x9245;
        var UNMASKED_RENDERER_WEBGL = 0x9246;
        function patchContext(proto) {
            if (!proto || !proto.getParameter) return;
            var origGet = proto.getParameter;
            proto.getParameter = function(param) {
                if (param === UNMASKED_VENDOR_WEBGL) return 'Google Inc. (AMD)';
                if (param === UNMASKED_RENDERER_WEBGL) return 'ANGLE (AMD, AMD Radeon Graphics (RADV NAVI48), OpenGL 4.6)';
                return origGet.apply(this, arguments);
            };
        }
        if (typeof WebGLRenderingContext !== 'undefined') patchContext(WebGLRenderingContext.prototype);
        if (typeof WebGL2RenderingContext !== 'undefined') patchContext(WebGL2RenderingContext.prototype);
    } catch (e) {}
})();
"""


class WebLoginDialog(BorderlessDialog):
    """
    Chromium browser dialog for Roblox authentication and session browsing.
    Supports two modes:
    - 'login': Captures credentials on authentication / 2FA.
    - 'browser': Enters Roblox session with the selected account's .ROBLOSECURITY pre-injected.
    """

    account_authenticated = pyqtSignal(object)  # Emitted with the saved RobloxAccount

    def __init__(
        self,
        storage: StorageManager,
        api: RobloxAPI,
        runner: Optional[Any] = None,
        target_account: Optional[RobloxAccount] = None,
        default_group: str = "Default",
        initial_url: Optional[str] = None,
        mode: str = "login",  # 'login' or 'browser'
        parent: Optional[QtWidgets.QWidget] = None,
        auto_login: bool = False,
    ):
        super().__init__(parent, title="Roblox Web Login", can_maximize=True, resizable=True)
        # Promote from Dialog to independent Window so Wayland/X11 compositors grant native keyboard and seat focus
        self.setWindowFlags(QtCore.Qt.WindowType.Window | QtCore.Qt.WindowType.FramelessWindowHint)
        self.storage = storage
        self.api = api
        self.runner = runner
        self.target_account = target_account
        self.default_group = default_group or "Default"
        self.initial_url = initial_url or (ROBLOX_HOME_URL if mode == "browser" else ROBLOX_LOGIN_URL)
        self.mode = mode
        self.auto_login = auto_login
        self.new_account: Optional[RobloxAccount] = None

        self._detected_cookie: Optional[str] = None
        self._captured_password: str = ""
        self._pw_timer: Optional[QTimer] = None
        self._2fa_watcher_timer: Optional[QTimer] = None
        self._2fa_attempts: int = 0
        self._2fa_rejected_codes: set = set()
        self._2fa_submitted_code: str = ""
        self._is_verifying: bool = False
        self._is_saved: bool = False

        if not WEBENGINE_AVAILABLE:
            if self.mode == "browser" and self.target_account:
                self.setWindowTitle(f"Open in Browser - @{self.target_account.username}")
            elif self.target_account:
                self.setWindowTitle(f"Re-authenticate @{self.target_account.username}")
            else:
                self.setWindowTitle("Add Roblox Account")
            self.resize(500, 200)
            self.setMinimumSize(440, 160)
            self.setMaximumSize(580, 240)
        else:
            if self.mode == "browser" and self.target_account:
                self.setWindowTitle(f"Roblox Browser - @{self.target_account.username} ({self.target_account.display_name or ''})")
            elif self.target_account:
                self.setWindowTitle(f"Roblox Web Login - Re-authenticate @{self.target_account.username}")
            else:
                self.setWindowTitle("Roblox Web Login - AERAM")
            self.resize(1080, 800)
            self.setMinimumSize(850, 620)

        self._init_ui()

    def showEvent(self, event):
        super().showEvent(event)
        QtCore.QTimer.singleShot(150, lambda: self.view.setFocus(QtCore.Qt.FocusReason.OtherFocusReason) if hasattr(self, "view") and self.view else None)

    def _init_ui(self):
        layout = self.body_layout

        if not WEBENGINE_AVAILABLE:
            self._init_fallback_ui(layout)
            return

        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ─── Top Navigation & Action Toolbar ───
        top_bar = QtWidgets.QFrame()
        top_bar.setObjectName("webLoginTopBar")
        top_bar.setStyleSheet(
            "QFrame#webLoginTopBar { background-color: #050505; border-bottom: 1px solid #222222; } "
            "QPushButton { background-color: #0d0d0d; border: 1px solid #2a2a2a; color: #cccccc; "
            "padding: 4px 10px; font-size: 11px; font-weight: bold; border-radius: 0px; } "
            "QPushButton:hover { background-color: #181818; border-color: #444444; color: #ffffff; } "
            "QPushButton:pressed { background-color: #080808; } "
            "QLineEdit { background-color: #080808; border: 1px solid #2a2a2a; color: #ffffff; "
            "padding: 4px 8px; font-size: 11px; border-radius: 0px; } "
            "QLineEdit:focus { border-color: #ffffff; background-color: #0d0d0d; } "
            "QComboBox { background-color: #080808; border: 1px solid #2a2a2a; color: #cccccc; "
            "padding: 4px 8px; font-size: 11px; border-radius: 0px; } "
            "QComboBox:hover { border-color: #444444; }"
        )
        top_bar_layout = QtWidgets.QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(12, 8, 12, 8)
        top_bar_layout.setSpacing(6)

        # Navigation buttons
        self.btn_back = QtWidgets.QPushButton("<")
        self.btn_back.setToolTip("Go Back")
        self.btn_back.setFixedWidth(28)
        top_bar_layout.addWidget(self.btn_back)

        self.btn_forward = QtWidgets.QPushButton(">")
        self.btn_forward.setToolTip("Go Forward")
        self.btn_forward.setFixedWidth(28)
        top_bar_layout.addWidget(self.btn_forward)

        self.btn_reload = QtWidgets.QPushButton("Reload")
        self.btn_reload.setToolTip("Reload Page")
        top_bar_layout.addWidget(self.btn_reload)

        self.btn_home = QtWidgets.QPushButton("Home")
        self.btn_home.setToolTip("Go to Roblox Home" if self.mode == "browser" else "Go to Roblox Login")
        top_bar_layout.addWidget(self.btn_home)

        # Header Title / Target Indicator
        top_bar_layout.addSpacing(4)
        if self.mode == "browser" and self.target_account:
            lbl_info = QtWidgets.QLabel(
                f"Browsing as: <b style='color: #ffffff;'>@{self.target_account.username}</b>"
            )
            lbl_info.setStyleSheet("color: #dddddd; font-size: 11px;")
            top_bar_layout.addWidget(lbl_info)
        elif self.target_account:
            lbl_info = QtWidgets.QLabel(
                f"Re-authenticating: <b style='color: #ffffff;'>@{self.target_account.username}</b>"
            )
            lbl_info.setStyleSheet("color: #dddddd; font-size: 11px;")
            top_bar_layout.addWidget(lbl_info)
        else:
            lbl_info = QtWidgets.QLabel("Roblox Account Login")
            lbl_info.setStyleSheet("color: #dddddd; font-size: 11px; font-weight: bold;")
            top_bar_layout.addWidget(lbl_info)

            # Group selection dropdown
            top_bar_layout.addSpacing(6)
            lbl_grp = QtWidgets.QLabel("Group:")
            lbl_grp.setStyleSheet("color: #777777; font-size: 11px;")
            top_bar_layout.addWidget(lbl_grp)

            self.group_combo = QtWidgets.QComboBox()
            self.group_combo.setMinimumWidth(110)
            existing_groups = self.storage.list_groups()
            for g in existing_groups:
                self.group_combo.addItem(g)
            if self.default_group in existing_groups:
                self.group_combo.setCurrentText(self.default_group)
            elif self.default_group:
                self.group_combo.addItem(self.default_group)
                self.group_combo.setCurrentText(self.default_group)
            top_bar_layout.addWidget(self.group_combo)

        # Interactive URL address bar in browser mode
        if self.mode == "browser":
            top_bar_layout.addSpacing(6)
            self.url_bar = QtWidgets.QLineEdit()
            self.url_bar.setText(self.initial_url)
            self.url_bar.returnPressed.connect(self._navigate_url_bar)
            top_bar_layout.addWidget(self.url_bar, stretch=1)

            # Security Quick Actions (Sign Out All Sessions, Security Settings)
            self.btn_security = QtWidgets.QPushButton("Security")
            self.btn_security.setToolTip("Account security, session management, and settings")
            sec_menu = QtWidgets.QMenu(self.btn_security)
            sec_menu.addAction("Log Out of All Other Sessions", self._action_sign_out_all_sessions)
            sec_menu.addSeparator()
            sec_menu.addAction("Open Security Settings", lambda: self.view.setUrl(QUrl("https://www.roblox.com/my/account#!/security")))
            sec_menu.addAction("Clear Browser Cache", self._clear_browser_cache)
            self.btn_security.setMenu(sec_menu)
            top_bar_layout.addWidget(self.btn_security)

            # Dedicated 2FA / Email Input helper button
            self.btn_2fa = QtWidgets.QPushButton("Enter 2FA Code")
            self.btn_2fa.setObjectName("btn2FA")
            self.btn_2fa.setToolTip("Enter 6-digit code from Email, Authenticator App, or Backup (Shortcut: F2 or Ctrl+Shift+A)")
            self.btn_2fa.setStyleSheet(
                "QPushButton#btn2FA { background-color: #161616; border: 1px solid #444444; color: #ffffff; font-weight: bold; } "
                "QPushButton#btn2FA:hover { background-color: #ffffff; border-color: #ffffff; color: #000000; }"
            )
            self.btn_2fa.clicked.connect(self._prompt_and_submit_2fa)
            top_bar_layout.addWidget(self.btn_2fa)
        else:
            top_bar_layout.addStretch()

            self.btn_2fa = QtWidgets.QPushButton("Enter 2FA Code")
            self.btn_2fa.setObjectName("btn2FA")
            self.btn_2fa.setToolTip("Enter 6-digit code from Email, Authenticator App, or Backup (Shortcut: F2 or Ctrl+Shift+A)")
            self.btn_2fa.setStyleSheet(
                "QPushButton#btn2FA { background-color: #161616; border: 1px solid #444444; color: #ffffff; font-weight: bold; } "
                "QPushButton#btn2FA:hover { background-color: #ffffff; border-color: #ffffff; color: #000000; }"
            )
            self.btn_2fa.clicked.connect(self._prompt_and_submit_2fa)
            top_bar_layout.addWidget(self.btn_2fa)

            # Check Login session button (manual trigger fallback for login mode)
            self.btn_check_login = QtWidgets.QPushButton("Check Session")
            self.btn_check_login.setToolTip("Click if you are already logged in on the page and want to force-capture now")
            self.btn_check_login.clicked.connect(self._manual_check_login)
            top_bar_layout.addWidget(self.btn_check_login)

        # Dedicated Auto Log In helper button if account credentials are known
        if self.target_account and self.target_account.username and self.target_account.password:
            self.btn_auto_login = QtWidgets.QPushButton("Auto Log In")
            self.btn_auto_login.setObjectName("btnAutoLogin")
            self.btn_auto_login.setToolTip(f"Automatically fill @{self.target_account.username}, password, and solve 2FA challenge")
            self.btn_auto_login.setStyleSheet(
                "QPushButton#btnAutoLogin { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; } "
                "QPushButton#btnAutoLogin:hover { background-color: #e0e0e0; border-color: #e0e0e0; color: #000000; }"
            )
            self.btn_auto_login.clicked.connect(self._perform_auto_login)
            top_bar_layout.addWidget(self.btn_auto_login)

        # Rich Environment Indicator Badge
        self.badge_rich = QtWidgets.QLabel("RICH ENVIRONMENT")
        self.badge_rich.setToolTip(
            "Hardware-accelerated environment hardening active:\n"
            "- WebGL native GPU (AMD Radeon RADV Mesa OpenGL 4.6)\n"
            "- Chrome 133 runtime, csi, and app polyfills\n"
            "- Standard Linux Chromium PluginArray populated\n"
            "- Automation indicators (navigator.webdriver) suppressed\n"
            "- Coherent platform (Linux x86_64) & 8-core / 8GB memory biometrics"
        )
        self.badge_rich.setStyleSheet(
            "color: #cccccc; font-size: 10px; font-weight: bold; font-family: monospace; "
            "background-color: #0d0d0d; border: 1px solid #2a2a2a; padding: 4px 8px; border-radius: 0px;"
        )
        top_bar_layout.addWidget(self.badge_rich)

        # Keyboard shortcuts for quick 2FA code dialog
        self._shortcut_f2 = QtGui.QShortcut(QtGui.QKeySequence("F2"), self)
        self._shortcut_f2.activated.connect(self._prompt_and_submit_2fa)
        self._shortcut_2fa = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+A"), self)
        self._shortcut_2fa.activated.connect(self._prompt_and_submit_2fa)

        # Open current page in system default browser button
        self.btn_open_external = QtWidgets.QPushButton("External")
        self.btn_open_external.setToolTip("Open Roblox page in your default system browser (Chrome, Firefox, etc.)")
        self.btn_open_external.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(self.view.url()))
        top_bar_layout.addWidget(self.btn_open_external)

        # Close button
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        top_bar_layout.addWidget(self.btn_close)

        layout.addWidget(top_bar)

        # ─── Load Progress Bar ───
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(2)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #111111; border: none; } "
            "QProgressBar::chunk { background-color: #ffffff; }"
        )
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # ─── Isolated Chromium WebEngine ───
        # Use an off-the-record profile so logins are completely isolated and never persist to disk
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpUserAgent(CHROME_USER_AGENT)

        self.page = RobloxWebPage(self.profile, self, navigation_handler=self._handle_roblox_protocol)

        for s in (self.profile.settings(), self.page.settings()):
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, False)
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanPaste, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.AllowWindowActivationFromJavaScript, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.LinksIncludedInFocusChain, True)

        # Install stealth anti-fingerprinting & richness injection script at DocumentCreation
        stealth_script = QWebEngineScript()
        stealth_script.setName("aeram_environment_richness")
        stealth_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        stealth_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        stealth_script.setRunsOnSubFrames(True)
        stealth_script.setSourceCode(ROBLOX_ENVIRONMENT_RICHNESS_JS)
        self.profile.scripts().insert(stealth_script)

        # Install global script running across all main frames and cross-origin subframes/iframes
        subframe_fix = QWebEngineScript()
        subframe_fix.setName("aeram_subframe_focus_fix")
        subframe_fix.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        subframe_fix.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        subframe_fix.setRunsOnSubFrames(True)
        subframe_fix.setSourceCode(ROBLOX_2FA_DOM_FIX_JS)
        self.profile.scripts().insert(subframe_fix)

        self.cookie_store = self.profile.cookieStore()
        if self.mode == "login":
            self.cookie_store.cookieAdded.connect(self._on_cookie_added)

        # If in browser mode, pre-inject the account's .ROBLOSECURITY session cookie
        if self.mode == "browser" and self.target_account and self.target_account.cookie:
            self._inject_account_cookie(self.target_account.cookie)

        self.view = FocusableWebEngineView(self.content_widget)
        self.view.setPage(self.page)
        self.page.setBackgroundColor(QtGui.QColor("#0e0e0e"))

        # Hook navigation buttons
        self.btn_back.clicked.connect(self.view.back)
        self.btn_forward.clicked.connect(self.view.forward)
        self.btn_reload.clicked.connect(self.view.reload)
        home_target = ROBLOX_HOME_URL if self.mode == "browser" else ROBLOX_LOGIN_URL
        self.btn_home.clicked.connect(lambda: self.view.setUrl(QUrl(home_target)))

        # Hook page events
        self.view.loadProgress.connect(self._on_load_progress)
        self.view.loadFinished.connect(self._on_load_finished)
        self.view.urlChanged.connect(self._on_url_changed)

        layout.addWidget(self.view, stretch=1)

        # ─── Bottom Status & Verification Banner ───
        bottom_bar = QtWidgets.QFrame()
        bottom_bar.setObjectName("webLoginBottomBar")
        bottom_bar.setStyleSheet(
            "QFrame#webLoginBottomBar { background-color: #080808; border-top: 1px solid #1c1c1c; }"
        )
        bottom_bar_layout = QtWidgets.QHBoxLayout(bottom_bar)
        bottom_bar_layout.setContentsMargins(14, 8, 14, 8)
        bottom_bar_layout.setSpacing(10)

        if self.mode == "browser":
            user_desc = f"@{self.target_account.username}" if self.target_account else "selected account"
            self.status_label = QtWidgets.QLabel(
                f"Browsing Roblox session for {user_desc}. Changes made in this window reflect live on your account."
            )
            self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
        else:
            self.status_label = QtWidgets.QLabel(
                "Enter your Roblox credentials above. Complete any 2FA or Arkose verification puzzles. "
                "AERAM will automatically detect your login and securely encrypt your session into the vault."
            )
            self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
        self.status_label.setWordWrap(True)
        bottom_bar_layout.addWidget(self.status_label, stretch=1)

        layout.addWidget(bottom_bar)

        # Navigate to target URL
        self.view.setUrl(QUrl(self.initial_url))

    def _init_fallback_ui(self, layout: QtWidgets.QVBoxLayout):
        """Minimal fallback UI rendered when PyQt6-WebEngine is unavailable."""
        self.setStyleSheet(
            "QDialog { background-color: #000000; } "
            "QLabel { color: #cccccc; font-size: 11px; } "
            "QLineEdit { background-color: #080808; border: 1px solid #222222; color: #ffffff; "
            "padding: 6px 10px; font-family: monospace; font-size: 11px; border-radius: 0px; } "
            "QLineEdit:focus { border-color: #ffffff; background-color: #0d0d0d; } "
            "QComboBox { background-color: #080808; border: 1px solid #222222; color: #cccccc; "
            "padding: 3px 8px; font-size: 11px; border-radius: 0px; } "
            "QComboBox:hover { border-color: #444444; } "
            "QPushButton { background-color: #0d0d0d; border: 1px solid #222222; color: #cccccc; "
            "padding: 5px 14px; font-size: 11px; font-weight: bold; border-radius: 0px; } "
            "QPushButton:hover { background-color: #161616; color: #ffffff; border-color: #444444; } "
            "QPushButton#primaryBtn { background-color: #ffffff; border: 1px solid #ffffff; color: #000000; font-weight: bold; } "
            "QPushButton#primaryBtn:hover { background-color: #e0e0e0; border-color: #e0e0e0; color: #000000; } "
            "QPushButton#primaryBtn:pressed { background-color: #888888; color: #000000; }"
        )

        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        # Header row: Title on left, group selector on right
        header_row = QtWidgets.QHBoxLayout()
        header_row.setSpacing(10)

        if self.mode == "browser" and self.target_account:
            lbl_title = QtWidgets.QLabel(f"Session: <b style='color: #ffffff;'>@{self.target_account.username}</b>")
        elif self.target_account:
            lbl_title = QtWidgets.QLabel(f"Re-authenticating <b style='color: #ffffff;'>@{self.target_account.username}</b>")
        else:
            lbl_title = QtWidgets.QLabel("<b style='font-size: 12px; color: #ffffff;'>Add Account</b>")
        header_row.addWidget(lbl_title)
        header_row.addStretch()

        if self.mode != "browser" and not self.target_account:
            lbl_grp = QtWidgets.QLabel("Group:")
            lbl_grp.setStyleSheet("color: #777777; font-size: 11px;")
            header_row.addWidget(lbl_grp)

            self.group_combo = QtWidgets.QComboBox()
            self.group_combo.setMinimumWidth(110)
            existing_groups = self.storage.list_groups()
            for g in existing_groups:
                self.group_combo.addItem(g)
            if self.default_group in existing_groups:
                self.group_combo.setCurrentText(self.default_group)
            elif self.default_group:
                self.group_combo.addItem(self.default_group)
                self.group_combo.setCurrentText(self.default_group)
            header_row.addWidget(self.group_combo)

        layout.addLayout(header_row)

        if self.mode == "browser":
            info_lbl = QtWidgets.QLabel(
                "Embedded WebEngine (PyQt6-WebEngine) is not installed on this system.<br>"
                "Install <code>python-pyqt6-webengine</code> or <code>PyQt6-WebEngine</code> to use the built-in Roblox session browser."
            )
            info_lbl.setWordWrap(True)
            info_lbl.setStyleSheet("color: #aaaaaa; font-size: 11px;")
            layout.addWidget(info_lbl)

            action_row = QtWidgets.QHBoxLayout()
            action_row.setSpacing(8)
            action_row.addStretch()

            btn_cancel = QtWidgets.QPushButton("Close")
            btn_cancel.clicked.connect(self.reject)
            action_row.addWidget(btn_cancel)

            layout.addLayout(action_row)
            return

        # Cookie input field (login fallback)
        self.fallback_cookie_edit = QtWidgets.QLineEdit()
        self.fallback_cookie_edit.setPlaceholderText("Paste .ROBLOSECURITY session cookie...")
        layout.addWidget(self.fallback_cookie_edit)

        # Bottom actions row: Status text on left, Cancel & Authenticate on right
        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(8)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #888888; font-size: 10px;")
        self.status_label.setWordWrap(True)
        action_row.addWidget(self.status_label, stretch=1)

        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        action_row.addWidget(btn_cancel)

        btn_auth = QtWidgets.QPushButton("Authenticate")
        btn_auth.setObjectName("primaryBtn")
        btn_auth.clicked.connect(lambda: self._process_detected_cookie(self.fallback_cookie_edit.text().strip()))
        self.fallback_cookie_edit.returnPressed.connect(btn_auth.click)
        action_row.addWidget(btn_auth)

        layout.addLayout(action_row)

    # ─── Navigation & Loading Handlers ───

    def _inject_account_cookie(self, cookie_value: str):
        """Pre-injects the account's .ROBLOSECURITY cookie into the WebEngine cookie store."""
        try:
            cookie_bytes = cookie_value.strip().encode("utf-8")
            cookie = QNetworkCookie(b".ROBLOSECURITY", cookie_bytes)
            cookie.setDomain(".roblox.com")
            cookie.setPath("/")
            cookie.setHttpOnly(True)
            cookie.setSecure(True)
            cookie.setExpirationDate(QDateTime.currentDateTime().addYears(1))
            self.cookie_store.setCookie(cookie, QUrl("https://www.roblox.com"))
            log.info("Injected .ROBLOSECURITY cookie into WebEngine session store.")
        except Exception as e:
            log.error(f"Failed to inject account cookie into WebEngine store: {e}")

    def _handle_roblox_protocol(self, url_str: str):
        """Handles browser navigation requests to roblox: and roblox-player: protocol schemes."""
        log.info(f"Handling Roblox launch protocol from browser: {url_str}")
        params = parse_roblox_launch_url(url_str)
        place_id_str = params.get("placeid") or params.get("place_id")
        job_id = params.get("gameinstanceid") or params.get("game_instance_id")

        place_id = None
        if place_id_str:
            try:
                place_id = int(place_id_str)
            except (ValueError, TypeError):
                pass

        if self.target_account and self.runner:
            try:
                if hasattr(self, "status_label"):
                    self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
                    self.status_label.setText(
                        f"Launching place {place_id or ''} for @{self.target_account.username} via AERAM..."
                    )
                self.runner.launch_instance(self.target_account, place_id=place_id, job_id=job_id)
                return
            except Exception as e:
                log.error(f"Failed to launch via InstanceRunner from browser: {e}")
                if hasattr(self, "status_label"):
                    self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
                    self.status_label.setText(f"Launch failed: {e}")
                return

        # Fallback to system mocktail handler
        try:
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #ffffff; font-size: 11px;")
            from core.mocktail_manager import MocktailManager
            info = MocktailManager().detect()
            bin_name = str(info.bin_path) if info.is_installed and info.bin_path else "mocktail"
            subprocess.Popen([bin_name, url_str])
        except Exception as e:
            log.error(f"Failed to launch mocktail directly: {e}")
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
                self.status_label.setText(f"System launch failed: {e}")

    def _navigate_url_bar(self):
        """Navigates WebEngine to the URL specified in the address bar."""
        if not hasattr(self, "url_bar"):
            return
        text = self.url_bar.text().strip()
        if not text:
            return
        if not text.startswith("http://") and not text.startswith("https://"):
            text = "https://" + text
        self.view.setUrl(QUrl(text))

    def _on_load_progress(self, progress: int):
        if not hasattr(self, "progress_bar"):
            return
        if progress < 100:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(progress)
        else:
            self.progress_bar.setVisible(False)

    def _on_load_finished(self, success: bool):
        if hasattr(self, "btn_back"):
            self.btn_back.setEnabled(self.view.history().canGoBack())
            self.btn_forward.setEnabled(self.view.history().canGoForward())

        if hasattr(self, "url_bar"):
            self.url_bar.setText(self.view.url().toString())

        # Inject comprehensive DOM fix to neutralize rogue backdrops and unblock 2FA
        if hasattr(self, "page") and self.page:
            self.page.runJavaScript(ROBLOX_2FA_DOM_FIX_JS)

        # Ensure password capture timer is running in login mode so newly created passwords are saved
        if self.mode == "login" and not self._pw_timer:
            self._pw_timer = QTimer(self)
            self._pw_timer.setInterval(200)
            self._pw_timer.timeout.connect(self._poll_password_field)
            self._pw_timer.start()

        # Trigger auto-login if requested or account credentials are present
        if getattr(self, "auto_login", False) and self.target_account and self.target_account.username and self.target_account.password:
            current_url = self.view.url().toString() if hasattr(self, "view") else ""
            if "roblox.com/login" in current_url or self.mode == "login":
                QTimer.singleShot(700, self._perform_auto_login)

    def _perform_auto_login(self):
        """
        Automatically fills in username and password, clicks Log In,
        and starts the automated 2FA observer to enter TOTP code upon prompt.
        """
        if not self.target_account or not self.target_account.username or not self.target_account.password:
            if hasattr(self, "status_label"):
                self.status_label.setText("Account lacks username or password for auto-login.")
            return

        username = self.target_account.username
        password = self.target_account.password
        totp_secret = self.target_account.totp_secret or ""

        u_json = json.dumps(username)
        p_json = json.dumps(password)

        if hasattr(self, "status_label"):
            self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
            self.status_label.setText(f"Auto Log In: Injecting credentials for @{username}...")

        js = f"""
        (function(u, p) {{
            function setVal(inp, val) {{
                if (!inp) return false;
                try {{
                    var descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                    if (descriptor && descriptor.set) {{
                        descriptor.set.call(inp, val);
                    }} else {{
                        inp.value = val;
                    }}
                }} catch(e) {{
                    inp.value = val;
                }}
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}

            var userInp = document.getElementById('login-username') || 
                          document.querySelector('input[name="username"]') || 
                          document.querySelector('input[autocomplete="username"]');
            var passInp = document.getElementById('login-password') || 
                          document.querySelector('input[name="password"]') || 
                          document.querySelector('input[type="password"]');

            var filledUser = setVal(userInp, u);
            var filledPass = setVal(passInp, p);

            if (filledUser && filledPass) {{
                setTimeout(function() {{
                    var loginBtn = document.getElementById('login-button') || 
                                   document.querySelector('button[type="submit"]') || 
                                   document.querySelector('.login-button');
                    if (loginBtn) {{
                        loginBtn.removeAttribute('disabled');
                        loginBtn.click();
                    }}
                }}, 350);
                return true;
            }}
            return false;
        }})({u_json}, {p_json});
        """
        if hasattr(self, "view") and self.view and hasattr(self.view, "page"):
            self.view.page().runJavaScript(js, self._on_credentials_injected)

        # Start 2FA auto-injection poll timer if TOTP secret exists
        if totp_secret:
            self._start_2fa_auto_watcher()

    def _on_credentials_injected(self, success: Any):
        if success:
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #ffffff; font-size: 11px;")
                self.status_label.setText("Credentials filled and Login submitted. Awaiting 2FA / Session capture...")
        else:
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #cccccc; font-size: 11px;")
                self.status_label.setText("Could not locate login fields. You may already be on a challenge or logged in.")

    def _start_2fa_auto_watcher(self):
        """Polls every 800ms for 2FA challenge and automatically injects TOTP code."""
        if getattr(self, "_2fa_watcher_timer", None) and self._2fa_watcher_timer.isActive():
            return
        self._2fa_watcher_timer = QTimer(self)
        self._2fa_watcher_timer.setInterval(800)
        self._2fa_attempts = 0
        self._2fa_rejected_codes = set()
        self._2fa_submitted_code = ""
        self._2fa_watcher_timer.timeout.connect(self._check_and_inject_2fa)
        self._2fa_watcher_timer.start()

    def _check_and_inject_2fa(self):
        if not self.target_account or not self.target_account.totp_secret:
            if hasattr(self, "_2fa_watcher_timer") and self._2fa_watcher_timer:
                self._2fa_watcher_timer.stop()
            return
        if self._is_saved:
            if hasattr(self, "_2fa_watcher_timer") and self._2fa_watcher_timer:
                self._2fa_watcher_timer.stop()
            return

        self._2fa_attempts = getattr(self, "_2fa_attempts", 0) + 1
        if self._2fa_attempts > 75:  # Timeout after ~60s
            if hasattr(self, "_2fa_watcher_timer") and self._2fa_watcher_timer:
                self._2fa_watcher_timer.stop()
            return

        from core.totp import generate_totp, get_totp_remaining_seconds
        code = generate_totp(self.target_account.totp_secret)
        if not code:
            return

        # If this exact code was already rejected or used, wait for the next 30-second TOTP rotation
        if code in getattr(self, "_2fa_rejected_codes", set()):
            rem = get_totp_remaining_seconds(30)
            if hasattr(self, "status_label") and not self._is_verifying:
                self.status_label.setStyleSheet("color: #ffd700; font-size: 11px; font-weight: bold;")
                self.status_label.setText(
                    f"Code {code[:3]} {code[3:]} already used. Waiting for next TOTP rotation ({rem}s remaining)..."
                )
            return

        code_json = json.dumps(code)
        js = """
        (function(code) {
            function find2FAChallenge(doc) {
                if (!doc) return null;

                var selectors = [
                    'input[placeholder*="6-digit" i]',
                    'input[placeholder*="code" i]',
                    'input[inputmode="numeric"]',
                    'input[name*="code" i]',
                    'input[name*="twoStep" i]',
                    'input[type="text"][maxlength="6"]',
                    'input[type="number"][maxlength="6"]'
                ];

                for (var s = 0; s < selectors.length; s++) {
                    var inputs = doc.querySelectorAll(selectors[s]);
                    for (var i = 0; i < inputs.length; i++) {
                        var inp = inputs[i];
                        if (!inp || inp.offsetParent === null) continue;
                        if (inp.id === 'login-username' || inp.id === 'login-password' ||
                            inp.name === 'username' || inp.name === 'password' || inp.type === 'password') {
                            continue;
                        }
                        var rect = inp.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;

                        // Check modal/dialog container around input
                        var modal = inp.closest('[role="dialog"], .modal-dialog, .modal-content, [class*="modal" i], .two-step-verification, [class*="two-step" i], [class*="challenge" i], [class*="verification" i]') || inp.parentElement;
                        var modalText = (modal ? (modal.innerText || modal.textContent || '') : '').toLowerCase();
                        var placeholder = (inp.getAttribute('placeholder') || '').toLowerCase();

                        // Must explicitly indicate a 2-step or security challenge
                        var is2FA = (
                            placeholder.indexOf('6-digit') !== -1 ||
                            modalText.indexOf('2-step') !== -1 ||
                            modalText.indexOf('two-step') !== -1 ||
                            modalText.indexOf('authenticator') !== -1 ||
                            modalText.indexOf('security code') !== -1 ||
                            modalText.indexOf('sent to your email') !== -1 ||
                            modalText.indexOf('sent an email') !== -1 ||
                            modalText.indexOf('sent a code') !== -1
                        );

                        // Must not be the main login box
                        if (modalText.indexOf('login to roblox') !== -1 && modalText.indexOf('2-step') === -1 && modalText.indexOf('authenticator') === -1) {
                            continue;
                        }

                        if (is2FA) {
                            return { inp: inp, modal: modal || doc.body, modalText: modalText, doc: doc };
                        }
                    }
                }

                var iframes = doc.querySelectorAll('iframe');
                for (var f = 0; f < iframes.length; f++) {
                    try {
                        var subDoc = iframes[f].contentDocument || (iframes[f].contentWindow && iframes[f].contentWindow.document);
                        var found = find2FAChallenge(subDoc);
                        if (found) return found;
                    } catch(e) {}
                }
                return null;
            }

            var challenge = find2FAChallenge(document);
            if (!challenge) {
                var bodyText = (document.body ? (document.body.innerText || document.body.textContent || '') : '').toLowerCase();
                if (bodyText.indexOf('protect your account') !== -1 || bodyText.indexOf('add a passkey') !== -1 || bodyText.indexOf('new password') !== -1) {
                    return { status: "protect_account" };
                }
                return { status: "none" };
            }

            var inp = challenge.inp;
            var modal = challenge.modal;
            var modalText = challenge.modalText;
            var doc = challenge.doc;

            // 4. Detect rejected/used code errors displayed by Roblox
            var isCodeUsedOrInvalid = (
                modalText.indexOf('already been used') !== -1 ||
                modalText.indexOf('enter a new code') !== -1 ||
                modalText.indexOf('invalid code') !== -1 ||
                modalText.indexOf('incorrect code') !== -1 ||
                modalText.indexOf('code has expired') !== -1
            );
            if (isCodeUsedOrInvalid) {
                try {
                    var desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                    if (desc && desc.set) desc.set.call(inp, '');
                    else inp.value = '';
                } catch(e) { inp.value = ''; }
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                return { status: "code_rejected", code: code };
            }

            // 5. Challenge type discrimination
            var isAuthApp = (
                modalText.indexOf('authenticator') !== -1 ||
                modalText.indexOf('totp') !== -1
            );

            var isEmail = !isAuthApp && (
                modalText.indexOf('sent to your email') !== -1 ||
                modalText.indexOf('sent an email') !== -1 ||
                modalText.indexOf('sent you an email') !== -1 ||
                modalText.indexOf('code sent to your email') !== -1 ||
                modalText.indexOf('check your inbox') !== -1 ||
                modalText.indexOf('verify your email') !== -1 ||
                modalText.indexOf('sent a 6-digit code to your email') !== -1
            );

            var isPhone = !isAuthApp && !isEmail && (
                modalText.indexOf('sent a code to your phone') !== -1 ||
                modalText.indexOf('sent to your phone') !== -1 ||
                modalText.indexOf('sent a text message') !== -1 ||
                modalText.indexOf('code sent via sms') !== -1
            );

            if (isEmail) {
                if (inp.value === code) {
                    try {
                        var d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                        if (d && d.set) d.set.call(inp, '');
                        else inp.value = '';
                    } catch(e) { inp.value = ''; }
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return { status: "email_challenge" };
            }

            if (isPhone) {
                if (inp.value === code) {
                    try {
                        var d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                        if (d && d.set) d.set.call(inp, '');
                        else inp.value = '';
                    } catch(e) { inp.value = ''; }
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return { status: "phone_challenge" };
            }

            if (!isAuthApp) {
                isAuthApp = (
                    modalText.indexOf('two-step') !== -1 ||
                    modalText.indexOf('2-step') !== -1 ||
                    modalText.indexOf('security code') !== -1
                );
            }

            if (!isAuthApp) {
                return { status: "none" };
            }

            // 6. Find and click Verify button with full synthetic mouse/pointer event chain
            function findVerifyButtons() {
                var root = inp.closest('[role="dialog"], .modal-dialog, .modal-content, [class*="modal" i]') || modal || document;
                var btns = root.querySelectorAll('button, input[type="submit"], [role="button"], a');
                var results = [];
                for (var b = 0; b < btns.length; b++) {
                    var btn = btns[b];
                    if (!btn || btn.offsetParent === null) continue;
                    var txt = (btn.innerText || btn.textContent || btn.value || '').trim().toLowerCase();
                    var aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (txt.indexOf('passkey') !== -1 || aria.indexOf('passkey') !== -1 ||
                        txt.indexOf('cancel') !== -1 || txt.indexOf('resend') !== -1 ||
                        txt.indexOf('another') !== -1 || txt.indexOf('help') !== -1 ||
                        txt.indexOf('close') !== -1 || aria.indexOf('close') !== -1) {
                        continue;
                    }
                    if (txt === 'verify' || aria === 'verify' ||
                        (txt.indexOf('verify') !== -1 && txt.indexOf('another') === -1 && txt.indexOf('method') === -1) ||
                        btn.classList.contains('btn-primary-md') || btn.classList.contains('btn-primary')) {
                        results.push(btn);
                    }
                }
                return results;
            }

            function triggerVerifyClick(btn) {
                if (!btn) return;
                try {
                    btn.removeAttribute('disabled');
                    btn.disabled = false;
                    btn.focus();
                    var opts = { bubbles: true, cancelable: true, view: window, buttons: 1 };
                    try { btn.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch(e) {}
                    try { btn.dispatchEvent(new MouseEvent('mousedown', opts)); } catch(e) {}
                    try { btn.dispatchEvent(new PointerEvent('pointerup', opts)); } catch(e) {}
                    try { btn.dispatchEvent(new MouseEvent('mouseup', opts)); } catch(e) {}
                    try { btn.dispatchEvent(new MouseEvent('click', opts)); } catch(e) {}
                    btn.click();
                } catch(e) {}
            }

            function submit2FA() {
                // Dispatch Enter on input
                try {
                    inp.focus();
                    var kOpts = { bubbles: true, cancelable: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13, view: window };
                    inp.dispatchEvent(new KeyboardEvent('keydown', kOpts));
                    inp.dispatchEvent(new KeyboardEvent('keypress', kOpts));
                    inp.dispatchEvent(new KeyboardEvent('keyup', kOpts));
                    if (inp.form) inp.form.requestSubmit();
                } catch(e) {}

                // Trigger Verify button clicks
                var buttons = findVerifyButtons();
                var clickedAny = false;
                for (var i = 0; i < buttons.length; i++) {
                    triggerVerifyClick(buttons[i]);
                    clickedAny = true;
                }
                return clickedAny;
            }

            // 7. Inject code if not filled
            if (inp.value !== code) {
                try {
                    if (inp._valueTracker) {
                        inp._valueTracker.setValue('');
                    }
                    var descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                    if (descriptor && descriptor.set) {
                        descriptor.set.call(inp, code);
                    } else {
                        inp.value = code;
                    }
                } catch(err) {
                    inp.value = code;
                }
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));

                // Check "Trust this device" checkbox
                try {
                    var cb = modal.querySelector('input[type="checkbox"]');
                    if (cb && !cb.checked) {
                        cb.click();
                    }
                } catch(e) {}

                // Trigger submission after brief delays
                setTimeout(submit2FA, 100);
                setTimeout(submit2FA, 300);

                return { status: "injected" };
            }

            // Code is already filled: continue submitting until page advances or errors
            var clicked = submit2FA();
            return { status: "already_injected", clicked: clicked };
        })(__CODE_JSON__);
        """.replace("__CODE_JSON__", code_json)
        if hasattr(self, "view") and self.view and hasattr(self.view, "page"):
            def _on_2fa_checked(res):
                if not isinstance(res, dict):
                    return
                status = res.get("status")
                if status == "code_rejected":
                    rej_code = res.get("code") or code
                    self._2fa_rejected_codes.add(rej_code)
                    from core.totp import get_totp_remaining_seconds
                    rem = get_totp_remaining_seconds(30)
                    if hasattr(self, "status_label"):
                        self.status_label.setStyleSheet("color: #ffd700; font-size: 11px; font-weight: bold;")
                        self.status_label.setText(
                            f"Code {rej_code[:3]} {rej_code[3:]} already used. Waiting for next TOTP rotation ({rem}s remaining)..."
                        )
                elif status == "protect_account":
                    if hasattr(self, "_2fa_watcher_timer") and self._2fa_watcher_timer:
                        self._2fa_watcher_timer.stop()
                    if hasattr(self, "status_label"):
                        self.status_label.setStyleSheet("color: #cccccc; font-size: 11px;")
                        self.status_label.setText("Account security screen: Please proceed on the page.")
                elif status == "email_challenge":
                    if hasattr(self, "status_label"):
                        self.status_label.setStyleSheet("color: #ffd700; font-size: 11px; font-weight: bold;")
                        self.status_label.setText("Email verification code detected! Please enter the 6-digit code sent to your email.")
                elif status == "phone_challenge":
                    if hasattr(self, "status_label"):
                        self.status_label.setStyleSheet("color: #ffd700; font-size: 11px; font-weight: bold;")
                        self.status_label.setText("SMS / Phone verification code detected! Please enter the code sent to your phone.")
                elif status == "injected":
                    self._2fa_submitted_code = code
                    if hasattr(self, "status_label"):
                        self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
                        self.status_label.setText(f"Authenticator 2FA detected! Injected code {code[:3]} {code[3:]} and submitting...")
                elif status == "already_injected":
                    if hasattr(self, "status_label") and not self._is_verifying:
                        self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
                        self.status_label.setText(f"Authenticator code {code[:3]} {code[3:]} entered. Submitting verification...")
            self.view.page().runJavaScript(js, _on_2fa_checked)

    def _prompt_and_submit_2fa(self):
        """
        Opens a clean native dialog prompting for a 6-digit authenticator or backup code,
        then injects it directly into the Roblox 2FA challenge via React's prototype value setter.
        """
        code, ok = BorderlessInputDialog.getText(
            self,
            "Two-Step Verification",
            "Enter your 6-digit verification code (from Email, Authenticator, or Backup):",
        )
        if not ok or not code:
            return

        code = code.strip()
        code_json = json.dumps(code)

        js = f"""
        (function(code) {{
            function tryInject(doc) {{
                if (!doc) return false;
                var selectors = [
                    'input[placeholder*="6-digit" i]',
                    'input[placeholder*="code" i]',
                    'input[inputmode="numeric"]',
                    'input[name*="code" i]',
                    'input[name*="twoStep" i]',
                    'input[type="text"][maxlength="6"]',
                    'input[type="number"][maxlength="6"]',
                    'input[type="text"]'
                ];
                for (var s = 0; s < selectors.length; s++) {{
                    var inputs = doc.querySelectorAll(selectors[s]);
                    for (var i = 0; i < inputs.length; i++) {{
                        var inp = inputs[i];
                        if (inp && inp.offsetParent !== null) {{
                            try {{
                                var descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                                if (descriptor && descriptor.set) {{
                                    descriptor.set.call(inp, code);
                                }} else {{
                                    inp.value = code;
                                }}
                            }} catch(err) {{
                                inp.value = code;
                            }}
                            inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            inp.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: 'Enter', keyCode: 13 }}));

                            setTimeout(function() {{
                                var container = inp.closest('form, [role="dialog"], .modal-content, .two-step-verification, [class*="verification" i]') || inp.parentElement || doc;
                                var buttons = container.querySelectorAll('button, input[type="submit"]');
                                for (var b = 0; b < buttons.length; b++) {{
                                    var btn = buttons[b];
                                    var txt = (btn.innerText || btn.textContent || btn.value || '').trim().toLowerCase();
                                    var aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                                    if (txt.indexOf('passkey') !== -1 || aria.indexOf('passkey') !== -1 ||
                                        txt.indexOf('cancel') !== -1 || txt.indexOf('resend') !== -1 ||
                                        txt.indexOf('another') !== -1 || txt.indexOf('help') !== -1) {{
                                        continue;
                                    }}
                                    if (txt === 'verify' || txt === 'submit' || txt === 'continue' || txt === 'ok') {{
                                        btn.removeAttribute('disabled');
                                        btn.click();
                                        break;
                                    }}
                                }}
                            }}, 150);
                            return true;
                        }}
                    }}
                }}
                var iframes = doc.querySelectorAll('iframe');
                for (var f = 0; f < iframes.length; f++) {{
                    try {{
                        var subDoc = iframes[f].contentDocument || (iframes[f].contentWindow && iframes[f].contentWindow.document);
                        if (subDoc && tryInject(subDoc)) return true;
                    }} catch(e) {{}}
                }}
                return false;
            }}
            return tryInject(document);
        }})({code_json});
        """
        if hasattr(self, "view") and self.view and hasattr(self.view, "page"):
            self.view.page().runJavaScript(js, self._on_2fa_submit_result)

    def _on_2fa_submit_result(self, result):
        if hasattr(self, "status_label"):
            if result:
                self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
                self.status_label.setText("2FA code filled and verification submitted to Roblox.")
            else:
                self.status_label.setStyleSheet("color: #cccccc; font-size: 11px;")
                self.status_label.setText("2FA code dispatched. Complete verification on page.")

        # If in login mode, initiate periodic password capture from input fields
        if self.mode == "login":
            if not self._pw_timer:
                self._pw_timer = QTimer(self)
                self._pw_timer.setInterval(200)
                self._pw_timer.timeout.connect(self._poll_password_field)
                self._pw_timer.start()

            current_url = self.view.url().toString()
            if any(target in current_url for target in ("/home", "/discover", "/games", "/my/avatar")):
                self.cookie_store.loadAllCookies()

    def _action_sign_out_all_sessions(self):
        """Signs out of all other active sessions for the current account."""
        cookie = ""
        acc = self.target_account
        if acc and acc.cookie:
            cookie = acc.cookie
        elif self._detected_cookie:
            cookie = self._detected_cookie

        if not cookie:
            QMessageBox.warning(self, "Sign Out Sessions", "No active session token available.")
            return

        user_label = f"@{acc.username}" if acc else "this account"
        ans = QMessageBox.question(
            self,
            "Sign Out All Other Sessions",
            f"Are you sure you want to sign out of all other sessions for {user_label}?\n\n"
            "- This will invalidate all other logged-in browsers and devices.\n"
            "- AERAM will keep this account logged in with a freshly rotated session cookie.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        if hasattr(self, "status_label"):
            self.status_label.setStyleSheet("color: #cccccc; font-size: 11px; font-weight: bold;")
            self.status_label.setText("Signing out of all other sessions...")
            QtWidgets.QApplication.processEvents()

        def two_fa_callback(info: Optional[Dict[str, Any]] = None, *args, **kwargs) -> str:
            code, ok_pressed = BorderlessInputDialog.getText(
                self,
                "Two-Step Verification",
                f"Enter 6-digit Authenticator / Backup code for {user_label}:",
            )
            return code.strip() if ok_pressed else ""

        ok, new_cookie, msg = self.api.sign_out_of_all_sessions(cookie, two_fa_callback=two_fa_callback)
        if ok and new_cookie:
            if acc:
                acc.cookie = new_cookie
                self.storage.save_account(acc)
                self.account_authenticated.emit(acc)
            self._detected_cookie = new_cookie
            self._inject_account_cookie(new_cookie)
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
                self.status_label.setText("Signed out of all other sessions successfully! Fresh token saved.")
            QMessageBox.information(
                self,
                "Sign Out Successful",
                f"Successfully signed out of all other sessions for {user_label}!\n\n"
                "A fresh session token has been securely stored in AERAM.",
            )
            # Reload page with the new rotated session
            self.view.reload()
        else:
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #888888; font-size: 11px; font-weight: bold;")
                self.status_label.setText(f"Sign out failed: {msg}")
            QMessageBox.warning(
                self,
                "Sign Out Failed",
                f"Could not sign out of other sessions: {msg}",
            )

    def _clear_browser_cache(self):
        """Clears embedded browser HTTP cache, memory cache, and compiled shader cache."""
        try:
            if hasattr(self, "profile") and self.profile:
                self.profile.clearHttpCache()
            if hasattr(self, "status_label"):
                self.status_label.setStyleSheet("color: #ffffff; font-size: 11px;")
                self.status_label.setText("Browser HTTP cache and local shader storage successfully cleared.")
        except Exception as e:
            log.error(f"Failed to clear browser cache: {e}")

    def _poll_password_field(self):
        """Replicates RAM's JavaScript password reader from Roblox login page."""
        if not hasattr(self, "page") or not self.page or self._is_saved:
            if self._pw_timer and self._pw_timer.isActive():
                self._pw_timer.stop()
            return
        js_code = """
        (function() {
            var el = document.getElementById('login-password') || 
                     document.getElementById('signup-password') ||
                     document.querySelector('input[type="password"]');
            return el ? el.value : '';
        })();
        """
        self.page.runJavaScript(js_code, self._on_password_evaluated)

    def _on_password_evaluated(self, val: Any):
        if isinstance(val, str) and val.strip():
            self._captured_password = val.strip()

    def _on_url_changed(self, url: QUrl):
        url_str = url.toString()
        log.debug(f"WebEngine URL changed: {url_str}")

        if hasattr(self, "url_bar"):
            self.url_bar.setText(url_str)

        # In browser mode, do not auto-save / exit on navigation
        if self.mode == "browser":
            return

        # Check if redirected to post-login endpoints in login mode
        if any(target in url_str for target in ("/home", "/discover", "/games", "/my/avatar")):
            if not self._is_saved and not self._is_verifying:
                self.status_label.setStyleSheet("color: #cccccc; font-size: 11px; font-weight: bold;")
                self.status_label.setText("Redirect detected. Checking session credentials...")
                self.cookie_store.loadAllCookies()

    # ─── Cookie Interception & Enrichment ───

    def _on_cookie_added(self, cookie: "QNetworkCookie"):
        """Triggered whenever a cookie is set in the off-the-record profile."""
        if self.mode != "login":
            return
        try:
            name = cookie.name().data().decode("utf-8", errors="ignore")
        except Exception:
            return

        if name == ".ROBLOSECURITY":
            try:
                val = cookie.value().data().decode("utf-8", errors="ignore").strip()
            except Exception:
                return

            if val and val != self._detected_cookie and not self._is_saved:
                self._detected_cookie = val
                log.info("Detected .ROBLOSECURITY cookie from WebEngine cookie store.")
                self._process_detected_cookie(val)

    def _manual_check_login(self):
        """Triggered when the user clicks 'Check Session'."""
        if self._is_verifying or self._is_saved:
            return
        self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
        self.status_label.setText("Scanning cookie store for active Roblox session...")
        QtWidgets.QApplication.processEvents()
        if hasattr(self, "cookie_store") and self.cookie_store:
            self.cookie_store.loadAllCookies()

        if self._detected_cookie:
            self._process_detected_cookie(self._detected_cookie)
        else:
            # Short delay in case cookies are still loading
            QTimer.singleShot(600, self._check_detected_after_flush)

    def _check_detected_after_flush(self):
        if self._detected_cookie and not self._is_saved:
            self._process_detected_cookie(self._detected_cookie)
        elif not self._is_saved:
            self.status_label.setStyleSheet("color: #cccccc; font-size: 11px;")
            self.status_label.setText(
                "No active Roblox session cookie detected yet. Please ensure you are logged into roblox.com."
            )

    def _process_detected_cookie(self, raw_cookie: str, attempt: int = 1):
        """Validates and persists the intercepted session cookie."""
        if self._is_verifying or self._is_saved:
            return

        cookie = clean_cookie(raw_cookie)
        if not cookie or len(cookie) < 40:
            return

        self._is_verifying = True
        self.status_label.setStyleSheet("color: #cccccc; font-size: 11px; font-weight: bold;")
        self.status_label.setText("Session detected. Verifying account with Roblox API...")
        QtWidgets.QApplication.processEvents()

        # Enrich account data via API
        data = self.api.enrich_account(cookie)
        if not data:
            if attempt < 3:
                # Retry in 1.2 seconds in case the backend session handshake is still propagating
                log.debug(f"Session validation attempt {attempt} pending, retrying in 1200ms...")
                self._is_verifying = False
                QTimer.singleShot(1200, lambda: self._process_detected_cookie(raw_cookie, attempt=attempt + 1))
                return

            self._is_verifying = False
            self.status_label.setStyleSheet("color: #888888; font-size: 11px; font-weight: bold;")
            self.status_label.setText(
                "Session token received but could not be validated by Roblox API. You can continue or click 'Check Session'."
            )
            return

        self._finalize_account(cookie, data)

    def _finalize_account(self, cookie: str, data: Dict[str, Any]):
        """Persists the enriched account into the vault and closes the dialog."""
        self._is_saved = True
        self._is_verifying = False

        username = data["username"]
        display_name = data["display_name"]
        user_id = data["user_id"]
        avatar_url = data["avatar_url"]
        robux = data["robux_balance"]

        if self._pw_timer and self._pw_timer.isActive():
            self._pw_timer.stop()

        if self.target_account:
            # Re-authenticating existing account
            if self.target_account.username.lower() == username.lower():
                acc = self.target_account
                acc.cookie = cookie
                if self._captured_password:
                    acc.password = self._captured_password
                acc.display_name = display_name
                acc.user_id = user_id
                acc.avatar_url = avatar_url
                acc.robux_balance = robux
            else:
                # Logged in as a different account than the target
                existing = self.storage.find_account_by_username(username)
                acc_id = existing.account_id if existing else f"acc_{uuid.uuid4().hex[:12]}"
                pw = self._captured_password or (existing.password if existing else "")
                acc = RobloxAccount(
                    account_id=acc_id,
                    username=username,
                    display_name=display_name,
                    user_id=user_id,
                    cookie=cookie,
                    password=pw,
                    avatar_url=avatar_url,
                    robux_balance=robux,
                    group=self.target_account.group,
                    notes=self.target_account.notes,
                )
        else:
            # Adding new account
            group = self.group_combo.currentText().strip() if hasattr(self, "group_combo") else self.default_group
            group = group or "Default"
            existing = self.storage.find_account_by_username(username)
            acc_id = existing.account_id if existing else f"acc_{uuid.uuid4().hex[:12]}"
            pw = self._captured_password or (existing.password if existing else "")
            acc = RobloxAccount(
                account_id=acc_id,
                username=username,
                display_name=display_name,
                user_id=user_id,
                cookie=cookie,
                password=pw,
                avatar_url=avatar_url,
                robux_balance=robux,
                group=group,
                notes=existing.notes if existing else "",
            )

        # Save to local encrypted vault
        self.storage.save_account(acc)
        self.new_account = acc
        self.account_authenticated.emit(acc)

        self.status_label.setStyleSheet("color: #ffffff; font-size: 11px; font-weight: bold;")
        self.status_label.setText(
            f"Successfully authenticated as @{username} ({display_name}). Session saved to vault."
        )
        QtWidgets.QApplication.processEvents()

        # Stop browser engine to terminate active network sessions if initialized
        if hasattr(self, "view") and self.view:
            try:
                self.view.stop()
            except Exception:
                pass

        # Brief delay to display success feedback to user, then accept
        QTimer.singleShot(800, self.accept)

    def closeEvent(self, event: QtGui.QCloseEvent):
        if hasattr(self, "_pw_timer") and self._pw_timer and self._pw_timer.isActive():
            self._pw_timer.stop()
        if hasattr(self, "_2fa_watcher_timer") and self._2fa_watcher_timer and self._2fa_watcher_timer.isActive():
            self._2fa_watcher_timer.stop()
        try:
            if hasattr(self, "view") and self.view:
                self.view.stop()
                if hasattr(self, "page") and self.page:
                    self.page.deleteLater()
                self.view.deleteLater()
        except Exception:
            pass
        super().closeEvent(event)
