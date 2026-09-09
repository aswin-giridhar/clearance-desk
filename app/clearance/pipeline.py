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
import re
import time
from dataclasses import dataclass, field

from . import cache
from .extract import Element, extract_elements
from .mcp_client import MCPClickHouse
from .score import report_tier, tier_for

ORG_WINDOW_START = "2021-07-01"
PROMINENCE_DAYS = 365


def _wiki_variants(name: str) -> list[str]:
    """Wikipedia article paths to try for a name.

    Screenplays write names in ALL CAPS, so 'JACK DAWSON' must be title-cased to
    reach 'Jack_Dawson'. But .title() mangles acronyms and Mc/Mac names, and the
    damage is silent: Wikipedia redirects mean 'Ibm' still returns 20,508 hits
    against IBM's real 1,618,996, so nothing looks broken while the risk tier drops
    from CRITICAL to MEDIUM. Measured under-reporting: IBM 79x, BBC 73x,
    McDonald's 304x -- on exactly the brands most likely to object.

    Both the as-written and the title-cased form are therefore queried, and the
    higher figure wins. Because they go into the same IN list, this costs no extra
    query.
    """
    n = name.strip()
    out = [n.replace(" ", "_")]
    if n.isupper():
        out.append(n.title().replace(" ", "_"))
        # .title() also flattens internal capitals: MCDONALD -> "Mcdonald", but the
        # article is "McDonald". Verified live: "RONALD MCDONALD" scored LOW on 228
        # views because neither variant was the real path. Add the Mc/Mac form.
        mac = re.sub(r"\b(Mc|Mac)([a-z])",
                     lambda m: m.group(1) + m.group(2).upper(), n.title())
        if mac != n.title():
            out.append(mac.replace(" ", "_"))
    return list(dict.fromkeys(out))


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
    territories: list = field(default_factory=list)   # top language editions by share
    trend_pct: float | None = None                    # 90d vs previous 90d, percent
    trend_new: bool = False                           # attention began inside the window
    terr_more: int = 0                                # editions beyond the ones shown


@dataclass
class Report:
    findings: list[Finding]
    elements_checked: int
    overall: str
    elapsed_s: float
    queries_run: int          # real ClickHouse round-trips
    coverage: list[str]
    warnings: list[str] = field(default_factory=list)
    partial: bool = False     # scan did not complete for every element
    cache_hits: int = 0       # lookups served from cache (no round-trip)


COVERAGE = [
    "Real people: imdb.actors joined to imdb.roles (817,718 people; snapshot ends 2008).",
    "Prior titles: imdb.movies (388,269 films; snapshot ends 2008).",
    f"Organisations: youtube.youtube uploads from {ORG_WINDOW_START} onward "
    "(~537M rows of the 4.56bn-row table).",
    f"Current exposure: wiki.wikistat hourly pageviews, last {PROMINENCE_DAYS} days, "
    "all language editions — updated to today.",
]


async def _exposure_batch(mcp: MCPClickHouse, names: list[str]) -> tuple[dict, list[str], bool]:
    """Current exposure for every element, in two queries for the whole scene.

    Query A groups by (path, project), which yields BOTH the per-territory split and
    the global total (summed across language editions) -- so the territory feature
    costs nothing extra over the plain total it replaces.

    Query B compares the last 90 days against the 90 before it. A name whose
    attention is climbing is a name getting riskier to use, which is information a
    static name-match cannot produce. Both questions are only answerable because
    wikistat holds 638bn rows of hourly, per-language pageviews updated to today.
    """
    if not names:
        return {}, [], False
    paths = sorted({v for n in names for v in _wiki_variants(n)})
    in_list = ", ".join("'" + _esc(p) + "'" for p in paths)

    sql_territory = (
        "SELECT path, project, sum(hits) AS hits\n"
        "FROM wiki.wikistat\n"
        f"WHERE path IN ({in_list})\n"
        f"  AND time >= now() - INTERVAL {PROMINENCE_DAYS} DAY\n"
        "GROUP BY path, project ORDER BY hits DESC"
    )
    sql_trend = (
        "SELECT path,\n"
        "       sumIf(hits, time >= now() - INTERVAL 90 DAY) AS recent,\n"
        "       sumIf(hits, time <  now() - INTERVAL 90 DAY) AS prior\n"
        "FROM wiki.wikistat\n"
        f"WHERE path IN ({in_list})\n"
        "  AND time >= now() - INTERVAL 180 DAY\n"
        "GROUP BY path"
    )

    # Key includes the windows: changing PROMINENCE_DAYS must not silently
    # serve numbers computed under the old window.
    ck = f"exposure:d{PROMINENCE_DAYS}:t90:" + "|".join(paths)
    hit = cache.get(ck)
    ran = False
    if hit is None:
        terr = (await mcp.run_query(sql_territory)).get("rows", [])
        tren = (await mcp.run_query(sql_trend)).get("rows", [])
        hit = {"territory": terr, "trend": tren}
        cache.put(ck, hit)
        ran = True

    by_path: dict[str, dict] = {}
    for path, project, hits in hit["territory"]:
        d = by_path.setdefault(path, {"total": 0, "langs": 0, "territories": []})
        d["total"] += int(hits or 0)
        d["langs"] += 1
        d["territories"].append((project, int(hits or 0)))
    for path, recent, prior in hit["trend"]:
        d = by_path.setdefault(path, {"total": 0, "langs": 0, "territories": []})
        r, p_ = int(recent or 0), int(prior or 0)
        d["trend_pct"] = round(100.0 * (r - p_) / p_, 1) if p_ > 0 else None
        d["trend_new"] = p_ == 0 and r > 0   # attention started inside the window

    out = {}
    for n in names:
        # Take whichever casing variant Wikipedia actually knows about.
        cands = [by_path.get(v) for v in _wiki_variants(n)]
        cands = [c for c in cands if c]
        d = max(cands, key=lambda c: c.get("total", 0)) if cands else {}
        terrs = sorted(d.get("territories", []), key=lambda t: -t[1])[:4]
        total = d.get("total", 0)
        out[n] = {
            "total": total,
            "langs": d.get("langs", 0),
            "trend_pct": d.get("trend_pct"),
            "trend_new": d.get("trend_new", False),
            "territories_total": len(d.get("territories", [])),
            "territories": [
                {"project": p, "hits": h, "share": round(100.0 * h / total, 1) if total else 0}
                for p, h in terrs
            ],
        }
    return out, [sql_territory, sql_trend], ran


