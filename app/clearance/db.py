"""ClickHouse access layer.

The public ClickHouse demo cluster applies `max_rows_to_read=1e9` with
`read_overflow_mode='break'`. That combination makes an over-large query return a
*partial answer with no error*: a `count()` over the 638-billion-row wikistat table
comes back as exactly 1,000,885,548 and the accompanying `max(time)` is wrong too.

A clearance report that silently under-reports collisions is worse than no report,
so every connection here forces `read_overflow_mode='throw'`. Truncation becomes a
raised exception we can surface, never a quiet wrong number.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Sequence

import clickhouse_connect

CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8443"))
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "demo")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")

QUERY_TIMEOUT_S = 55


class QueryTruncated(RuntimeError):
    """The cluster hit max_rows_to_read. Any result would have been partial."""


@dataclass
class QueryResult:
    """A query's rows plus the SQL that produced them.

    The SQL is carried alongside the rows because the UI shows it next to every
    finding: a reader can re-run it and check the claim themselves.
    """

    sql: str
    columns: Sequence[str]
    rows: list[tuple]
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]


def get_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        secure=True,
        settings={
            # Turn silent truncation into a loud failure. See module docstring.
            "read_overflow_mode": "throw",
            "max_execution_time": QUERY_TIMEOUT_S,
        },
    )


_client = None


def client():
    global _client
    if _client is None:
        _client = get_client()
    return _client


def run(sql: str, params: dict | None = None) -> QueryResult:
    """Run a read-only query, converting truncation into QueryTruncated."""
    import time

    started = time.time()
    try:
        res = client().query(sql, parameters=params or {})
    except Exception as exc:  # noqa: BLE001 - inspect message, then re-raise narrowly
        # ClickHouse error 158 == exceeded max_rows_to_read.
        if "158" in str(exc) or "max_rows_to_read" in str(exc):
            raise QueryTruncated(
                "Query would exceed the cluster read limit, so any answer would be "
                "partial. Narrow it (filter on the table's sort key) and retry."
            ) from exc
        raise
    return QueryResult(
        sql=sql.strip(),
        columns=list(res.column_names),
        rows=[tuple(r) for r in res.result_rows],
        elapsed_s=time.time() - started,
    )
