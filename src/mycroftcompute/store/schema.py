"""Our own copy of the history, because the backends do not keep one.

Grafana Cloud's free tier drops metrics after 14 days. Datadog keeps more but a drop rule or a
custom-metric plan can shorten it without anyone noticing. Our loop needs months: the thing that
makes this a subscription rather than a one-off audit is being able to say *"last month we told
you X, you did Y, here is what changed"*, and that sentence needs last month.

**SQLite, not Postgres.** The predecessor repo used Postgres and this will too once there is a web
app with tenants and row-level security to enforce. Today there is a CLI, and SQLite is in the
standard library, needs no server, and holds this shape of data comfortably: daily aggregates for
a 120-engineer team are a few hundred rows a day. The schema below ports to Postgres unchanged
except for the id column.

**Writes are idempotent by natural key.** A 30-day pull run daily overlaps 29 days with the last
one. Appending would multiply a month's spend by thirty; the second pull must overwrite the days
it covers and leave the rest alone. SQLite treats NULLs as distinct in a unique index, and half
these columns are legitimately NULL - a backend with no user label, a family with no segments -
so the primary key is an explicit joined string with NULLs folded to a sentinel, rather than the
columns themselves.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

#: Applied in order on every open. Each statement must be safe to re-run.
STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pull (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        source     TEXT NOT NULL,
        backend    TEXT,
        first_day  TEXT NOT NULL,
        last_day   TEXT NOT NULL,
        total_cost TEXT NOT NULL,
        vendors    TEXT NOT NULL
    )
    """,
    # One row per (day, model, and every dimension the backend could attribute it to). The
    # natural key is what makes a re-pull overwrite rather than double.
    """
    CREATE TABLE IF NOT EXISTS usage (
        row_key               TEXT PRIMARY KEY,
        day                   TEXT NOT NULL,
        model                 TEXT NOT NULL,
        who                   TEXT,
        repo                  TEXT,
        query_source          TEXT,
        effort                TEXT,
        input_tokens          INTEGER,
        cached_input_tokens   INTEGER,
        cache_write_tokens    INTEGER,
        cache_write_1h_tokens INTEGER,
        output_tokens         INTEGER,
        requests              INTEGER,
        cost                  TEXT,
        first_seen            TEXT NOT NULL,
        last_seen             TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS usage_by_day ON usage (day)",
    # Findings are kept per pull rather than upserted, because the interesting question is how
    # one finding moved between two runs - which needs both runs.
    """
    CREATE TABLE IF NOT EXISTS finding (
        pull_id     INTEGER NOT NULL REFERENCES pull (id) ON DELETE CASCADE,
        finding_key TEXT NOT NULL,
        kind        TEXT NOT NULL,
        subject     TEXT,
        title       TEXT NOT NULL,
        ceiling     TEXT,
        PRIMARY KEY (pull_id, finding_key)
    )
    """,
)

#: What a NULL becomes inside the natural key. A literal that cannot appear in a model name, a
#: developer identity or a repository, so it can never collide with a real value.
NULL_SENTINEL = "\x00"
