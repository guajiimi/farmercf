#!/usr/bin/env python3
"""
farmercf — Pure HTTP Cloudflare Workers AI account farmer (CLI).

Anti-detection: random TLS, locale, country, email domain, password pattern per registration.
Captcha solver: CapSolver → Solverify → 2Captcha (auto-fallback).
Neuron tracking: GraphQL aiInferenceAdaptiveGroups (no inference needed).

Usage:
  python main.py farm --count 5
  python main.py neuron
  python main.py neuron --account <account_id>
  python main.py neuron --live
  python main.py monitor --interval 300
  python main.py refresh-tokens --email gsuite@domain.com --password ...
"""

import asyncio
import json
import sys
import argparse
import os
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

from core import AccountFarmer, NeuronTracker

CONFIG_PATH = Path(__file__).parent / "config.json"

# Load .env at startup
load_dotenv(Path(__file__).parent / ".env")


def load_config() -> dict:
    """Load config from .env first, then config.json as fallback."""
    cfg = {}
    # From .env
    cfg["email_provider"] = os.environ.get("EMAIL_PROVIDER", "tempmail")
    cfg["imap_host"] = os.environ.get("IMAP_HOST", "imap.gmail.com")
    cfg["imap_port"] = int(os.environ.get("IMAP_PORT", "993"))
    cfg["imap_user"] = os.environ.get("IMAP_USER", "")
    cfg["imap_pass"] = os.environ.get("IMAP_PASS", "")
    cfg["imap_timeout"] = int(os.environ.get("IMAP_TIMEOUT", "240"))
    cfg["capsolver_api_key"] = os.environ.get("CAPSOLVER_API_KEY", "")
    cfg["solverify_api_key"] = os.environ.get("SOLVERIFY_API_KEY", "")
    cfg["twocaptcha_api_key"] = os.environ.get("TWOCAPTCHA_API_KEY", "")
    cfg["proxy_file"] = os.environ.get("PROXY_FILE", "proxy.txt")
    cfg["accounts_file"] = os.environ.get("ACCOUNTS_FILE", "accounts.json")
    cfg["farm_create_retries"] = int(os.environ.get("FARM_CREATE_RETRIES", "6"))
    cfg["inject_9router"] = os.environ.get("INJECT_9ROUTER", "false").lower() == "true"

    # Override with config.json if exists
    if CONFIG_PATH.exists():
        file_cfg = json.loads(CONFIG_PATH.read_text())
        cfg.update({k: v for k, v in file_cfg.items() if v})

    return cfg


def setup_logger(verbose: bool = False):
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")


# ── Commands ────────────────────────────────────────────────

async def cmd_farm(args):
    """Farm new Cloudflare accounts."""
    cfg = load_config()
    farmer = AccountFarmer(cfg)
    count = args.count

    logger.info(f"Starting farm: count={count}")
    results = await farmer.farm_batch(count, log=logger.info)

    print(json.dumps(results, indent=2))

    logger.info(f"Done: {results['active']} active, {results['partial']} partial, {results['failed']} failed")

    if results["active"] == 0 and results["partial"] == 0:
        sys.exit(1)


async def cmd_neuron(args):
    """Check neuron usage (stored or live via GraphQL)."""
    cfg = load_config()
    tracker = NeuronTracker(
        accounts_file=cfg.get("accounts_file", "accounts.json"),
        usage_file="neuron_usage.json",
    )

    if args.live:
        logger.info("Live checking all accounts via GraphQL...")
        result = await tracker.check_all_live()
        print(json.dumps(result, indent=2))
        valid = result.get("valid_accounts", 0)
        used = result.get("total_neurons_used", 0)
        remaining = result.get("total_neurons_remaining", 0)
        logger.info(f"Accounts: {valid} valid | Used: {used:.2f} | Remaining: {remaining:.2f}")
    elif args.account:
        # Check specific account
        if args.live:
            accounts = json.loads(Path(cfg.get("accounts_file", "accounts.json")).read_text())
            acc = next((a for a in accounts if a.get("account_id") == args.account), None)
            if not acc:
                logger.error(f"Account {args.account} not found")
                sys.exit(1)
            if not acc.get("api_token"):
                logger.error("Account has no API token")
                sys.exit(1)
            result = await tracker.check_live(acc["account_id"], acc["api_token"], acc.get("email", ""))
            print(json.dumps(result, indent=2))
        else:
            result = tracker.get_account_usage(args.account)
            print(json.dumps(result, indent=2))
    else:
        # Show stored summary
        result = tracker.get_all_usage()
        print(json.dumps(result, indent=2))
        logger.info(f"Total: {result['total_accounts']} accounts | Used: {result['total_neurons_used']} | Remaining: {result['total_neurons_remaining']}")


async def cmd_monitor(args):
    """Continuous monitoring — check neurons at intervals."""
    cfg = load_config()
    tracker = NeuronTracker(
        accounts_file=cfg.get("accounts_file", "accounts.json"),
        usage_file="neuron_usage.json",
    )
    interval = args.interval

    logger.info(f"Monitoring every {interval}s. Press Ctrl+C to stop.")

    while True:
        try:
            result = await tracker.check_all_live()
            total = result.get("valid_accounts", 0)
            used = result.get("total_neurons_used", 0)
            remaining = result.get("total_neurons_remaining", 0)
            pct = result.get("total_pct_used", 0)
            logger.info(f"Accounts={total} | Used={used:.2f} | Remaining={remaining:.2f} | {pct:.1f}% used")

            # Alert if any account is near limit
            for acc in result.get("accounts", []):
                if acc.get("pct_used", 0) > 80:
                    logger.warning(f"⚠️  {acc.get('email', acc.get('account_id', '?'))} at {acc['pct_used']:.1f}%")

        except Exception as e:
            logger.error(f"Monitor error: {e}")

        await asyncio.sleep(interval)


