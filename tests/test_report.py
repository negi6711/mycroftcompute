"""The rendered page.

Most of these assert the things the report must never do. Layout can be argued about; a dollar
figure presented as a saving cannot, because a reader who repeats it and is wrong does not come
back.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from mycroftcompute.analysis.findings import FindingKind, analyse
from mycroftcompute.economics.prices import (
    MAX_PRICE_AGE_DAYS,
    PRICES_CHECKED_ON,
)
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord
from mycroftcompute.report.text import WIDTH, render
from mycroftcompute.vendors import Vendor

TODAY = PRICES_CHECKED_ON

AUGUST = list(range(1, 29))
WEEKDAYS = [d for d in AUGUST if date(2026, 8, d).weekday() < 5]


def _usage(model: str, days: list[int], **counts: int) -> ParsedUsage:
    records = [
        UsageRecord(
            day=date(2026, 8, d),
            model=model,
            input_tokens=counts.get("fresh", 0),
            cached_input_tokens=counts.get("cached", 0),
            cache_write_tokens=counts.get("writes", 0),
            output_tokens=counts.get("output", 0),
        )
        for d in days
    ]
    return ParsedUsage(
        vendor=Vendor.ANTHROPIC,
        records=records,
        fields_present=frozenset(
            {
                "day",
                "model",
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
            }
        ),
        rows_seen=len(records),
        rows_accepted=len(records),
        source_kind="metrics",
    )


COLD = _usage(
    "claude-sonnet-4-6", WEEKDAYS, fresh=9_000_000, cached=200_000, output=150_000
)
# Spread across three models so none dominates the bill, because a dominant model gives the
# model-mix finding something real to say. This fixture exists to prove the page can be empty.
WARM = _usage(
    "claude-haiku-4-5", WEEKDAYS, fresh=500_000, cached=9_000_000, writes=100_000, output=150_000
)


def _render(parsed: ParsedUsage, *, today: date = TODAY) -> str:
    return render(parsed, analyse(parsed), today=today)


def _prose(parsed: ParsedUsage, *, today: date = TODAY) -> str:
    """The report with its line breaks collapsed.

    Prose is wrapped to the page width, so a sentence can land across two lines. Asserting on
    the wrapped form would make every copy edit a test failure over something that is not the
    point; line width has its own test.
    """
    return re.sub(r"\s+", " ", _render(parsed, today=today))


class TestTheCeilingIsNeverASaving:
    def test_money_is_introduced_as_a_ceiling(self) -> None:
        assert "At most $486.00 over this period" in _render(COLD)
        assert "It is a ceiling, not a saving" in _prose(COLD)

    def test_the_words_save_and_saving_never_describe_our_figure(self) -> None:
        out = _render(COLD)
        for line in out.splitlines():
            if "$" in line and "at most" not in line.lower():
                assert "you could save" not in line.lower()
                assert "savings of" not in line.lower()

    def test_list_pricing_is_disclosed_as_a_ceiling_too(self) -> None:
        # Negotiated rates and batch tiers only ever make the real figure smaller, and a reader
        # on a committed-use discount should know our number is high before they quote it.
        prose = _prose(COLD)
        assert "Those are list prices" in prose
        assert "correspondingly high" in prose


class TestTheCostDiscrepancy:
    """When a backend reports its own cost and ours differs, the reader gets the likeliest
    explanation rather than the most flattering one."""

    @staticmethod
    def _with_writes(reported: str) -> ParsedUsage:
        # Opus 5.5: $4 input, $0.20 cached read, $5/M 5-minute write, $8/M 1-hour, $20 output.
        record = UsageRecord(
            day=date(2026, 8, 1),
            model="claude-opus-5-5",
            input_tokens=4,
            cached_input_tokens=47_506,
            cache_write_tokens=11_392,
            output_tokens=1_682,
            cost=Decimal(reported),
        )
        return ParsedUsage(
            vendor=Vendor.ANTHROPIC,
            records=[record],
            fields_present=frozenset(
                {"day", "model", "input_tokens", "cached_input_tokens",
                 "cache_write_tokens", "output_tokens", "cost"}
            ),
            rows_seen=1,
            rows_accepted=1,
            source_kind="metrics",
        )

    def test_a_gap_the_one_hour_ttl_explains_says_so(self) -> None:
        # The real numbers from a live Grafana Cloud read. The whole 30% gap was the cache-write
        # TTL, while the page was busy suggesting a negotiated rate.
        page = _prose(self._with_writes("0.14"))
        assert "At the one-hour cache-write rate our figure would be" in page
        # The provenance footer mentions negotiated rates generally; the discrepancy
        # paragraph must not offer them as *this* gap's explanation.
        assert "negotiated rate or a batch tier" not in page

    def test_it_says_which_way_the_uncertainty_runs(self) -> None:
        page = _prose(self._with_writes("0.14"))
        assert "understating rather than inflating" in page

    def test_a_gap_the_ttl_cannot_explain_still_points_at_a_discount(self) -> None:
        # Reported far below our figure: no TTL choice produces that, so it is a rate.
        page = _prose(self._with_writes("0.02"))
        assert "negotiated rate or a batch tier" in page
        assert "one-hour cache-write rate" not in page


class TestProvenance:
    def test_a_metrics_read_cites_no_line_numbers_rather_than_fake_ones(self) -> None:
        # Daily aggregates queried from a backend have no rows to point at. "L0, L0, L0" is a
        # citation to nothing, which is worse than no citation because it looks like one.
        assert "From rows" not in _render(COLD)

    def test_but_the_finding_still_carries_every_row_it_used(self) -> None:
        # The renderer decides not to print them; the finding must still hold them, or a source
        # that does have line numbers would silently under-cite.
        analysis = analyse(COLD)
        caching = next(f for f in analysis.findings if f.kind is FindingKind.UNCACHED_INPUT)
        assert len(caching.source_lines) == len(WEEKDAYS)

    def test_the_price_check_date_is_printed(self) -> None:
        assert PRICES_CHECKED_ON.isoformat() in _render(COLD)

    def test_it_states_that_no_prompts_were_seen(self) -> None:
        assert "have not seen any of your prompts" in _prose(COLD)


class TestAnAgedPriceTable:
    def test_a_stale_table_is_flagged_beside_the_price_date(self) -> None:
        text = _prose(COLD, today=PRICES_CHECKED_ON + timedelta(days=MAX_PRICE_AGE_DAYS + 1))
        assert "Warning: Model prices last checked" in text

    def test_expired_promotional_pricing_is_flagged_too(self) -> None:
        assert "Promotional pricing has ended" in _prose(COLD, today=date(2027, 1, 1))

    def test_a_current_table_carries_no_warning(self) -> None:
        assert "Warning:" not in _prose(COLD)


class TestWhatWeRead:
    def test_singular_nouns_are_singular(self) -> None:
        # "1 model(s)" reads like a draft, and a reader who sees one of those starts looking
        # for the others.
        out = _render(COLD)
        assert "1 model: claude-sonnet-4-6" in out
        assert "model(s)" not in out

    def test_plural_nouns_are_plural(self) -> None:
        two = ParsedUsage(
            vendor=Vendor.ANTHROPIC,
            records=[
                UsageRecord(
                    day=date(2026, 8, 1), model=m, input_tokens=1_000_000, output_tokens=50_000
                )
                for m in ("claude-sonnet-4-6", "claude-haiku-4-5")
            ],
            fields_present=frozenset({"day", "model", "input_tokens", "output_tokens"}),
            rows_seen=2,
            rows_accepted=2,
            source_kind="metrics",
        )
        assert "2 models: claude-haiku-4-5, claude-sonnet-4-6" in _render(two)

    def test_skipped_rows_are_declared(self) -> None:
        # Silently dropping a row we could not read is how a total quietly goes wrong.
        partial = _usage("claude-sonnet-4-6", [1], fresh=1_000_000, output=50_000)
        partial.rows_rejected = 1
        assert "1 skipped because we could not read them" in _render(partial)

    def test_it_says_where_it_read_rather_than_calling_everything_a_file(self) -> None:
        out = _render(COLD)
        assert "your metrics backend" in out
        assert "this file" not in out


class TestMissingCapabilitiesAreDeclared:
    def test_a_source_without_cached_tokens_says_which_finding_it_cost(self) -> None:
        blind = ParsedUsage(
            vendor=Vendor.ANTHROPIC,
            records=[
                UsageRecord(
                    day=date(2026, 8, 1), model="claude-sonnet-4-6",
                    input_tokens=9_000_000, output_tokens=150_000,
                )
            ],
            fields_present=frozenset({"day", "model", "input_tokens", "output_tokens"}),
            rows_seen=1,
            rows_accepted=1,
            source_kind="metrics",
        )
        assert "does not report cached tokens" in _prose(blind)

    def test_a_cost_only_source_reports_what_was_billed_and_says_it_is_theirs(self) -> None:
        # Money but no token counts: their figure, not ours, and we cannot check it.
        cost_only = ParsedUsage(
            vendor=Vendor.GOOGLE,
            records=[
                UsageRecord(
                    day=date(2026, 8, 1), model="gemini-3-8-flash", cost=Decimal("412.55")
                )
            ],
            fields_present=frozenset({"day", "model", "cost"}),
            rows_seen=1,
            rows_accepted=1,
            currency="USD",
            source_kind="metrics",
        )
        out = _prose(cost_only)
        assert "$412.55 as reported by your source" in out
        assert "their figure rather than ours" in out


class TestLayout:
    def test_nothing_overflows_the_page_width(self) -> None:
        # Reports get pasted into email and Slack, where a wrapped table becomes unreadable.
        for fixture in (COLD, WARM):
            for line in _render(fixture).splitlines():
                assert len(line) <= WIDTH, line

    def test_the_cannot_tell_section_appears_for_every_finding(self) -> None:
        out = _render(COLD)
        headings = out.count("What your metrics backend cannot tell you")
        findings = out.count("FINDING ")
        assert headings == findings > 0
