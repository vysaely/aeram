"""
Unit and integration tests for AERAM Authentication Lifecycle State Machine,
Arkose CAPTCHA challenge detection, retry suppression, and session persistence.
Standard Python unittest suite.
"""

import os
import sys
import time
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import io
import json
from pathlib import Path

from core.models import RobloxAccount
from core.auth_state import AccountAuthState, AccountAuthStateManager, AuthStateInfo
from core.roblox_api import RobloxAPI
from core.storage import StorageManager
from core.instance_runner import InstanceRunner


class TestAuthStateManager(unittest.TestCase):
    """Tests for AccountAuthStateManager state transitions and deduplication."""

    def test_default_state(self):
        mgr = AccountAuthStateManager()
        state = mgr.get_state("acc_unknown")
        self.assertEqual(state, AccountAuthState.STORED)

    def test_state_transitions(self):
        mgr = AccountAuthStateManager()
        aid = "acc_1"

        mgr.set_state(aid, AccountAuthState.VALIDATING)
        self.assertEqual(mgr.get_state(aid), AccountAuthState.VALIDATING)

        mgr.mark_ready(aid)
        self.assertEqual(mgr.get_state(aid), AccountAuthState.READY)

        mgr.set_state(aid, AccountAuthState.AUTHENTICATING)
        self.assertEqual(mgr.get_state(aid), AccountAuthState.AUTHENTICATING)

        mgr.mark_running(aid)
        self.assertEqual(mgr.get_state(aid), AccountAuthState.RUNNING)

    def test_mark_verification_required_halts_launch(self):
        mgr = AccountAuthStateManager()
        aid = "acc_challenge"

        mgr.mark_verification_required(aid, challenge_type="captcha", challenge_id="ch-uuid-1", challenge_metadata="blob123")
        info = mgr.get_state_info(aid)

        self.assertEqual(info.state, AccountAuthState.VERIFICATION_REQUIRED)
        self.assertEqual(info.challenge_type, "captcha")
        self.assertEqual(info.challenge_id, "ch-uuid-1")
        self.assertEqual(info.challenge_metadata, "blob123")

        # Launch must be rejected
        can_launch, reason = mgr.can_launch(aid)
        self.assertFalse(can_launch)
        self.assertIn("Verification required", reason)

    def test_mark_session_expired_halts_launch(self):
        mgr = AccountAuthStateManager()
        aid = "acc_expired"

        mgr.mark_session_expired(aid, reason="HTTP 401: Unauthorized")
        self.assertEqual(mgr.get_state(aid), AccountAuthState.SESSION_EXPIRED)

        can_launch, reason = mgr.can_launch(aid)
        self.assertFalse(can_launch)
        self.assertIn("expired", reason.lower())

    def test_reset_verification_status(self):
        mgr = AccountAuthStateManager()
        aid = "acc_reset"

        mgr.mark_verification_required(aid)
        self.assertFalse(mgr.can_launch(aid)[0])

        mgr.reset_verification_status(aid)
        self.assertEqual(mgr.get_state(aid), AccountAuthState.READY)
        can_launch, _ = mgr.can_launch(aid)
        self.assertTrue(can_launch)

    def test_launch_deduplication_lock(self):
        mgr = AccountAuthStateManager()
        aid = "acc_concurrent"

        # First acquisition succeeds
        self.assertTrue(mgr.acquire_launch_lock(aid))
        self.assertTrue(mgr.is_launching(aid))

        # Second acquisition while in progress must fail
        self.assertFalse(mgr.acquire_launch_lock(aid))

        # Release lock
        mgr.release_launch_lock(aid)
        self.assertFalse(mgr.is_launching(aid))

        # Re-acquisition now succeeds
        self.assertTrue(mgr.acquire_launch_lock(aid))
        mgr.release_launch_lock(aid)


