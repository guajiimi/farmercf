"""
farmercf.core.email — Multi-provider email verification for Cloudflare signup.

Same pattern as mimogue: .env-driven, multiple disposable email providers.

Providers:
  imap       — Custom domains forwarding to Gmail IMAP (catch-all)
  generator  — generator.email (60+ rotating domains, free)
  tempmail   — api.tempmail.lol (auto-assigned domains)
  emailfake  — emailfake.com (persistent session, ~39 domains)
  mix        — Random mix of above (except imap)

CF sends verification LINKS with token (not OTP codes).
This module extracts the token from /email-verification?token=...

Config via .env:
  EMAIL_PROVIDER=tempmail
  IMAP_USER=you@gmail.com
  IMAP_PASS=your-app-password
  DOMAINS=yourdomain1.com,yourdomain2.com  (for imap mode, catch-all)
"""

import os
import re
import time
import json
import asyncio
import imaplib
import email as email_lib
import hashlib
from loguru import logger

# ── Config from .env ──────────────────────────────────────────
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "tempmail").lower()
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASS = os.environ.get("IMAP_PASS", "")
IMAP_TIMEOUT = int(os.environ.get("IMAP_TIMEOUT", "240"))
DOMAINS = [d.strip() for d in os.environ.get("DOMAINS", "").split(",") if d.strip()]

# ── Provider state (thread-safe for asyncio) ──────────────────
_tempmail_tokens: dict[str, str] = {}  # email → inbox token
_emailfake_sessions: dict[str, dict] = {}  # email → {session, token, last_hash}
_email_provider_map: dict[str, str] = {}  # email → provider name
_checked_emails: set[str] = set()  # dedup

_MIX_PROVIDERS = ["tempmail", "generator", "emailfake"]


# ── CF verification link patterns ─────────────────────────────
CF_TOKEN_PATTERNS = [
    re.compile(r"/email-verification\?token=([A-Za-z0-9_\-]+)"),
    re.compile(r"[?&]token=([A-Za-z0-9_\-]{40,})"),
    re.compile(r"verify[_-]?token=([A-Za-z0-9_\-]{20,})"),
]


def _extract_cf_token(text: str) -> str | None:
    """Extract Cloudflare verification token from email body."""
    for pattern in CF_TOKEN_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def _clean_html(text: str) -> str:
    """Strip HTML tags for text extraction."""
    text = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    return text


# ═══════════════════════════════════════════════════════════════
#  Provider: IMAP (custom domains / Gmail catch-all)
# ═══════════════════════════════════════════════════════════════

async def generate_imap_email(exclude_domain: str = None) -> str:
    """Generate email using custom IMAP domains (catch-all forwarding)."""
    import random
    pool = DOMAINS
    if not pool:
        raise ValueError("IMAP mode: no DOMAINS configured in .env")
    available = [d for d in pool if d != exclude_domain]
    if not available:
        available = pool
    domain = random.choice(available)
    suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=8)) + str(random.randint(100, 999))
    addr = f"{suffix}@{domain}"
    _email_provider_map[addr] = "imap"
    return addr


async def poll_imap_verification(
    email_addr: str,
    timeout: int = None,
    log=logger.info,
) -> str | None:
    """Poll IMAP for CF verification email. Searches by TO + FROM fallback."""
    timeout = timeout or IMAP_TIMEOUT
    log(f"[IMAP] polling for {email_addr} (max {timeout}s)...")
    seen = set()
    start = time.time()

    while time.time() - start < timeout:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(IMAP_USER, IMAP_PASS)
            mail.select("INBOX")

            # Primary: search by TO header
            status, messages = mail.search(None, f'(TO "{email_addr}")')
            if status != "OK" or not messages[0]:
                # Fallback: search by FROM cloudflare
                status, messages = mail.search(None, '(FROM "cloudflare.com")')

            if status == "OK" and messages[0]:
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
                            ct = part.get_content_type()
                            if ct in ("text/plain", "text/html"):
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                    token = _extract_cf_token(body)
                    if token:
                        mail.logout()
                        log(f"[IMAP] token found for {email_addr}")
                        return token

            mail.logout()
        except Exception as e:
            log(f"[IMAP] error: {e}")

        await asyncio.sleep(8)

    log(f"[IMAP] timeout — no verification email for {email_addr}")
    return None


