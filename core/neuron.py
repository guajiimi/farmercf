"""
farmercf.core.neuron — Neuron usage tracker via Cloudflare GraphQL API.

Queries `aiInferenceAdaptiveGroups` to get real-time neuron usage per account.
Requires Account Analytics Read permission on the API token.

This is the VERIFIED method (June 2026):
  - GraphQL query returns totalNeurons (used) per account per day
  - No need to trigger inference just to check — reads stored analytics
  - 10,000 neuron limit is hardcoded (free plan), not exposed via API
  - totalNeurons appears ~10 seconds after an inference call

Storage: neuron_usage.json (auto-created, auto-pruned to 7 days).
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

from curl_cffi.requests import AsyncSession
from loguru import logger

NEURON_QUOTA = 10_000  # per account per day (hardcoded free plan)
USAGE_FILE = "neuron_usage.json"

# GraphQL endpoint + query
CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

GRAPHQL_QUERY = """
query NeuronUsage($accountTag: String!, $date: String!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      aiInferenceAdaptiveGroups(
        limit: 1
        filter: {date: $date}
      ) {
        max {
          totalNeurons
        }
      }
    }
  }
}
""".strip()


class NeuronTracker:
    """Track and query neuron usage across all farmed accounts via GraphQL.

    Two modes:
      1. Stored mode — read from neuron_usage.json (no API calls, instant)
      2. Live mode — query CF GraphQL for real-time data (requires Analytics perm)
    """

    def __init__(self, accounts_file: str = "accounts.json", usage_file: str = USAGE_FILE):
        self.accounts_file = Path(accounts_file)
        self.usage_file = Path(usage_file)
        self._lock = asyncio.Lock()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        if not self.usage_file.exists():
            return {}
        try:
            return json.loads(self.usage_file.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}

    def _save(self, data: dict):
        """Save with 7-day pruning."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        pruned = {k: v for k, v in data.items() if k >= cutoff}
        self.usage_file.write_text(json.dumps(pruned, indent=2))

    async def record(self, account_id: str, email: str, neurons: float, source: str = "live"):
        """Record neuron usage (from live check or inference)."""
        if neurons < 0:
            return
        async with self._lock:
            data = self._load()
            today = self._today()
            if today not in data:
                data[today] = {}
            if account_id not in data[today]:
                data[today][account_id] = {"email": email, "neurons": 0.0, "requests": 0, "source": source}
            data[today][account_id]["neurons"] = max(data[today][account_id]["neurons"], neurons)
            data[today][account_id]["requests"] += 1
            data[today][account_id]["source"] = source
            self._save(data)

    async def check_live(self, account_id: str, api_token: str, email: str = "") -> dict:
        """Query CF GraphQL for real-time neuron usage.

        This does NOT trigger inference — reads stored analytics data.
        Requires Account Analytics Read permission on token.
        """
        today = self._today()

        async with AsyncSession(impersonate="chrome131") as s:
            resp = await s.post(
                CF_GRAPHQL_URL,
                json={
                    "query": GRAPHQL_QUERY,
                    "variables": {
                        "accountTag": account_id,
                        "date": today,
                    },
                },
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            data = resp.json()

        # Parse GraphQL response
        neurons_used = 0.0
        token_valid = False
        error = None

        if data.get("errors"):
            err_msgs = [e.get("message", "") for e in data["errors"]]
            error = "; ".join(err_msgs)
            # Check if it's a permissions error
            if "not authorized" in error.lower() or "forbidden" in error.lower():
                error = "token lacks Account Analytics Read permission"
            logger.warning(f"[Neuron] GraphQL error for {account_id}: {error}")
        elif data.get("data", {}).get("viewer", {}).get("accounts"):
            accounts = data["data"]["viewer"]["accounts"]
            if accounts:
                groups = accounts[0].get("aiInferenceAdaptiveGroups", [])
                if groups:
                    neurons_used = float(groups[0].get("max", {}).get("totalNeurons", 0))
                    token_valid = True
                else:
                    # No inference today = 0 neurons used (valid)
                    token_valid = True
            else:
                token_valid = True  # No account data but no error = valid token

        # Record to storage
        if token_valid:
            await self.record(account_id, email, neurons_used, source="graphql")

        return {
            "account_id": account_id,
            "email": email,
            "token_valid": token_valid,
            "neurons_used": round(neurons_used, 2),
            "neurons_remaining": max(0, NEURON_QUOTA - neurons_used),
            "quota": NEURON_QUOTA,
            "pct_used": round(neurons_used / NEURON_QUOTA * 100, 1),
            "date": today,
            "error": error,
        }

    async def check_all_live(self) -> dict:
        """Check all active accounts via GraphQL (no inference needed)."""
        if not self.accounts_file.exists():
            return {"error": "accounts.json not found"}

        accounts = json.loads(self.accounts_file.read_text())
        # Only check accounts with tokens (any status with api_token)
        active = [a for a in accounts if a.get("api_token") and a.get("account_id")]

        results = []
        for acc in active:
            try:
                r = await self.check_live(
                    acc["account_id"],
                    acc["api_token"],
                    acc.get("email", ""),
                )
                results.append(r)
            except Exception as e:
                results.append({
                    "account_id": acc.get("account_id", "?"),
                    "email": acc.get("email", "?"),
                    "error": str(e),
                    "token_valid": False,
                })

        # Build summary
        valid = [r for r in results if r.get("token_valid")]
        total_used = sum(r.get("neurons_used", 0) for r in valid)
        total_accounts = len(valid)

        return {
            "date": self._today(),
            "total_accounts_checked": len(results),
            "valid_accounts": total_accounts,
            "total_neurons_used": round(total_used, 2),
            "total_neurons_remaining": max(0, total_accounts * NEURON_QUOTA - total_used),
            "total_quota": total_accounts * NEURON_QUOTA,
            "total_pct_used": round(total_used / (total_accounts * NEURON_QUOTA) * 100, 1) if total_accounts > 0 else 0,
            "accounts": results,
        }

    def get_account_usage(self, account_id: str, date: str = "") -> dict:
        """Get stored usage for one account (instant, no API call)."""
        date = date or self._today()
        data = self._load()
        entry = data.get(date, {}).get(account_id)
        if not entry:
            return {
                "account_id": account_id,
                "date": date,
                "neurons_used": 0,
                "neurons_remaining": NEURON_QUOTA,
                "requests": 0,
                "quota": NEURON_QUOTA,
                "pct_used": 0.0,
            }
        used = round(entry["neurons"], 2)
        return {
            "account_id": account_id,
            "date": date,
            "email": entry.get("email", ""),
            "neurons_used": used,
            "neurons_remaining": max(0, NEURON_QUOTA - used),
            "requests": entry.get("requests", 0),
            "quota": NEURON_QUOTA,
            "pct_used": round(used / NEURON_QUOTA * 100, 1),
        }

    def get_all_usage(self, date: str = "") -> dict:
        """Get stored usage summary for all accounts (instant)."""
        date = date or self._today()
        data = self._load()
        day_data = data.get(date, {})

        accounts = []
        total_used = 0.0
        total_requests = 0

        for account_id, entry in day_data.items():
            used = round(entry["neurons"], 2)
            total_used += used
            total_requests += entry.get("requests", 0)
            accounts.append({
                "account_id": account_id,
                "email": entry.get("email", ""),
                "neurons_used": used,
                "neurons_remaining": max(0, NEURON_QUOTA - used),
                "requests": entry.get("requests", 0),
                "quota": NEURON_QUOTA,
                "pct_used": round(used / NEURON_QUOTA * 100, 1),
            })

        accounts.sort(key=lambda x: x["neurons_used"], reverse=True)
        total_accounts = len(accounts)
        total_quota = total_accounts * NEURON_QUOTA

        return {
            "date": date,
            "total_accounts": total_accounts,
            "total_neurons_used": round(total_used, 2),
            "total_neurons_remaining": max(0, total_quota - total_used),
            "total_quota": total_quota,
            "total_requests": total_requests,
            "total_pct_used": round(total_used / total_quota * 100, 1) if total_quota > 0 else 0,
            "accounts": accounts,
        }
