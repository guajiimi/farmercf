"""
cfcaptcha.core.farmer — Pure HTTP Cloudflare Workers AI account farmer.

Flow (reverse-engineered from real signup HAR):
  bootstrap → captcha/challenge → solve Turnstile → user/create →
  persistence/user(emailVerificationRequest) → poll IMAP →
  user/email-verification → accounts → user/tokens → validate

Zero browser dependency. Uses curl_cffi with Chrome TLS fingerprint
impersonation (impersonate="chrome131") to evade CF challenges.

Supports multiple captcha solver backends:
  - Solverify (default) — createTask/getTaskResult pattern
  - 2Captcha — in.php/res.php pattern
"""

import asyncio
import json
import time
import base64
import random
import string
import re
import imaplib
import email as email_lib
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession, RequestsError
from loguru import logger


# ── Constants ────────────────────────────────────────────────
CF_API = "https://dash.cloudflare.com/api/v4"
CF_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"
FALLBACK_SITEKEY = "0x4AAAAAAAJel0iaAR3mgkjp"
STRATUS_COMMIT = "43768e5f0b36b3c6c3c5ed00afa10affa55b38db"

# Workers AI permission group IDs (3 — includes account read)
API_TOKEN_PERMISSIONS = [
    {"id": "644535f4ed854494a59cb289d634b257"},
    {"id": "a92d2450e05d4e7bb7d0a64968f83d11"},
    {"id": "bacc64e0f6c34fc0883a1223f938a104"},
]

HEADERS = {"x-cross-site-security": "dash"}
DEFAULT_TIMEOUT = 45


# ── Proxy helpers ────────────────────────────────────────────
def parse_proxy_line(line: str) -> str | None:
    """Parse proxy.txt line into curl_cffi proxy URL.

    Accepts:
      host:port:user:pass        -> http://user:pass@host:port
      host:port                  -> http://host:port
      http://user:pass@host:port -> pass-through
      socks5://...               -> pass-through
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
    """Load proxies from file."""
    if not proxy_file.exists():
        return []
    proxies = []
    for line in proxy_file.read_text().splitlines():
        p = parse_proxy_line(line)
        if p:
            proxies.append(p)
    return proxies


# ── HTTP helpers ─────────────────────────────────────────────
async def _req(session, method, url, payload=None, extra=None, proxy=None):
    """curl_cffi request with Chrome TLS fingerprint."""
    kwargs = {"timeout": DEFAULT_TIMEOUT}
    if payload is not None:
        kwargs["json"] = payload
        kwargs["headers"] = {**HEADERS, "content-type": "application/json", **(extra or {})}
    elif extra:
        kwargs["headers"] = {**HEADERS, **extra}
    else:
        kwargs["headers"] = HEADERS
    if proxy:
        kwargs["proxy"] = proxy

    for _ in range(3):
        try:
            r = await session.request(method, url, **kwargs)
            try:
                return r.json()
            except (json.JSONDecodeError, ValueError):
                return {"_status": r.status_code, "_text": r.text}
        except RequestsError:
            await asyncio.sleep(2)
    return None


async def get_json(s, url, proxy=None):
    return await _req(s, "GET", url, proxy=proxy)


async def post_json(s, url, payload, extra=None, proxy=None):
    return await _req(s, "POST", url, payload, extra, proxy=proxy)


async def put_json(s, url, payload, proxy=None):
    return await _req(s, "PUT", url, payload, proxy=proxy)


# ── Email/Password generators ────────────────────────────────
def gen_email(domain: str, base: str = "") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase, k=8)) + str(random.randint(100, 999))
    if base and domain == "gmail.com":
        # Gmail plus addressing: base+suffix@gmail.com
        return f"{base}+{suffix}@{domain}"
    return suffix + "@" + domain


def gen_password() -> str:
    return "".join(random.choices(string.ascii_letters, k=10)) + str(random.randint(10, 99)) + "-Aa1!"


def make_legal_stamp(country: str = "id") -> str:
    raw = f"ts:{int(time.time()*1000)}/stratus_commit:{STRATUS_COMMIT}/country:{country}"
    return base64.b64encode(raw.encode()).decode()


# ── IMAP email verification ──────────────────────────────────
async def poll_imap_verification(
    email_addr: str,
    imap_host: str,
    imap_port: int,
    imap_user: str,
    imap_pass: str,
    timeout: int = 240,
    log=logger.info,
) -> str | None:
    """Poll Gmail via IMAP for CF verification link, return token."""
    log(f"[IMAP] polling for {email_addr} (max {timeout}s)...")
    seen = set()
    start = time.time()

    while time.time() - start < timeout:
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            mail.login(imap_user, imap_pass)
            mail.select("INBOX")

            status, messages = mail.search(None, f'(TO "{email_addr}")')
            if status == "OK":
                for num in messages[0].split():
                    if num in seen:
                        continue
                    seen.add(num)

                    status, data = mail.fetch(num, "(RFC822)")
                    if status != "OK":
                        continue

                    msg = email_lib.message_from_bytes(data[0][1])
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                    # CF verification link patterns
                    link = re.search(r"/email-verification\?token=([A-Za-z0-9_\-]+)", body)
                    if link:
                        mail.logout()
                        return link.group(1)
                    tok = re.search(r"[?&]token=([A-Za-z0-9_\-]{40,})", body)
                    if tok:
                        mail.logout()
                        return tok.group(1)

            mail.logout()
        except Exception as e:
            log(f"[IMAP] error: {e}")

        await asyncio.sleep(8)

    log("[IMAP] timeout — no verification email")
    return None


# ── API token verification ───────────────────────────────────
async def verify_token(account_id: str, api_token: str, log=logger.info) -> bool:
    """Validate token by calling Workers AI inference."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/meta/llama-3.2-1b-instruct"
    async with AsyncSession(impersonate="chrome131") as s:
        resp = await post_json(
            s, url, {"prompt": "Say hi in one word"},
            extra={"Authorization": f"Bearer {api_token}"},
        )
    if resp and resp.get("success"):
        log(f"[Verify] WORKS: {resp['result'].get('response', '')[:40]}")
        return True
    log(f"[Verify] failed: {resp.get('errors') if resp else 'no response'}")
    return False