# ═══════════════════════════════════════════════════════════════
#  Provider: tempmail.lol
# ═══════════════════════════════════════════════════════════════

async def generate_tempmail_email(exclude_domain: str = None) -> str:
    """Generate email via tempmail.lol v2 API."""
    import requests as _r

    for attempt in range(5):
        try:
            r = await asyncio.to_thread(
                _r.post,
                "https://api.tempmail.lol/v2/inbox/create",
                json={},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except Exception as e:
            await asyncio.sleep(2 ** attempt)
            continue

        if r.status_code == 429:
            await asyncio.sleep(2 ** attempt + 1)
            continue
        if r.status_code not in (200, 201):
            raise ValueError(f"tempmail.lol returned {r.status_code}")

        data = r.json()
        addr = data.get("address")
        token = data.get("token")
        if not addr or not token:
            raise ValueError(f"tempmail.lol invalid: {data}")
        _tempmail_tokens[addr] = token
        _email_provider_map[addr] = "tempmail"
        logger.info(f"[tempmail] {addr}")
        return addr

    raise ValueError("tempmail.lol failed after 5 retries")


async def poll_tempmail_email(email_addr: str, timeout: int = 240, log=logger.info) -> str | None:
    """Poll tempmail.lol v2 inbox for CF verification token."""
    import requests as _r

    token = _tempmail_tokens.get(email_addr)
    if not token:
        log(f"[tempmail] no token for {email_addr}")
        return None

    start = time.time()
    while time.time() - start < timeout:
        try:
            r = await asyncio.to_thread(
                _r.get,
                f"https://api.tempmail.lol/v2/inbox?token={token}",
                timeout=12,
            )
            if r.status_code == 429:
                await asyncio.sleep(3)
                continue
            if r.status_code != 200:
                await asyncio.sleep(5)
                continue

            data = r.json()
            if data.get("expired"):
                log(f"[tempmail] inbox expired for {email_addr}")
                return None

            emails = data.get("emails", [])
            for msg in emails:
                raw = msg.get("body", "") + " " + msg.get("html", "")
                content_hash = hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()
                if content_hash in _checked_emails:
                    continue
                _checked_emails.add(content_hash)

                # Check if from Cloudflare
                sender = (msg.get("sender", "") + msg.get("from", "")).lower()
                if "cloudflare" not in sender and "noreply" not in sender:
                    continue

                text = _clean_html(raw)
                cf_token = _extract_cf_token(text)
                if cf_token:
                    log(f"[tempmail] CF token found for {email_addr}")
                    return cf_token
        except Exception as e:
            log(f"[tempmail] poll error: {e}")

        await asyncio.sleep(8)

    log(f"[tempmail] timeout for {email_addr}")
    return None


# ═══════════════════════════════════════════════════════════════
#  Provider: generator.email
# ═══════════════════════════════════════════════════════════════

_GENERATOR_FALLBACK_DOMAINS = [
    "lellol.dev", "neosstudy.work", "reestore.site", "vexaluno.xyz",
    "nabomail.com", "shortweb.live", "aircourriel.com",
    "tools-capcut.com", "cloudyourfast.net", "acqq.dev", "xsnipersquad.com",
    "fboxmail.com", "contactpage.online", "afterjune.site", "kenari.online",
]

_generator_domains: list[str] = []


def _fetch_generator_domains():
    """Fetch live domain list from generator.email."""
    global _generator_domains
    import requests as _r
    try:
        r = _r.get(
            "https://generator.email/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=12,
        )
        if r.status_code == 200:
            found = set(re.findall(
                r'"([a-z0-9-]+\.(?:com|net|org|io|me|co|info|biz|site|email|host|online|top|dev|xyz|click|cloud|shop|tech|space|fun|icu|work|live|world|life|store|pro|digital|zone|club|pw|cc|ml|ga|cf|gq|tk))"',
                r.text,
            ))
            found = {d for d in found if len(d) > 5 and "." in d}
            if found:
                _generator_domains = sorted(found)
                logger.info(f"[generator] {len(_generator_domains)} domains scraped")
                return
    except Exception as e:
        logger.warning(f"[generator] scrape failed: {e}")
    _generator_domains = list(_GENERATOR_FALLBACK_DOMAINS)
    logger.warning(f"[generator] using {len(_generator_domains)} fallback domains")


async def generate_generator_email(exclude_domain: str = None) -> str:
    """Generate email using generator.email domains."""
    import random
    if not _generator_domains:
        await asyncio.to_thread(_fetch_generator_domains)

    pool = _generator_domains
    available = [d for d in pool if d != exclude_domain]
    if not available:
        available = pool
    domain = random.choice(available)
    first = random.choice("abcdefghijklmnopqrstuvwxyz")
    suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=8)) + str(random.randint(100, 999))
    addr = f"{suffix}@{domain}"
    _email_provider_map[addr] = "generator"
    return addr