class TestInstanceRunnerLifecycleIntegration(unittest.TestCase):
    """Integration tests verifying InstanceRunner lifecycle safety."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.sample_account = RobloxAccount(
            account_id="acc_test123",
            username="TestPlayer",
            display_name="TestAlias",
            user_id=12345678,
            cookie="TEST_COOKIE_TOKEN_SECRET",
            group="Default",
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_launch_stops_and_records_on_challenge(self):
        storage = StorageManager(base_dir=self.base_dir)
        storage.save_account(self.sample_account)

        api = MagicMock()

        runner = InstanceRunner(storage, api=api)

        # Force verification required state directly to verify suppression
        runner.auth_state_manager.mark_verification_required(
            self.sample_account.account_id,
            challenge_type="captcha",
            challenge_id="ch-id-123",
            challenge_metadata="blob",
        )

        with self.assertRaises(RuntimeError) as ctx:
            runner.launch_instance(self.sample_account)

        self.assertIn("verification required", str(ctx.exception).lower())
        self.assertEqual(runner.auth_state_manager.get_state(self.sample_account.account_id), AccountAuthState.VERIFICATION_REQUIRED)

        # Second launch attempt must be rejected immediately
        with self.assertRaises(RuntimeError) as ctx2:
            runner.launch_instance(self.sample_account)

        self.assertIn("verification required", str(ctx2.exception).lower())

        # Stored cookie must NOT be deleted or modified
        saved_acc = storage.get_account(self.sample_account.account_id)
        self.assertIsNotNone(saved_acc)
        self.assertEqual(saved_acc.cookie, self.sample_account.cookie)

    def test_multi_launch_skips_challenge_accounts(self):
        storage = StorageManager(base_dir=self.base_dir)
        storage.save_account(self.sample_account)

        api = MagicMock()
        runner = InstanceRunner(storage, api=api)

        # Flag the account as verification required
        runner.auth_state_manager.mark_verification_required(self.sample_account.account_id)

        progress_calls = []
        def on_prog(idx, total, acc, warning):
            progress_calls.append((acc.username, warning))

        launched = runner.launch_multi([self.sample_account], on_progress=on_prog, stagger_delay=0.0)

        self.assertEqual(len(launched), 0)
        self.assertEqual(len(progress_calls), 1)
        self.assertIn("Skipped", progress_calls[0][1])

    def test_sign_out_of_all_sessions_no_challenge(self):
        """Verify standard logout from all sessions returns the freshly rotated session cookie."""
        api = RobloxAPI()
        mock_resp = MagicMock()
        mock_headers = MagicMock()
        mock_headers.get_all.return_value = [".ROBLOSECURITY=fresh_rotated_cookie_123; domain=.roblox.com; path=/; HttpOnly"]
        mock_resp.info.return_value = mock_headers
        mock_resp.headers = mock_headers
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(api, "get_csrf_token", return_value="csrf_test_token"):
            ok, new_cookie, msg = api.sign_out_of_all_sessions("initial_cookie_val")
            self.assertTrue(ok)
            self.assertEqual(new_cookie, "fresh_rotated_cookie_123")
            self.assertIn("Successfully signed out", msg)

    def test_sign_out_of_all_sessions_with_2fa_challenge(self):
        """Verify 2-step verification challenge interception, TOTP verification, and session rotation."""
        import base64
        api = RobloxAPI()

        meta_dict = {
            "userId": "45678",
            "challengeId": "sub_ch_123",
            "actionType": "Generic",
        }
        raw_meta = base64.b64encode(json.dumps(meta_dict).encode("utf-8")).decode("utf-8")

        # 1. 403 Challenge Error
        challenge_headers = {
            "rblx-challenge-type": "twostepverification",
            "rblx-challenge-id": "generic_ch_999",
            "rblx-challenge-metadata": raw_meta,
            "x-csrf-token": "csrf_sample_val",
        }
        http_403 = urllib.error.HTTPError(
            url="https://auth.roblox.com/v1/logoutfromallsessionsandreauthenticate",
            code=403,
            msg="Forbidden",
            hdrs=challenge_headers,
            fp=io.BytesIO(b'{"errors":[{"code":0,"message":"Challenge is required"}]}'),
        )

        # 2. 200 OK after 2FA retry
        mock_retry_resp = MagicMock()
        mock_retry_resp.headers = {
            "set-cookie": ".ROBLOSECURITY=rotated_2fa_cookie_999; domain=.roblox.com; path=/; HttpOnly"
        }
        mock_retry_resp.__enter__.return_value = mock_retry_resp

        # Mock the helper methods
        with patch("urllib.request.urlopen", side_effect=http_403), \
             patch.object(api, "get_csrf_token", return_value="csrf_sample_val"), \
             patch.object(api, "_verify_two_step_code", return_value=(True, "valid_2fa_token", "")) as mock_verify, \
             patch.object(api, "_continue_challenge", return_value=True) as mock_continue, \
             patch.object(api, "_retry_logout_with_challenge", return_value=(True, "rotated_2fa_cookie_999", "Successfully signed out of all other sessions!")) as mock_retry:

            callback_called = []
            def my_2fa_callback(info):
                callback_called.append(info)
                return "654321"

            ok, new_cookie, msg = api.sign_out_of_all_sessions("old_cookie", two_fa_callback=my_2fa_callback)

            self.assertTrue(ok)
            self.assertEqual(new_cookie, "rotated_2fa_cookie_999")
            self.assertEqual(len(callback_called), 1)
            self.assertEqual(callback_called[0]["userId"], "45678")
            self.assertEqual(callback_called[0]["challengeId"], "sub_ch_123")
            mock_verify.assert_called_once()
            mock_continue.assert_called_once_with(
                generic_id="generic_ch_999",
                token="valid_2fa_token",
                cookie="old_cookie",
                csrf_token="csrf_sample_val",
                sub_challenge_id="sub_ch_123",
                action_type="Generic",
            )
            mock_retry.assert_called_once()

    def test_sign_out_of_all_sessions_2fa_cancelled(self):
        """Verify clean exit when user cancels or skips the 2FA code prompt."""
        import base64
        api = RobloxAPI()

        meta_dict = {"userId": "45678", "challengeId": "sub_ch_123", "actionType": "Generic"}
        raw_meta = base64.b64encode(json.dumps(meta_dict).encode("utf-8")).decode("utf-8")

        challenge_headers = {
            "rblx-challenge-type": "twostepverification",
            "rblx-challenge-id": "generic_ch_999",
            "rblx-challenge-metadata": raw_meta,
        }
        http_403 = urllib.error.HTTPError(
            url="https://auth.roblox.com/v1/logoutfromallsessionsandreauthenticate",
            code=403,
            msg="Forbidden",
            hdrs=challenge_headers,
            fp=io.BytesIO(b'{"errors":[{"code":0,"message":"Challenge is required"}]}'),
        )

        with patch("urllib.request.urlopen", side_effect=http_403), \
             patch.object(api, "get_csrf_token", return_value="csrf_sample_val"):
            # User cancels (returns empty code)
            ok, new_cookie, msg = api.sign_out_of_all_sessions("old_cookie", two_fa_callback=lambda info: "")
            self.assertFalse(ok)
            self.assertIsNone(new_cookie)
            self.assertIn("cancelled", msg)

    def test_change_password_success_without_2fa(self):
        """Test standard password change with Set-Cookie rotation."""
        api = RobloxAPI()
        mock_resp = MagicMock()
        cookie_header = {
            "set-cookie": ".ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS.--fresh-cookie-val|_rotated; Path=/; Domain=.roblox.com; HttpOnly"
        }
        mock_resp.info.return_value = cookie_header
        mock_resp.headers = cookie_header
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(api, "get_csrf_token", return_value="csrf_test_token"):
            ok, new_cookie, msg = api.change_password(
                cookie="old_cookie",
                current_password="OldPassword123!",
                new_password="NewPassword456!",
            )
            self.assertTrue(ok)
            self.assertEqual(new_cookie, "_|WARNING:-DO-NOT-SHARE-THIS.--fresh-cookie-val|_rotated")
            self.assertIn("successfully", msg)

    def test_change_password_with_2fa_challenge(self):
        """Test password change triggering a Two-Step Verification challenge and auto-retrying."""
        import base64
        api = RobloxAPI()

        meta_dict = {"userId": "123456", "challengeId": "sub_ch_777", "actionType": "PasswordChange"}
        raw_meta = base64.b64encode(json.dumps(meta_dict).encode("utf-8")).decode("utf-8")

        challenge_headers = {
            "rblx-challenge-type": "twostepverification",
            "rblx-challenge-id": "generic_pwd_ch",
            "rblx-challenge-metadata": raw_meta,
        }
        http_403 = urllib.error.HTTPError(
            url="https://auth.roblox.com/v2/user/passwords/change",
            code=403,
            msg="Forbidden",
            hdrs=challenge_headers,
            fp=io.BytesIO(b'{"errors":[{"code":0,"message":"Challenge is required"}]}'),
        )

        with patch("urllib.request.urlopen", side_effect=http_403), \
             patch.object(api, "get_csrf_token", return_value="csrf_test_token"), \
             patch.object(api, "_verify_two_step_code", return_value=(True, "verification_token_abc", "")), \
             patch.object(api, "_continue_challenge", return_value=True), \
             patch.object(api, "_retry_password_change_with_challenge", return_value=(True, "cookie_post_2fa", "Password changed successfully!")) as mock_retry:

            ok, new_cookie, msg = api.change_password(
                cookie="old_cookie",
                current_password="OldPass!",
                new_password="NewPass!",
                two_fa_callback=lambda info: "123456",
            )
            self.assertTrue(ok)
            self.assertEqual(new_cookie, "cookie_post_2fa")
            mock_retry.assert_called_once()

    def test_generate_strong_password_utility(self):
        """Test password generator utility meets complexity rules."""
        from ui.dialogs import generate_strong_password
        pwd = generate_strong_password(18)
        self.assertEqual(len(pwd), 18)
        self.assertTrue(any(c.islower() for c in pwd))
        self.assertTrue(any(c.isupper() for c in pwd))
        self.assertTrue(any(c.isdigit() for c in pwd))
        self.assertTrue(any(c in "!@#$%^&*()-_=+" for c in pwd))

    def test_logout_session_api(self):
        """Test revoking session via auth.roblox.com/v2/logout."""
        api = RobloxAPI()
        with patch.object(api, "get_csrf_token", return_value="test_csrf_token"), \
             patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_url.return_value.__enter__.return_value = mock_resp

            ok, err = api.logout_session("fake_cookie_123")
            self.assertTrue(ok)
            self.assertIn("logged out", err.lower())

    def test_friends_manager_api(self):
        """Test friend requests decline/accept and unfriend API calls."""
        api = RobloxAPI()
        with patch.object(api, "get_csrf_token", return_value="test_csrf_token"), \
             patch("urllib.request.urlopen") as mock_url:
            # Decline all requests
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"success": true}'
            mock_url.return_value.__enter__.return_value = mock_resp

            ok, err = api.decline_all_friend_requests("fake_cookie")
            self.assertTrue(ok)

            # Accept single request
            ok, csrf = api.accept_friend_request(target_user_id=12345, cookie="fake_cookie", csrf_token="test_csrf")
            self.assertTrue(ok)

            # Unfriend single user
            ok, csrf = api.unfriend_user(target_user_id=67890, cookie="fake_cookie", csrf_token="test_csrf")
            self.assertTrue(ok)

    def test_join_privacy_api(self):
        """Test the set_join_privacy endpoint."""
        api = RobloxAPI()
        with patch.object(api, "get_csrf_token", return_value="test_csrf_token"), \
             patch("urllib.request.urlopen") as mock_url:
            mock_resp_set = MagicMock()
            mock_resp_set.status = 200
            mock_resp_set.read.return_value = b'{"gameJoinPrivacy": "NoOne"}'
            mock_url.return_value.__enter__.return_value = mock_resp_set

            ok, err = api.set_join_privacy("NoOne", "fake_cookie")
            self.assertTrue(ok)

    def test_storage_currency_and_privacy_updates(self):
        """Test SQLite persistence for pending_robux and join_privacy."""
        import tempfile
        from pathlib import Path
        from core.storage import StorageManager
        from core.models import RobloxAccount

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir) / "test_storage"
            storage = StorageManager(base_dir=base_path)

            acc = RobloxAccount(
                account_id="acc_curr_test",
                username="EconomyTester",
                display_name="Tester",
                user_id=88888,
                cookie="_|TEST_COOKIE|_",
                robux_balance=500,
                pending_robux=50,
                join_privacy="Friends",
            )
            storage.save_account(acc)

            # Reload and verify
            loaded = storage.get_account("acc_curr_test")
            self.assertEqual(loaded.robux_balance, 500)
            self.assertEqual(loaded.pending_robux, 50)
            self.assertEqual(loaded.join_privacy, "Friends")

    def test_login_with_credentials_direct_success(self):
        """Test login_with_credentials direct success when cookie is received."""
        api = RobloxAPI()

        class MockResponse:
            def __init__(self, cookie_val):
                self.headers = {"set-cookie": f".ROBLOSECURITY={cookie_val}; path=/"}
            def read(self):
                return b'{"user": {"id": 12345, "name": "DirectUser", "displayName": "DirectDisp"}}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        err_403 = urllib.error.HTTPError("https://auth.roblox.com/v2/login", 403, "Forbidden", {"x-csrf-token": "CSRF_TOKEN_ABC"}, None)

        with patch("urllib.request.urlopen", side_effect=[err_403, MockResponse("NEW_COOKIE_VALUE_123")]):
            with patch.object(api, "validate_cookie_and_get_user", return_value={"id": 12345, "name": "DirectUser", "displayName": "DirectDisp"}):
                ok, cookie, user_data, msg = api.login_with_credentials("DirectUser", "Password123")
                self.assertTrue(ok)
                self.assertEqual(cookie, "NEW_COOKIE_VALUE_123")
                self.assertIsNotNone(user_data)
                self.assertEqual(user_data["name"], "DirectUser")

    def test_login_with_credentials_2fa_challenge(self):
        """Test login_with_credentials when 2FA challenge is issued and resolved via TOTP."""
        import base64
        api = RobloxAPI()

        meta = {"userId": "55555", "challengeId": "sub_chal_99", "actionType": "Login"}
        meta_b64 = base64.b64encode(json.dumps(meta).encode("utf-8")).decode("utf-8")

        err_csrf = urllib.error.HTTPError("https://auth.roblox.com/v2/login", 403, "Forbidden", {"x-csrf-token": "CSRF_TOKEN_2FA"}, None)
        err_2fa = urllib.error.HTTPError(
            "https://auth.roblox.com/v2/login",
            403,
            "Forbidden",
            {
                "rblx-challenge-type": "twostepverification",
                "rblx-challenge-id": "gen_chal_88",
                "rblx-challenge-metadata": meta_b64,
            },
            io.BytesIO(b'{"errors":[]}')
        )

        class MockFinalResponse:
            def __init__(self):
                self.headers = {"set-cookie": ".ROBLOSECURITY=COOKIE_AFTER_2FA_SUCCESS; path=/"}
            def read(self):
                return b'{"user": {"id": 55555, "name": "TwoFAUser"}}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch("urllib.request.urlopen", side_effect=[err_csrf, err_2fa, MockFinalResponse()]):
            with patch.object(api, "_verify_two_step_code", return_value=(True, "VERIFY_TOKEN_XYZ", "")):
                with patch.object(api, "_continue_challenge", return_value=True):
                    with patch.object(api, "validate_cookie_and_get_user", return_value={"id": 55555, "name": "TwoFAUser"}):
                        ok, cookie, user_data, msg = api.login_with_credentials("TwoFAUser", "Password123", totp_secret="JBSWY3DPEHPK3PXP")
                        self.assertTrue(ok)
                        self.assertEqual(cookie, "COOKIE_AFTER_2FA_SUCCESS")

    def test_login_with_credentials_captcha_required(self):
        """Test login_with_credentials returns CAPTCHA_REQUIRED when Arkose challenge is present."""
        api = RobloxAPI()

        err_csrf = urllib.error.HTTPError("https://auth.roblox.com/v2/login", 403, "Forbidden", {"x-csrf-token": "CSRF_TOKEN_CAPTCHA"}, None)
        err_captcha = urllib.error.HTTPError(
            "https://auth.roblox.com/v2/login",
            403,
            "Forbidden",
            {"rblx-challenge-type": "captcha", "rblx-challenge-id": "arkose_123"},
            io.BytesIO(b'{"errors":[]}')
        )

        with patch("urllib.request.urlopen", side_effect=[err_csrf, err_captcha]):
            ok, cookie, user_data, msg = api.login_with_credentials("CaptchaUser", "Password123")
            self.assertFalse(ok)
            self.assertIsNone(cookie)
            self.assertEqual(msg, "CAPTCHA_REQUIRED")

    def test_login_with_credentials_generic_challenge_string(self):
        """Test login_with_credentials maps 'Challenge is required to authorize the request' to CAPTCHA_REQUIRED."""
        api = RobloxAPI()

        err_csrf = urllib.error.HTTPError("https://auth.roblox.com/v2/login", 403, "Forbidden", {"x-csrf-token": "CSRF_TOKEN_CHALLENGE"}, None)
        err_challenge = urllib.error.HTTPError(
            "https://auth.roblox.com/v2/login",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"errors":[{"code":0,"message":"Challenge is required to authorize the request"}]}')
        )

        with patch("urllib.request.urlopen", side_effect=[err_csrf, err_challenge]):
            ok, cookie, user_data, msg = api.login_with_credentials("ChallengeUser", "Password123")
            self.assertFalse(ok)
            self.assertIsNone(cookie)
            self.assertEqual(msg, "CAPTCHA_REQUIRED")

    def test_login_with_credentials_rate_limited(self):
        """Test login_with_credentials correctly detects HTTP 429 and parses Retry-After."""
        api = RobloxAPI()

        err_429 = urllib.error.HTTPError(
            "https://auth.roblox.com/v2/login",
            429,
            "Too Many Requests",
            {"Retry-After": "25"},
            io.BytesIO(b'{"errors":[{"code":429,"message":"Too many requests"}]}')
        )

        with patch("urllib.request.urlopen", side_effect=err_429):
            ok, cookie, user_data, msg = api.login_with_credentials("RateLimitUser", "Password123")
            self.assertFalse(ok)
            self.assertIsNone(cookie)
            self.assertEqual(msg, "RATE_LIMITED:25")

    def test_batch_auto_login_worker_sequential_flow(self):
        """Test BatchAutoLoginWorker handles sequential account processing and Arkose protection."""
        from ui.dialogs import BatchAutoLoginWorker

        api = RobloxAPI()
        storage = MagicMock()

        acc1 = RobloxAccount(account_id="a1", username="UserOne", display_name="UserOne", user_id=1001, cookie="", password="Pwd1", totp_secret="JBSWY3DPEHPK3PXP")
        acc2 = RobloxAccount(account_id="a2", username="UserTwo", display_name="UserTwo", user_id=1002, cookie="", password="Pwd2")
        acc3 = RobloxAccount(account_id="a3", username="UserThree", display_name="UserThree", user_id=1003, cookie="", password="")  # Missing password

        started_events = []
        status_updates = []
        finished_events = []
        batch_done = []

        worker = BatchAutoLoginWorker(
            accounts=[acc1, acc2, acc3],
            api=api,
            storage=storage,
            pacing_delay=0.1,  # Short delay for fast test
            use_jitter=False,
            auto_browser_captcha=False,
        )
        worker.account_started.connect(lambda idx, tot, acc: started_events.append((idx, acc.username)))
        worker.account_status_update.connect(lambda acc, st, col: status_updates.append((acc.username, st)))
        worker.account_finished.connect(lambda acc, ok, msg: finished_events.append((acc.username, ok, msg)))
        worker.batch_complete.connect(lambda s, f, c: batch_done.append((s, f, c)))

        # Mock api.login_with_credentials:
        # acc1 succeeds directly
        # acc2 triggers captcha
        def mock_login(username, pwd, totp):
            if username == "UserOne":
                return True, "COOKIE_ACC1", {"id": 1001, "displayName": "User 1"}, "OK"
            elif username == "UserTwo":
                return False, None, None, "CAPTCHA_REQUIRED"
            return False, None, None, "Fail"

        with patch.object(api, "login_with_credentials", side_effect=mock_login):
            worker.run()

        self.assertEqual(len(batch_done), 1)
        success_cnt, fail_cnt, web_cnt = batch_done[0]
        self.assertEqual(success_cnt, 1)
        self.assertEqual(fail_cnt, 2)  # UserTwo (verification required without browser) + UserThree (missing pwd)
        self.assertEqual(web_cnt, 0)

        self.assertEqual(len(finished_events), 3)
        self.assertEqual(finished_events[0], ("UserOne", True, "Success"))
        self.assertEqual(finished_events[1], ("UserTwo", False, "Verification required (Click 'Verify Selected in Browser')"))
        self.assertEqual(finished_events[2], ("UserThree", False, "Missing password in vault"))


if __name__ == "__main__":
    unittest.main()



