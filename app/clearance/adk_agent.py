"""The clearance pipeline expressed as a Google ADK workflow agent.

Why a SequentialAgent rather than a single LlmAgent with tools:

    The brief asks for a *deterministic, multi-step* agent. ADK's workflow agents
    (SequentialAgent, ParallelAgent, LoopAgent) exist for exactly that — the control
    flow is fixed by the agent graph, not decided by a model at runtime. Only the
    reasoning inside each step is model-driven. That is the property that makes the
    same screenplay produce the same clearance report.

Structure:

    SequentialAgent "clearance_pipeline"
      ├── LlmAgent "element_extractor"   Gemini on Vertex AI, temperature 0,
      │                                  response schema -> typed element list
      └── LlmAgent "clearance_analyst"   FunctionTool -> the official
                                         mcp-clickhouse MCP server

A note on why the ClickHouse connection is a FunctionTool rather than ADK's
McpToolset: ADK 2.8.0's McpToolset imports `mcp.shared.session`, which the installed
mcp 2.2.0 no longer exposes. Rather than downgrade a proven MCP path, the tool wraps
the same stdio session this app already uses — so ADK still drives every ClickHouse
call, through the official MCP server, at runtime.
"""
from __future__ import annotations

import json
import os
import uuid

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from pydantic import BaseModel, Field
from typing import Literal

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
APP_NAME = "clearance_desk"

# Set once at startup by main.py so the ADK tool can reach the live MCP session.
_mcp = None


def bind_mcp(session) -> None:
    global _mcp
    _mcp = session


async def query_clickhouse(sql: str) -> dict:
    """Run a read-only SQL query against ClickHouse via the official MCP server.

    Use this to check whether a name in a screenplay collides with something real.
    Useful tables:
      imdb.actors(id, first_name, last_name)  and imdb.roles(actor_id, movie_id)
      imdb.movies(id, name, year, rank)       — titles up to 2008
      wiki.wikistat(time, project, path, hits) — hourly Wikipedia pageviews,
          ORDER BY (path, time); always filter on `path`, and always bound the
          time range, or the query exceeds the cluster read limit.

    Args:
        sql: A single SELECT statement. No writes are permitted.

    Returns:
        A dict with `columns` and `rows`, or `error` describing why it failed.
    """
    if _mcp is None or not _mcp.ready:
        return {"error": "ClickHouse MCP session is not connected."}
    stripped = sql.strip().rstrip(";")
    if not stripped.lower().startswith(("select", "with")):
        return {"error": "Only SELECT statements are allowed."}
    try:
        return await _mcp.run_query(stripped)
    except Exception as exc:  # surfaced to the agent as data, never swallowed
        return {"error": f"{type(exc).__name__}: {exc}"}


class ClearableElement(BaseModel):
    """One name in a screenplay that requires clearance."""
    text: str = Field(description="The name exactly as written in the script")
    kind: Literal["person", "organisation", "title"]
    context: str = Field(description="Short quote showing how it is used")


class ExtractedElements(BaseModel):
    elements: list[ClearableElement]

EXTRACTOR_INSTRUCTION = """You are a script clearance analyst preparing an E&O insurance report.

Extract every element from the screenplay extract that requires clearance:
- person: character names (first + last, or a single stage name).
- organisation: businesses, brands, products, institutions named in dialogue or action.
- title: any in-world work referenced (films, songs, books, TV shows).

Rules:
- Report each name exactly as written in the script.
- Include a character even if they appear only once.
- Do NOT include generic nouns ("the diner", "a cop") or plain place names.
- Do NOT invent elements that are not in the text.

Return only the structured list."""

ANALYST_INSTRUCTION = """You are a clearance analyst. The extracted elements are in
state under 'elements'.

For each element, use query_clickhouse to establish two things:
  1. Existence — does something real carry this name? (imdb.actors joined to
     imdb.roles for people; imdb.movies for titles)
  2. Exposure — is it prominent enough to notice and object? Sum wiki.wikistat hits
     for the matching `path` over the last 365 days. Screenplay names are in capitals
     but Wikipedia titles are not, so try both the as-written and title-cased path.

Then summarise the clearance risk for each element in one short paragraph, citing the
figures you actually retrieved. Never state a number you did not get back from a query."""


def build_pipeline() -> SequentialAgent:
    """Construct the deterministic two-step clearance workflow."""
    extractor = LlmAgent(
        name="element_extractor",
        model=MODEL,
        description="Pulls clearable names out of a screenplay extract.",
        instruction=EXTRACTOR_INSTRUCTION,
        output_key="elements",
        # ADK requires the response schema here, not inside generate_content_config.
        output_schema=ExtractedElements,
        generate_content_config=types.GenerateContentConfig(
            temperature=0,  # deterministic: same script -> same element list
        ),
    )
    analyst = LlmAgent(
        name="clearance_analyst",
        model=MODEL,
        description="Checks each element against ClickHouse and rates the risk.",
        instruction=ANALYST_INSTRUCTION,
        tools=[FunctionTool(query_clickhouse)],
    )
    return SequentialAgent(
        name="clearance_pipeline",
        description="Deterministic two-step script clearance: extract, then verify.",
        sub_agents=[extractor, analyst],
    )


_runner: InMemoryRunner | None = None
_ex_runner: InMemoryRunner | None = None


def runner() -> InMemoryRunner:
    """Runner over the whole SequentialAgent (both steps)."""
    global _runner
    if _runner is None:
        _runner = InMemoryRunner(agent=build_pipeline(), app_name=APP_NAME)
    return _runner


def extractor_runner() -> InMemoryRunner:
    """Runner over step one only.

    /api/scan drives the extractor and then hands off to deterministic Python for
    search, scoring and reporting -- that hand-off is the whole point of the design,
    and it is why the report is reproducible rather than re-reasoned each run.
    """
    global _ex_runner
    if _ex_runner is None:
        _ex_runner = InMemoryRunner(agent=build_pipeline().sub_agents[0], app_name=APP_NAME)
    return _ex_runner


async def extract_via_adk(script_text: str) -> list[dict]:
    """Run only the extractor step of the ADK pipeline and return typed elements.

    The deterministic search, scoring and reporting downstream are ordinary Python,
    so this is where the ADK agent does its work on the critical path.
    """
    r = extractor_runner()
    # A fresh session id per call. A fixed id works for the first scan and raises
    # AlreadyExistsError on every one after it — and each extraction is independent,
    # so there is nothing to carry between them anyway.
    uid, sid = "clearance", f"extract-{uuid.uuid4().hex[:12]}"
    await r.session_service.create_session(app_name=APP_NAME, user_id=uid, session_id=sid)
    msg = types.Content(role="user", parts=[types.Part(text=script_text)])
    text = ""
    async for ev in r.run_async(user_id=uid, session_id=sid, new_message=msg):
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                if getattr(p, "text", None):
                    text = p.text
    if not text:
        raise RuntimeError("ADK extractor returned no content")
    data = json.loads(text)
    seen, out = set(), []
    for e in data.get("elements", []):
        key = (e["text"].strip().lower(), e["kind"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": e["text"].strip(), "kind": e["kind"],
                    "context": e.get("context", "")})
    return out
