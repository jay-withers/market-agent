"""`marketagent import-pg`: copy the PostgreSQL database into the DuckDB one, once.

The move off the Flexible Server. It attaches Postgres read-only through
DuckDB's own postgres extension, so every value crosses with its type intact
(NUMERIC to DECIMAL, jsonb to JSON, arrays to lists) and no Postgres driver is
needed in the image. Inside one database block, which holds the blob's lease
like any job, it empties every table, copies each one by column name, moves
each id sequence past the highest id copied, and prints the row counts on both
sides. The block commits and uploads only if every count matches.

Run it with no job running, just before the release that switches the jobs to
DuckDB, so nothing lands in Postgres after the copy. `make import-pg` supplies
the connection string with an Entra token and opens the server's firewall for
the length of the run.
"""

from __future__ import annotations

import logging

from .db import database

logger = logging.getLogger(__name__)

# Every table in schema/001-schema.sql except schema_migrations, which belongs
# to the DuckDB file: Postgres's rows name its own sql/ files.
TABLES = (
    "companies",
    "prices",
    "news",
    "news_analysis",
    "portfolio",
    "portfolio_watchlist",
    "positions",
    "agent_runs",
    "ai_decisions",
    "trades",
    "daily_performance",
    "benchmarks",
    "daily_summaries",
    "weekly_reviews",
)

# The tables with an id sequence, named `<table>_id_seq` in the schema.
SEQUENCED = (
    "news",
    "news_analysis",
    "portfolio",
    "positions",
    "agent_runs",
    "ai_decisions",
    "trades",
    "daily_summaries",
    "weekly_reviews",
)


class CountMismatch(RuntimeError):
    """A table copied a different number of rows than Postgres holds."""


def run(dsn: str) -> list[tuple[str, int, int]]:
    """Copy every table from the Postgres at `dsn`. Returns (table, source, copied)."""
    counts = []
    with database().connection() as conn:
        # ATTACH is outside the transaction the block's first statement opens.
        raw = conn._raw
        raw.execute("INSTALL postgres")
        raw.execute("LOAD postgres")
        # Closing the block's connection detaches it again.
        raw.execute("ATTACH ? AS pg (TYPE postgres, READ_ONLY)", [dsn])
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
            conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM pg.public.{table}")
            source = conn.execute(f"SELECT count(*) FROM pg.public.{table}").fetchone()[0]
            copied = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            counts.append((table, source, copied))
        for table in SEQUENCED:
            conn.execute(
                f"SELECT setval('{table}_id_seq', (SELECT coalesce(max(id), 0) FROM {table}))"
                f" WHERE EXISTS (SELECT 1 FROM {table})"
            )
        mismatched = [c for c in counts if c[1] != c[2]]
        if mismatched:
            # Raised inside the block, so it rolls back and uploads nothing.
            raise CountMismatch(", ".join(f"{t}: {s} != {c}" for t, s, c in mismatched))
    return counts