# ── 9router DB injection ─────────────────────────────────────
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
    """Pure HTTP Cloudflare account farmer.

    One session = one account. Uses curl_cffi with Chrome TLS
    fingerprint impersonation — zero browser dependency.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.domain = cfg.get("farm_domain", "")
        self.captcha_key = cfg.get("solverify_api_key", "") or cfg.get("twocaptcha_api_key", "")
        self.captcha_provider = cfg.get("captcha_provider", "solverify")
        self.proxy_file = Path(cfg.get("proxy_file", ""))
        self.proxies = load_proxies(self.proxy_file) if self.proxy_file else []
        self.accounts_file = Path(cfg.get("accounts_file", "accounts.json"))

        # IMAP config
        self.imap_host = cfg.get("imap_host", "")
        self.imap_port = int(cfg.get("imap_port", 993))
        self.imap_user = cfg.get("imap_user", "")
        self.imap_pass = cfg.get("imap_pass", "")

        # 9router injection
        self.inject_9router = cfg.get("inject_9router", False)

        # Solver
        self._solver = None
        provider = cfg.get("captcha_provider", "2captcha")
        if provider == "solverify" and cfg.get("solverify_api_key"):
            from .solverify import CaptchaSolver
            self._solver = CaptchaSolver("solverify", cfg["solverify_api_key"])
        elif provider == "capsolver" and cfg.get("capsolver_api_key"):
            from .solverify import CaptchaSolver
            self._solver = CaptchaSolver("capsolver", cfg["capsolver_api_key"])
        elif provider == "2captcha" and cfg.get("twocaptcha_api_key"):
            from .solverify import CaptchaSolver
            self._solver = CaptchaSolver("2captcha", cfg["twocaptcha_api_key"])

    def _random_proxy(self) -> str | None:
        if not self.proxies:
            return None
        return random.choice(self.proxies)

    async def farm_one(self, log=logger.info) -> dict | None:
        """Farm one Cloudflare account. Full flow via pure HTTP.

        Returns account dict or None on failure.
        """
        session = None
        email_addr = password = None
        created = None
        max_retries = self.cfg.get("farm_create_retries", 6)

        # ── Phase 1: Create account (retry with fresh sessions) ──
        for attempt in range(1, max_retries + 1):
            s = AsyncSession(impersonate="chrome131", headers=HEADERS)
            proxy = self._random_proxy()
            if proxy:
                log(f"[Create {attempt}/{max_retries}] proxy={proxy[:40]}...")
            else:
                log(f"[Create {attempt}/{max_retries}] no proxy")

            try:
                # Step 1: Bootstrap
                boot = await get_json(s, f"{CF_API}/system/bootstrap", proxy=proxy)
                if not (boot and boot.get("success")):
                    raise RuntimeError(f"bootstrap failed: {boot}")
                sec_token = boot["result"]["data"]["data"]["security_token"]
                country = boot["result"]["data"].get("ip_country", "id").lower()

                # Step 2: Get sitekey from challenge
                chal = await get_json(s, f"{CF_API}/captcha/challenge?context=signup", proxy=proxy)
                sitekey = (
                    chal["result"]["key"]
                    if chal and chal.get("success") and chal.get("result")
                    else FALLBACK_SITEKEY
                )
                log(f"[Create] country={country} sitekey={sitekey[:20]}...")

                # Step 3: Solve Turnstile
                token = await self._solve_captcha(sitekey, log)
                if not token:
                    raise RuntimeError("captcha solve failed")

                # Step 4: Create user
                email_addr = gen_email(self.domain, self.cfg.get("farm_email_base", ""))
                password = gen_password()
                resp = await post_json(
                    s, f"{CF_API}/user/create",
                    {
                        "email": email_addr,
                        "password": password,
                        "mrk_optin": True,
                        "security_token": sec_token,
                        "method": "Onboarding: New_v2",
                        "locale": "en-US",
                        "legal_stamp": make_legal_stamp(country),
                        "opt_ins": {},
                        "mrktCheckboxDisplayed": False,
                        "hCaptchaDisplayed": False,
                        "cf_challenge_response": token,
                    },
                    extra={"Referer": "https://dash.cloudflare.com/"},
                    proxy=proxy,
                )

                if resp and resp.get("success"):
                    created = resp["result"]
                    session = s
                    log(f"[Create] SUCCESS user_id={created['id']} email={email_addr}")
                    break

                log(f"[Create] failed: {resp.get('errors') if resp else resp}")

            except Exception as e:
                log(f"[Create] error: {e}")

            await s.close()
            await asyncio.sleep(random.randint(4, 10))

        if not created:
            return None

        # ── Phase 2: Verify + Token ──
        try:
            # Trigger verification email
            await post_json(session, f"{CF_API}/persistence/user", {"emailVerificationRequest": "welcome"})

            # Poll IMAP for verification token
            verified = False
            vtok = await poll_imap_verification(
                email_addr,
                self.imap_host, self.imap_port,
                self.imap_user, self.imap_pass,
                timeout=self.cfg.get("imap_timeout", 240),
                log=log,
            )
            if vtok:
                vr = await put_json(session, f"{CF_API}/user/email-verification", {"token": vtok})
                verified = bool(vr and vr.get("success"))
            log(f"[Verify] email_verified={verified}")

            # Get account ID
            accts = await get_json(session, f"{CF_API}/accounts?per_page=100")
            account_id = (
                accts["result"][0]["id"]
                if accts and accts.get("success") and accts.get("result")
                else None
            )
            log(f"[Account] id={account_id}")

            # Create API token (only after email verification)
            api_token = None
            if verified and account_id:
                tk = await post_json(session, f"{CF_API}/user/tokens", {
                    "name": "workers-ai",
                    "condition": {},
                    "policies": [{
                        "effect": "allow",
                        "resources": {f"com.cloudflare.api.account.{account_id}": "*"},
                        "permission_groups": API_TOKEN_PERMISSIONS,
                    }],
                })
                if tk and tk.get("success"):
                    api_token = tk["result"]["value"]
                    log(f"[Token] {api_token[:20]}...")
                    log("[Token] waiting 12s for propagation...")
                    await asyncio.sleep(12)
                else:
                    log(f"[Token] failed: {tk.get('errors') if tk else tk}")
            elif not verified:
                log("[Token] skipped — email not verified")

        finally:
            await session.close()

        # ── Phase 3: Validate + Save ──
        status = "created"
        if api_token and account_id:
            status = "active" if await verify_token(account_id, api_token, log=log) else "token_unverified"
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
            "status": status,
        }

        # Save to accounts.json
        self._save_account(acc)

        # Inject to 9router DB if enabled
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
                await asyncio.sleep(random.uniform(2, 5))

        return results

    async def _solve_captcha(self, sitekey: str, log=logger.info) -> str | None:
        """Solve Turnstile using configured provider."""
        if self._solver:
            return await self._solver.solve_turnstile(
                sitekey=sitekey,
                page_url=CF_SIGNUP_URL,
                log=log,
            )
        else:
            log("[Captcha] no solver configured")
            return None

    def _load_accounts(self) -> list[dict]:
        if self.accounts_file.exists():
            return json.loads(self.accounts_file.read_text())
        return []

    def _save_account(self, acc: dict):
        accs = self._load_accounts()
        accs.append(acc)
        self.accounts_file.write_text(json.dumps(accs, indent=2))
