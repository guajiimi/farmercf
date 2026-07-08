"""
cfcaptcha.core.twocaptcha — 2Captcha HTTP client for Turnstile solving.

Handles: sitekey scraping, action scraping, HTTP solve polling, and DOM injection.
"""

import json
import time
import asyncio

import httpx
from loguru import logger


class TwoCaptchaClient:
    """2Captcha Turnstile integration — scrape, solve via HTTP, inject into page."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    async def scrape_sitekey(self, page, fallback: str = "") -> str:
        """Scrape Turnstile sitekey from page DOM. 3 methods: data-sitekey, iframe src, window.__CF$cv$params."""
        try:
            sitekey = await page.evaluate(r"""() => {
                // Method 1: data-sitekey attribute
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // Method 2: inside Turnstile iframe src
                for (const iframe of document.querySelectorAll('iframe')) {
                    const src = iframe.src || '';
                    const m = src.match(/[?&]sitekey=([^&]+)/);
                    if (m) return decodeURIComponent(m[1]);
                }
                // Method 3: window.__CF$cv$params
                try {
                    const raw = JSON.stringify(window.__CF$cv$params || {});
                    const m2 = raw.match(/sitekey["']?\s*:\s*["']([^"']+)["']/);
                    if (m2) return m2[1];
                } catch(e) {}
                return null;
            }""")
            if sitekey and len(str(sitekey).strip()) > 10:
                logger.info(f"[2Captcha] sitekey from DOM: {sitekey}")
                return str(sitekey).strip()
        except Exception as e:
            logger.warning(f"[2Captcha] scrape sitekey error: {e}")
        if fallback:
            logger.info(f"[2Captcha] using fallback sitekey: {fallback}")
        return fallback

    async def scrape_turnstile_action(self, page) -> str | None:
        """Extract data-action from Turnstile widget on page."""
        try:
            action = await page.evaluate(r"""() => {
                const el = document.querySelector('[data-action], .cf-turnstile, [data-cf-turnstile-response]');
                if (el && el.getAttribute('data-action')) return el.getAttribute('data-action');
                for (const iframe of document.querySelectorAll('iframe')) {
                    const src = iframe.src || '';
                    const m = src.match(/[?&]action=([^&]+)/);
                    if (m) return decodeURIComponent(m[1]);
                }
                return null;
            }""")
            if action:
                return str(action).strip()
        except Exception:
            pass
        return None

    async def solve_via_2captcha(
        self, sitekey: str, page_url: str, action: str | None = None, cdata: str | None = None
    ) -> str | None:
        """Submit Turnstile to 2Captcha createTask API (v2) and poll for solution token. Pure HTTP — no browser."""
        api_key = self.cfg.get("twocaptcha_api_key", "")
        if not api_key:
            logger.warning("[2Captcha] no API key configured, skipping")
            return None

        timeout = self.cfg.get("twocaptcha_timeout", 120)
        logger.info(f"[2Captcha] submitting Turnstile via createTask (sitekey={sitekey[:20]}..., timeout={timeout}s)")

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                task_payload = {
                    "clientKey": api_key,
                    "task": {
                        "type": "TurnstileTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                }
                if action:
                    task_payload["task"]["action"] = action
                    logger.info(f"[2Captcha] action: {action}")
                if cdata:
                    task_payload["task"]["data"] = cdata

                r = await client.post("https://api.2captcha.com/createTask", json=task_payload)
                resp = r.json()
                if resp.get("errorId", 0) != 0:
                    logger.warning(f"[2Captcha] createTask error: {resp.get('errorDescription', resp)}")
                    return None
                task_id = resp.get("taskId")
                logger.info(f"[2Captcha] task created: {task_id}")

                await asyncio.sleep(8)  # initial wait
                deadline = time.time() + timeout
                while time.time() < deadline:
                    r2 = await client.post(
                        "https://api.2captcha.com/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                    )
                    res = r2.json()
                    if res.get("status") == "ready":
                        token = res["solution"]["token"]
                        logger.success(f"[2Captcha] Turnstile solved! token_len={len(token)}")
                        return token
                    if res.get("errorId", 0) != 0:
                        logger.warning(f"[2Captcha] error: {res.get('errorDescription')}")
                        return None
                    await asyncio.sleep(5)

                logger.warning("[2Captcha] Turnstile timeout")
                return None
            except Exception as e:
                logger.error(f"[2Captcha] error: {e}")
                return None

    async def inject_token(self, page, token: str) -> bool:
        """Inject solved 2Captcha token into the page's Turnstile hidden inputs."""
        try:
            await page.evaluate(f"""() => {{
                const tok = {json.dumps(token)};
                // Set all possible Turnstile response inputs
                const names = ['cf-turnstile-response', 'cf_challenge_response', 'cf-turnstile-response-0'];
                for (const name of names) {{
                    document.getElementsByName(name).forEach(el => {{
                        // Use React native setter to bypass controlled inputs
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSetter.call(el, tok);
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }});
                }}
                // Try Turnstile success callback
                try {{
                    const widget = document.querySelector('[data-callback]');
                    const cbName = widget && widget.getAttribute('data-callback');
                    if (cbName && window[cbName]) window[cbName](tok);
                }} catch(e) {{}}
                // Try direct turnstile API
                try {{
                    if (window.__cf_chl_opt && window.__cf_chl_opt.cFRq) {{
                        window.__cf_chl_opt.cFRq(tok);
                    }}
                }} catch(e) {{}}
            }}""")
            logger.info(f"[2Captcha] token injected into DOM (len={len(token)})")
            return True
        except Exception as e:
            logger.error(f"[2Captcha] inject error: {e}")
            return False

    async def solve_turnstile_http(
        self, sitekey: str, page_url: str, action: str | None = None, log=logger.info
    ) -> str | None:
        """Standalone HTTP solve via createTask API (v2) — no browser needed. Used by farmer module."""
        api_key = self.cfg.get("twocaptcha_api_key", "") if isinstance(self.cfg, dict) else str(self.cfg)
        if not api_key:
            log("[2Captcha] no API key")
            return None
        timeout = self.cfg.get("twocaptcha_timeout", 120) if isinstance(self.cfg, dict) else 120

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                task_payload = {
                    "clientKey": api_key,
                    "task": {
                        "type": "TurnstileTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                }
                if action:
                    task_payload["task"]["action"] = action

                r = await client.post("https://api.2captcha.com/createTask", json=task_payload)
                resp = r.json()
                if resp.get("errorId", 0) != 0:
                    log(f"[2Captcha] createTask error: {resp.get('errorDescription', resp)}")
                    return None
                task_id = resp.get("taskId")
                log(f"[2Captcha] task created: {task_id}")

                await asyncio.sleep(8)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    r2 = await client.post(
                        "https://api.2captcha.com/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                    )
                    res = r2.json()
                    if res.get("status") == "ready":
                        token = res["solution"]["token"]
                        log(f"[2Captcha] solved! token_len={len(token)}")
                        return token
                    if res.get("errorId", 0) != 0:
                        log(f"[2Captcha] error: {res.get('errorDescription')}")
                        return None
                    await asyncio.sleep(5)
                log("[2Captcha] timeout")
                return None
            except Exception as e:
                log(f"[2Captcha] error: {e}")
                return None