async def poll_generator_email(email_addr: str, timeout: int = 240, log=logger.info) -> str | None:
    """Poll generator.email inbox for CF verification token."""
    import requests as _r

    username, domain = email_addr.split("@")
    start = time.time()

    while time.time() - start < timeout:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Cookie": f'embx=[%22{email_addr}%22]; sign={email_addr}; surl={domain}/{username}',
            }
            r = await asyncio.to_thread(
                _r.get,
                f"https://generator.email/{domain}/{username}",
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                if "cloudflare" not in r.text.lower():
                    await asyncio.sleep(8)
                    continue
                text = _clean_html(r.text)
                cf_token = _extract_cf_token(text)
                if cf_token:
                    log(f"[generator] CF token found for {email_addr}")
                    return cf_token
        except Exception as e:
            log(f"[generator] poll error: {e}")

        await asyncio.sleep(8)

    log(f"[generator] timeout for {email_addr}")
    return None


# ═══════════════════════════════════════════════════════════════
#  Provider: emailfake.com
# ═══════════════════════════════════════════════════════════════

_emailfake_domains: list[str] = []


def _fetch_emailfake_domains():
    """Fetch live domain list from emailfake.com."""
    global _emailfake_domains
    import requests as _r
    try:
        r = _r.get("https://emailfake.com/", timeout=12,
                    headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            found = re.findall(r'data-domain="([^"]+)"', r.text)
            if not found:
                found = re.findall(r'>([a-z0-9-]+\.[a-z]{2,})<', r.text)
            if found:
                _emailfake_domains = list(set(found))
                logger.info(f"[emailfake] {len(_emailfake_domains)} domains")
                return
    except Exception as e:
        logger.warning(f"[emailfake] scrape failed: {e}")
    _emailfake_domains = ["fexbox.org", "tenvil.com", "kennedy.orangotan.app"]
    logger.warning(f"[emailfake] using {len(_emailfake_domains)} fallback domains")


async def generate_emailfake_email(exclude_domain: str = None) -> str:
    """Generate email on emailfake.com with persistent session."""
    import random
    import requests as _r

    if not _emailfake_domains:
        await asyncio.to_thread(_fetch_emailfake_domains)

    pool = _emailfake_domains
    available = [d for d in pool if d != exclude_domain]
    if not available:
        available = pool
    domain = random.choice(available)
    fmt = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=8)) + str(random.randint(100, 999))
    addr = f"{fmt}@{domain}"

    try:
        session = _r.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://emailfake.com",
            "Origin": "https://emailfake.com",
        })

        # Validate domain
        val = await asyncio.to_thread(
            session.post,
            "https://emailfake.com/check_adres_validation3.php",
            data={"usr": fmt, "dmn": domain},
            timeout=8,
        )
        if "good" not in val.text.strip().lower():
            raise ValueError(f"emailfake: domain {domain} not valid")

        # Visit inbox page → get recieved token
        await asyncio.to_thread(
            session.get,
            f"https://emailfake.com/{domain}/{fmt}",
            allow_redirects=False, timeout=8,
        )
        inbox_resp = await asyncio.to_thread(
            session.get, "https://emailfake.com/", timeout=10,
        )
        token_match = re.search(r'recieved:\s*["\']([^"\']+)["\']', inbox_resp.text)
        token = token_match.group(1) if token_match else ""
        if not token:
            raise ValueError("emailfake: no recieved token")

        init_hash = await asyncio.to_thread(
            session.post,
            "https://emailfake.com/del_mail.php",
            data={"recieved": token},
            timeout=8,
        )
        _emailfake_sessions[addr] = {
            "session": session,
            "token": token,
            "last_hash": init_hash.text.strip(),
        }
        _email_provider_map[addr] = "emailfake"
        logger.info(f"[emailfake] {addr}")
        return addr
    except Exception:
        _emailfake_sessions.pop(addr, None)
        raise


