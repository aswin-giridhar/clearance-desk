"""Clearance Desk - web application.

Serves a single page that takes screenplay text and returns a clearance report.
The MCP session to ClickHouse is opened once at startup (cold start ~15s) and
reused for every request, never spawned per-request.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clearance.mcp_client import MCPClickHouse
from clearance.pipeline import run_clearance

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent.parent / "samples"

state: dict = {"mcp": None, "error": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp = MCPClickHouse()
    try:
        await mcp.start()
        state["mcp"] = mcp
    except Exception as exc:  # surface, never swallow - see /healthz
        state["error"] = str(exc)
    yield
    if state["mcp"]:
        await state["mcp"].stop()


app = FastAPI(title="Clearance Desk", lifespan=lifespan)


class ScanRequest(BaseModel):
    script: str = Field(min_length=10, max_length=20000)


@app.get("/healthz")
async def healthz():
    mcp = state["mcp"]
    return {
        "mcp_connected": bool(mcp and mcp.ready),
        "mcp_tools": mcp.tools if mcp else [],
        "error": state["error"],
    }


@app.get("/api/sample")
async def sample():
    f = SAMPLES / "scene.txt"
    return {"script": f.read_text() if f.exists() else ""}


@app.post("/api/scan")
async def scan(req: ScanRequest):
    mcp = state["mcp"]
    if not (mcp and mcp.ready):
        raise HTTPException(503, f"ClickHouse MCP server unavailable: {state['error']}")
    rep = await run_clearance(req.script, mcp)
    return JSONResponse({
        "overall": rep.overall,
        "elements_checked": rep.elements_checked,
        "queries_run": rep.queries_run,
        "elapsed_s": round(rep.elapsed_s, 1),
        "coverage": rep.coverage,
        "warnings": rep.warnings,
        "findings": [{
            "element": f.element, "kind": f.kind, "matched": f.matched,
            "detail": f.detail, "tier": f.tier, "reason": f.reason,
            "advice": f.advice, "prominence": f.prominence,
            "languages": f.languages, "sql": f.sql,
        } for f in rep.findings],
    })


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
