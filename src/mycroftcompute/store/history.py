"""Saving a read, and comparing it with the last one.

The comparison is the product's reason to be a subscription. A one-off audit tells a team what
their bill looks like; they read it, act or do not, and never need it again. What they cannot get
anywhere else is *"last month we said your cache hit rate was 2%, and the ceiling on fixing it
was $486. It is now 61%, and that ceiling is $77. The thing you changed worked."*

Two rules keep that honest, and both are about resisting the temptation to claim credit:

**A finding that stopped firing is reported as stopped, not as saved.** We cannot know that our
report caused the change - a team migrates, a project ends, someone rewrites a prompt for reasons
of their own. The history says what moved. It does not say who moved it.

**Spend that fell is not a saving either.** A quieter month is a quieter month. Comparing two
periods of different lengths, or a month containing a holiday, would manufacture a result from
a calendar. So the comparison states the periods alongside the numbers and refuses to compute a
percentage when the two are not the same length.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mycroftcompute.analysis.findings import Analysis, Finding
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord
from mycroftcompute.store.schema import NULL_SENTINEL, SCHEMA_VERSION, STATEMENTS


def finding_key(finding: Finding) -> str:
    """A finding's identity across runs.

    Keyed by kind and subject, so "the model-mix finding about claude-opus-5" is the same finding
    next month while "the model-mix finding about gpt-6-astra" is a new one. That is the right
    granularity: a finding whose subject changed is genuinely different news.
    """
    return f"{finding.kind.value}:{finding.subject or '-'}"


def _row_key(record: UsageRecord) -> str:
    parts = (
        record.day.isoformat(),
        record.model,
        record.user,
        record.repo,
        record.query_source,
        record.effort,
    )
    return "\x1f".join(p if p is not None else NULL_SENTINEL for p in parts)


def open_store(path: Path) -> sqlite3.Connection:
    """Open, creating and migrating as needed. Safe to call on every run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    with connection:
        for statement in STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return connection


def save(
    connection: sqlite3.Connection,
    parsed: ParsedUsage,
    analysis: Analysis,
    *,
    backend: str | None = None,
    now: datetime | None = None,
) -> int:
    """Record one read. Returns the pull id.

    Usage rows are upserted by natural key, so re-reading an overlapping window corrects those
    days rather than adding to them. Findings are appended per pull, because comparing two runs
    needs both of them.
    """
    stamp = (now or datetime.now(tz=UTC)).isoformat()
    days = sorted({r.day for r in parsed.records})
    total = analysis.costed.total if analysis.costed else Decimal(0)

    with connection:
        cursor = connection.execute(
            "INSERT INTO pull (fetched_at, source, backend, first_day, last_day, total_cost, "
            "vendors) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                stamp,
                parsed.source_kind,
                backend,
                days[0].isoformat() if days else "",
                days[-1].isoformat() if days else "",
                str(total),
                ",".join(sorted(str(v) for v in parsed.all_vendors)),
            ),
        )
        pull_id = int(cursor.lastrowid or 0)

        connection.executemany(
            """
            INSERT INTO usage (
                row_key, day, model, who, repo, query_source, effort,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, requests, cost, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (row_key) DO UPDATE SET
                input_tokens          = excluded.input_tokens,
                cached_input_tokens   = excluded.cached_input_tokens,
                cache_write_tokens    = excluded.cache_write_tokens,
                cache_write_1h_tokens = excluded.cache_write_1h_tokens,
                output_tokens         = excluded.output_tokens,
                requests              = excluded.requests,
                cost                  = excluded.cost,
                last_seen             = excluded.last_seen
            """,
            [
                (
                    _row_key(r),
                    r.day.isoformat(),
                    r.model,
                    r.user,
                    r.repo,
                    r.query_source,
                    r.effort,
                    r.input_tokens,
                    r.cached_input_tokens,
                    r.cache_write_tokens,
                    r.cache_write_1h_tokens,
                    r.output_tokens,
                    r.requests,
                    str(r.cost) if r.cost is not None else None,
                    stamp,
                    stamp,
                )
                for r in parsed.records
            ],
        )

        connection.executemany(
            "INSERT OR REPLACE INTO finding (pull_id, finding_key, kind, subject, title, ceiling)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    pull_id,
                    finding_key(f),
                    f.kind.value,
                    f.subject,
                    f.title,
                    str(f.ceiling) if f.ceiling is not None else None,
                )
                for f in analysis.findings
            ],
        )
    return pull_id