async def _person(mcp: MCPClickHouse, name: str):
    parts = name.strip().split()
    if len(parts) < 2:
        # A mononym ("MADONNA", "CHER") cannot be split into first/last, so the
        # people table cannot be queried. Return the same 3-tuple shape as every
        # other path -- returning a 2-tuple here crashed the request with a bare 500.
        return [], [], False
    sql = (
        "SELECT first_name, last_name, count() AS credits\n"
        "FROM imdb.roles\n"
        "INNER JOIN imdb.actors ON imdb.actors.id = imdb.roles.actor_id\n"
        f"WHERE lower(first_name) = lower('{_esc(parts[0])}')\n"
        f"  AND lower(last_name) = lower('{_esc(parts[-1])}')\n"
        "GROUP BY first_name, last_name ORDER BY credits DESC LIMIT 5"
    )
    rows = cache.get(sql)
    if rows is not None:
        return rows, [sql], True          # cache hit: no round-trip
    res = await mcp.run_query(sql)
    rows = res.get("rows", [])
    cache.put(sql, rows)
    return rows, [sql], False


def _title_variants(title: str) -> list[str]:
    """Title forms to search for.

    imdb.movies stores titles with a leading article moved to the end, the legacy
    IMDb convention: "The Godfather" is stored as "Godfather, The". Searching only
    the natural form silently misses exact matches -- verified: "The Gathering"
    returns nothing, while "Gathering, The" returns three films (1977, 1998, 2002).
    Under-reporting collisions is the worst direction for a clearance tool to fail
    in, so both forms are searched.
    """
    t = title.strip()
    out = [t]
    for art in ("The ", "A ", "An "):
        if t.lower().startswith(art.lower()):
            out.append(f"{t[len(art):]}, {art.strip()}")
            break
    return out


async def _title(mcp: MCPClickHouse, name: str):
    """Prior films sharing, or containing, a proposed title."""
    variants = _title_variants(name)
    conds = " OR ".join(f"positionCaseInsensitive(name, '{_esc(v)}') > 0" for v in variants)
    exact = " OR ".join(f"lower(name) = lower('{_esc(v)}')" for v in variants)
    sql = (
        f"SELECT name, year, rank, ({exact}) AS exact\n"
        "FROM imdb.movies\n"
        f"WHERE ({conds}) AND year > 1800\n"
        "ORDER BY exact DESC, rank DESC, year DESC LIMIT 6"
    )
    rows = cache.get(sql)
    if rows is not None:
        return rows, [sql], True
    res = await mcp.run_query(sql)
    rows = res.get("rows", [])
    cache.put(sql, rows)
    return rows, [sql], False


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
    rows = cache.get(sql)
    if rows is not None:
        return rows, [sql], True          # cache hit: no round-trip
    res = await mcp.run_query(sql)
    rows = res.get("rows", [])
    cache.put(sql, rows)
    return rows, [sql], False


