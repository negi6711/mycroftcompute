"""The sample team: an invented company, shown by `mycroftcompute --sample` and on the website.

Nothing here is hand-written output. The team's daily Claude Code metrics are built below in the
same shape a Grafana or Datadog read produces (per developer, model, request kind and effort, per
day), then saved into an ordinary history file by the same `save` a real read goes through.
Everything downstream - the text report and since-last-time - is the real engine
reading that file, so "the arithmetic is real" stays literally true.

The token shapes are anchored on a live Claude Code read (Grafana Cloud, 24-25 Sep 2026): about
96% of input served from cache, cache writes the largest single line, and a trickle of output.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from mycroftcompute.analysis.findings import analyse
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord
from mycroftcompute.store.history import open_store, save
from mycroftcompute.vendors import Vendor

#: The date the sample's prices are checked against. Reports refuse to print from a stale price
#: table, so callers pass today's date; this is only the day the sample was generated.
GENERATED_ON = date(2026, 9, 25)

#: Invented developers at a reserved example domain. Multipliers give the team a realistic
#: shape: three heavy users, a long middle, a few light ones.
TEAM: dict[str, float] = {
    "priya": 4.2,
    "marco": 3.4,
    "sven": 3.0,
    "ana": 1.3,
    "ben": 1.2,
    "chioma": 1.2,
    "dev": 1.1,
    "eli": 1.1,
    "farah": 1.0,
    "gus": 1.0,
    "hana": 1.0,
    "ivan": 0.9,
    "jo": 0.9,
    "kai": 0.9,
    "lena": 0.8,
    "mo": 0.8,
    "nia": 0.8,
    "omar": 0.7,
    "pia": 0.7,
    "raj": 0.6,
    "sam": 0.6,
    "tao": 0.5,
    "uma": 0.5,
    "zoe": 0.4,
}

#: Two developers leave scheduled agent jobs running over weekends, at a fraction of a weekday.
WEEKEND_RUNNERS = {"marco": 0.5, "sven": 0.4}

#: One weekday's traffic for a developer with multiplier 1.0, scaled up to a heavy Opus team.
SCALE = 2.2


def _day_rows(
    housekeeping_model: str, write_factor: float
) -> list[tuple[str, str, str, int, int, int, int]]:
    """(model, request kind, effort, fresh, cached, writes, output) for one developer-day.

    Main work and subagents sit on warm session caches; the agent's own housekeeping calls are
    short and uncached, which is exactly why they are cheap to move.
    """
    return [
        ("claude-opus-5", "main", "high", 1_500, 9_000_000, int(420_000 * write_factor), 70_000),
        (
            "claude-sonnet-5",
            "subagent",
            "medium",
            1_000,
            3_000_000,
            int(200_000 * write_factor),
            40_000,
        ),
        (housekeeping_model, "auxiliary", "low", 180_000, 0, 0, 18_000),
    ]


def _records(
    start: date, days: int, *, housekeeping: str, write_factor: float
) -> list[UsageRecord]:
    out: list[UsageRecord] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        weekend = day.weekday() >= 5
        for person, weight in TEAM.items():
            if weekend:
                if person not in WEEKEND_RUNNERS:
                    continue
                weight = weight * WEEKEND_RUNNERS[person]
            factor = weight * SCALE
            for model, kind, effort, fresh, cached, writes, output in _day_rows(
                housekeeping, write_factor
            ):
                out.append(
                    UsageRecord(
                        day=day,
                        model=model,
                        input_tokens=int(fresh * factor),
                        cached_input_tokens=int(cached * factor),
                        cache_write_tokens=int(writes * factor),
                        output_tokens=int(output * factor),
                        user=f"{person}@example.com",
                        query_source=kind,
                        effort=effort,
                    )
                )
    return out


def _parsed(records: list[UsageRecord]) -> ParsedUsage:
    """Exactly the fields a Claude Code read from Grafana or Datadog carries."""
    return ParsedUsage(
        vendor=Vendor.ANTHROPIC,
        records=records,
        fields_present=frozenset(
            {
                "day",
                "model",
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "user",
                "query_source",
                "effort",
            }
        ),
        rows_seen=len(records),
        rows_accepted=len(records),
        source_kind="metrics",
    )


def august() -> ParsedUsage:
    """The first read: housekeeping runs on Opus, the model everything else uses."""
    return _parsed(_records(date(2026, 8, 1), 28, housekeeping="claude-opus-5", write_factor=1.0))


def september() -> ParsedUsage:
    """The next month: housekeeping moved to Haiku, and a few things that rewrote the cache
    mid-session stopped. Equal 28-day windows, so the comparison is allowed a percentage."""
    return _parsed(
        _records(date(2026, 8, 29), 28, housekeeping="claude-haiku-4-5", write_factor=0.75)
    )


def write_history(path: Path) -> list[int]:
    """Save both reads into a history file, as two monthly pulls would. Returns the pull ids.

    Refuses to overwrite: a sample written over a real history would be a disaster, so
    the caller deletes an old sample file deliberately if it wants a fresh one.
    """
    if path.exists():
        raise FileExistsError(f"{path} already exists; the sample never overwrites a history file")
    connection = open_store(path)
    try:
        ids = []
        for parsed, fetched in (
            (august(), datetime(2026, 8, 29, 9, tzinfo=UTC)),
            (september(), datetime(2026, 9, 26, 9, tzinfo=UTC)),
        ):
            ids.append(save(connection, parsed, analyse(parsed), backend="grafana", now=fetched))
        return ids
    finally:
        connection.close()
