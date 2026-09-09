"""Step 1 of the pipeline: pull clearable elements out of script text.

This is the only step where the model has latitude. It is constrained by a response
schema so the output is a typed list, not prose -- everything downstream is
deterministic code operating on that list.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from google import genai
from google.genai import types

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "a2a-hackathon-499316")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client


ELEMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "elements": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING", "description": "The name exactly as written"},
                    "kind": {"type": "STRING", "enum": ["person", "organisation", "title"]},
                    "context": {"type": "STRING", "description": "Short quote showing how it is used"},
                },
                "required": ["text", "kind", "context"],
            },
        }
    },
    "required": ["elements"],
}

INSTRUCTION = """You are a script clearance analyst preparing an E&O insurance report.

Extract every element from this screenplay extract that requires clearance:
- person: character names (first + last). These are checked against real people.
- organisation: businesses, brands, products, institutions, publications named in
  dialogue or action.
- title: any in-world work referenced (films, songs, books, TV shows).

Rules:
- Report the name exactly as written in the script.
- Include a character even if they appear once.
- Do NOT include: real public figures used as themselves in a documentary sense,
  generic nouns ("the diner", "a cop"), or place names unless they are a business.
- Do NOT invent elements that are not in the text.

Return only the structured list."""


@dataclass
class Element:
    text: str
    kind: str
    context: str


def extract_elements(script_text: str) -> list[Element]:
    """Extract clearable elements. Cached by script hash.

    The call runs at temperature 0 against a fixed response schema, so the same
    script deterministically yields the same list -- which makes caching it safe
    and keeps a repeated demo from re-billing a Gemini call.
    """
    import hashlib
    from . import cache as _cache
    key = "extract:" + hashlib.sha256(script_text.encode()).hexdigest()[:32]
    hit = _cache.get(key)
    if hit is not None:
        return [Element(**e) for e in hit]
    out = _extract_uncached(script_text)
    _cache.put(key, [{"text": e.text, "kind": e.kind, "context": e.context} for e in out])
    return out


def _extract_uncached(script_text: str) -> list[Element]:
    resp = client().models.generate_content(
        model=MODEL,
        contents=f"{INSTRUCTION}\n\n--- SCRIPT EXTRACT ---\n{script_text}",
        config=types.GenerateContentConfig(
            temperature=0,  # deterministic: same script -> same element list
            response_mime_type="application/json",
            response_schema=ELEMENT_SCHEMA,
        ),
    )
    data = json.loads(resp.text)
    seen, out = set(), []
    for e in data.get("elements", []):
        key = (e["text"].strip().lower(), e["kind"])
        if key in seen:
            continue
        seen.add(key)
        out.append(Element(text=e["text"].strip(), kind=e["kind"], context=e.get("context", "")))
    return out
