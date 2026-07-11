"""
farmercf.core.farmer — Pure HTTP Cloudflare Workers AI account farmer.

Flow (reverse-engineered from real signup HAR):
  bootstrap → captcha/challenge → solve Turnstile → user/create →
  persistence/user(emailVerificationRequest) → poll IMAP →
  user/email-verification → accounts → user/tokens → validate

Zero browser dependency. curl_cffi with random Chrome TLS fingerprint
per registration for anti-detection.

Anti-detection features:
  - Random TLS fingerprint (10 Chrome versions)
  - Random locale/Accept-Language (12 locales)
  - Random legal_stamp country (12 countries)
  - Random email domain (5 domains)
  - Random password pattern (5 patterns)
  - Human-like delays between steps
  - Proxy rotation with auto-fallback

Supports captcha solver providers with auto-fallback:
  CapSolver → Solverify → 2Captcha
"""

import asyncio
import json
import time
import base64
import random
import string
import re
import uuid
import sqlite3
import fcntl
from pathlib import Path
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession, RequestsError
from loguru import logger

from .constants import (
    CF_API, CF_SIGNUP_URL, FALLBACK_SITEKEY, STRATUS_COMMIT,
    API_TOKEN_PERMISSIONS, HEADERS, DEFAULT_TIMEOUT,
    TLS_FINGERPRINTS, LOCALE_POOL, LEGAL_COUNTRIES, EMAIL_DOMAINS,
    PASSWORD_PATTERNS,
)
from .captcha import CaptchaSolver
from .email import poll_imap_verification