# ----------------------------------------------------------------- the comparison ---


@dataclass(frozen=True, slots=True)
class Movement:
    """One finding, and what happened to it since the previous read."""

    finding_key: str
    title: str

    #: "appeared", "resolved", or "continued".
    state: str

    was: Decimal | None = None
    now: Decimal | None = None

    @property
    def delta(self) -> Decimal | None:
        if self.was is None or self.now is None:
            return None
        return self.now - self.was


@dataclass(slots=True)
class Comparison:
    """Two reads, side by side. Empty when there is nothing to compare against yet."""

    previous_period: tuple[date, date] | None = None
    current_period: tuple[date, date] | None = None

    previous_total: Decimal | None = None
    current_total: Decimal | None = None

    movements: list[Movement] = field(default_factory=list)

    @property
    def has_history(self) -> bool:
        return self.previous_period is not None

    @property
    def comparable_periods(self) -> bool:
        """Whether the two windows are the same length.

        A 30-day read against a 14-day one produces a 53% "fall" that is entirely a calendar
        artefact. Where the windows differ we show both totals and refuse the percentage.
        """
        if not (self.previous_period and self.current_period):
            return False
        return (self.previous_period[1] - self.previous_period[0]) == (
            self.current_period[1] - self.current_period[0]
        )

    @property
    def spend_delta(self) -> Decimal | None:
        if self.previous_total is None or self.current_total is None:
            return None
        return self.current_total - self.previous_total


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except ArithmeticError:
        return None


def compare(connection: sqlite3.Connection, pull_id: int) -> Comparison:
    """This pull against the last one that covered different days. See `comparison`."""
    with closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT id, first_day, last_day, total_cost FROM pull WHERE id <= ? ORDER BY id DESC",
            (pull_id,),
        )
        rows = cursor.fetchall()
    return comparison(rows, pull_id, lambda pid: _findings(connection, pid))


def comparison(
    rows: Sequence[Mapping[str, Any]],
    pull_id: int,
    findings_of: Callable[[int], dict[str, tuple[str, Decimal | None]]],
) -> Comparison:
    """The comparison itself, for any store that can list its pulls and their findings.

    `rows` are the pulls up to and including this one, newest first, each with `id`,
    `first_day`, `last_day` and `total_cost`. Kept separate from the SQLite queries so any store
    that can list its pulls gives "since last time" the same meaning.

    "Since last time" means the previous period, not the previous run. Re-reading a window to
    correct it, or a daily pull over a rolling 14 days, overlaps the read before it; comparing
    the two printed "24-27 Sep against 24-26 Sep" on a live stack, which tells a reader nothing.
    So the comparison skips back to the most recent earlier read whose period does not overlap
    this one, and says nothing when there is none yet.
    """
    if not rows or int(rows[0]["id"]) != pull_id:
        return Comparison()

    current = rows[0]
    out = Comparison(
        current_period=_period(current),
        current_total=_decimal(current["total_cost"]),
    )
    here = out.current_period
    previous = next(
        (
            row
            for row in rows[1:]
            if here is not None
            and (period := _period(row)) is not None
            and (period[1] < here[0] or period[0] > here[1])
        ),
        None,
    )
    if previous is None:
        return out

    out.previous_period = _period(previous)
    out.previous_total = _decimal(previous["total_cost"])

    now_findings = findings_of(int(current["id"]))
    was_findings = findings_of(int(previous["id"]))

    for key, (title, ceiling) in now_findings.items():
        if key in was_findings:
            out.movements.append(
                Movement(key, title, "continued", was=was_findings[key][1], now=ceiling)
            )
        else:
            out.movements.append(Movement(key, title, "appeared", now=ceiling))

    for key, (title, ceiling) in was_findings.items():
        if key not in now_findings:
            out.movements.append(Movement(key, title, "resolved", was=ceiling))

    out.movements.sort(key=lambda m: (m.state, m.finding_key))
    return out


def _period(row: Mapping[str, Any]) -> tuple[date, date] | None:
    try:
        return _as_date(row["first_day"]), _as_date(row["last_day"])
    except (TypeError, ValueError):
        return None


def _findings(
    connection: sqlite3.Connection, pull_id: int
) -> dict[str, tuple[str, Decimal | None]]:
    with closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT finding_key, title, ceiling FROM finding WHERE pull_id = ?", (pull_id,)
        )
        return {r["finding_key"]: (r["title"], _decimal(r["ceiling"])) for r in cursor.fetchall()}


