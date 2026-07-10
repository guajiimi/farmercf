"""
farmercf.core.neuron — Neuron usage tracker.

Tracks Workers AI neuron consumption per-account per-day by reading
the `cf-ai-neurons` response header from /ai/run calls.

Storage: neuron_usage.json (auto-created, auto-pruned to 7 days).
Quota: 10,000 neurons/day per account (Cloudflare free tier).
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

from curl_cffi.requests import AsyncSession
from loguru import logger


NEURON_QUOTA = 10_000  # per account per day
USAGE_FILE = "neuron_usage.json"


class NeuronTracker:
    """Track and query neuron usage across all farmed accounts.

    Two modes:
      1. Record mode — called after each /ai/run to log neuron consumption
      2. Query mode — proactive check via CF API (makes a test inference)
    """

    def __init__(self, accounts_file: str = "accounts.json", usage_file: str = USAGE_FILE):
        self.accounts_file = Path(accounts_file)
        self.usage_file = Path(usage_file)
        self._lock = asyncio.Lock()

    # ── Storage helpers ──────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        """Load usage file. Structure:
        {
            "2026-07-10": {
                "account_id_1": {"email": "...", "neurons": 4521.5, "requests": 87},
                "account_id_2": {...}
            }
        }
        """
        if not self.usage_file.exists():
            return {}
        try:
            return json.loads(self.usage_file.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}

    def _save(self, data: dict):
        # Prune entries older than 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        pruned = {k: v for k, v in data.items() if k >= cutoff}
        self.usage_file.write_text(json.dumps(pruned, indent=2))

    # ── Record mode (called after /ai/run) ──────────────────

    async def record(self, account_id: str, email: str, neurons: float):
        """Record neuron usage from a single /ai/run call."""
        if neurons <= 0:
            return
        async with self._lock:
            data = self._load()
            today = self._today()
            if today not in data:
                data[today] = {}
            if account_id not in data[today]:
                data[today][account_id] = {"email": email, "neurons": 0.0, "requests": 0}
            data[today][account_id]["neurons"] += neurons
            data[today][account_id]["requests"] += 1
            self._save(data)

    # ── Query mode ──────────────────────────────────────────

    def get_account_usage(self, account_id: str, date: str = "") -> dict:
        """Get usage for one account on a given day (default: today)."""
        date = date or self._today()
        data = self._load()
        day_data = data.get(date, {})
        entry = day_data.get(account_id)
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
        """Get usage summary for all accounts."""
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

        # Sort by usage descending
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

    # ── Live check (makes a test inference) ─────────────────

    async def check_live(self, account_id: str, api_token: str, email: str = "") -> dict:
        """Make a minimal inference to check live neuron usage.

        Reads `cf-ai-neurons` header from the response.
        Also records the usage automatically.
        """
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/meta/llama-3.2-1b-instruct"
        from .farmer import _req_with_headers, HEADERS

        neurons = 0.0
        valid = False
        async with AsyncSession(impersonate="chrome131") as s:
            resp, headers = await _req_with_headers(
                s, "POST", url, {"prompt": "hi"},
                extra={"Authorization": f"Bearer {api_token}"},
            )
            raw = headers.get("cf-ai-neurons") or headers.get("Cf-Ai-Neurons", "0")
            try:
                neurons = float(raw)
            except (ValueError, TypeError):
                neurons = 0.0
            valid = bool(resp and resp.get("success"))

        if valid:
            await self.record(account_id, email, neurons)

        return {
            "account_id": account_id,
            "email": email,
            "token_valid": valid,
            "neurons_this_call": round(neurons, 2),
            "today": self.get_account_usage(account_id),
        }

    # ── Batch check all accounts ────────────────────────────

    async def check_all_live(self) -> dict:
        """Check all active accounts with live inference.

        Returns combined: stored usage + live verification.
        """
        if not self.accounts_file.exists():
            return {"error": "accounts.json not found"}

        accounts = json.loads(self.accounts_file.read_text())
        active = [a for a in accounts if a.get("api_token") and a.get("status") in ("active", "verified_no_token")]

        results = []
        for acc in active:
            try:
                r = await self.check_live(
                    acc["account_id"], acc["api_token"], acc.get("email", "")
                )
                results.append(r)
            except Exception as e:
                results.append({
                    "account_id": acc.get("account_id", "?"),
                    "email": acc.get("email", "?"),
                    "error": str(e),
                })

        # Build summary
        summary = self.get_all_usage()
        summary["live_checked"] = len(results)
        summary["live_results"] = results
        return summary