async def poll_emailfake_email(email_addr: str, timeout: int = 240, log=logger.info) -> str | None:
    """Poll emailfake.com inbox for CF verification token."""
    import requests as _r

    session_data = _emailfake_sessions.get(email_addr)
    if not session_data:
        log(f"[emailfake] no session for {email_addr}")
        return None

    session = session_data["session"]
    token = session_data["token"]
    start = time.time()

    while time.time() - start < timeout:
        try:
            r = await asyncio.to_thread(
                session.post,
                "https://emailfake.com/del_mail.php",
                data={"recieved": token},
                timeout=10,
            )
            new_hash = r.text.strip()
            if new_hash != session_data["last_hash"]:
                session_data["last_hash"] = new_hash
                # New email — fetch inbox page
                inbox = await asyncio.to_thread(
                    session.get, "https://emailfake.com/", timeout=10,
                )
                text = _clean_html(inbox.text)
                cf_token = _extract_cf_token(text)
                if cf_token:
                    log(f"[emailfake] CF token found for {email_addr}")
                    return cf_token
        except Exception as e:
            log(f"[emailfake] poll error: {e}")

        await asyncio.sleep(8)

    log(f"[emailfake] timeout for {email_addr}")
    return None


# ═══════════════════════════════════════════════════════════════
#  Unified Interface
# ═══════════════════════════════════════════════════════════════

async def generate_email(provider: str = None, exclude_domain: str = None) -> tuple[str, str]:
    """Generate email using configured provider.

    Returns (email_addr, provider_name).
    """
    provider = (provider or EMAIL_PROVIDER).lower()

    if provider == "mix":
        import random
        provider = random.choice(_MIX_PROVIDERS)

    if provider == "imap":
        addr = await generate_imap_email(exclude_domain)
    elif provider == "tempmail":
        addr = await generate_tempmail_email(exclude_domain)
    elif provider == "generator":
        addr = await generate_generator_email(exclude_domain)
    elif provider == "emailfake":
        addr = await generate_emailfake_email(exclude_domain)
    else:
        raise ValueError(f"Unknown email provider: {provider}")

    return addr, _email_provider_map.get(addr, provider)


async def poll_verification(email_addr: str, timeout: int = None, log=logger.info) -> str | None:
    """Poll for CF verification token using the provider that generated the email.

    Auto-detects provider from _email_provider_map.
    """
    timeout = timeout or IMAP_TIMEOUT
    provider = _email_provider_map.get(email_addr, EMAIL_PROVIDER)

    if provider == "imap":
        return await poll_imap_verification(email_addr, timeout, log)
    elif provider == "tempmail":
        return await poll_tempmail_email(email_addr, timeout, log)
    elif provider == "generator":
        return await poll_generator_email(email_addr, timeout, log)
    elif provider == "emailfake":
        return await poll_emailfake_email(email_addr, timeout, log)
    else:
        log(f"[Email] unknown provider for {email_addr}: {provider}")
        return None
