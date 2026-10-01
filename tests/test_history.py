"""Our own history, and the comparison that makes this a subscription.

Two invariants carry most of these tests.

**A re-pull corrects, it never doubles.** A 30-day window read daily overlaps 29 days with the
last read. Appending would multiply a month's spend by thirty, and it would do it silently -
every figure still plausible, every one wrong.

**The comparison reports what moved, not what we achieved.** A finding that stopped firing is a
finding that stopped firing; a quieter month is a quieter month. Claiming either as a saving is
the fastest way to lose a reader who knows why their bill actually changed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from mycroftcompute.analysis.findings import Analysis, Finding, FindingKind, analyse
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord
from mycroftcompute.report.text import render
from mycroftcompute.store.history import compare, finding_key, open_store, save, stored_days
from mycroftcompute.vendors import Vendor

FIELDS = frozenset(
    {
        "day",
        "model",
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
        "user",
        "repo",
    }
)


def _usage(days: list[date], *, fresh: int, cached: int, user: str | None = "ana") -> ParsedUsage:
    records = [
        UsageRecord(
            day=d,
            model="claude-opus-5",
            input_tokens=fresh,
            cached_input_tokens=cached,
            output_tokens=fresh // 20,
            user=user,
            repo="payments",
        )
        for d in days
    ]
    return ParsedUsage(
        vendor=Vendor.ANTHROPIC,
        records=records,
        fields_present=FIELDS,
        rows_seen=len(records),
        rows_accepted=len(records),
        source_kind="metrics",
    )


AUGUST = [date(2026, 8, d) for d in range(1, 29)]
SEPTEMBER = [date(2026, 9, d) for d in range(1, 29)]

# A cold cache, then the same workload after someone fixed their prompt ordering.
COLD = _usage(AUGUST, fresh=9_000_000, cached=200_000)
WARM = _usage(SEPTEMBER, fresh=500_000, cached=9_000_000)


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    connection = open_store(tmp_path / "history.db")
    yield connection
    connection.close()


_COUNTS = {
    "usage": "SELECT COUNT(*) FROM usage",
    "pull": "SELECT COUNT(*) FROM pull",
    "finding": "SELECT COUNT(*) FROM finding",
}


def _rows(connection, table: str) -> int:  # type: ignore[no-untyped-def]
    return int(connection.execute(_COUNTS[table]).fetchone()[0])


class TestARePullCorrectsRatherThanDoubles:
    def test_the_same_window_read_twice_holds_one_copy(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        save(store, COLD, analyse(COLD))
        assert _rows(store, "usage") == len(AUGUST)

    def test_overlapping_windows_keep_the_union_not_the_sum(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        overlapping = _usage(AUGUST[14:] + SEPTEMBER[:7], fresh=1_000, cached=0)
        save(store, overlapping, analyse(overlapping))
        assert _rows(store, "usage") == len(AUGUST) + 7

    def test_a_corrected_day_takes_the_newer_figure(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        corrected = _usage([AUGUST[0]], fresh=42, cached=0)
        save(store, corrected, analyse(corrected))
        row = store.execute(
            "SELECT input_tokens FROM usage WHERE day = ?", (AUGUST[0].isoformat(),)
        ).fetchone()
        assert row["input_tokens"] == 42

    def test_rows_with_no_identity_still_deduplicate(self, store) -> None:  # type: ignore[no-untyped-def]
        # SQLite treats NULLs as distinct in a unique index, so a backend with no user label
        # would have appended a fresh copy of every row on every run. The natural key folds
        # NULLs to a sentinel for exactly this.
        anonymous = _usage(AUGUST, fresh=1_000, cached=0, user=None)
        save(store, anonymous, analyse(anonymous))
        save(store, anonymous, analyse(anonymous))
        assert _rows(store, "usage") == len(AUGUST)

    def test_the_history_outlives_what_the_backend_keeps(self, store) -> None:  # type: ignore[no-untyped-def]
        # The whole reason this exists: Grafana Cloud's free tier drops metrics at 14 days.
        save(store, COLD, analyse(COLD))
        save(store, WARM, analyse(WARM))
        held = stored_days(store)
        assert held == (AUGUST[0], SEPTEMBER[-1])
        assert (held[1] - held[0]).days > 14


class TestTheComparison:
    def test_the_first_read_has_nothing_to_compare_against(self, store) -> None:  # type: ignore[no-untyped-def]
        since = compare(store, save(store, COLD, analyse(COLD)))
        assert not since.has_history
        assert since.movements == []

    def test_a_finding_that_stopped_firing_is_reported_as_stopped(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        since = compare(store, save(store, WARM, analyse(WARM)))
        resolved = [m for m in since.movements if m.state == "resolved"]
        assert any(m.finding_key.startswith(FindingKind.UNCACHED_INPUT.value) for m in resolved)

    def test_a_finding_that_persists_carries_both_ceilings(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        since = compare(store, save(store, WARM, analyse(WARM)))
        moved = next(m for m in since.movements if m.state == "continued" and m.delta)
        assert moved.was is not None and moved.now is not None
        assert moved.now < moved.was

    def test_a_new_finding_is_marked_new(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, WARM, analyse(WARM))
        since = compare(store, save(store, COLD, analyse(COLD)))
        assert any(m.state == "appeared" for m in since.movements)

    def test_spend_is_compared_when_the_windows_match(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        since = compare(store, save(store, WARM, analyse(WARM)))
        assert since.comparable_periods
        assert since.spend_delta is not None and since.spend_delta < 0

    def test_rereading_the_same_days_is_not_a_last_time(self, store) -> None:  # type: ignore[no-untyped-def]
        # A correction, not a new period. Comparing it printed "24-27 Sep against 24-26 Sep".
        save(store, COLD, analyse(COLD))
        since = compare(store, save(store, COLD, analyse(COLD)))
        assert not since.has_history

    def test_an_overlapping_read_skips_back_to_the_last_distinct_period(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        save(store, WARM, analyse(WARM))
        rerun = _usage(SEPTEMBER[3:], fresh=500_000, cached=9_000_000)
        since = compare(store, save(store, rerun, analyse(rerun)))
        assert since.previous_period == (AUGUST[0], AUGUST[-1])

    def test_unequal_windows_refuse_the_percentage(self, store) -> None:  # type: ignore[no-untyped-def]
        # A 28-day read against a 7-day one produces a 75% "fall" that is entirely a calendar
        # artefact, and a reader who quotes it looks foolish.
        save(store, COLD, analyse(COLD))
        short = _usage(SEPTEMBER[:7], fresh=9_000_000, cached=200_000)
        since = compare(store, save(store, short, analyse(short)))
        assert not since.comparable_periods

    def test_the_key_is_stable_across_runs_but_splits_by_subject(self) -> None:
        def f(subject: str) -> Finding:
            return Finding(
                kind=FindingKind.MODEL_MIX,
                title="t",
                evidence=(),
                cannot_tell=(),
                next_action="a",
                subject=subject,
            )

        assert finding_key(f("claude-opus-5")) == finding_key(f("claude-opus-5"))
        assert finding_key(f("claude-opus-5")) != finding_key(f("gpt-6-astra"))


class TestThePage:
    def _page(self, store) -> str:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD))
        since = compare(store, save(store, WARM, analyse(WARM)))
        return render(WARM, analyse(WARM), today=date(2026, 9, 23), since=since)

    def test_it_names_both_periods_rather_than_just_the_change(self, store) -> None:  # type: ignore[no-untyped-def]
        assert "Comparing 2026-09-01 to 2026-09-28 against 2026-08-01 to 2026-08-28" in self._page(
            store
        )

    def test_it_refuses_to_take_credit_for_a_finding_that_stopped(self, store) -> None:  # type: ignore[no-untyped-def]
        page = self._page(store)
        assert "No longer firing" in page
        assert "we are not going to claim it did" in page

    def test_a_first_read_prints_no_comparison_at_all(self, store) -> None:  # type: ignore[no-untyped-def]
        since = compare(store, save(store, COLD, analyse(COLD)))
        assert "SINCE LAST TIME" not in render(
            COLD, analyse(COLD), today=date(2026, 9, 23), since=since
        )


class TestWhatIsStored:
    def test_a_read_records_its_period_backend_and_total(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, analyse(COLD), backend="grafana", now=datetime(2026, 9, 1, tzinfo=UTC))
        row = store.execute("SELECT * FROM pull").fetchone()
        assert row["backend"] == "grafana"
        assert row["first_day"] == "2026-08-01"
        assert Decimal(row["total_cost"]) > 0
        assert row["vendors"] == "anthropic"

    def test_an_empty_analysis_stores_a_pull_with_no_findings(self, store) -> None:  # type: ignore[no-untyped-def]
        save(store, COLD, Analysis())
        assert _rows(store, "pull") == 1
        assert _rows(store, "finding") == 0

    def test_opening_an_existing_store_does_not_disturb_it(self, tmp_path: Path) -> None:
        path = tmp_path / "history.db"
        first = open_store(path)
        save(first, COLD, analyse(COLD))
        first.close()

        second = open_store(path)
        assert _rows(second, "usage") == len(AUGUST)
        second.close()
