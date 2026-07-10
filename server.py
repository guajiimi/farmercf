#!/usr/bin/env python3
"""
farmercf — Pure HTTP Cloudflare Workers AI account farmer.

Flow: bootstrap → captcha/challenge → solve Turnstile → user/create →
email verify → accounts → user/tokens → validate

Supports: Capsolver, 2Captcha (createTask v2), Solverify
"""

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, HTMLResponse
import uvicorn
from loguru import logger

from core import AccountFarmer, NeuronTracker

CONFIG_PATH = Path(__file__).parent / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text())

farm_tasks: dict[str, dict] = {}
farmer: AccountFarmer | None = None
tracker: NeuronTracker | None = None

app = FastAPI(title="farmercf", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def startup():
    global farmer, tracker
    farmer = AccountFarmer(CONFIG)
    tracker = NeuronTracker(
        accounts_file=CONFIG.get("accounts_file", "accounts.json"),
        usage_file="neuron_usage.json",
    )
    logger.info(f"[Farmer] configured: provider={CONFIG.get('captcha_provider', 'capsolver')} domain={CONFIG.get('farm_domain', '')}")


@app.get("/")
async def root():
    return HTMLResponse(
        "<html><body style='background:#111;color:#eee;font-family:monospace;"
        "padding:40px'><h2 style='color:#38bdf8'>farmercf</h2>"
        "<p>Pure HTTP Cloudflare Workers AI account farmer.</p>"
        "<p>POST /farm &rarr; farm accounts</p>"
        "<p>GET /neuron-usage &rarr; neuron usage summary</p>"
        "<p>GET /neuron-usage/live &rarr; live check all accounts</p>"
        "<p>GET /health &middot; GET /farm/result?id=</p></body></html>"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "provider": CONFIG.get("captcha_provider", "capsolver")}


@app.post("/farm")
async def farm(req_body: dict = None):
    count = (req_body or {}).get("count", 1)
    task_id = str(uuid.uuid4())
    farm_tasks[task_id] = {"status": "processing", "ts": asyncio.get_event_loop().time()}

    async def run():
        try:
            results = await farmer.farm_batch(count)
            farm_tasks[task_id] = {"status": "done", "ts": asyncio.get_event_loop().time(), "results": results}
        except Exception as e:
            farm_tasks[task_id] = {"status": "error", "ts": asyncio.get_event_loop().time(), "error": str(e)}

    asyncio.create_task(run())
    return {"task_id": task_id, "status": "accepted"}


@app.get("/farm/result")
async def farm_result(id: str = ""):
    task = farm_tasks.get(id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return task


# ── Neuron Usage Endpoints ───────────────────────────────────

@app.get("/neuron-usage")
async def neuron_usage(
    date: str = Query("", description="UTC date YYYY-MM-DD (default: today)"),
    account_id: str = Query("", description="Specific account ID (default: all)"),
):
    """Get neuron usage summary from stored data (no API calls).

    Reads from neuron_usage.json which is updated on every /ai/run call
    and on /neuron-usage/live checks.
    """
    if not tracker:
        return JSONResponse(status_code=503, content={"error": "tracker not initialized"})

    if account_id:
        return tracker.get_account_usage(account_id, date)
    return tracker.get_all_usage(date)


@app.get("/neuron-usage/live")
async def neuron_usage_live():
    """Live check — makes a minimal inference on each active account
    to get real-time neuron data from cf-ai-neurons header.

    Records usage automatically. Takes ~2-3s per account.
    """
    if not tracker:
        return JSONResponse(status_code=503, content={"error": "tracker not initialized"})

    task_id = str(uuid.uuid4())
    farm_tasks[task_id] = {"status": "processing", "ts": asyncio.get_event_loop().time()}

    async def run():
        try:
            result = await tracker.check_all_live()
            farm_tasks[task_id] = {"status": "done", "ts": asyncio.get_event_loop().time(), "result": result}
        except Exception as e:
            farm_tasks[task_id] = {"status": "error", "ts": asyncio.get_event_loop().time(), "error": str(e)}

    asyncio.create_task(run())
    return {"task_id": task_id, "status": "accepted", "note": "GET /farm/result?id= to poll"}


@app.get("/neuron-usage/check")
async def neuron_check_single(
    account_id: str = Query(..., description="Account ID"),
    api_token: str = Query(..., description="API token"),
    email: str = Query("", description="Email (for display)"),
):
    """Live check single account — makes one inference, returns neuron data."""
    if not tracker:
        return JSONResponse(status_code=503, content={"error": "tracker not initialized"})

    result = await tracker.check_live(account_id, api_token, email)
    return result


def main():
    global farmer, tracker
    host = CONFIG.get("host", "0.0.0.0")
    port = CONFIG.get("port", 8107)
    logger.info(f"farmercf listening on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
