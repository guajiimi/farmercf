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

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from loguru import logger

from core import AccountFarmer

CONFIG_PATH = Path(__file__).parent / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text())

farm_tasks: dict[str, dict] = {}
farmer: AccountFarmer | None = None

app = FastAPI(title="farmercf", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def startup():
    global farmer
    farmer = AccountFarmer(CONFIG)
    logger.info(f"[Farmer] configured: provider={CONFIG.get('captcha_provider', 'capsolver')} domain={CONFIG.get('farm_domain', '')}")


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


def main():
    host = CONFIG.get("host", "0.0.0.0")
    port = CONFIG.get("port", 8107)
    logger.info(f"farmercf listening on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
