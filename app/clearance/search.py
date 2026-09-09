"""Collision searches against real-world entities.

A clearance report answers two separate questions about every name in a script:

  1. Does something real already carry this name?   (existence)
  2. Is it prominent enough to notice and sue?      (exposure)

Existence comes from the IMDb and YouTube tables, which are historical snapshots.
Exposure comes from wiki.wikistat -- hourly Wikipedia pageviews per article per
language, current to this morning. Splitting the two is what lets a 2008 snapshot
still produce a risk score that reflects today.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import QueryTruncated, run

# wikistat is ORDER BY (path, time). Filtering on path uses the sort key and stays
# far under the read limit; filtering on anything else scans the whole 638bn rows.
ORG_WINDOW_START = "2021-07-01"
PROMINENCE_WINDOW = "toStartOfDay(now() - INTERVAL 365 DAY)"


@dataclass
class Collision:
    kind: str           # person | title | organisation
    matched: str
    detail: str
    prominence: int = 0      # Wikipedia pageviews, last 365d, all languages
    languages: int = 0       # how many language editions carry an article
    evidence_sql: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "matched": self.matched, "detail": self.detail,
            "prominence": self.prominence, "languages": self.languages,
            "evidence_sql": self.evidence_sql,
        }


@dataclass
class ElementReport:
    element: str
    kind: str
    collisions: list[Collision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _wiki_path(name: str) -> str:
    return name.strip().replace(" ", "_")


def prominence(name: str) -> tuple[int, int, str]:
    """Total Wikipedia pageviews and language count for a name, last 365 days.

    Returns (hits, languages, sql). Zero means 'no Wikipedia article by that exact
    title' -- which is itself a useful clearance signal: a name nobody looks up is
    a name nobody sues over.
    """
    sql = f"""
SELECT sum(hits) AS hits, uniqExact(project) AS langs
FROM wiki.wikistat
WHERE path = {{p:String}} AND time >= {PROMINENCE_WINDOW}
""".strip()
    try:
        r = run(sql, {"p": _wiki_path(name)})
    except QueryTruncated:
        return 0, 0, sql
    if not r.rows:
        return 0, 0, sql
    hits, langs = r.rows[0]
    return int(hits or 0), int(langs or 0), sql


def check_person(full_name: str) -> ElementReport:
    """Real people who share a character's name."""
    rep = ElementReport(element=full_name, kind="person")
    parts = full_name.strip().split()
    if len(parts) < 2:
        rep.notes.append("Single-word name: skipped person check (too generic to clear).")
        return rep
    first, last = parts[0], parts[-1]
    sql = """
SELECT first_name, last_name, count() AS credits
FROM imdb.roles
INNER JOIN imdb.actors ON imdb.actors.id = imdb.roles.actor_id
WHERE lower(first_name) = lower({f:String}) AND lower(last_name) = lower({l:String})
GROUP BY first_name, last_name
ORDER BY credits DESC
LIMIT 5
""".strip()
    try:
        r = run(sql, {"f": first, "l": last})
    except QueryTruncated as e:
        rep.notes.append(str(e))
        return rep
    hits, langs, psql = prominence(full_name)
    for row in r.dicts():
        rep.collisions.append(Collision(
            kind="person",
            matched=f"{row['first_name']} {row['last_name']}",
            detail=f"credited on {row['credits']} title(s) in the IMDb snapshot",
            prominence=hits, languages=langs,
            evidence_sql=sql,
        ))
    if not rep.collisions and hits > 0:
        rep.collisions.append(Collision(
            kind="person", matched=full_name,
            detail="no screen credits found, but a Wikipedia article by this exact name is being read",
            prominence=hits, languages=langs, evidence_sql=psql,
        ))
    return rep


def check_title(title: str) -> ElementReport:
    """Prior films sharing, or containing, a proposed title.

    Exact match is the legally interesting case. Substring match matters too --
    a distributor will still object to a title that reads as a sequel to theirs --
    so both are reported, tagged differently.
    """
    rep = ElementReport(element=title, kind="title")
    sql = """
SELECT name, year, rank,
       lower(name) = lower({t:String}) AS exact
FROM imdb.movies
WHERE positionCaseInsensitive(name, {t:String}) > 0 AND year > 1800
ORDER BY exact DESC, rank DESC, year DESC
LIMIT 8
""".strip()
    try:
        r = run(sql, {"t": title})
    except QueryTruncated as e:
        rep.notes.append(str(e))
        return rep
    rep.notes.append("Title index covers films up to 2008 (IMDb snapshot); newer titles are not covered.")
    hits, langs, _ = prominence(title)
    for row in r.dicts():
        kind_of_match = "exact title match" if row["exact"] else "contains the title"
        rank = row["rank"]
        rep.collisions.append(Collision(
            kind="title",
            matched=f"{row['name']} ({row['year']})",
            detail=f"{kind_of_match}; IMDb rank {rank if rank else 'unrated'}",
            prominence=hits if row["exact"] else 0,
            languages=langs if row["exact"] else 0,
            evidence_sql=sql,
        ))
    return rep


def check_organisation(name: str) -> ElementReport:
    """Real organisations whose public channel carries this name.

    Scoped to the final six months of the YouTube crawl. The table is
    ORDER BY (toStartOfMonth(upload_date), uploader), so filtering on that exact
    expression lets ClickHouse prune parts and read ~537M rows instead of 4.56bn.
    Filtering on the raw `upload_date` column does NOT prune and trips the read
    limit, so the sort-key form here is load-bearing, not stylistic.
    """
    rep = ElementReport(element=name, kind="organisation")
    sql = """
SELECT uploader, max(uploader_sub_count) AS subs, count() AS videos
FROM youtube.youtube
WHERE toStartOfMonth(upload_date) >= toDate({since:String})
  AND positionCaseInsensitive(uploader, {n:String}) > 0
GROUP BY uploader
HAVING subs > 0
ORDER BY subs DESC
LIMIT 5
""".strip()
    try:
        r = run(sql, {"n": name, "since": ORG_WINDOW_START})
    except QueryTruncated as e:
        rep.notes.append(str(e))
        return rep
    rep.notes.append(
        f"Organisation scan covers uploads from {ORG_WINDOW_START} onward "
        "(the most recent six months of the crawl), not the full archive."
    )
    hits, langs, _ = prominence(name)
    for row in r.dicts():
        rep.collisions.append(Collision(
            kind="organisation",
            matched=row["uploader"],
            detail=f"{int(row['subs']):,} subscribers, {row['videos']} video(s)",
            prominence=hits, languages=langs, evidence_sql=sql,
        ))
    return rep


CHECKS = {"person": check_person, "title": check_title, "organisation": check_organisation}