# ------------------------------------------------------------- reading a pull back ---


@dataclass(frozen=True, slots=True)
class Pull:
    """One stored read, as a history lists it."""

    id: int
    fetched_at: datetime
    source: str
    backend: str | None
    first_day: date
    last_day: date
    total: Decimal


def pulls(connection: sqlite3.Connection) -> list[Pull]:
    """Every stored read, newest first. Reads with no dated rows are skipped: there is no
    period to show and nothing to compare."""
    with closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT id, fetched_at, source, backend, first_day, last_day, total_cost "
            "FROM pull ORDER BY id DESC"
        )
        rows = cursor.fetchall()
    out = []
    for row in rows:
        period = _period(row)
        if period is None:
            continue
        out.append(
            Pull(
                id=int(row["id"]),
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
                source=row["source"],
                backend=row["backend"],
                first_day=period[0],
                last_day=period[1],
                total=_decimal(row["total_cost"]) or Decimal(0),
            )
        )
    return out


#: Which record field each stored column fills. A column that is NULL on every row of a pull
#: means the source never reported it, and the rebuilt read must say so rather than claim zeros:
#: "no cached tokens" and "this backend does not report cached tokens" are different findings.
_COLUMN_FIELDS = {
    "input_tokens": "input_tokens",
    "cached_input_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "output_tokens": "output_tokens",
    "requests": "requests",
    "cost": "cost",
    "who": "user",
    "repo": "repo",
    "query_source": "query_source",
    "effort": "effort",
}


def load_pull(connection: sqlite3.Connection, pull_id: int) -> ParsedUsage:
    """Rebuild a stored read so the engine can analyse it again. See `parsed_from_rows`."""
    with closing(connection.cursor()) as cursor:
        cursor.execute("SELECT * FROM pull WHERE id = ?", (pull_id,))
        pull = cursor.fetchone()
        if pull is None:
            raise KeyError(f"no stored read with id {pull_id}")
        cursor.execute(
            "SELECT * FROM usage WHERE day BETWEEN ? AND ? ORDER BY day, model, who",
            (pull["first_day"], pull["last_day"]),
        )
        rows = cursor.fetchall()
    return parsed_from_rows(pull, rows)


def parsed_from_rows(pull: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> ParsedUsage:
    """A read rebuilt from its pull row and the stored usage rows for its period.

    Shared by every store. A later read that overlapped these days will have corrected them in
    place, so an old pull shows its days as we now know them - the same numbers the next
    comparison uses.
    """
    from mycroftcompute.sources.records import dominant
    from mycroftcompute.vendors import Vendor

    records = [
        UsageRecord(
            day=_as_date(r["day"]),
            model=r["model"],
            input_tokens=r["input_tokens"],
            cached_input_tokens=r["cached_input_tokens"],
            cache_write_tokens=r["cache_write_tokens"],
            cache_write_1h_tokens=r["cache_write_1h_tokens"],
            output_tokens=r["output_tokens"],
            requests=r["requests"],
            cost=_decimal(r["cost"]),
            user=r["who"],
            repo=r["repo"],
            query_source=r["query_source"],
            effort=r["effort"],
        )
        for r in rows
    ]
    present = {"day", "model"} | {
        field_name
        for column, field_name in _COLUMN_FIELDS.items()
        if any(r[column] is not None for r in rows)
    }
    vendors = {Vendor(v) for v in str(pull["vendors"]).split(",") if v}
    return ParsedUsage(
        vendor=dominant(records, vendors) if vendors else Vendor.ANTHROPIC,
        vendors=frozenset(vendors),
        records=records,
        fields_present=frozenset(present),
        rows_seen=len(records),
        rows_accepted=len(records),
        currency="USD" if "cost" in present else None,
        source_kind=str(pull["source"]),
    )


def _as_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def stored_days(connection: sqlite3.Connection) -> tuple[date, date] | None:
    """The whole window we hold, which outlives what the backend still has."""
    with closing(connection.cursor()) as cursor:
        cursor.execute("SELECT MIN(day) AS first, MAX(day) AS last FROM usage")
        row = cursor.fetchone()
    if row is None or row["first"] is None:
        return None
    return date.fromisoformat(row["first"]), date.fromisoformat(row["last"])
