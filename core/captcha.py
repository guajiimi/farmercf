"""
farmercf.core.captcha — Unified Turnstile solver with auto-fallback.

Providers (tried in order until one succeeds):
  1. Capsolver   — AntiTurnstileTaskProxyLess
  2. Solverify   — turnstile task type
  3. 2Captcha    — TurnstileTaskProxyless (createTask v2)

All providers use curl_cffi with Chrome TLS fingerprint for consistency.
Each solve is serialized via semaphore to prevent rate-limiting.
"""

import asyncio
import time
import random
from loguru import logger
from curl_cffi.requests import AsyncSession

# Provider URLs
CAPSOLVER_URL = "https://api.capsolver.com"
SOLVERIFY_URL = "https://solver.solverify.net"
TWOCAPTCHA_URL = "https://api.2captcha.com"

# Serialized — one solve at a time to avoid rate limits
_solve_semaphore = asyncio.Semaphore(1)


class CaptchaSolver:
    """Unified Turnstile solver with auto-fallback across providers.

    Args:
        providers: list of (provider_name, api_key) tuples, tried in order.
                   e.g. [("capsolver", "CAP-xxx"), ("2captcha", "xxx")]
    """

    def __init__(self, providers: list[tuple[str, str]] | None = None, **kwargs):
        if providers:
            self.providers = [(p, k) for p, k in providers if k]
        else:
            # Fallback to kwargs
            self.providers = []
            for name, key in [
                ("capsolver", kwargs.get("capsolver_api_key", "")),
                ("solverify", kwargs.get("solverify_api_key", "")),
                ("2captcha", kwargs.get("twocaptcha_api_key", "")),
            ]:
                if key:
                    self.providers.append((name, key))

    async def solve_turnstile(
        self,
        sitekey: str,
        page_url: str,
        action: str = "signup",
        cdata: str = "signup",
        timeout: int = 135,
        log=logger.info,
    ) -> str | None:
        """Solve Turnstile, trying each provider until one succeeds."""
        if not self.providers:
            log("[Captcha] no solver configured")
            return None

        for i, (provider, api_key) in enumerate(self.providers):
            log(f"[Captcha] trying {provider} ({i+1}/{len(self.providers)})...")
            try:
                token = await self._solve_with_provider(
                    provider, api_key, sitekey, page_url, action, cdata, timeout, log
                )
                if token:
                    log(f"[Captcha] {provider} solved! token_len={len(token)}")
                    return token
                log(f"[Captcha] {provider} failed, trying next...")
            except Exception as e:
                log(f"[Captcha] {provider} exception: {e}")

            # Brief delay between provider attempts
            if i < len(self.providers) - 1:
                await asyncio.sleep(random.uniform(1, 3))

        log("[Captcha] all providers exhausted")
        return None

    async def _solve_with_provider(self, provider, api_key, sitekey, page_url, action, cdata, timeout, log):
        if provider == "capsolver":
            return await self._solve_capsolver(api_key, sitekey, page_url, action, timeout, log)
        elif provider == "solverify":
            return await self._solve_solverify(api_key, sitekey, page_url, action, cdata, timeout, log)
        elif provider == "2captcha":
            return await self._solve_2captcha(api_key, sitekey, page_url, action, timeout, log)
        else:
            log(f"[Captcha] unknown provider: {provider}")
            return None

    async def _solve_capsolver(self, api_key, sitekey, page_url, action, timeout, log):
        async with _solve_semaphore:
            async with AsyncSession(impersonate="chrome131") as s:
                task = {
                    "clientKey": api_key,
                    "task": {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                }
                if action:
                    task["task"]["metadata"] = {"action": action}

                r = await s.post(f"{CAPSOLVER_URL}/createTask", json=task, timeout=30)
                created = r.json()

                if created.get("errorId", 0) != 0:
                    log(f"[Capsolver] createTask failed: {created.get('errorDescription', created)}")
                    return None

                task_id = created.get("taskId")
                if not task_id:
                    log(f"[Capsolver] no taskId: {created}")
                    return None

                log(f"[Capsolver] task={task_id}")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    r2 = await s.post(
                        f"{CAPSOLVER_URL}/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                        timeout=30,
                    )
                    res = r2.json()
                    if res.get("errorId", 0) != 0:
                        log(f"[Capsolver] error: {res.get('errorDescription')}")
                        return None
                    if res.get("status") == "ready":
                        return res.get("solution", {}).get("token", "")
                log("[Capsolver] timeout")
                return None

    async def _solve_solverify(self, api_key, sitekey, page_url, action, cdata, timeout, log):
        async with _solve_semaphore:
            async with AsyncSession(impersonate="chrome131") as s:
                task = {
                    "clientKey": api_key,
                    "task": {
                        "type": "turnstile",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                        "action": action,
                        "cdata": cdata,
                    },
                }

                r = await s.post(f"{SOLVERIFY_URL}/createTask", json=task, timeout=30)
                created = r.json()

                if not created or not created.get("taskId"):
                    log(f"[Solverify] createTask failed: {created}")
                    return None

                task_id = created["taskId"]
                log(f"[Solverify] task={task_id}")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    r2 = await s.post(
                        f"{SOLVERIFY_URL}/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                        timeout=30,
                    )
                    res = r2.json()
                    if not res:
                        continue
                    if res.get("status") == "completed":
                        return res["solution"]["value"]
                    if res.get("errorId"):
                        log(f"[Solverify] error: {res.get('errorCode')} {res.get('errorDescription')}")
                        return None
                log("[Solverify] timeout")
                return None

    async def _solve_2captcha(self, api_key, sitekey, page_url, action, timeout, log):
        async with _solve_semaphore:
            async with AsyncSession(impersonate="chrome131") as s:
                task = {
                    "clientKey": api_key,
                    "task": {
                        "type": "TurnstileTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                }
                if action:
                    task["task"]["action"] = action

                r = await s.post(f"{TWOCAPTCHA_URL}/createTask", json=task, timeout=30)
                created = r.json()

                if created.get("errorId", 0) != 0:
                    log(f"[2Captcha] createTask failed: {created.get('errorDescription', created)}")
                    return None

                task_id = created.get("taskId")
                if not task_id:
                    log(f"[2Captcha] no taskId: {created}")
                    return None

                log(f"[2Captcha] task={task_id}")
                await asyncio.sleep(8)  # 2Captcha needs initial wait
                deadline = time.time() + timeout
                while time.time() < deadline:
                    r2 = await s.post(
                        f"{TWOCAPTCHA_URL}/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                        timeout=30,
                    )
                    res = r2.json()
                    if res.get("status") == "ready":
                        return res["solution"]["token"]
                    if res.get("errorId", 0) != 0:
                        log(f"[2Captcha] error: {res.get('errorDescription')}")
                        return None
                    await asyncio.sleep(5)
                log("[2Captcha] timeout")
                return None
