"""
farmercf.core.email — IMAP email verification for Cloudflare signup.

Extracts verification token from Cloudflare welcome email via IMAP polling.
Supports Gmail, G Suite, and any IMAP-accessible mailbox.

Multi-domain aware: each registration may use a different email domain,
but all mail is polled from the configured IMAP mailbox (catch-all or plus-addressing).
"""

import re
import time
import asyncio
import imaplib
import email as email_lib
from loguru import logger


async def poll_imap_verification(
    email_addr: str,
    imap_host: str,
    imap_port: int,
    imap_user: str,
    imap_pass: str,
    timeout: int = 240,
    log=logger.info,
) -> str | None:
    """Poll IMAP for Cloudflare verification email, return token.

    Searches by TO header matching the registration email address.
    Falls back to FROM cloudflare.com if TO search returns nothing.
    """
    log(f"[IMAP] polling for {email_addr} (max {timeout}s)...")
    seen = set()
    start = time.time()

    while time.time() - start < timeout:
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            mail.login(imap_user, imap_pass)
            mail.select("INBOX")

            # Primary: search by TO header (catch-all / plus-addressing)
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

                    # CF verification link patterns
                    link = re.search(r"/email-verification\?token=([A-Za-z0-9_\-]+)", body)
                    if link:
                        mail.logout()
                        log(f"[IMAP] token found for {email_addr}")
                        return link.group(1)
                    tok = re.search(r"[?&]token=([A-Za-z0-9_\-]{40,})", body)
                    if tok:
                        mail.logout()
                        log(f"[IMAP] token found for {email_addr}")
                        return tok.group(1)

            mail.logout()
        except Exception as e:
            log(f"[IMAP] error: {e}")

        await asyncio.sleep(8)

    log(f"[IMAP] timeout — no verification email for {email_addr}")
    return None