# ── Proxy helpers ────────────────────────────────────────────
def parse_proxy_line(line: str) -> str | None:
    """Parse proxy.txt line into curl_cffi proxy URL.

    Accepts:
      host:port:user:pass  -> http://user:pass@host:port
      host:port            -> http://host:port
      http://user:pass@host:port -> pass-through
      socks5://...         -> pass-through
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{line}"
    return None


def load_proxies(proxy_file: Path) -> list[str]:
    if not proxy_file.exists():
        return []
    proxies = []
    for line in proxy_file.read_text().splitlines():
        p = parse_proxy_line(line)
        if p:
            proxies.append(p)
    return proxies


# ── Anti-Detection: Random Identity Generator ─────────────────
class FakeIdentity:
    """Generate unique fingerprint per registration."""

    def __init__(self):
        self.tls = random.choice(TLS_FINGERPRINTS)
        self.locale, self.accept_lang = random.choice(LOCALE_POOL)
        self.country = random.choice(LEGAL_COUNTRIES)
        self.domain = random.choice(EMAIL_DOMAINS)
        self.pwd_pattern = random.choice(PASSWORD_PATTERNS)
        self.platform = random.choice(["Windows", "macOS", "Linux", "Chrome OS"])

    def gen_email(self, base: str = "") -> str:
        suffix = "".join(random.choices(string.ascii_lowercase, k=8)) + str(random.randint(100, 999))
        if base and self.domain == "gmail.com":
            return f"{base}+{suffix}@{self.domain}"
        return f"{suffix}@{self.domain}"

    def gen_password(self) -> str:
        letters = "".join(random.choices(string.ascii_letters, k=14))
        return self.pwd_pattern(letters)

    def make_legal_stamp(self) -> str:
        raw = f"ts:{int(time.time()*1000)}/stratus_commit:{STRATUS_COMMIT}/country:{self.country}"
        return base64.b64encode(raw.encode()).decode()

    def make_headers(self) -> dict:
        """Generate unique headers for this identity."""
        return {
            **HEADERS,
            "Accept-Language": self.accept_lang,
            "sec-ch-ua-platform": f'"{self.platform}"',
        }

    def describe(self) -> str:
        return f"tls={self.tls} locale={self.locale} country={self.country} domain={self.domain} platform={self.platform}"


# ── HTTP helpers ─────────────────────────────────────────────
async def _req(session, method, url, payload=None, extra=None, proxy=None, headers=None):
    """curl_cffi request with Chrome TLS fingerprint."""
    base_headers = headers or HEADERS
    kwargs = {"timeout": DEFAULT_TIMEOUT}
    if payload is not None:
        kwargs["json"] = payload
        kwargs["headers"] = {**base_headers, "content-type": "application/json", **(extra or {})}
    elif extra:
        kwargs["headers"] = {**base_headers, **extra}
    else:
        kwargs["headers"] = base_headers
    if proxy:
        kwargs["proxy"] = proxy

    for attempt in range(3):
        try:
            r = await session.request(method, url, **kwargs)
            try:
                return r.json()
            except (json.JSONDecodeError, ValueError):
                return {"_status": r.status_code, "_text": r.text}
        except RequestsError as e:
            if attempt < 2:
                await asyncio.sleep(2)
    return None


async def _req_with_headers(session, method, url, payload=None, extra=None, proxy=None, headers=None):
    """Same as _req but returns (json, response_headers) tuple."""
    base_headers = headers or HEADERS
    kwargs = {"timeout": DEFAULT_TIMEOUT}
    if payload is not None:
        kwargs["json"] = payload
        kwargs["headers"] = {**base_headers, "content-type": "application/json", **(extra or {})}
    elif extra:
        kwargs["headers"] = {**base_headers, **extra}
    else:
        kwargs["headers"] = base_headers
    if proxy:
        kwargs["proxy"] = proxy

    for attempt in range(3):
        try:
            r = await session.request(method, url, **kwargs)
            try:
                return r.json(), dict(r.headers)
            except (json.JSONDecodeError, ValueError):
                return {"_status": r.status_code, "_text": r.text}, dict(r.headers)
        except RequestsError as e:
            if attempt < 2:
                await asyncio.sleep(2)
    return None, {}


async def get_json(s, url, proxy=None, headers=None):
    return await _req(s, "GET", url, proxy=proxy, headers=headers)

async def post_json(s, url, payload, extra=None, proxy=None, headers=None):
    return await _req(s, "POST", url, payload, extra, proxy=proxy, headers=headers)

async def put_json(s, url, payload, proxy=None, headers=None):
    return await _req(s, "PUT", url, payload, proxy=proxy, headers=headers)


# ── API token verification ───────────────────────────────────
async def verify_token(account_id: str, api_token: str, log=logger.info) -> tuple[bool, float]:
    """Validate token by calling Workers AI inference.

    Returns (valid: bool, neurons_used: float).
    """
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/meta/llama-3.2-1b-instruct"
    neurons = 0.0
    async with AsyncSession(impersonate="chrome131") as s:
        resp, headers = await _req_with_headers(
            s, "POST", url, {"prompt": "Say hi in one word"},
            extra={"Authorization": f"Bearer {api_token}"},
        )
        if not resp:
            log("[Verify] no response")
            return False, 0.0
        raw = headers.get("cf-ai-neurons") or headers.get("Cf-Ai-Neurons", "0")
        try:
            neurons = float(raw)
        except (ValueError, TypeError):
            neurons = 0.0
    if resp.get("success"):
        log(f"[Verify] WORKS: {resp.get('result', {}).get('response', '')[:40]} | neurons={neurons}")
        return True, neurons
    log(f"[Verify] failed: {resp.get('errors', resp)}")
    return False, 0.0


# ── 9router DB injection ──────────────────────────────────────
ROUTER_DB = Path.home() / ".9router" / "db" / "data.sqlite"
ROUTER_CONNECTION_NAME = "CfcFarmer"


def _is_transient_sqlite_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in ("malformed", "database is locked", "database is busy", "disk i/o"))


def inject_to_9router(api_token: str, account_id: str) -> int | None:
    """Inject into 9router providerConnections. Returns priority or None."""
    if not ROUTER_DB.exists():
        return None
    if not api_token or not account_id:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    for attempt in range(1, 6):
        try:
            conn = sqlite3.connect(str(ROUTER_DB), timeout=30.0)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider=?",
                ("cloudflare-ai",),
            )
            next_priority = (cur.fetchone()[0] or 0) + 1

            data = json.dumps({
                "apiKey": api_token,
                "testStatus": "active",
                "providerSpecificData": {
                    "accountId": account_id,
                    "connectionProxyEnabled": False,
                    "connectionProxyUrl": "",
                    "connectionNoProxy": "",
                },
                "backoffLevel": 0,
            })

            conn_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO providerConnections
                   (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (conn_id, "cloudflare-ai", "apikey",
                 f"{ROUTER_CONNECTION_NAME} #{next_priority}", None,
                 next_priority, data, now, now),
            )
            conn.commit()
            conn.close()
            return next_priority
        except sqlite3.IntegrityError:
            return None
        except sqlite3.Error as e:
            if _is_transient_sqlite_error(e) and attempt < 5:
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            return None
    return None


# ── Core farmer ──────────────────────────────────────────────
class AccountFarmer:
    """Pure HTTP Cloudflare account farmer with anti-detection.

    One session = one account. Each registration gets a unique
    TLS fingerprint, locale, country, email domain, and password pattern.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proxy_file = Path(cfg.get("proxy_file", ""))
        self.proxies = load_proxies(self.proxy_file) if self.proxy_file else []
        self.accounts_file = Path(cfg.get("accounts_file", "accounts.json"))

        # IMAP config
        self.imap_host = cfg.get("imap_host", "")
        self.imap_port = int(cfg.get("imap_port", 993))
        self.imap_user = cfg.get("imap_user", "")
        self.imap_pass = cfg.get("imap_pass", "")

        # Email domains (can override defaults)
        self.email_domains = cfg.get("email_domains", [])
        self.email_base = cfg.get("farm_email_base", "")

        # 9router injection
        self.inject_9router = cfg.get("inject_9router", False)

        # Captcha solver — unified with auto-fallback
        self._solver = CaptchaSolver(
            providers=[
                ("capsolver", cfg.get("capsolver_api_key", "")),
                ("solverify", cfg.get("solverify_api_key", "")),
                ("2captcha", cfg.get("twocaptcha_api_key", "")),
            ]
        )

    def _random_proxy(self) -> str | None:
        if not self.proxies:
            return None
        return random.choice(self.proxies)

    async def farm_one(self, log=logger.info) -> dict | None:
        """Farm one Cloudflare account. Full flow via pure HTTP.

        Each call generates a unique identity (TLS, locale, country, domain, password).
        Returns account dict or None on failure.
        """
        # Generate unique identity for this registration
        identity = FakeIdentity()
        if self.email_domains:
            identity.domain = random.choice(self.email_domains)

        log(f"[Farm] identity: {identity.describe()}")

        created = None
        email_addr = None
        password = None
        session = None
        max_retries = self.cfg.get("farm_create_retries", 6)

        # ── Phase 1: Create account (retry with fresh identity each time) ──
        for attempt in range(1, max_retries + 1):
            # Fresh identity per retry attempt too
            if attempt > 1:
                identity = FakeIdentity()
                if self.email_domains:
                    identity.domain = random.choice(self.email_domains)
                log(f"[Create {attempt}/{max_retries}] new identity: {identity.describe()}")

            s = AsyncSession(impersonate=identity.tls, headers=identity.make_headers())
            proxy = self._random_proxy()

            try:
                # Step 1: Bootstrap
                boot = await get_json(s, f"{CF_API}/system/bootstrap", proxy=proxy)
                if not (boot and boot.get("success")):
                    raise RuntimeError(f"bootstrap failed: {boot}")
                sec_token = boot["result"]["data"]["data"]["security_token"]
                boot_country = boot["result"]["data"].get("ip_country", "id").lower()

                # Use boot country if available, else identity country
                country = boot_country if boot_country in LEGAL_COUNTRIES else identity.country

                # Step 2: Get sitekey from challenge
                chal = await get_json(
                    s, f"{CF_API}/captcha/challenge?context=signup",
                    proxy=proxy, headers=identity.make_headers()
                )
                sitekey = (
                    chal["result"]["key"]
                    if chal and chal.get("success") and chal.get("result")
                    else FALLBACK_SITEKEY
                )
                log(f"[Create] country={country} sitekey={sitekey[:20]}...")

                # Step 3: Solve Turnstile (with auto-fallback)
                token = await self._solve_captcha(sitekey, log)
                if not token:
                    raise RuntimeError("captcha solve failed")

                # Human-like delay
                await asyncio.sleep(random.uniform(2, 5))

                # Step 4: Create user
                email_addr = identity.gen_email(self.email_base)
                password = identity.gen_password()
                legal_stamp = identity.make_legal_stamp()

                resp = await post_json(
                    s, f"{CF_API}/user/create",
                    {
                        "email": email_addr,
                        "password": password,
                        "mrk_optin": True,
                        "security_token": sec_token,
                        "method": "Onboarding: New_v2",
                        "locale": identity.locale,
                        "legal_stamp": legal_stamp,
                        "opt_ins": {},
                        "mrktCheckboxDisplayed": False,
                        "hCaptchaDisplayed": False,
                        "cf_challenge_response": token,
                    },
                    extra={"Referer": "https://dash.cloudflare.com/"},
                    proxy=proxy,
                    headers=identity.make_headers(),
                )

                if resp and resp.get("success"):
                    created = resp["result"]
                    session = s  # Keep session alive for Phase 2
                    log(f"[Create] SUCCESS user_id={created['id']} email={email_addr}")
                    break

                err = resp.get("errors", resp) if resp else "no response"
                log(f"[Create] failed: {err}")

            except Exception as e:
                log(f"[Create] error: {e}")

            # Cleanup failed session
            await s.close()
            session = None

            # Human-like backoff
            delay = random.uniform(4, 10)
            log(f"[Create] retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)

        if not created or not session:
            return None

        # ── Phase 2: Verify + Token ──
        account_id = None
        api_token = None
        verified = False

        try:
            # Trigger verification email
            await post_json(session, f"{CF_API}/persistence/user",
                            {"emailVerificationRequest": "welcome"},
                            headers=identity.make_headers())

            # Poll IMAP for verification token
            vtok = await poll_imap_verification(
                email_addr,
                self.imap_host, self.imap_port,
                self.imap_user, self.imap_pass,
                timeout=self.cfg.get("imap_timeout", 240),
                log=log,
            )
            if vtok:
                vr = await put_json(session, f"{CF_API}/user/email-verification",
                                    {"token": vtok},
                                    headers=identity.make_headers())
                verified = bool(vr and vr.get("success"))
            log(f"[Verify] email_verified={verified}")

            # Get account ID
            accts = await get_json(session, f"{CF_API}/accounts?per_page=100",
                                   headers=identity.make_headers())
            if accts and accts.get("success") and accts.get("result"):
                account_id = accts["result"][0]["id"]
            log(f"[Account] id={account_id}")

            # Create API token with 5 verified permissions
            if verified and account_id:
                # Retry token creation up to 3 times
                for tk_attempt in range(1, 4):
                    tk = await post_json(session, f"{CF_API}/user/tokens", {
                        "name": f"workers-ai-{tk_attempt}",
                        "condition": {},
                        "policies": [{
                            "effect": "allow",
                            "resources": {f"com.cloudflare.api.account.{account_id}": "*"},
                            "permission_groups": API_TOKEN_PERMISSIONS,
                        }],
                    }, headers=identity.make_headers())

                    if tk and tk.get("success"):
                        api_token = tk["result"]["value"]
                        log(f"[Token] created (attempt {tk_attempt}): {api_token[:20]}...")
                        log("[Token] waiting 12s for propagation...")
                        await asyncio.sleep(12)
                        break
                    else:
                        log(f"[Token] attempt {tk_attempt} failed: {tk.get('errors') if tk else tk}")
                        if tk_attempt < 3:
                            await asyncio.sleep(5)
            elif not verified:
                log("[Token] skipped — email not verified")

        finally:
            # Always close session
            if session:
                await session.close()

        # ── Phase 3: Validate + Save ──
        status = "created"
        neurons_used = 0.0
        if api_token and account_id:
            valid, neurons_used = await verify_token(account_id, api_token, log=log)
            status = "active" if valid else "token_unverified"
        elif verified:
            status = "verified_no_token"

        acc = {
            "email": email_addr,
            "password": password,
            "user_id": created["id"],
            "account_id": account_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "email_verified": verified,
            "api_token": api_token,
            "neurons_quota": 10000,
            "neurons_used_verify": round(neurons_used, 2),
            "status": status,
            "identity": {
                "tls": identity.tls,
                "locale": identity.locale,
                "country": identity.country,
                "domain": identity.domain,
                "platform": identity.platform,
            },
        }

        self._save_account(acc)

        if self.inject_9router and api_token and account_id:
            pr = inject_to_9router(api_token, account_id)
            if pr:
                log(f"[9router] injected connection #{pr}")

        return acc

    async def farm_batch(self, count: int, log=logger.info) -> dict:
        """Farm multiple accounts sequentially.

        Returns summary: {total, active, partial, failed, accounts}
        """
        results = {"total": count, "active": 0, "partial": 0, "failed": 0, "accounts": []}

        for i in range(count):
            log(f"[Batch {i+1}/{count}] starting...")
            try:
                acc = await self.farm_one(log=log)
            except Exception as e:
                log(f"[Batch {i+1}] EXCEPTION: {e}")
                results["failed"] += 1
                continue

            if acc and acc.get("status") == "active":
                results["active"] += 1
                log(f"[Batch {i+1}] DONE — {acc['email']} | {acc['account_id']}")
            elif acc:
                results["partial"] += 1
                log(f"[Batch {i+1}] PARTIAL — {acc['email']} | {acc['status']}")
            else:
                results["failed"] += 1
                log(f"[Batch {i+1}] FAIL")

            if acc:
                results["accounts"].append(acc)

            if i < count - 1:
                delay = random.uniform(3, 8)
                log(f"[Batch] waiting {delay:.1f}s...")
                await asyncio.sleep(delay)

        return results

    async def _solve_captcha(self, sitekey: str, log=logger.info) -> str | None:
        """Solve Turnstile using configured providers with auto-fallback."""
        return await self._solver.solve_turnstile(
            sitekey=sitekey,
            page_url=CF_SIGNUP_URL,
            log=log,
        )

    def _load_accounts(self) -> list[dict]:
        if self.accounts_file.exists():
            try:
                return json.loads(self.accounts_file.read_text())
            except (json.JSONDecodeError, ValueError):
                return []
        return []

    def _save_account(self, acc: dict):
        """Thread-safe save with file lock (prevents race condition)."""
        with open(self.accounts_file, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                content = f.read()
                accs = json.loads(content) if content.strip() else []
            except (json.JSONDecodeError, ValueError):
                accs = []
            accs.append(acc)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(accs, indent=2))
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
