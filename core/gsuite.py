"""
farmercf.core.gsuite — G Suite OAuth login for Cloudflare Dashboard.

G Suite accounts bypass 2FA, making them ideal for:
  - Token creation on legacy accounts (missing Analytics Read permission)
  - Programmatic dashboard access without Camoufox

Flow:
  1. Navigate to CF login page
  2. Enter email → Google OAuth redirect
  3. Google login (email + password, no 2FA on G Suite)
  4. Accept OAuth consent
  5. Extract CF dashboard cookies
  6. Use cookies for API calls (token creation, permission management)

Requires: Camoufox browser (not curl_cffi — Google login needs full browser).
"""

import asyncio
import json
from pathlib import Path
from loguru import logger

CF_LOGIN_URL = "https://dash.cloudflare.com/login"


async def gsuite_login(
    email: str,
    password: str,
    cookie_file: str = "gsuite_cookies.json",
    headless: bool = True,
    log=logger.info,
) -> dict | None:
    """Login to Cloudflare via Google OAuth (G Suite, no 2FA).

    Returns dict with cookies + session info, or None on failure.
    Uses Camoufox for anti-bot bypass.
    """
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        log("[G Suite] camoufox not installed — pip install camoufox")
        return None

    log(f"[G Suite] starting login for {email}...")

    async with AsyncCamoufox(headless=headless, humanize=True) as browser:
        page = await browser.new_page()

        try:
            # Step 1: Go to CF login
            await page.goto(CF_LOGIN_URL, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(2)

            # Step 2: Click "Continue with Google"
            google_btn = page.locator('button:has-text("Google"), a:has-text("Google")')
            if await google_btn.count() > 0:
                await google_btn.first.click()
                log("[G Suite] clicked Google login")
            else:
                # Some CF versions have it as a data-provider attr
                google_link = page.locator('[data-provider="google"], [data-test="google-login"]')
                if await google_link.count() > 0:
                    await google_link.first.click()
                    log("[G Suite] clicked Google login (data-provider)")
                else:
                    log("[G Suite] Google button not found")
                    return None

            await page.wait_for_load_state("networkidle", timeout=30000)
            await asyncio.sleep(2)

            # Step 3: Google login — enter email
            email_input = page.locator('input[type="email"]')
            await email_input.wait_for(timeout=15000)
            await email_input.fill(email)
            await page.locator('#identifierNext, button:has-text("Next"), button:has-text("Continue")').first.click()
            log(f"[G Suite] entered email: {email}")

            await asyncio.sleep(3)

            # Step 4: Enter password
            pw_input = page.locator('input[type="password"]')
            await pw_input.wait_for(timeout=15000)
            await pw_input.fill(password)
            await page.locator('#passwordNext, button:has-text("Next")').first.click()
            log("[G Suite] entered password")

            # Step 5: Wait for OAuth consent page or CF redirect
            await asyncio.sleep(5)

            # Check for consent page
            consent_btn = page.locator('button:has-text("Allow"), button:has-text("Accept"), button:has-text("Continue")')
            if await consent_btn.count() > 0:
                await consent_btn.first.click()
                log("[G Suite] accepted OAuth consent")
                await asyncio.sleep(3)

            # Step 6: Wait for CF dashboard
            await page.wait_for_url("**/dash.cloudflare.com/**", timeout=30000)
            log("[G Suite] landed on CF dashboard!")

            # Step 7: Extract cookies
            cookies = await page.context.cookies()
            cookie_data = {c["name"]: c["value"] for c in cookies if "cloudflare" in c.get("domain", "")}

            # Also get CF session token from localStorage
            local_storage = await page.evaluate("() => JSON.stringify(localStorage)")
            try:
                ls_data = json.loads(local_storage) if local_storage else {}
            except (json.JSONDecodeError, TypeError):
                ls_data = {}

            result = {
                "email": email,
                "cookies": cookie_data,
                "localStorage": ls_data,
                "url": page.url,
            }

            # Save cookies
            Path(cookie_file).write_text(json.dumps(result, indent=2))
            log(f"[G Suite] cookies saved to {cookie_file}")

            return result

        except Exception as e:
            log(f"[G Suite] login error: {e}")
            # Take screenshot for debugging
            try:
                await page.screenshot(path="gsuite_error.png")
                log("[G Suite] screenshot saved: gsuite_error.png")
            except Exception:
                pass
            return None


async def create_token_with_cookies(
    cookies: dict,
    account_id: str,
    permissions: list[dict] | None = None,
    log=logger.info,
) -> str | None:
    """Create API token using G Suite dashboard cookies.

    Args:
        cookies: dict of CF cookies from gsuite_login()
        account_id: CF account ID
        permissions: list of {"id": "..."} permission group IDs

    Returns API token string or None.
    """
    if permissions is None:
        from .constants import API_TOKEN_PERMISSIONS
        permissions = API_TOKEN_PERMISSIONS

    from curl_cffi.requests import AsyncSession
    from .constants import CF_API, HEADERS

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    async with AsyncSession(impersonate="chrome131") as s:
        # Create token
        resp = await s.post(
            f"{CF_API}/user/tokens",
            json={
                "name": "workers-ai-gsuite",
                "condition": {},
                "policies": [{
                    "effect": "allow",
                    "resources": {f"com.cloudflare.api.account.{account_id}": "*"},
                    "permission_groups": permissions,
                }],
            },
            headers={**HEADERS, "Cookie": cookie_header, "content-type": "application/json"},
            timeout=30,
        )
        data = resp.json()
        if data.get("success"):
            token = data["result"]["value"]
            log(f"[G Suite] token created: {token[:20]}...")
            return token

        log(f"[G Suite] token creation failed: {data.get('errors')}")
        return None
