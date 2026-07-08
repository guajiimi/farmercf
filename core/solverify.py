"""
cfcaptcha.core.solverify — Multi-provider Turnstile solver client.

Supports:
  - Solverify — createTask/getTaskResult pattern
  - Capsolver — createTask/getTaskResult pattern (similar API)
  - 2Captcha — in.php/res.php pattern (fallback)

Serialized with a semaphore to prevent rate-limiting.
"""

import asyncio
import time
from loguru import logger

from curl_cffi.requests import AsyncSession


# ── Provider URLs ────────────────────────────────────────────
SOLVERIFY_URL = "https://solver.solverify.net"
CAPSOLVER_URL = "https://api.capsolver.com"

# Serialized globally — prevent rate-limiting
_solve_semaphore = asyncio.Semaphore(1)


class CaptchaSolver:
    """Multi-provider Turnstile solver — pure HTTP, no browser."""

    def __init__(self, provider: str = "2captcha", api_key: str = ""):
        self.provider = provider
        self.api_key = api_key

    async def solve_turnstile(
        self,
        sitekey: str,
        page_url: str,
        action: str = "signup",
        cdata: str = "signup",
        timeout: int = 135,
        log=logger.info,
    ) -> str | None:
        """Solve Turnstile. Returns token or None."""
        if not self.api_key:
            log(f"[{self.provider}] no API key configured")
            return None

        if self.provider == "solverify":
            return await self._solve_solverify(sitekey, page_url, action, cdata, timeout, log)
        elif self.provider == "capsolver":
            return await self._solve_capsolver(sitekey, page_url, action, cdata, timeout, log)
        elif self.provider == "2captcha":
            return await self._solve_2captcha(sitekey, page_url, action, timeout, log)
        else:
            log(f"[{self.provider}] unknown provider")
            return None

    async def _solve_solverify(self, sitekey, page_url, action, cdata, timeout, log):
        """Solverify: createTask/getTaskResult pattern."""
        async with _solve_semaphore:
            async with AsyncSession(impersonate="chrome131") as s:
                task_payload = {
                    "clientKey": self.api_key,
                    "task": {
                        "type": "turnstile",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                        "action": action,
                        "cdata": cdata,
                    },
                }

                try:
                    r = await s.post(f"{SOLVERIFY_URL}/createTask", json=task_payload, timeout=30)
                    created = r.json()
                except Exception as e:
                    log(f"[Solverify] createTask error: {e}")
                    return None

                if not created or not created.get("taskId"):
                    log(f"[Solverify] createTask failed: {created}")
                    return None

                task_id = created["taskId"]
                log(f"[Solverify] task created: {task_id}")

                deadline = time.time() + timeout
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    try:
                        r2 = await s.post(
                            f"{SOLVERIFY_URL}/getTaskResult",
                            json={"clientKey": self.api_key, "taskId": task_id},
                            timeout=30,
                        )
                        res = r2.json()
                    except Exception:
                        continue

                    if not res:
                        continue
                    if res.get("status") == "completed":
                        token = res["solution"]["value"]
                        log(f"[Solverify] solved! token_len={len(token)}")
                        return token
                    if res.get("errorId"):
                        log(f"[Solverify] error: {res.get('errorCode')} {res.get('errorDescription')}")
                        return None

                log("[Solverify] timeout")
                return None

    async def _solve_capsolver(self, sitekey, page_url, action, cdata, timeout, log):
        """Capsolver: createTask/getTaskResult pattern."""
        async with _solve_semaphore:
            async with AsyncSession(impersonate="chrome131") as s:
                task_payload = {
                    "clientKey": self.api_key,
                    "task": {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                }
                if action:
                    task_payload["task"]["metadata"] = {"action": action}

                try:
                    r = await s.post(f"{CAPSOLVER_URL}/createTask", json=task_payload, timeout=30)
                    created = r.json()
                except Exception as e:
                    log(f"[Capsolver] createTask error: {e}")
                    return None

                if created.get("errorId", 0) != 0:
                    log(f"[Capsolver] createTask failed: {created.get('errorDescription', created)}")
                    return None

                task_id = created.get("taskId")
                if not task_id:
                    log(f"[Capsolver] no taskId: {created}")
                    return None

                log(f"[Capsolver] task created: {task_id}")

                deadline = time.time() + timeout
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    try:
                        r2 = await s.post(
                            f"{CAPSOLVER_URL}/getTaskResult",
                            json={"clientKey": self.api_key, "taskId": task_id},
                            timeout=30,
                        )
                        res = r2.json()
                    except Exception:
                        continue

                    if res.get("errorId", 0) != 0:
                        log(f"[Capsolver] error: {res.get('errorDescription')}")
                        return None
                    if res.get("status") == "ready":
                        token = res.get("solution", {}).get("token", "")
                        log(f"[Capsolver] solved! token_len={len(token)}")
                        return token

                log("[Capsolver] timeout")
                return None

    async def _solve_2captcha(self, sitekey, page_url, action, timeout, log):
        """2Captcha: createTask/getTaskResult pattern (API v2). Pure HTTP."""

        async with _solve_semaphore:
            async with AsyncSession(impersonate="chrome131") as s:
                task_payload = {
                    "clientKey": self.api_key,
                    "task": {
                        "type": "TurnstileTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                }
                if action:
                    task_payload["task"]["action"] = action

                try:
                    r = await s.post("https://api.2captcha.com/createTask", json=task_payload, timeout=30)
                    created = r.json()
                except Exception as e:
                    log(f"[2Captcha] createTask error: {e}")
                    return None

                if created.get("errorId", 0) != 0:
                    log(f"[2Captcha] createTask failed: {created.get('errorDescription', created)}")
                    return None

                task_id = created.get("taskId")
                if not task_id:
                    log(f"[2Captcha] no taskId: {created}")
                    return None

                log(f"[2Captcha] task created: {task_id}")

                deadline = time.time() + timeout
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    try:
                        r2 = await s.post(
                            "https://api.2captcha.com/getTaskResult",
                            json={"clientKey": self.api_key, "taskId": task_id},
                            timeout=30,
                        )
                        res = r2.json()
                    except Exception:
                        continue

                    if res.get("status") == "ready":
                        token = res["solution"]["token"]
                        log(f"[2Captcha] solved! token_len={len(token)}")
                        return token
                    if res.get("errorId", 0) != 0:
                        log(f"[2Captcha] error: {res.get('errorDescription')}")
                        return None

                log("[2Captcha] timeout")
                return None