async def run_clearance(script_text: str, mcp: MCPClickHouse) -> Report:
    started = time.time()
    warnings: list[str] = []
    queries = 0
    cache_hits = 0

    # 1. EXTRACT (the only step with model latitude)
    elements: list[Element] = await asyncio.to_thread(extract_elements, script_text)
    # One dense paste could otherwise spend a quarter of the shared hourly query
    # budget in a single click, before any judge opens the page.
    MAX_ELEMENTS = 12
    if len(elements) > MAX_ELEMENTS:
        warnings.append(
            f"Scene contains {len(elements)} clearable elements; checking the first "
            f"{MAX_ELEMENTS} to stay within the shared cluster's hourly query budget."
        )
        elements = elements[:MAX_ELEMENTS]

    # One batched exposure query for the whole scene, before the per-element loop.
    prom_map, prom_sqls, prom_ran = await _exposure_batch(mcp, [e.text for e in elements])
    if prom_sqls:
        queries += 2 if prom_ran else 0
        cache_hits += 0 if prom_ran else 1

    findings: list[Finding] = []
    for el in elements:
        rows, sqls, cached = [], [], False
        try:
            if el.kind == "person":
                rows, sqls, cached = await _person(mcp, el.text)
            elif el.kind == "title":
                rows, sqls, cached = await _title(mcp, el.text)
            else:
                rows, sqls, cached = await _org(mcp, el.text)
            if cached:
                cache_hits += 1
            else:
                queries += 1
        except RuntimeError as exc:
            warnings.append(f"{el.text}: {exc}")
            continue

        exp = prom_map.get(el.text, {})
        hits, langs = exp.get("total", 0), exp.get("langs", 0)
        territories, trend_pct = exp.get("territories", []), exp.get("trend_pct")
        terr_more = max(0, exp.get("territories_total", 0) - len(territories))
        trend_new = exp.get("trend_new", False)
        # prom_sql covers the whole scene, so label it rather than presenting it as
        # evidence specific to this one finding.
        sqls.extend("-- exposure lookup (batched for the whole scene)\n" + q for q in prom_sqls)

        if not rows:
            # Nothing shares the name in the indexed sources. Only report it if the
            # name nonetheless draws real attention (a famous fictional name still
            # carries risk); otherwise it is genuinely clear.
            if hits > 0:
                s = tier_for(hits, langs)
                # A single word that is also a place, a concept or a myth ("Phoenix",
                # "Mercury") will match an article that has nothing to do with a person.
                # We cannot tell what the article is about from pageview data alone, so
                # the uncertainty is disclosed rather than silently scored as a person.
                ambiguous = el.kind == "person" and len(el.text.split()) < 2
                note = ("no database match. A Wikipedia article of this exact name is "
                        "actively read, but a single-word name may refer to a place, a "
                        "concept or a brand rather than a person — confirm what the "
                        "article is about before acting on this"
                        if ambiguous else
                        "no database match, but a Wikipedia article of this exact "
                        "name is actively read")
                findings.append(Finding(
                    element=el.text, kind=el.kind, matched=el.text,
                    detail=note,
                    tier=s.tier, reason=s.reason, advice=s.advice,
                    prominence=hits, languages=langs, sql=sqls,
                    territories=territories, trend_pct=trend_pct,
                    trend_new=trend_new, terr_more=terr_more))
            continue

        # One finding per element, not one per matching row. Five YouTube channels
        # containing "Coca-Cola" is one clearance issue, not five, and repeating the
        # identical exposure figure five times makes the report harder to read.
        matches = []
        credits = 0
        for row in rows[:5]:
            if el.kind == "person":
                matches.append(f"{row[0]} {row[1]} ({row[2]} credits)")
                credits = max(credits, int(row[2]))
            elif el.kind == "title":
                mark = "exact" if row[3] else "contains"
                matches.append(f"{row[0]} ({row[1]}, {mark})")
            else:
                matches.append(f"{row[0]} ({int(row[1]):,} subs)")

        primary = matches[0]
        if len(matches) > 1:
            detail = f"{primary} — and {len(matches) - 1} other match(es): " + "; ".join(matches[1:])
        else:
            detail = primary
        label = {"person": "real person", "title": "existing title",
                 "organisation": "real organisation"}[el.kind]
        s_ = tier_for(hits, langs, credits)
        findings.append(Finding(
            element=el.text, kind=el.kind, matched=primary,
            detail=f"{label}. {detail}" if len(matches) > 1 else label,
            tier=s_.tier, reason=s_.reason, advice=s_.advice,
            prominence=hits, languages=langs, sql=sqls,
            territories=territories, trend_pct=trend_pct,
                    trend_new=trend_new, terr_more=terr_more))

    # A verdict computed from an incomplete scan must not present as final. The
    # badge itself carries the flag: a reader who skims only the badge would
    # otherwise take a partial result as the whole answer.
    partial = bool(warnings)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "CLEAR": 4}
    findings.sort(key=lambda f: (order[f.tier], -f.prominence))
    return Report(
        findings=findings,
        elements_checked=len(elements),
        overall=report_tier([f.tier for f in findings]),
        elapsed_s=time.time() - started,
        queries_run=queries,
        cache_hits=cache_hits,
        coverage=COVERAGE,
        warnings=warnings,
        partial=partial,
    )
