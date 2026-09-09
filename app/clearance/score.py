"""Risk tiering.

The thresholds below were derived, not chosen. A probe set of known-high names
(Taylor Swift 10.6M views/yr, Barack Obama 8.9M, Tom Hanks 5.5M, Coca-Cola 2.4M,
Nike 1.0M), known-mid (Wetherspoons 180k, Jack Ryan 73k, Ealing Studios 45k) and
known-low (Jack Dawson 32k, Blue Sun 923, Wernham Hogg 80, invented names 0) was
measured against wiki.wikistat, and the boundaries placed where those groups
actually separate.

Two honest caveats:
  * "Ealing Studios" (44,625) sits near the MEDIUM/HIGH boundary -- that one case
    would flip if the boundary moved ~12%. Every other probe is comfortably inside
    its band.
  * Invented names score exactly 0, which is the property that matters most: the
    gate can distinguish "nothing real by this name" from "something real".
"""
from __future__ import annotations

from dataclasses import dataclass

# pageviews per year -> tier
CRITICAL, HIGH, MEDIUM, LOW = 500_000, 50_000, 5_000, 1

TIERS = ["CLEAR", "LOW", "MEDIUM", "HIGH", "CRITICAL"]

ADVICE = {
    "CRITICAL": "Do not use as written. A globally recognised real entity carries this name; "
                "an E&O underwriter will require a change or a signed release.",
    "HIGH":     "Change recommended. A real, well-known entity carries this name. If the script "
                "must keep it, obtain written clearance before principal photography.",
    "MEDIUM":   "Flag to production counsel. A real entity exists with meaningful public presence. "
                "Usually cleared with a disclaimer, occasionally requires a change.",
    "LOW":      "Likely clearable. Something real shares the name but has negligible public "
                "profile. Record the check and move on.",
    "CLEAR":    "No collision found in the indexed sources. Note the coverage limits below.",
}


@dataclass
class Scored:
    tier: str
    reason: str
    advice: str


def tier_for(prominence: int, languages: int, credits: int = 0) -> Scored:
    """Score one collision.

    Prominence (Wikipedia pageviews over the last 365 days) is the primary signal
    because it measures *current* attention -- the thing that determines whether an
    entity would notice the use and has the standing to object. Language count is a
    secondary signal: an entity known in 100 language editions is recognised
    internationally, which matters for a film with worldwide distribution.
    """
    if prominence >= CRITICAL:
        t = "CRITICAL"
    elif prominence >= HIGH:
        t = "HIGH"
    elif prominence >= MEDIUM:
        t = "MEDIUM"
    elif prominence >= LOW:
        t = "LOW"
    else:
        t = "CLEAR"

    # International recognition escalates one tier: worldwide distribution means
    # worldwide exposure to objection.
    if languages >= 25 and t in ("MEDIUM", "HIGH"):
        t = TIERS[min(TIERS.index(t) + 1, len(TIERS) - 1)]
        note = f"escalated: recognised across {languages} language editions"
    else:
        note = ""

    bits = []
    if prominence:
        bits.append(f"{prominence:,} Wikipedia pageviews in the last 365 days")
    if languages:
        bits.append(f"{languages} language edition(s)")
    if credits:
        bits.append(f"{credits} screen credit(s)")
    if not bits:
        bits.append("no public footprint found in the indexed sources")
    reason = "; ".join(bits) + (f" ({note})" if note else "")
    return Scored(tier=t, reason=reason, advice=ADVICE[t])


def report_tier(tiers: list[str]) -> str:
    """A report is as risky as its worst finding."""
    if not tiers:
        return "CLEAR"
    return max(tiers, key=lambda t: TIERS.index(t))
