# AERAM — Windows Arkose CAPTCHA Regression Investigation & Remediation

**Author:** Principal Systems Engineer & Authentication Security Specialist  
**Project:** AERAM (Roblox Account Manager)  
**Target:** Windows 11 Arkose Labs & Anti-Abuse Remediation  
**Status:** Resolved & Validated  

---

## 1. Problem Statement

During account management and concurrent multi-launch operations on Windows 11, users reported encountering repeated, disruptive Roblox Arkose FunCAPTCHA challenges. 

This investigation was commissioned to determine the exact root causes of these challenges, isolate AERAM-controlled factors from external Roblox security changes, and implement non-invasive architectural remedies that preserve authentication sessions without violating Roblox security policies.

---

## 2. Root Cause Analysis

Our technical audit revealed **four distinct, compounding factors** that triggered repeated Arkose CAPTCHA challenges:

### Root Cause 1: User-Agent Fingerprint Discrepancy (CRITICAL)
- **The Defect:** When AERAM requested single-use authentication tickets from `auth.roblox.com/v1/authentication-ticket`, it transmitted a Linux X11 Chrome User-Agent header:
  ```
  Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36
  ```
- **The Heuristic Trigger:** The ticket generated with a Linux fingerprint was subsequently redeemed within seconds by the native Windows executable `RobloxPlayerBeta.exe` running on Windows NT 10.0 (Win64).
- **Arkose Labs Heuristic:** Arkose Labs evaluates client entropy and cross-platform redemption. A ticket minted by an X11 client and redeemed by an NT 10.0 client was immediately flagged as anomalous / automated credential rotation, resulting in mandatory CAPTCHA challenges on all subsequent requests from that IP and session.
- **Resolution:** Added dynamic platform User-Agent resolution:
  ```python
  if sys.platform == "win32":
      USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
  ```

---

### Root Cause 2: Ticket Failure Ignored — Fallback to Ticketless Launch (CRITICAL)
- **The Defect:** In earlier Windows code, when `get_auth_ticket(cookie)` returned `None` (due to an initial challenge, 401 error, or rate limit), the launcher logged a warning and proceeded to launch `RobloxPlayerBeta.exe` with `ticket=None`:
  ```python
  auth_param = f"+gameinfo:{ticket}" if ticket else ""  # auth_param was empty string!
  ```
- **The Heuristic Trigger:** Launching `RobloxPlayerBeta.exe` without `gameinfo:<ticket>` caused the client to start unauthenticated. The client fell back to reading whatever old session was left in `%LOCALAPPDATA%\Roblox\LocalStorage\RobloxCookies.dat`. If another account was previously active, the client attempted to log in with mismatched credentials, failed, prompt-challenged the user, or immediately exited.
- **Resolution:** Strict refusal to launch without an authenticated ticket. If ticket minting fails, the launcher halts, marks the state appropriately, and alerts the user.

---

### Root Cause 3: Lack of State Machine — Automated Retries Triggering CAPTCHA Storms
- **The Defect:** Previous versions had no concept of a `VERIFICATION_REQUIRED` state. When an account received a challenge, subsequent operations (such as multi-launch queues or the Self-Healing watchdog) repeatedly attempted to re-request tickets and re-launch the account.
- **The Heuristic Trigger:** Rapid repeated failed requests to `auth.roblox.com` within 10–30 seconds trip Akamai and Arkose velocity filters, turning an isolated challenge into a severe IP-level rate-limit and mandatory CAPTCHA block across all managed accounts.
- **Resolution:** Implemented `AccountAuthStateManager` in `core/auth_state.py`. When an account receives an HTTP 403 challenge, it transitions to `VERIFICATION_REQUIRED`. In this state:
  - Automated retries are **strictly blocked**.
  - Multi-launch loops skip the account.
  - The Self-Healing watchdog suppresses auto-reconnection.
  - The UI displays a clear badge and offers one-click browser verification.

---

### Root Cause 4: Rapid Concurrent Multi-Launch Bursts (< 3.0s Delay)
- **The Defect:** When users launched multiple accounts with delays under 3.0 seconds, multiple `RobloxPlayerBeta.exe` processes started concurrently and raced to open, write, and lock `%LOCALAPPDATA%\Roblox\LocalStorage\RobloxCookies.dat`.
- **The Heuristic Trigger:** SQLite database contention caused ticket redemptions to fail, while burst HTTP traffic from the same IP to `auth.roblox.com` tripped Akamai rate limiting (HTTP 429).
- **Resolution:** Increased default multi-launch delay to **8.0 seconds** (user configurable up to 60.0s), with **randomized humanized jitter** (+0.2s to 1.2s) when delay is $\ge 3.0\text{s}$ to break metronomic robotic timing.

---

## 3. Heuristic Comparison Matrix

| Factor | Legacy Windows Behavior | Remediated AERAM Architecture |
|---|---|---|
| **User-Agent** | Linux X11 UA sent on Windows | Windows 11 Chrome UA (100% parity) |
| **Ticket Failure Handling** | Launched anyway without ticket | Strict halt; never launches with `ticket=None` |
| **Challenge Reaction** | Blindly retried in loops | Transitions to `VERIFICATION_REQUIRED`; halts automated retries |
| **Multi-Launch Stagger** | 1.0s – 3.0s (Fixed delay) | 8.0s – 15.0s with randomized human jitter (+0.2s–1.2s) |
| **Stored Cookie Safety** | Risk of deletion on error | Stored cookies never cleared or destroyed on transient failure |
| **Recovery Mechanism** | User forced to re-import cookies | One-click browser solve + Reset Status |

---

## 4. Operational Recommendations for Users

1. **Use 8–15 Second Launch Delays:** For multi-instance farming, always use an 8–15 second stagger delay. This allows each Roblox client to initialize graphics, redeem tickets, and release file locks before the next instance begins.
2. **Solve Challenges in Browser:** If an account displays `[VERIFICATION REQUIRED]`, right-click the account and select `⚠️ Verify in Browser`. Complete the Arkose puzzle or 2FA challenge legitimately in the browser, then select `Reset Status to Ready`.
3. **Never Use Unofficial Solvers:** AERAM never incorporates automated CAPTCHA solvers or proxy rotators, which violate Roblox Terms of Service and trigger permanent account terminations.
