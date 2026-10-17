"""
Client for official Roblox public REST APIs.
Uses standard library urllib (zero external dependencies).
Fetches authenticated user identity, Robux balances, avatar headshots, and game details.
"""

import json
import logging
import time
import random
import base64
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List, Callable


log = logging.getLogger("RobloxAPI")


@dataclass
class AuthTicketResult:
    """Detailed result of an auth ticket request including challenge diagnostics."""
    ticket: Optional[str] = None
    success: bool = False
    status_code: Optional[int] = None
    is_challenge: bool = False
    challenge_type: Optional[str] = None       # e.g. "captcha", "2stepverification"
    challenge_id: Optional[str] = None
    challenge_metadata: Optional[str] = None
    is_expired: bool = False
    is_rate_limited: bool = False
    error_message: str = ""


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"


class RobloxAPI:
    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout

    @staticmethod
    def _format_cookie_header(cookie: Optional[str]) -> Optional[str]:
        if not cookie:
            return None
        val = cookie.strip()
        if ".ROBLOSECURITY=" in val:
            parts = val.split(".ROBLOSECURITY=")
            val = parts[1].split(";")[0].strip()
        val = val.strip('"').strip("'")
        return f".ROBLOSECURITY={val}" if val else None

    def _get(self, url: str, cookie: Optional[str] = None) -> Optional[Any]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        fmt = self._format_cookie_header(cookie)
        if fmt:
            headers["Cookie"] = fmt

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read().decode("utf-8")
                return json.loads(data)
        except urllib.error.HTTPError as e:
            log.debug(f"HTTP error {e.code} for {url}: {e.reason}")
            return None
        except Exception as e:
            log.debug(f"Network error for {url}: {e}")
            return None

    def _post(self, url: str, payload: Dict[str, Any], cookie: Optional[str] = None) -> Optional[Dict[str, Any]]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        fmt = self._format_cookie_header(cookie)
        if fmt:
            headers["Cookie"] = fmt

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data_str = resp.read().decode("utf-8")
                return json.loads(data_str)
        except urllib.error.HTTPError as e:
            log.debug(f"HTTP error {e.code} for {url}: {e.reason}")
            return None
        except Exception as e:
            log.debug(f"Network error for {url}: {e}")
            return None

    def validate_cookie(self, cookie: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        Validates cookie against Roblox auth endpoint.
        Returns (is_valid, user_data, status_reason).
        status_reason is one of: 'OK', 'EXPIRED', 'RATE_LIMITED', 'HTTP_<code>', 'NETWORK_ERROR'
        """
        if not cookie:
            return False, None, "EMPTY_COOKIE"
        url = "https://users.roblox.com/v1/users/authenticated"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cookie": f".ROBLOSECURITY={cookie}",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data and "id" in data:
                    return True, data, "OK"
                return False, None, "INVALID_RESPONSE"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, None, "EXPIRED"
            if e.code == 429:
                return False, None, "RATE_LIMITED"
            return False, None, f"HTTP_{e.code}"
        except Exception as e:
            return False, None, f"NETWORK_ERROR_{type(e).__name__}"

    def validate_cookie_and_get_user(self, cookie: str) -> Optional[Dict[str, Any]]:
        """
        Validates the .ROBLOSECURITY cookie against Roblox auth endpoint.
        Returns dict with keys 'id', 'name', 'displayName' if valid, None if invalid.
        """
        is_valid, user_data, _ = self.validate_cookie(cookie)
        return user_data if is_valid else None

    def get_authenticated_user(self, cookie: str) -> Optional[Dict[str, Any]]:
        """Validates the .ROBLOSECURITY cookie and returns authenticated user metadata."""
        return self.validate_cookie_and_get_user(cookie)

    def get_avatar_headshot(self, user_id: int) -> Optional[str]:
        """Fetches the 150x150 avatar headshot thumbnail URL."""
        if not user_id:
            return None
        url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=false"
        data = self._get(url)
        if data and "data" in data and len(data["data"]) > 0:
            item = data["data"][0]
            if item.get("state") == "Completed":
                return item.get("imageUrl")
        return None

    def get_robux_balance(self, user_id: int, cookie: str) -> int:
        """Fetches current Robux currency balance for the authenticated user."""
        if not user_id or not cookie:
            return 0
        url = f"https://economy.roblox.com/v1/users/{user_id}/currency"
        data = self._get(url, cookie=cookie)
        if data and "robux" in data:
            return int(data["robux"])
        return 0

    def enrich_account(self, cookie: str) -> Optional[Dict[str, Any]]:
        """
        Performs a full enrichment pass for an account using its session cookie:
        Identity + Headshot Thumbnail + Robux Balance.
        """
        user_info = self.validate_cookie_and_get_user(cookie)
        if not user_info:
            return None

        user_id = user_info["id"]
        username = user_info["name"]
        display_name = user_info.get("displayName") or username

        avatar_url = self.get_avatar_headshot(user_id) or ""
        robux = self.get_robux_balance(user_id, cookie)

        return {
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "robux_balance": robux,
        }

    def get_game_details(self, place_id: int, cookie: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetches public experience details and universe ID for a Place ID."""
        url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
        data = self._get(url, cookie=cookie)
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None

    def resolve_username_to_user_id(self, username: str) -> Optional[int]:
        """Resolves a Roblox username to its 64-bit numerical user ID."""
        if not username:
            return None
        url = "https://users.roblox.com/v1/usernames/users"
        res = self._post(url, {"usernames": [username], "excludeBannedUsers": False})
        if res and "data" in res and len(res["data"]) > 0:
            return res["data"][0].get("id")
        return None

    def get_user_presence(self, user_id: int, cookie: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves user presence state (InGame, Online, Studio) including active Place ID and Game ID.
        Passing an authenticated cookie reveals presence for friends who restrict presence visibility.
        """
        if not user_id:
            return None
        url = "https://presence.roblox.com/v1/presence/users"
        res = self._post(url, {"userIds": [user_id]}, cookie=cookie)
        if res and "userPresences" in res and len(res["userPresences"]) > 0:
            return res["userPresences"][0]
        return None

    def get_users_presences(self, user_ids: List[int], cookie: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves presence states for multiple users in a single batch request (up to 100).
        Reveals active placeId and gameId (job ID) for users in game with public joins or friends.
        """
        if not user_ids:
            return []
        url = "https://presence.roblox.com/v1/presence/users"
        # Roblox allows up to 100 user IDs per batch request
        results = []
        for i in range(0, len(user_ids), 100):
            chunk = user_ids[i:i+100]
            res = self._post(url, {"userIds": chunk}, cookie=cookie)
            if res and "userPresences" in res:
                results.extend(res["userPresences"])
        return results

    def check_user_joinable(self, user_id: int, cookie: Optional[str] = None) -> Dict[str, Any]:
        """
        Checks whether a user is currently in-game and has joins enabled.
        Returns dict with:
        - in_game: bool (userPresenceType == 2)
        - joinable: bool (gameId is present and non-empty)
        - place_id: Optional[int]
        - root_place_id: Optional[int]
        - game_id: Optional[str]
        - last_location: str
        """
        pres = self.get_user_presence(user_id, cookie=cookie)
        if not pres:
            return {"in_game": False, "joinable": False, "place_id": None, "root_place_id": None, "game_id": None, "last_location": ""}

        pres_type = pres.get("userPresenceType", 0)
        in_game = (pres_type == 2)
        game_id = pres.get("gameId")
        place_id = pres.get("placeId")
        root_place_id = pres.get("rootPlaceId")
        last_loc = pres.get("lastLocation", "")

        return {
            "in_game": in_game,
            "joinable": bool(in_game and game_id),
            "place_id": place_id,
            "root_place_id": root_place_id,
            "game_id": game_id,
            "last_location": last_loc,
            "presence_type": pres_type,
        }

    def get_player_token_thumbnails(
        self,
        player_tokens: List[str],
        cookie: Optional[str] = None,
        size: str = "150x150",
    ) -> List[Dict[str, Any]]:
        """
        Fetches AvatarHeadShot thumbnails for playerTokens returned by public server queries.
        Returns list of dicts with targetId, state, and imageUrl.
        """
        if not player_tokens:
            return []

        url = "https://thumbnails.roblox.com/v1/batch"
        results = []
        for i in range(0, len(player_tokens), 100):
            chunk = player_tokens[i:i+100]
            payload = [
                {
                    "requestId": f"tok_{idx}",
                    "token": tok,
                    "type": "AvatarHeadShot",
                    "size": size,
                    "format": "Png",
                    "isCircular": False,
                }
                for idx, tok in enumerate(chunk)
            ]
            res = self._post(url, payload, cookie=cookie)
            if res and isinstance(res, dict) and "data" in res:
                results.extend(res["data"])
        return results



    def request_auth_ticket(self, cookie: str) -> AuthTicketResult:
        """
        Acquires a single-use authentication ticket from auth.roblox.com
        for native Windows client launching.
        Returns AuthTicketResult containing ticket or detailed challenge/expiry diagnostics.
        """
        if not cookie:
            return AuthTicketResult(success=False, error_message="No session cookie provided")

        url = "https://auth.roblox.com/v1/authentication-ticket"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com/",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
        }

        csrf_token = None
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ticket = resp.headers.get("rbx-authentication-ticket")
                if not ticket:
                    try:
                        data = json.loads(resp.read().decode("utf-8"))
                        ticket = data.get("authenticationTicket")
                    except Exception:
                        pass
                if ticket:
                    return AuthTicketResult(ticket=ticket, success=True, status_code=200)
        except urllib.error.HTTPError as e:
            if e.code == 403 and "x-csrf-token" in e.headers:
                csrf_token = e.headers["x-csrf-token"]
            elif e.code == 401:
                return AuthTicketResult(
                    success=False,
                    status_code=401,
                    is_expired=True,
                    error_message="Roblox session cookie expired or invalid (HTTP 401)",
                )
            elif e.code == 429:
                return AuthTicketResult(
                    success=False,
                    status_code=429,
                    is_rate_limited=True,
                    error_message="Rate limited by Roblox auth endpoint (HTTP 429)",
                )
            else:
                ch_type = e.headers.get("rblx-challenge-type")
                ch_id = e.headers.get("rblx-challenge-id")
                ch_meta = e.headers.get("rblx-challenge-metadata")
                body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
                if ch_type or any(k in body.lower() for k in ("challenge", "captcha", "verification")):
                    return AuthTicketResult(
                        success=False,
                        status_code=e.code,
                        is_challenge=True,
                        challenge_type=ch_type or "captcha",
                        challenge_id=ch_id,
                        challenge_metadata=ch_meta,
                        error_message="Roblox security challenge triggered (Arkose CAPTCHA)",
                    )
                return AuthTicketResult(
                    success=False,
                    status_code=e.code,
                    error_message=f"Auth ticket request failed: HTTP {e.code}",
                )
        except Exception as e:
            return AuthTicketResult(
                success=False,
                error_message=f"Network error requesting auth ticket: {e}",
            )

        if csrf_token:
            headers["x-csrf-token"] = csrf_token
            req2 = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req2, timeout=self.timeout) as resp:
                    ticket = resp.headers.get("rbx-authentication-ticket")
                    if not ticket:
                        try:
                            data = json.loads(resp.read().decode("utf-8"))
                            ticket = data.get("authenticationTicket")
                        except Exception:
                            pass
                    if ticket:
                        return AuthTicketResult(ticket=ticket, success=True, status_code=200)
            except urllib.error.HTTPError as e:
                ch_type = e.headers.get("rblx-challenge-type")
                ch_id = e.headers.get("rblx-challenge-id")
                ch_meta = e.headers.get("rblx-challenge-metadata")
                body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""

                if e.code == 401:
                    return AuthTicketResult(
                        success=False,
                        status_code=401,
                        is_expired=True,
                        error_message="Roblox session cookie expired or invalid (HTTP 401)",
                    )
                if e.code == 429:
                    return AuthTicketResult(
                        success=False,
                        status_code=429,
                        is_rate_limited=True,
                        error_message="Rate limited by Roblox auth endpoint (HTTP 429)",
                    )
                if ch_type or any(k in body.lower() for k in ("challenge", "captcha", "verification")):
                    return AuthTicketResult(
                        success=False,
                        status_code=e.code,
                        is_challenge=True,
                        challenge_type=ch_type or "captcha",
                        challenge_id=ch_id,
                        challenge_metadata=ch_meta,
                        error_message=f"Roblox verification required ({ch_type or 'Arkose CAPTCHA'})",
                    )
                return AuthTicketResult(
                    success=False,
                    status_code=e.code,
                    error_message=f"Auth ticket CSRF retry failed: HTTP {e.code}",
                )
            except Exception as e:
                return AuthTicketResult(
                    success=False,
                    error_message=f"Auth ticket CSRF retry failed: {e}",
                )

        return AuthTicketResult(
            success=False,
            error_message="No auth ticket received from server",
        )

    def get_csrf_token(self, cookie: str) -> Optional[str]:
        """
        Fetches an x-csrf-token from Roblox by sending a dummy POST request.
        """
        if not cookie:
            return None
        url = "https://auth.roblox.com/v1/authentication-ticket"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com/",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.headers.get("x-csrf-token")
        except urllib.error.HTTPError as e:
            if "x-csrf-token" in e.headers:
                return e.headers["x-csrf-token"]
        except Exception as e:
            log.debug(f"Failed to fetch CSRF token: {e}")
        return None

    def sign_out_of_all_sessions(
        self,
        cookie: str,
        two_fa_callback: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """
        Signs out of all other Roblox sessions and re-authenticates the current session.
        If the account has Two-Step Verification (authenticator) enabled, invokes
        `two_fa_callback` with challenge metadata so the user can enter their 6-digit TOTP code.

        Returns:
            Tuple of (success: bool, new_cookie: Optional[str], status_message: str)
        """
        if not cookie:
            return False, None, "No session cookie provided"

        url = "https://auth.roblox.com/v1/logoutfromallsessionsandreauthenticate"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.roblox.com/my/account",
            "Origin": "https://www.roblox.com",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
        }

        csrf_token = self.get_csrf_token(cookie)
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                new_cookie = self._extract_roblosecurity_cookie(resp)
                if new_cookie:
                    return True, new_cookie, "Successfully signed out of all other sessions!"
                return True, None, "Signed out of all other sessions (session maintained)"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset_sec = self._extract_retry_after(e.headers, default=15)
                time.sleep(reset_sec)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp_retry:
                        new_cookie = self._extract_roblosecurity_cookie(resp_retry)
                        if new_cookie:
                            return True, new_cookie, "Successfully signed out of all other sessions!"
                        return True, None, "Signed out of all other sessions (session maintained)"
                except urllib.error.HTTPError as e_retry:
                    e = e_retry
                except Exception as ex:
                    return False, None, str(ex)

            if e.code == 403:
                # 1. Refresh CSRF token if needed
                if "x-csrf-token" in e.headers and "rblx-challenge-type" not in e.headers:
                    fresh_csrf = e.headers["x-csrf-token"]
                    headers["X-CSRF-TOKEN"] = fresh_csrf
                    req_retry = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                    try:
                        with urllib.request.urlopen(req_retry, timeout=self.timeout) as resp2:
                            new_cookie = self._extract_roblosecurity_cookie(resp2)
                            return True, new_cookie, "Successfully signed out of all other sessions!"
                    except urllib.error.HTTPError as e_retry:
                        e = e_retry
                    except Exception as ex:
                        return False, None, str(ex)

                # 2. Check for Two-Step Verification challenge
                challenge_type = e.headers.get("rblx-challenge-type", "").lower()
                if challenge_type == "twostepverification":
                    if not two_fa_callback:
                        return False, None, "Two-step verification is required for this account."

                    generic_id = e.headers.get("rblx-challenge-id", "")
                    raw_meta = e.headers.get("rblx-challenge-metadata", "")
                    meta: Dict[str, Any] = {}
                    try:
                        meta = json.loads(base64.b64decode(raw_meta).decode("utf-8"))
                    except Exception:
                        try:
                            meta = json.loads(raw_meta)
                        except Exception:
                            meta = {}

                    user_id = str(meta.get("userId", ""))
                    sub_challenge_id = str(meta.get("challengeId", ""))
                    action_type = str(meta.get("actionType", "Generic"))
                    csrf = headers.get("X-CSRF-TOKEN") or self.get_csrf_token(cookie) or ""

                    err_msg = ""
                    for attempt in range(3):
                        prompt_info = {
                            "userId": user_id,
                            "challengeId": sub_challenge_id,
                            "genericChallengeId": generic_id,
                            "actionType": action_type,
                            "error": err_msg,
                            "attempt": attempt + 1,
                        }
                        code = two_fa_callback(prompt_info)
                        if not code:
                            return False, None, "Two-step verification skipped or cancelled by user."
                        code = str(code).strip()

                        # Verify code against 2SV endpoint
                        ok, token, verify_err = self._verify_two_step_code(
                            user_id=user_id,
                            challenge_id=sub_challenge_id,
                            action_type=action_type,
                            code=code,
                            cookie=cookie,
                            csrf_token=csrf,
                        )
                        if ok and token:
                            # Submit challenge continuation
                            self._continue_challenge(
                                generic_id=generic_id,
                                token=token,
                                cookie=cookie,
                                csrf_token=csrf,
                                sub_challenge_id=sub_challenge_id,
                                action_type=action_type,
                            )

                            # Re-send logout with challenge headers
                            ok_l, new_cookie, l_msg = self._retry_logout_with_challenge(
                                cookie=cookie,
                                csrf_token=csrf,
                                generic_id=generic_id,
                                meta=meta,
                                token=token,
                            )
                            if ok_l:
                                return True, new_cookie, "Successfully signed out of all other sessions!"
                            return False, None, l_msg
                        else:
                            err_msg = verify_err or "Invalid verification code. Please try again."

                    return False, None, f"2FA verification failed: {err_msg}"

                if e.code == 401:
                    return False, None, "Session cookie expired or invalid (HTTP 401)"
                if e.code == 429:
                    return False, None, "Rate limited by Roblox auth endpoint (HTTP 429)"
                return False, None, f"HTTP {e.code}: {e.reason}"
            elif e.code == 401:
                return False, None, "Session cookie expired or invalid (HTTP 401)"
            elif e.code == 429:
                return False, None, "Rate limited by Roblox auth endpoint (HTTP 429)"
            return False, None, f"HTTP {e.code}: {e.reason}"
        except Exception as ex:
            return False, None, str(ex)

    def change_password(
        self,
        cookie: str,
        current_password: str,
        new_password: str,
        two_fa_callback: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """
        Changes account password via https://auth.roblox.com/v2/user/passwords/change.
        Handles CSRF verification, Two-Step Verification (2SV / TOTP) challenges, and rotates cookies.

        Returns:
            Tuple of (success: bool, new_cookie: Optional[str], status_message: str)
        """
        if not cookie:
            return False, None, "No session cookie provided"
        if not current_password:
            return False, None, "Current password is required"
        if not new_password:
            return False, None, "New password cannot be empty"

        url = "https://auth.roblox.com/v2/user/passwords/change"
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.roblox.com/my/account",
            "Origin": "https://www.roblox.com",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        csrf_token = self.get_csrf_token(cookie)
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        payload = json.dumps({"currentPassword": current_password, "newPassword": new_password}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                new_cookie = self._extract_roblosecurity_cookie(resp)
                return True, new_cookie, "Password changed successfully!"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset_sec = self._extract_retry_after(e.headers, default=15)
                time.sleep(reset_sec)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp_retry:
                        new_cookie = self._extract_roblosecurity_cookie(resp_retry)
                        return True, new_cookie, "Password changed successfully!"
                except urllib.error.HTTPError as e_retry:
                    e = e_retry
                except Exception as ex:
                    return False, None, str(ex)

            if e.code == 403:
                # 1. Refresh CSRF token if needed
                if "x-csrf-token" in e.headers and "rblx-challenge-type" not in e.headers:
                    fresh_csrf = e.headers["x-csrf-token"]
                    headers["X-CSRF-TOKEN"] = fresh_csrf
                    req_retry = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                    try:
                        with urllib.request.urlopen(req_retry, timeout=self.timeout) as resp2:
                            new_cookie = self._extract_roblosecurity_cookie(resp2)
                            return True, new_cookie, "Password changed successfully!"
                    except urllib.error.HTTPError as e_retry:
                        e = e_retry
                    except Exception as ex:
                        return False, None, str(ex)

                # 2. Check for Two-Step Verification challenge
                challenge_type = e.headers.get("rblx-challenge-type", "").lower()
                if challenge_type == "twostepverification":
                    if not two_fa_callback:
                        return False, None, "Two-step verification is required to change password."

                    generic_id = e.headers.get("rblx-challenge-id", "")
                    raw_meta = e.headers.get("rblx-challenge-metadata", "")
                    meta: Dict[str, Any] = {}
                    try:
                        meta = json.loads(base64.b64decode(raw_meta).decode("utf-8"))
                    except Exception:
                        try:
                            meta = json.loads(raw_meta)
                        except Exception:
                            meta = {}

                    user_id = str(meta.get("userId", ""))
                    sub_challenge_id = str(meta.get("challengeId", ""))
                    action_type = str(meta.get("actionType", "Generic"))
                    csrf = headers.get("X-CSRF-TOKEN") or self.get_csrf_token(cookie) or ""

                    err_msg = ""
                    for attempt in range(3):
                        prompt_info = {
                            "userId": user_id,
                            "challengeId": sub_challenge_id,
                            "genericChallengeId": generic_id,
                            "actionType": action_type,
                            "error": err_msg,
                            "attempt": attempt + 1,
                        }
                        code = two_fa_callback(prompt_info)
                        if not code:
                            return False, None, "Two-step verification cancelled or skipped."
                        code = str(code).strip()

                        # Verify code against 2SV endpoint
                        ok, token, verify_err = self._verify_two_step_code(
                            user_id=user_id,
                            challenge_id=sub_challenge_id,
                            action_type=action_type,
                            code=code,
                            cookie=cookie,
                            csrf_token=csrf,
                        )
                        if ok and token:
                            # Submit challenge continuation
                            self._continue_challenge(
                                generic_id=generic_id,
                                token=token,
                                cookie=cookie,
                                csrf_token=csrf,
                                sub_challenge_id=sub_challenge_id,
                                action_type=action_type,
                            )

                            # Re-send password change with challenge headers
                            ok_p, new_cookie, p_msg = self._retry_password_change_with_challenge(
                                cookie=cookie,
                                csrf_token=csrf,
                                generic_id=generic_id,
                                meta=meta,
                                token=token,
                                current_password=current_password,
                                new_password=new_password,
                            )
                            if ok_p:
                                return True, new_cookie, "Password changed successfully!"
                            return False, None, p_msg
                        else:
                            err_msg = verify_err or "Invalid verification code. Please try again."

                    return False, None, f"2FA verification failed: {err_msg}"

            # Try parsing error message from response body
            try:
                body_bytes = e.read()
                if body_bytes:
                    body_json = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    errors = body_json.get("errors", [])
                    if errors and "message" in errors[0]:
                        return False, None, errors[0]["message"]
            except Exception:
                pass

            if e.code == 401:
                return False, None, "Session cookie expired or invalid (HTTP 401)"
            if e.code == 429:
                return False, None, "Rate limited by Roblox auth endpoint (HTTP 429)"
            return False, None, f"HTTP {e.code}: {e.reason}"
        except Exception as ex:
            return False, None, str(ex)

    def _verify_two_step_code(
        self,
        user_id: str,
        challenge_id: str,
        action_type: str,
        code: str,
        cookie: str,
        csrf_token: str,
    ) -> Tuple[bool, Optional[str], str]:
        """
        Submits a 2FA TOTP or backup code to twostepverification.roblox.com.
        Returns: (success, verificationToken, error_message)
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf_token,
        }

        clean_code = code.replace(" ", "").replace("-", "")
        is_backup = len(clean_code) >= 8 or not clean_code.isdigit()

        endpoints_to_try = []
        if is_backup:
            endpoints_to_try.append((
                f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/recovery-codes/verify",
                json.dumps({"challengeId": challenge_id, "code": clean_code}).encode("utf-8")
            ))
            endpoints_to_try.append((
                f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/authenticator/verify",
                json.dumps({"actionType": action_type, "challengeId": challenge_id, "code": clean_code}).encode("utf-8")
            ))
        else:
            endpoints_to_try.append((
                f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/authenticator/verify",
                json.dumps({"actionType": action_type, "challengeId": challenge_id, "code": clean_code}).encode("utf-8")
            ))
            endpoints_to_try.append((
                f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/email/verify",
                json.dumps({"actionType": action_type, "challengeId": challenge_id, "code": clean_code}).encode("utf-8")
            ))
            endpoints_to_try.append((
                f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/recovery-codes/verify",
                json.dumps({"challengeId": challenge_id, "code": clean_code}).encode("utf-8")
            ))

        last_err = "Invalid verification code"
        for url, body in endpoints_to_try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    token = data.get("verificationToken")
                    if token:
                        return True, token, ""
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    reset_sec = self._extract_retry_after(e.headers, default=5)
                    time.sleep(reset_sec)
                    try:
                        with urllib.request.urlopen(req, timeout=self.timeout) as resp_retry:
                            data = json.loads(resp_retry.read().decode("utf-8"))
                            token = data.get("verificationToken")
                            if token:
                                return True, token, ""
                    except Exception:
                        pass
                try:
                    err_data = json.loads(e.read().decode("utf-8"))
                    errors = err_data.get("errors", [])
                    if errors and "message" in errors[0]:
                        last_err = errors[0]["message"]
                except Exception:
                    last_err = f"HTTP {e.code}"
            except Exception as ex:
                last_err = str(ex)

        return False, None, last_err

    def _continue_challenge(
        self,
        generic_id: str,
        token: str,
        cookie: str,
        csrf_token: str,
        sub_challenge_id: str = "",
        action_type: str = "Generic",
    ) -> bool:
        """Notifies apis.roblox.com/challenge/v1/continue that the challenge is completed."""
        url = "https://apis.roblox.com/challenge/v1/continue"
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf_token,
        }
        c_meta: Dict[str, Any] = {
            "verificationToken": token,
            "rememberDevice": False,
        }
        if sub_challenge_id:
            c_meta["challengeId"] = sub_challenge_id
        if action_type:
            c_meta["actionType"] = action_type

        body = json.dumps({
            "challengeId": generic_id,
            "challengeType": "twostepverification",
            "challengeMetadata": json.dumps(c_meta),
        }).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status in (200, 204)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset_sec = self._extract_retry_after(e.headers, default=5)
                time.sleep(reset_sec)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp_retry:
                        return resp_retry.status in (200, 204)
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _retry_logout_with_challenge(
        self,
        cookie: str,
        csrf_token: str,
        generic_id: str,
        meta: Dict[str, Any],
        token: str,
    ) -> Tuple[bool, Optional[str], str]:
        """Re-sends the logout request with the verified challenge metadata."""
        url = "https://auth.roblox.com/v1/logoutfromallsessionsandreauthenticate"

        meta_payload = dict(meta)
        meta_payload["verificationToken"] = token
        meta_payload["rememberDevice"] = False
        raw_b64 = base64.b64encode(json.dumps(meta_payload).encode("utf-8")).decode("utf-8")

        headers = {
            "User-Agent": USER_AGENT,
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf_token,
            "rblx-challenge-id": generic_id,
            "rblx-challenge-type": "twostepverification",
            "rblx-challenge-metadata": raw_b64,
        }
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                new_cookie = self._extract_roblosecurity_cookie(resp)
                return True, new_cookie, "Successfully signed out of all other sessions!"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset_sec = self._extract_retry_after(e.headers, default=15)
                time.sleep(reset_sec)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp_retry:
                        new_cookie = self._extract_roblosecurity_cookie(resp_retry)
                        return True, new_cookie, "Successfully signed out of all other sessions!"
                except Exception as ex_retry:
                    return False, None, f"HTTP 429 (Rate limit): {ex_retry}"
            if e.code == 403 and "x-csrf-token" in e.headers:
                headers["X-CSRF-TOKEN"] = e.headers["x-csrf-token"]
                req2 = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req2, timeout=self.timeout) as resp2:
                        new_cookie = self._extract_roblosecurity_cookie(resp2)
                        return True, new_cookie, "Successfully signed out of all other sessions!"
                except Exception as ex2:
                    return False, None, str(ex2)
            return False, None, f"HTTP {e.code}: {e.reason}"
        except Exception as ex:
            return False, None, str(ex)

    def _retry_password_change_with_challenge(
        self,
        cookie: str,
        csrf_token: str,
        generic_id: str,
        meta: Dict[str, Any],
        token: str,
        current_password: str,
        new_password: str,
    ) -> Tuple[bool, Optional[str], str]:
        """Re-sends the password change request with the verified challenge metadata."""
        url = "https://auth.roblox.com/v2/user/passwords/change"
        meta_payload = dict(meta)
        meta_payload["verificationToken"] = token
        meta_payload["rememberDevice"] = False
        raw_b64 = base64.b64encode(json.dumps(meta_payload).encode("utf-8")).decode("utf-8")

        headers = {
            "User-Agent": USER_AGENT,
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRF-TOKEN": csrf_token,
            "rblx-challenge-id": generic_id,
            "rblx-challenge-type": "twostepverification",
            "rblx-challenge-metadata": raw_b64,
        }
        payload = json.dumps({"currentPassword": current_password, "newPassword": new_password}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                new_cookie = self._extract_roblosecurity_cookie(resp)
                return True, new_cookie, "Password changed successfully!"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset_sec = self._extract_retry_after(e.headers, default=15)
                time.sleep(reset_sec)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp_retry:
                        new_cookie = self._extract_roblosecurity_cookie(resp_retry)
                        return True, new_cookie, "Password changed successfully!"
                except Exception as ex_retry:
                    return False, None, f"HTTP 429 (Rate limit): {ex_retry}"
            if e.code == 403 and "x-csrf-token" in e.headers:
                headers["X-CSRF-TOKEN"] = e.headers["x-csrf-token"]
                req2 = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req2, timeout=self.timeout) as resp2:
                        new_cookie = self._extract_roblosecurity_cookie(resp2)
                        return True, new_cookie, "Password changed successfully!"
                except Exception as ex2:
                    return False, None, str(ex2)
            try:
                err_data = json.loads(e.read().decode("utf-8", errors="replace"))
                errors = err_data.get("errors", [])
                if errors and "message" in errors[0]:
                    return False, None, errors[0]["message"]
            except Exception:
                pass
            return False, None, f"HTTP {e.code}: {e.reason}"
        except Exception as ex:
            return False, None, str(ex)

    def _extract_retry_after(self, headers: Any, default: int = 15) -> int:
        """Extracts rate limit reset duration in seconds from HTTP response headers."""
        try:
            if headers:
                for key in ("x-ratelimit-reset", "retry-after"):
                    val = headers.get(key)
                    if val and str(val).isdigit():
                        return max(1, min(int(val) + 1, 60))
        except Exception:
            pass
        return default

    def _extract_roblosecurity_cookie(self, response) -> Optional[str]:
        """Extracts the updated .ROBLOSECURITY cookie value from HTTP response Set-Cookie headers."""
        headers = None
        if hasattr(response, "info") and callable(response.info):
            headers = response.info()
        elif hasattr(response, "headers"):
            headers = response.headers

        set_cookies = []
        if headers is not None:
            if hasattr(headers, "get_all"):
                set_cookies = headers.get_all("Set-Cookie") or headers.get_all("set-cookie") or []
            elif isinstance(headers, dict):
                val = headers.get("Set-Cookie") or headers.get("set-cookie")
                if isinstance(val, list):
                    set_cookies = val
                elif val:
                    set_cookies = [val]
            elif hasattr(headers, "get"):
                val = headers.get("Set-Cookie") or headers.get("set-cookie")
                if val:
                    set_cookies = [val]

        for header in set_cookies:
            # Handle multiple cookies or single cookie in header
            parts = str(header).split(";")
            for part in parts:
                part = part.strip()
                if part.startswith(".ROBLOSECURITY="):
                    val = part.split("=", 1)[1].strip()
                    if val and val != "deleted":
                        return val
        return None

    def get_auth_ticket(self, cookie: str) -> Optional[str]:
        """
        Acquires a single-use authentication ticket from auth.roblox.com
        for native Windows client launching, with automatic x-csrf-token handling.
        Returns ticket string if successful, None otherwise.
        """
        res = self.request_auth_ticket(cookie)
        return res.ticket if res.success else None

    def check_game_join_challenge(self, cookie: str, place_id: int = 3016661674, game_instance_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Probes Roblox gamejoin to verify if the session is currently flagged with
        a security challenge (Arkose CAPTCHA, 2FA, or bot-mitigation).
        """
        if not cookie:
            return {"challenged": False, "status_code": 0, "error": "No session cookie"}

        endpoint = "join-game-instance" if game_instance_id else "join-game"
        url = f"https://gamejoin.roblox.com/v1/{endpoint}"
        payload: Dict[str, Any] = {"placeId": place_id, "isTeleport": False}
        if game_instance_id:
            payload["gameInstanceId"] = game_instance_id

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Cookie": f".ROBLOSECURITY={cookie}",
                "Content-Type": "application/json",
                "User-Agent": "Roblox/Linux",
                "Origin": "https://www.roblox.com",
                "Referer": "https://www.roblox.com/",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return {"challenged": False, "status_code": resp.status}
        except urllib.error.HTTPError as e:
            if e.code == 403:
                ch_type = e.headers.get("rblx-challenge-type")
                ch_id = e.headers.get("rblx-challenge-id")
                ch_meta = e.headers.get("rblx-challenge-metadata")
                body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
                if ch_type or "challenge" in body.lower():
                    return {
                        "challenged": True,
                        "status_code": 403,
                        "challenge_type": ch_type or "captcha",
                        "challenge_id": ch_id,
                        "challenge_metadata": ch_meta,
                    }
            return {"challenged": False, "status_code": e.code}
        except Exception as e:
            return {"challenged": False, "error": str(e)}

    def get_place_details(self, place_id: int, cookie: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetches place and universe details from games.roblox.com/v1/games/multiget-place-details."""
        url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
        res = self._get(url, cookie=cookie)
        if res and isinstance(res, list) and len(res) > 0:
            return res[0]
        return None

    def get_universe_places(self, universe_id: int, cookie: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches all sub-places belonging to a game universe (e.g. Rogue Lineage Gaia/Khei)."""
        url = f"https://develop.roblox.com/v1/universes/{universe_id}/places?isUniverseCreation=false&limit=50"
        res = self._get(url, cookie=cookie)
        if res and isinstance(res, dict) and "data" in res:
            return res["data"]
        return []

    def get_public_servers(
        self,
        place_id: int,
        cookie: Optional[str] = None,
        sort_order: str = "Asc",
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetches live public game servers for a place, sorted by ascending or descending player count.
        Returns: Dict with "data" (list of servers) and "nextPageCursor".
        """
        # Roblox API strictly requires limit in (10, 25, 50, 100)
        if limit <= 10:
            norm_limit = 10
        elif limit <= 25:
            norm_limit = 25
        elif limit <= 50:
            norm_limit = 50
        else:
            norm_limit = 100

        url = f"https://games.roblox.com/v1/games/{place_id}/servers/0?sortOrder={sort_order}&limit={norm_limit}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        res = self._get(url, cookie=cookie)
        if res and isinstance(res, dict) and "data" in res:
            return res
        return {"data": [], "nextPageCursor": None}

    def get_blocked_users(self, cookie: str) -> List[Dict[str, Any]]:
        """
        Retrieves all blocked users for the authenticated account from apis.roblox.com.
        Handles pagination automatically.
        Returns a list of dicts: [{"id": 123456, "block_type": "Blocker"}, ...]
        """
        if not cookie:
            return []

        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        blocked_list = []
        cursor = None
        browser_id = str(random.randint(1000000000000000000, 9999999999999999999))

        while True:
            url = "https://apis.roblox.com/user-blocking-api/v1/users/get-blocked-users?count=50"
            if cursor:
                url += f"&cursor={urllib.parse.quote(cursor)}"

            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Cookie": f".ROBLOSECURITY={clean_cookie}; RBXEventTrackerV2=browserid={browser_id}",
                "Referer": "https://www.roblox.com/my/account",
                "Origin": "https://www.roblox.com",
            }

            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    data = payload.get("data", {})
                    users = data.get("blockedUsers") or []
                    for u in users:
                        u_id = u.get("blockedUserId")
                        if u_id:
                            blocked_list.append({
                                "id": u_id,
                                "block_type": u.get("blockManagerType", "Blocker"),
                            })
                    cursor = data.get("cursor")
                    if not cursor:
                        break
            except Exception as e:
                log.error(f"Failed to fetch blocked users: {e}")
                break

        return blocked_list

    def unblock_user(
        self,
        target_user_id: int,
        cookie: str,
        csrf_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Unblocks a target Roblox user ID.
        Returns (success: bool, current_csrf_token: Optional[str]).
        """
        if not target_user_id or not cookie:
            return False, csrf_token

        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        browser_id = str(random.randint(1000000000000000000, 9999999999999999999))
        url = f"https://apis.roblox.com/user-blocking-api/v1/users/{target_user_id}/unblock-user"

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}; RBXEventTrackerV2=browserid={browser_id}",
            "Referer": "https://www.roblox.com/my/account",
            "Origin": "https://www.roblox.com",
        }
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        req = urllib.request.Request(url, data=b"", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status in (200, 204):
                    return True, csrf_token
                return False, csrf_token
        except urllib.error.HTTPError as e:
            if e.code == 403 and "x-csrf-token" in e.headers:
                fresh_csrf = e.headers["x-csrf-token"]
                headers["X-CSRF-TOKEN"] = fresh_csrf
                req_retry = urllib.request.Request(url, data=b"", headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req_retry, timeout=self.timeout) as retry_resp:
                        return (retry_resp.status in (200, 204)), fresh_csrf
                except Exception as retry_err:
                    log.error(f"Unblock user {target_user_id} retry failed: {retry_err}")
                    return False, fresh_csrf
            log.error(f"HTTP {e.code} unblocking user {target_user_id}: {e}")
            return False, csrf_token
        except Exception as e:
            log.error(f"Network error unblocking user {target_user_id}: {e}")
            return False, csrf_token

    def logout_session(self, cookie: str) -> Tuple[bool, str]:
        """
        Invalidates and destroys the .ROBLOSECURITY session token on Roblox auth servers.
        Hits POST https://auth.roblox.com/v2/logout.
        Returns:
            Tuple of (success: bool, status_message: str)
        """
        if not cookie:
            return False, "No session cookie provided"

        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        url = "https://auth.roblox.com/v2/logout"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
            "Referer": "https://www.roblox.com",
            "Origin": "https://www.roblox.com",
        }
        csrf_token = self.get_csrf_token(clean_cookie)
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    return True, "Successfully logged out of session."
                return False, f"Unexpected response status: {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 403 and "x-csrf-token" in e.headers:
                fresh_csrf = e.headers["x-csrf-token"]
                headers["X-CSRF-TOKEN"] = fresh_csrf
                retry_req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(retry_req, timeout=self.timeout) as retry_resp:
                        if retry_resp.status == 200:
                            return True, "Successfully logged out of session."
                        return False, f"Unexpected retry status: {retry_resp.status}"
                except Exception as retry_err:
                    return False, f"Logout retry failed: {retry_err}"
            elif e.code == 401:
                return True, "Session already logged out / expired."
            return False, f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            return False, str(e)

    def decline_all_friend_requests(self, cookie: str) -> Tuple[bool, str]:
        """Declines all pending incoming friend requests for the account."""
        if not cookie:
            return False, "No cookie provided"
        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        url = "https://friends.roblox.com/v1/user/friend-requests/decline-all"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
            "Referer": "https://www.roblox.com/users/friends#!/friend-requests",
            "Origin": "https://www.roblox.com",
        }
        csrf = self.get_csrf_token(clean_cookie)
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf

        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status in (200, 204):
                    return True, "Successfully declined all friend requests."
                return False, f"Status {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 403 and "x-csrf-token" in e.headers:
                headers["X-CSRF-TOKEN"] = e.headers["x-csrf-token"]
                retry_req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(retry_req, timeout=self.timeout) as retry_resp:
                        return (retry_resp.status in (200, 204)), "Successfully declined all friend requests."
                except Exception as rerr:
                    return False, str(rerr)
            return False, f"HTTP {e.code}: {e.reason}"
        except Exception as ex:
            return False, str(ex)

    def get_friend_requests(self, cookie: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns list of pending friend requests."""
        if not cookie:
            return []
        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        url = f"https://friends.roblox.com/v1/my/friends/requests?sortOrder=Desc&limit={limit}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
            "Referer": "https://www.roblox.com",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", [])
        except Exception as e:
            log.warning(f"Failed to fetch friend requests: {e}")
            return []

    def accept_friend_request(
        self,
        target_user_id: int,
        cookie: str,
        csrf_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Accepts a friend request from target_user_id."""
        if not target_user_id or not cookie:
            return False, csrf_token
        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        url = f"https://friends.roblox.com/v1/users/{target_user_id}/accept-friend-request"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
            "Referer": "https://www.roblox.com",
            "Origin": "https://www.roblox.com",
        }
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return (resp.status in (200, 204)), csrf_token
        except urllib.error.HTTPError as e:
            if e.code == 403 and "x-csrf-token" in e.headers:
                fresh_csrf = e.headers["x-csrf-token"]
                headers["X-CSRF-TOKEN"] = fresh_csrf
                retry_req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(retry_req, timeout=self.timeout) as retry_resp:
                        return (retry_resp.status in (200, 204)), fresh_csrf
                except Exception:
                    return False, fresh_csrf
            return False, csrf_token
        except Exception:
            return False, csrf_token

    def get_friends(self, user_id: int, cookie: str = "") -> List[Dict[str, Any]]:
        """Returns list of friends for the specified user_id."""
        if not user_id:
            return []
        url = f"https://friends.roblox.com/v1/users/{user_id}/friends"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if cookie:
            clean_cookie = cookie.strip()
            if clean_cookie.startswith(".ROBLOSECURITY="):
                clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]
            headers["Cookie"] = f".ROBLOSECURITY={clean_cookie}"

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", [])
        except Exception as e:
            log.warning(f"Failed to fetch friends for {user_id}: {e}")
            return []

    def unfriend_user(
        self,
        target_user_id: int,
        cookie: str,
        csrf_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Removes a friend by target_user_id."""
        if not target_user_id or not cookie:
            return False, csrf_token
        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        url = f"https://friends.roblox.com/v1/users/{target_user_id}/unfriend"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
            "Referer": "https://www.roblox.com",
            "Origin": "https://www.roblox.com",
        }
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token

        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return (resp.status in (200, 204)), csrf_token
        except urllib.error.HTTPError as e:
            if e.code == 403 and "x-csrf-token" in e.headers:
                fresh_csrf = e.headers["x-csrf-token"]
                headers["X-CSRF-TOKEN"] = fresh_csrf
                retry_req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(retry_req, timeout=self.timeout) as retry_resp:
                        return (retry_resp.status in (200, 204)), fresh_csrf
                except Exception:
                    return False, fresh_csrf
            return False, csrf_token
        except Exception:
            return False, csrf_token

    def get_join_privacy(self, cookie: str) -> str:
        """
        Fetches the current game join privacy setting for the account.
        Returns 'NoOne', 'Friends', 'AllUsers', or 'Unknown'.
        """
        if not cookie:
            return "Unknown"
        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        url = "https://apis.roblox.com/user-settings-api/v1/user-settings"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("whoCanJoinMeInExperiences", data.get("gameJoinPrivacy", "Unknown"))
        except Exception:
            # Fallback to accountsettings
            try:
                url_fb = "https://accountsettings.roblox.com/v1/privacy/settings"
                req_fb = urllib.request.Request(url_fb, headers=headers, method="GET")
                with urllib.request.urlopen(req_fb, timeout=self.timeout) as resp_fb:
                    data_fb = json.loads(resp_fb.read().decode("utf-8"))
                    return data_fb.get("whoCanJoinMeInExperiences", data_fb.get("gameJoinPrivacy", "Unknown"))
            except Exception:
                return "Unknown"

    def set_join_privacy(self, setting: str, cookie: str) -> Tuple[bool, str]:
        """
        Sets the game join privacy setting ('NoOne', 'Friends', 'AllUsers', etc.)
        Supports modern APIs (user-settings-api whoCanJoinMeInExperiences),
        accountsettings.roblox.com, and classic follow-me-privacy.
        """
        if not cookie:
            return False, "No cookie provided"
        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        # Normalization
        setting_val = setting
        if setting in ("All", "Everyone", "AllUsers"):
            setting_val = "AllUsers"
            form_val = "All"
        elif setting in ("Friends", "FriendsOnly"):
            setting_val = "Friends"
            form_val = "Friends"
        elif setting in ("NoOne", "Ghost", "Nobody"):
            setting_val = "NoOne"
            form_val = "NoOne"
        else:
            form_val = setting_val

        csrf = self.get_csrf_token(clean_cookie)

        def _do_post(url: str, data: bytes, content_type: str, referer: str) -> Tuple[bool, str]:
            nonlocal csrf
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": content_type,
                "Cookie": f".ROBLOSECURITY={clean_cookie}",
                "Referer": referer,
                "Origin": "https://www.roblox.com",
            }
            if csrf:
                headers["X-CSRF-TOKEN"] = csrf

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status in (200, 204):
                        return True, f"Join privacy updated to '{setting_val}'."
                    return False, f"Status {resp.status}"
            except urllib.error.HTTPError as e:
                new_csrf = e.headers.get("x-csrf-token") or e.headers.get("X-CSRF-TOKEN")
                if e.code == 403 and new_csrf:
                    csrf = new_csrf
                    headers["X-CSRF-TOKEN"] = new_csrf
                    retry_req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                    try:
                        with urllib.request.urlopen(retry_req, timeout=self.timeout) as retry_resp:
                            if retry_resp.status in (200, 204):
                                return True, f"Join privacy updated to '{setting_val}'."
                            return False, f"Status {retry_resp.status}"
                    except Exception as rerr:
                        return False, str(rerr)
                return False, f"HTTP {e.code}: {e.reason}"
            except Exception as ex:
                return False, str(ex)

        # Attempt 1: Modern user-settings-api with whoCanJoinMeInExperiences
        payload1 = json.dumps({"whoCanJoinMeInExperiences": setting_val}).encode("utf-8")
        ok, msg = _do_post(
            "https://apis.roblox.com/user-settings-api/v1/user-settings",
            payload1,
            "application/json",
            "https://www.roblox.com/my/account#!/privacy",
        )
        if ok:
            return True, msg

        # Attempt 2: Modern user-settings-api with gameJoinPrivacy
        payload2 = json.dumps({"gameJoinPrivacy": setting_val}).encode("utf-8")
        ok, msg = _do_post(
            "https://apis.roblox.com/user-settings-api/v1/user-settings",
            payload2,
            "application/json",
            "https://www.roblox.com/my/account#!/privacy",
        )
        if ok:
            return True, msg

        # Attempt 3: accountsettings.roblox.com/v1/privacy
        ok, msg = _do_post(
            "https://accountsettings.roblox.com/v1/privacy",
            payload2,
            "application/json",
            "https://www.roblox.com/my/account#!/privacy",
        )
        if ok:
            return True, msg

        # Attempt 4: Classic Roblox web form endpoint (RAM method)
        form_data = urllib.parse.urlencode({"FollowMePrivacy": form_val}).encode("utf-8")
        ok, msg = _do_post(
            "https://www.roblox.com/account/settings/follow-me-privacy",
            form_data,
            "application/x-www-form-urlencoded",
            "https://www.roblox.com/my/account",
        )
        if ok:
            return True, msg

        return False, msg

    def get_currency_summary(self, user_id: int, cookie: str) -> Dict[str, int]:
        """
        Retrieves current Robux balance and pending Robux summary.
        Returns dict: {'robux': int, 'pending_robux': int}
        """
        result = {"robux": 0, "pending_robux": 0, "success": True}
        if not user_id or not cookie:
            return result

        clean_cookie = cookie.strip()
        if clean_cookie.startswith(".ROBLOSECURITY="):
            clean_cookie = clean_cookie[len(".ROBLOSECURITY="):]

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cookie": f".ROBLOSECURITY={clean_cookie}",
            "Referer": "https://www.roblox.com/transactions",
        }

        # 1. Fetch current Robux
        try:
            cur_url = f"https://economy.roblox.com/v1/users/{user_id}/currency"
            cur_req = urllib.request.Request(cur_url, headers=headers, method="GET")
            with urllib.request.urlopen(cur_req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                result["robux"] = int(data.get("robux", 0))
        except Exception as e:
            log.warning(f"Failed to fetch currency for user {user_id}: {e}")

        # 2. Fetch pending Robux from summary or transaction-totals
        try:
            sum_url = f"https://economy.roblox.com/v1/users/{user_id}/transaction-totals?timeFrame=Year&transactionType=summary"
            sum_req = urllib.request.Request(sum_url, headers=headers, method="GET")
            with urllib.request.urlopen(sum_req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                result["pending_robux"] = int(data.get("pendingRobuxTotal", 0))
        except Exception:
            try:
                # Fallback summary endpoint
                sum_url_fb = f"https://economy.roblox.com/v1/users/{user_id}/summary"
                sum_req_fb = urllib.request.Request(sum_url_fb, headers=headers, method="GET")
                with urllib.request.urlopen(sum_req_fb, timeout=self.timeout) as resp_fb:
                    data_fb = json.loads(resp_fb.read().decode("utf-8"))
                    result["pending_robux"] = int(data_fb.get("pendingRobuxTotal", data_fb.get("pendingRobux", 0)))
            except Exception:
                pass

        return result

    def login_with_credentials(
        self,
        username: str,
        password: str,
        totp_secret: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], str]:
        """
        Attempts direct automated login to Roblox auth servers using username,
        password, and optional 2FA TOTP authenticator secret.
        Returns: (success, cookie, user_data, message)
        """
        if not username or not password:
            return False, None, None, "Username and password are required"

        url = "https://auth.roblox.com/v2/login"
        payload = json.dumps({"ctype": "Username", "cvalue": username, "password": password}).encode("utf-8")

        headers = {
            "User-Agent": USER_AGENT,
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/login",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="133", "Not(A:Brand";v="99", "Google Chrome";v="133"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }

        # Step 1: Obtain CSRF token
        csrf_token = ""
        init_req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(init_req, timeout=self.timeout) as resp:
                cookie = self._extract_roblosecurity_cookie(resp)
                if cookie:
                    user_data = self.validate_cookie_and_get_user(cookie)
                    return True, cookie, user_data, "Successfully authenticated directly."
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "15"))
                return False, None, None, f"RATE_LIMITED:{retry_after}"
            if e.code == 403 and "x-csrf-token" in e.headers:
                csrf_token = e.headers["x-csrf-token"]
            else:
                try:
                    err_json = json.loads(e.read().decode("utf-8"))
                    msg = err_json.get("errors", [{}])[0].get("message", "")
                    if msg:
                        return False, None, None, msg
                except Exception:
                    pass
                return False, None, None, f"Login initial probe failed (HTTP {e.code})"
        except Exception as ex:
            return False, None, None, f"Network error during login: {ex}"

        if not csrf_token:
            return False, None, None, "Could not acquire CSRF token from Roblox auth server"

        headers["X-CSRF-TOKEN"] = csrf_token

        # Step 2: Post login with CSRF
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                cookie = self._extract_roblosecurity_cookie(resp)
                if cookie:
                    user_data = self.validate_cookie_and_get_user(cookie)
                    return True, cookie, user_data, "Successfully logged in."
                return False, None, None, "Login accepted but no session cookie received"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "15"))
                return False, None, None, f"RATE_LIMITED:{retry_after}"
            if e.code == 403:
                c_type = e.headers.get("rblx-challenge-type", "").lower()
                c_id = e.headers.get("rblx-challenge-id", "")
                c_meta_raw = e.headers.get("rblx-challenge-metadata", "")

                try:
                    decoded_ctype = base64.b64decode(c_type).decode("utf-8", errors="ignore").lower()
                    if decoded_ctype:
                        c_type = decoded_ctype
                except Exception:
                    pass

                if "captcha" in c_type or "arkose" in c_type:
                    return False, None, None, "CAPTCHA_REQUIRED"

                if "twostepverification" in c_type or "step" in c_type:
                    if not totp_secret:
                        return False, None, None, "2FA_REQUIRED"

                    # Parse challenge metadata
                    meta: Dict[str, Any] = {}
                    if c_meta_raw:
                        try:
                            meta = json.loads(base64.b64decode(c_meta_raw).decode("utf-8"))
                        except Exception:
                            try:
                                meta = json.loads(c_meta_raw)
                            except Exception:
                                pass

                    sub_id = meta.get("challengeId", "")
                    user_id = str(meta.get("userId", ""))
                    action_type = meta.get("actionType", "Login")

                    # Generate TOTP code
                    from core.totp import generate_totp
                    totp_code = generate_totp(totp_secret)
                    if not totp_code:
                        return False, None, None, "Failed to generate TOTP code from secret key"

                    # Verify 2FA
                    v_ok, token, v_err = self._verify_two_step_code(
                        user_id=user_id,
                        challenge_id=sub_id,
                        action_type=action_type,
                        code=totp_code,
                        cookie="",
                        csrf_token=csrf_token,
                    )
                    if not v_ok or not token:
                        return False, None, None, f"2FA verification failed: {v_err}"

                    # Continue challenge
                    self._continue_challenge(
                        generic_id=c_id,
                        token=token,
                        cookie="",
                        csrf_token=csrf_token,
                        sub_challenge_id=sub_id,
                        action_type=action_type,
                    )

                    # Retry login with challenge token
                    meta_payload = dict(meta)
                    meta_payload["verificationToken"] = token
                    meta_payload["rememberDevice"] = False
                    raw_b64 = base64.b64encode(json.dumps(meta_payload).encode("utf-8")).decode("utf-8")

                    headers["rblx-challenge-id"] = c_id
                    headers["rblx-challenge-type"] = "twostepverification"
                    headers["rblx-challenge-metadata"] = raw_b64

                    retry_req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                    try:
                        with urllib.request.urlopen(retry_req, timeout=self.timeout) as retry_resp:
                            cookie = self._extract_roblosecurity_cookie(retry_resp)
                            if cookie:
                                user_data = self.validate_cookie_and_get_user(cookie)
                                return True, cookie, user_data, "Successfully logged in via 2FA."
                            return False, None, None, "2FA succeeded but no session cookie received"
                    except urllib.error.HTTPError as retry_e:
                        if retry_e.code == 429:
                            retry_after = int(retry_e.headers.get("Retry-After", "15"))
                            return False, None, None, f"RATE_LIMITED:{retry_after}"
                        retry_ctype = retry_e.headers.get("rblx-challenge-type", "").lower()
                        try:
                            decoded_rc = base64.b64decode(retry_ctype).decode("utf-8", errors="ignore").lower()
                            if decoded_rc:
                                retry_ctype = decoded_rc
                        except Exception:
                            pass
                        if retry_e.code == 403:
                            if "captcha" in retry_ctype or "arkose" in retry_ctype or retry_e.headers.get("rblx-challenge-id"):
                                return False, None, None, "CAPTCHA_REQUIRED"
                        try:
                            err_data = json.loads(retry_e.read().decode("utf-8"))
                            msg = err_data.get("errors", [{}])[0].get("message", f"HTTP {retry_e.code}")
                            if any(k in msg.lower() for k in ("challenge", "captcha", "verification")):
                                return False, None, None, "CAPTCHA_REQUIRED"
                            return False, None, None, msg
                        except Exception:
                            return False, None, None, f"2FA retry failed: HTTP {retry_e.code}"

                # If challenge ID or challenge header present and not 2FA, it's a captcha/visual challenge
                if c_id or "challenge" in c_type:
                    return False, None, None, "CAPTCHA_REQUIRED"

            # Fallback error extraction
            try:
                err_data = json.loads(e.read().decode("utf-8"))
                errors = err_data.get("errors", [])
                if errors and "message" in errors[0]:
                    msg = errors[0]["message"]
                    if any(k in msg.lower() for k in ("challenge", "captcha", "verification")):
                        return False, None, None, "CAPTCHA_REQUIRED"
                    return False, None, None, msg
            except Exception:
                pass
            return False, None, None, f"HTTP {e.code}: {e.reason}"
        except Exception as ex:
            return False, None, None, str(ex)



