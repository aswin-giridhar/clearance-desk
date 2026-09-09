"""The clearance pipeline.

Deliberately a fixed sequence of steps in ordinary Python, not an agent loop that
decides its own control flow at runtime. Two reasons:

  * The brief asks for a *deterministic, multi-step* agent. Determinism here means
    the same script produces the same report: step order is code, and the one model
    call that has latitude (extraction) runs at temperature 0 against a response
    schema.
  * 2026 evidence is that multi-agent systems do not beat a single agent at equal
    token budget on factual work, and that ungrounded self-critique measurably hurts
    accuracy. So there is no debate step and no self-review step; every check is
    grounded in rows returned by ClickHouse.

Steps:
  1. EXTRACT   Gemini -> typed list of clearable elements
  2. SEARCH    each element -> collisions, via the official mcp-clickhouse server
  3. EXPOSE    each collision -> current Wikipedia attention (wiki.wikistat)
  4. SCORE     derived thresholds -> risk tier
  5. REPORT    assemble the clearance report, carrying the SQL for every finding
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .extract import Element, extract_elements
from .mcp_client import MCPClickHouse
from .score import report_tier, tier_for

ORG_WINDOW_START = "2021-07-01"
PROMINENCE_DAYS = 365


def _wiki_path(name: str) -> str:
    """Map a script name to a Wikipedia article path.

    Screenplays write character names in ALL CAPS by convention, but Wikipedia
    titles are title-case. Querying wikistat for 'JACK_DAWSON' returns zero and the
    element scores CLEAR when it should score MEDIUM, so all-caps input is
    normalised. Mixed-case input is left alone -- 'iPhone' must not become 'Iphone'.
    """
    n = name.strip()
    if n.isupper():
        n = n.title()
    return n.replace(" ", "_")


def _word_boundary_pattern(name: str) -> str:
    """Case-insensitive whole-phrase regex, so 'Ealing' does not match 'Healing'."""
    import re as _re
    return "(?i)\\b" + _re.escape(name.strip()).replace("\\ ", "\\s+") + "\\b"


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", "\\'")


@dataclass
class Finding:
    element: str
    kind: str
    matched: str
    detail: str
    tier: str
    reason: str
    advice: str
    prominence: int
    languages: int
    sql: list[str] = field(default_factory=list)


@dataclass
class Report:
    findings: list[Finding]
    elements_checked: int
    overall: str
    elapsed_s: float
    queries_run: int
    coverage: list[str]
    warnings: list[str] = field(default_factory=list)


COVERAGE = [
    "Real people: imdb.actors joined to imdb.roles (817,718 people; snapshot ends 2008).",
    "Prior titles: imdb.movies (388,269 films; snapshot ends 2008).",
    f"Organisations: youtube.youtube uploads from {ORG_WINDOW_START} onward "
    "(~537M rows of the 4.56bn-row table).",
    f"Current exposure: wiki.wikistat hourly pageviews, last {PROMINENCE_DAYS} days, "
    "all language editions — updated to today.",
]


async def _prominence(mcp: MCPClickHouse, name: str) -> tuple[int, int, str]:
    sql = (
        "SELECT sum(hits) AS hits, uniqExact(project) AS langs\n"
        "FROM wiki.wikistat\n"
        f"WHERE path = '{_esc(_wiki_path(name))}'\n"
        f"  AND time >= now() - INTERVAL {PROMINENCE_DAYS} DAY"
    )
    try:
        res = await mcp.run_query(sql)
    except RuntimeError:
        return 0, 0, sql
    rows = res.get("rows") if isinstance(res, dict) else None
    if not rows or rows[0][0] is None:
        return 0, 0, sql
    return int(rows[0][0] or 0), int(rows[0][1] or 0), sql


async def _person(mcp: MCPClickHouse, name: str):
    parts = name.strip().split()
    if len(parts) < 2:
        return [], []
    sql = (
        "SELECT first_name, last_name, count() AS credits\n"
        "FROM imdb.roles\n"
        "INNER JOIN imdb.actors ON imdb.actors.id = imdb.roles.actor_id\n"
        f"WHERE lower(first_name) = lower('{_esc(parts[0])}')\n"
        f"  AND lower(last_name) = lower('{_esc(parts[-1])}')\n"
        "GROUP BY first_name, last_name ORDER BY credits DESC LIMIT 5"
    )
    res = await mcp.run_query(sql)
    return res.get("rows", []), [sql]


async def _title(mcp: MCPClickHouse, name: str):
    sql = (
        "SELECT name, year, rank, lower(name) = lower('%s') AS exact\n"
        "FROM imdb.movies\n"
        "WHERE positionCaseInsensitive(name, '%s') > 0 AND year > 1800\n"
        "ORDER BY exact DESC, rank DESC, year DESC LIMIT 6" % (_esc(name), _esc(name))
    )
    res = await mcp.run_query(sql)
    return res.get("rows", []), [sql]


async def _org(mcp: MCPClickHouse, name: str):
    # toStartOfMonth(upload_date) is the table's sort key expression. Filtering on
    # the raw column does not prune parts and trips the 1bn-row read limit.
    sql = (
        "SELECT uploader, max(uploader_sub_count) AS subs, count() AS videos\n"
        "FROM youtube.youtube\n"
        f"WHERE toStartOfMonth(upload_date) >= toDate('{ORG_WINDOW_START}')\n"
        f"  AND match(uploader, '{_esc(_word_boundary_pattern(name))}')\n"
        "GROUP BY uploader HAVING subs > 0 ORDER BY subs DESC LIMIT 5"
    )
    res = await mcp.run_query(sql)
    return res.get("rows", []), [sql]


async def run_clearance(script_text: str, mcp: MCPClickHouse) -> Report:
    started = time.time()
    warnings: list[str] = []
    queries = 0

    # 1. EXTRACT (the only step with model latitude)
    elements: list[Element] = await asyncio.to_thread(extract_elements, script_text)

    findings: list[Finding] = []
    for el in elements:
        rows, sqls = [], []
        try:
            if el.kind == "person":
                rows, sqls = await _person(mcp, el.text)
            elif el.kind == "title":
                rows, sqls = await _title(mcp, el.text)
            else:
                rows, sqls = await _org(mcp, el.text)
            queries += 1
        except RuntimeError as exc:
            warnings.append(f"{el.text}: {exc}")
            continue

        hits, langs, psql = await _prominence(mcp, el.text)
        queries += 1
        sqls.append(psql)

        if not rows:
            # Nothing shares the name in the indexed sources. Only report it if the
            # name nonetheless draws real attention (a famous fictional name still
            # carries risk); otherwise it is genuinely clear.
            if hits > 0:
                s = tier_for(hits, langs)
                findings.append(Finding(
                    element=el.text, kind=el.kind, matched=el.text,
                    detail="no database match, but a Wikipedia article of this exact "
                           "name is actively read",
                    tier=s.tier, reason=s.reason, advice=s.advice,
                    prominence=hits, languages=langs, sql=sqls))
            continue

        for row in rows[:5]:
            if el.kind == "person":
                matched = f"{row[0]} {row[1]}"
                detail = f"real person, {row[2]} screen credit(s)"
                credits = int(row[2])
            elif el.kind == "title":
                matched = f"{row[0]} ({row[1]})"
                kind_of = "exact title match" if row[3] else "existing title contains this"
                detail = f"{kind_of}; IMDb rank {row[2] or 'unrated'}"
                credits = 0
            else:
                matched = row[0]
                detail = f"real organisation, {int(row[1]):,} subscribers, {row[2]} video(s)"
                credits = 0
            s = tier_for(hits, langs, credits)
            findings.append(Finding(
                element=el.text, kind=el.kind, matched=matched, detail=detail,
                tier=s.tier, reason=s.reason, advice=s.advice,
                prominence=hits, languages=langs, sql=sqls))

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "CLEAR": 4}
    findings.sort(key=lambda f: (order[f.tier], -f.prominence))
    return Report(
        findings=findings,
        elements_checked=len(elements),
        overall=report_tier([f.tier for f in findings]),
        elapsed_s=time.time() - started,
        queries_run=queries,
        coverage=COVERAGE,
        warnings=warnings,
    )