async def cmd_refresh_tokens(args):
    """Re-create API tokens for existing accounts via G Suite login."""
    from core.gsuite import gsuite_login, create_token_with_cookies

    cfg = load_config()
    accounts_file = Path(cfg.get("accounts_file", "accounts.json"))
    if not accounts_file.exists():
        logger.error("accounts.json not found")
        sys.exit(1)

    accounts = json.loads(accounts_file.read_text())
    logger.info(f"Found {len(accounts)} accounts. Starting G Suite login...")

    # Login via G Suite
    cookies = await gsuite_login(
        email=args.email,
        password=args.password,
        headless=not args.headed,
        log=logger.info,
    )
    if not cookies:
        logger.error("G Suite login failed")
        sys.exit(1)

    logger.info("G Suite login successful. Creating tokens...")

    updated = 0
    for acc in accounts:
        if not acc.get("account_id"):
            continue
        if acc.get("status") == "active" and acc.get("api_token") and not args.force:
            logger.info(f"Skipping {acc.get('email', '?')} — already active")
            continue

        token = await create_token_with_cookies(
            cookies["cookies"],
            acc["account_id"],
            log=logger.info,
        )
        if token:
            acc["api_token"] = token
            acc["status"] = "active"
            updated += 1
            logger.info(f"Updated token for {acc.get('email', '?')}")
        else:
            logger.warning(f"Failed to create token for {acc.get('email', '?')}")

        await asyncio.sleep(2)

    # Save updated accounts
    accounts_file.write_text(json.dumps(accounts, indent=2))
    logger.info(f"Done: {updated}/{len(accounts)} tokens updated")


async def cmd_permissions(args):
    """List available permission groups from CF Dashboard (requires G Suite login)."""
    from core.gsuite import gsuite_login
    from core.constants import CF_API, HEADERS
    from curl_cffi.requests import AsyncSession

    cfg = load_config()

    cookies = await gsuite_login(
        email=args.email,
        password=args.password,
        headless=not args.headed,
        log=logger.info,
    )
    if not cookies:
        logger.error("G Suite login failed")
        sys.exit(1)

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies["cookies"].items())

    async with AsyncSession(impersonate="chrome131") as s:
        resp = await s.get(
            f"{CF_API}/user/tokens/permission_groups",
            headers={**HEADERS, "Cookie": cookie_header},
            timeout=30,
        )
        data = resp.json()

    if data.get("success"):
        groups = data.get("result", [])
        ai_groups = [g for g in groups if "ai" in g.get("name", "").lower() or "analytics" in g.get("name", "").lower()]
        print(f"\nFound {len(groups)} permission groups ({len(ai_groups)} AI/Analytics related):\n")
        for g in ai_groups:
            print(f"  {g['id']}  {g['name']}")
    else:
        logger.error(f"Failed: {data.get('errors')}")


# ── Entry point ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="farmercf — Cloudflare Workers AI account farmer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  farm           Farm new accounts
  neuron         Check neuron usage (stored or live via GraphQL)
  monitor        Continuous neuron monitoring
  refresh-tokens Re-create API tokens via G Suite login
  permissions    List CF permission groups (requires G Suite)
        """.strip(),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # farm
    p_farm = sub.add_parser("farm", help="Farm new accounts")
    p_farm.add_argument("--count", "-n", type=int, default=1, help="Number of accounts to farm")
    p_farm.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    # neuron
    p_neuron = sub.add_parser("neuron", help="Check neuron usage")
    p_neuron.add_argument("--live", "-l", action="store_true", help="Live check via GraphQL")
    p_neuron.add_argument("--account", "-a", type=str, help="Specific account ID")

    # monitor
    p_mon = sub.add_parser("monitor", help="Continuous monitoring")
    p_mon.add_argument("--interval", "-i", type=int, default=300, help="Check interval in seconds")

    # refresh-tokens
    p_rt = sub.add_parser("refresh-tokens", help="Re-create API tokens via G Suite")
    p_rt.add_argument("--email", "-e", required=True, help="G Suite email")
    p_rt.add_argument("--password", "-p", required=True, help="G Suite password")
    p_rt.add_argument("--headed", action="store_true", help="Show browser window")
    p_rt.add_argument("--force", "-f", action="store_true", help="Re-create even if active")

    # permissions
    p_perms = sub.add_parser("permissions", help="List CF permission groups")
    p_perms.add_argument("--email", "-e", required=True, help="G Suite email")
    p_perms.add_argument("--password", "-p", required=True, help="G Suite password")
    p_perms.add_argument("--headed", action="store_true", help="Show browser window")

    args = parser.parse_args()

    # Setup
    verbose = getattr(args, "verbose", False)
    setup_logger(verbose)

    # Run
    cmd_map = {
        "farm": cmd_farm,
        "neuron": cmd_neuron,
        "monitor": cmd_monitor,
        "refresh-tokens": cmd_refresh_tokens,
        "permissions": cmd_permissions,
    }

    try:
        asyncio.run(cmd_map[args.command](args))
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
