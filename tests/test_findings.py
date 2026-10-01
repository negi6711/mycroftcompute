"""The three findings, and the things they refuse to say.

Half of these tests assert a *refusal*. That is deliberate: the product's whole claim is that its
numbers can be trusted, and the fastest way to lose that is a confident figure the data does not
support. A finding that stays silent when the evidence is thin is working correctly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mycroftcompute.analysis.findings import (
    AUTOMATED_WEEKEND_SHARE,
    CACHE_WRITE_SHARE,
    FindingKind,
    analyse,
    cache_writes,
    continuous_usage,
    premium_over_allocation,
    uncached_input,
)
from mycroftcompute.economics.prices import resolve_model
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord
from mycroftcompute.vendors import Vendor

#: What a token-bearing source reports. Kept explicit because `fields_present` is what the
#: findings gate on - a fixture that quietly claims a column it does not have would let a
#: finding run on absent data, which is the one failure these tests exist to catch.
TOKEN_FIELDS = frozenset(
    {"day", "model", "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"}
)


def _usage(
    records: list[UsageRecord],
    *,
    vendor: Vendor = Vendor.ANTHROPIC,
    fields: frozenset[str] = TOKEN_FIELDS,
) -> ParsedUsage:
    return ParsedUsage(
        vendor=vendor,
        records=records,
        fields_present=fields,
        rows_seen=len(records),
        rows_accepted=len(records),
        source_kind="metrics",
    )


def _rec(
    day: int,
    model: str,
    *,
    fresh: int = 0,
    cached: int = 0,
    writes: int = 0,
    output: int = 0,
    requests: int | None = None,
    user: str | None = None,
) -> UsageRecord:
    return UsageRecord(
        day=date(2026, 8, day),
        model=model,
        input_tokens=fresh,
        cached_input_tokens=cached,
        cache_write_tokens=writes,
        output_tokens=output,
        requests=requests,
        user=user,
    )


#: A team big enough for findings about its shape to mean anything. Spend is split evenly, so no
#: concentration finding fires and a fixture tests only what it is meant to test.
TEAM = ("ana", "ben", "cy", "dee", "eve")


AUGUST = list(range(1, 29))
WEEKDAYS = [d for d in AUGUST if date(2026, 8, d).weekday() < 5]


def _daily(
    model: str, days: list[int], *, team: tuple[str, ...] = (), **counts: int
) -> ParsedUsage:
    """A month of identical rows, optionally one per developer per day.

    Fixtures pick their days deliberately. A workload that runs at weekends trips the
    continuous-usage finding, which would otherwise contaminate a fixture meant to isolate
    caching - as the first draft of this file did.
    """
    fields = TOKEN_FIELDS | ({"requests"} if "requests" in counts else set())
    if team:
        fields |= {"user"}
        records = [_rec(d, model, user=u, **counts) for d in days for u in team]
    else:
        records = [_rec(d, model, **counts) for d in days]  # type: ignore[arg-type]
    return _usage(records, fields=frozenset(fields))


# A month of weekday agent traffic that almost never hits cache: the common case.
COLD = _daily(
    "claude-sonnet-4-6", WEEKDAYS, fresh=9_000_000, cached=200_000, output=150_000
)

# The same workload after someone fixed their prompt ordering.
# Spread across three models so none dominates the bill. A single dominant model gives the
# model-mix finding something real to say, which is correct behaviour but not what this fixture
# is for - it exists to prove the report can be empty.
WARM = _daily(
    "claude-haiku-4-5",
    WEEKDAYS,
    team=TEAM,
    fresh=500_000,
    cached=9_000_000,
    writes=100_000,
    output=150_000,
)

#: A cost-only export: money, no token counts. Google's billing export has this shape, and it is
#: why `knows_cache_split` exists as a gate rather than a zero.
COST_ONLY = ParsedUsage(
    vendor=Vendor.GOOGLE,
    records=[
        UsageRecord(day=date(2026, 8, 1), model="gemini-3-8-flash", cost=Decimal("412.55"))
    ],
    fields_present=frozenset({"day", "model", "cost"}),
    rows_seen=1,
    rows_accepted=1,
    currency="USD",
    source_kind="metrics",
)


class TestUncachedInput:
    def test_a_cold_workload_is_reported_with_its_arithmetic(self) -> None:
        f = uncached_input(COLD)
        assert f is not None
        assert f.kind is FindingKind.UNCACHED_INPUT
        # 20 weekdays x 9,000,000 uncached against 20 x 9,200,000 total.
        assert len(WEEKDAYS) == 20
        assert "2.2% hit rate" in f.evidence[0]
        assert "180,000,000 input tokens were billed at full list price" in f.evidence[1]

    def test_the_dollar_figure_is_the_gap_between_list_and_cached(self) -> None:
        f = uncached_input(COLD)
        assert f is not None
        # 180M tokens x ($3.00 list - $0.30 cached)/M = $486.00
        assert f.ceiling == Decimal("486.000")

    def test_it_is_called_a_ceiling_and_the_reason_is_printed(self) -> None:
        f = uncached_input(COLD)
        assert f is not None
        assert any("ceiling, not a saving" in c for c in f.cannot_tell)
        assert any("does not show prompt contents" in c for c in f.cannot_tell)

    def test_a_workload_that_already_caches_well_is_left_alone(self) -> None:
        assert uncached_input(WARM) is None

    def test_an_export_without_the_column_produces_nothing_rather_than_a_zero(self) -> None:
        parsed = COST_ONLY
        assert not parsed.knows_cache_split
        assert uncached_input(parsed) is None

    def test_and_the_analysis_says_why_that_finding_is_missing(self) -> None:
        a = analyse(COST_ONLY)
        assert any("does not report cached tokens" in n for n in a.notes)

    def test_fable_is_costed_at_its_own_cache_rate_not_the_usual_one(self) -> None:
        # Fable reads cache at 0.025x, not 0.1x, so the gap between list and cached is wider.
        # Using the standard multiplier would understate this ceiling by 7.5% of list.
        fable = _usage([_rec(1, "claude-fable-5-1", fresh=1_000_000, output=50_000)])
        f = uncached_input(fable)
        assert f is not None
        assert f.ceiling == Decimal("9.750")  # 1M x ($10.00 - $0.25)/M


class TestCacheWrites:
    """The money in the cache on a coding agent is in the writes, not the misses.

    LIVE is the shape of a real Claude Code read from Grafana Cloud (24-25 Sep 2026): 96% of input
    served from cache, so the uncached finding is silent, while writes are the largest line.
    """

    LIVE = _usage(
        [_rec(3, "claude-opus-5-5", fresh=1_278, cached=38_281_656, writes=1_670_394, output=227_771)]
    )

    def test_a_warm_agent_workload_still_has_a_cache_finding(self) -> None:
        assert uncached_input(self.LIVE) is None
        finding = cache_writes(self.LIVE)
        assert finding is not None
        assert finding.kind is FindingKind.CACHE_WRITES
        assert finding.title.endswith("of your bill went on writing the prompt cache")

    def test_the_ceiling_is_the_writes_priced_as_reads_instead(self) -> None:
        price = resolve_model("claude-opus-5-5")
        assert price is not None
        writes = Decimal(1_670_394) / Decimal(1_000_000)
        expected = writes * price.cache_write_rate(0) - writes * price.cache_read_rate(0)
        finding = cache_writes(self.LIVE)
        assert finding is not None and finding.ceiling == expected

    def test_it_shows_the_one_hour_figure_without_charging_it(self) -> None:
        finding = cache_writes(self.LIVE)
        assert finding is not None
        text = " ".join(finding.evidence)
        assert "$8.35 at the five-minute write rate" in text
        assert "At the one-hour rate the same writes would cost $13.36" in text
        assert "reads for every token written" in text

    def test_it_says_which_writes_it_cannot_tell_apart(self) -> None:
        finding = cache_writes(self.LIVE)
        assert finding is not None
        assert "ceiling, not a saving" in finding.cannot_tell[0]
        assert "five-minute rate" in finding.cannot_tell[1]

    def test_a_known_one_hour_split_is_billed_at_its_own_rate(self) -> None:
        # Transcripts report which writes were one-hour. Pricing them at the five-minute rate hid
        # a real finding: 27% of a live transcript bill read as 17.5%, under the threshold.
        record = UsageRecord(
            day=date(2026, 8, 3),
            model="claude-opus-5-5",
            input_tokens=1_278,
            cached_input_tokens=38_281_656,
            cache_write_tokens=1_670_394,
            cache_write_1h_tokens=1_500_000,
            output_tokens=227_771,
        )
        finding = cache_writes(_usage([record]))
        assert finding is not None
        price = resolve_model("claude-opus-5-5")
        assert price is not None
        expected = (
            Decimal(170_394) * price.cache_write_rate(0)
            + Decimal(1_500_000) * price.cache_write_1h_rate(0)
            - Decimal(1_670_394) * price.cache_read_rate(0)
        ) / Decimal(1_000_000)
        assert finding.ceiling == expected
        assert "billed at each write's own lifetime" in finding.evidence[0]
        assert not any("five-minute rate" in line for line in finding.cannot_tell)

    def test_writes_below_the_threshold_are_left_alone(self) -> None:
        assert Decimal("0.20") == CACHE_WRITE_SHARE
        assert cache_writes(WARM) is None

    def test_no_writes_means_no_finding(self) -> None:
        assert cache_writes(COLD) is None

    def test_an_export_without_the_cache_split_produces_nothing(self) -> None:
        assert cache_writes(COST_ONLY) is None

    def test_the_driver_runs_it(self) -> None:
        kinds = [f.kind for f in analyse(self.LIVE).findings]
        assert FindingKind.CACHE_WRITES in kinds


class TestPremiumOverAllocation:
    SHORT = _daily(
        "gpt-6-astra", AUGUST, fresh=700_000, output=50_000, requests=1_000
    )

    def test_a_flagship_answering_short_requests_is_flagged_as_a_candidate(self) -> None:
        f = premium_over_allocation(self.SHORT)
        assert f is not None
        assert f.subject == "gpt-6-astra"
        assert "averaging 700 input tokens" in f.title

    def test_it_names_a_specific_cheaper_model_and_prices_both(self) -> None:
        f = premium_over_allocation(self.SHORT)
        assert f is not None
        assert "gpt-5-6-luna" in f.evidence[1]
        # 19.6M tokens at $10/M vs $0.20/M.
        assert f.ceiling == Decimal("192.080")

    def test_it_refuses_to_claim_the_cheaper_model_would_do(self) -> None:
        f = premium_over_allocation(self.SHORT)
        assert f is not None
        assert any("can still be a hard question" in c for c in f.cannot_tell)
        assert "compare the outputs before moving anything" in f.next_action

    def test_long_requests_on_a_flagship_are_not_flagged(self) -> None:
        long_reqs = _daily("gpt-6-astra", [1], fresh=50_000_000, output=500_000, requests=1_000)
        assert premium_over_allocation(long_reqs) is None

    def test_without_a_request_count_the_finding_cannot_run(self) -> None:
        # Average tokens per request is the whole signal; there is no way to infer it.
        no_counts = _usage([_rec(1, "claude-fable-5-1", fresh=700_000, output=50_000)])
        assert premium_over_allocation(no_counts) is None

    def test_a_model_with_no_cheaper_sibling_is_not_flagged(self) -> None:
        cheapest = _daily("gpt-5-6-luna", [1], fresh=700_000, output=50_000, requests=1_000)
        assert premium_over_allocation(cheapest) is None


# Runs every day of the month, weekends included, across a whole team: the shape of a job.
FLAT = _daily("claude-haiku-4-5", AUGUST, team=TEAM, fresh=1_000_000, output=50_000)


class TestContinuousUsage:
    def test_a_workload_that_runs_every_day_is_flagged(self) -> None:
        f = continuous_usage(FLAT)
        assert f is not None
        assert f.kind is FindingKind.CONTINUOUS_USAGE
        assert "28.6%" in f.title  # 8 weekend days out of 28

    def test_it_points_at_the_batch_api_with_its_published_discount(self) -> None:
        f = continuous_usage(FLAT)
        assert f is not None
        assert "flat 50% discount" in f.next_action

    def test_it_does_not_call_the_automation_waste(self) -> None:
        f = continuous_usage(FLAT)
        assert f is not None
        assert any("all deliberate" in c for c in f.cannot_tell)
        assert f.ceiling is None  # nothing here is claimable as money

    def test_weekday_only_traffic_is_not_flagged(self) -> None:
        # August 2026: the 1st and 2nd are a weekend, so start on the 3rd and skip weekends.
        weekdays = _daily(
            "claude-haiku-4-5",
            [d for d in range(3, 29) if date(2026, 8, d).weekday() < 5],
            fresh=1_000_000,
            output=50_000,
        )
        assert continuous_usage(weekdays) is None

    def test_a_window_shorter_than_a_week_says_nothing(self) -> None:
        short = _daily("claude-haiku-4-5", [1, 2, 3], fresh=1_000_000, output=50_000)
        assert continuous_usage(short) is None

    def test_the_threshold_is_the_share_of_a_week_a_weekend_is(self) -> None:
        assert Decimal(2) / Decimal(7) > AUTOMATED_WEEKEND_SHARE

    def test_it_names_whose_traffic_runs_at_the_weekend(self) -> None:
        # The fastest way to tell a job from a person is the identity: a CI service account
        # shows up by name.
        bot = [
            _rec(d, "claude-haiku-4-5", fresh=5_000_000, user="ci-bot@example.com")
            for d in AUGUST
        ]
        people = [
            _rec(d, "claude-haiku-4-5", fresh=50_000, user=u)
            for d in WEEKDAYS
            for u in TEAM
        ]
        f = continuous_usage(_usage(bot + people, fields=TOKEN_FIELDS | {"user"}))
        assert f is not None
        assert f.subject == "ci-bot@example.com"
        assert "ci-bot@example.com: 100.0% of weekend input" in " ".join(f.evidence)


class TestEveryBlindCheckExplainsItself:
    """The report promises that any check it could not run is listed with its reason. These hold
    it to that - a finding that silently fails to run reads exactly like one that looked and
    found nothing."""

    def test_metrics_shaped_data_names_each_missing_dimension(self) -> None:
        notes = " ".join(analyse(FLAT).notes)
        assert "Repository concentration did not run" in notes
        assert "Claude Code does not emit one" in notes
        assert "housekeeping check did not run" in notes
        assert "short-request check did not run" in notes

    def test_a_team_check_that_can_run_is_not_listed(self) -> None:
        notes = " ".join(analyse(FLAT).notes)
        assert "Developer concentration" not in notes

    def test_both_team_checks_share_one_sentence(self) -> None:
        notes = analyse(_daily("claude-haiku-4-5", AUGUST, team=("ana",), fresh=1_000)).notes
        team_notes = [n for n in notes if "Developer concentration" in n]
        assert len(team_notes) == 1
        assert "weekend-usage check" in team_notes[0]

    def test_a_check_with_the_dimension_present_is_not_listed(self) -> None:
        with_requests = _daily("gpt-6-astra", AUGUST, team=TEAM, fresh=700_000, requests=1_000)
        assert not any("short-request" in n for n in analyse(with_requests).notes)


class TestTheWeekendCheckNeedsATeam:
    """With one person, "does not drop at the weekend" just means that person works weekends.
    The first run of this finding against a solo developer's own transcripts called their
    Saturday work a cron job, and the report said it with a straight face."""

    def test_a_solo_developer_working_every_weekend_is_not_a_cron_job(self) -> None:
        solo = _daily("claude-haiku-4-5", AUGUST, team=("ana",), fresh=1_000_000)
        assert continuous_usage(solo) is None

    def test_three_people_is_still_a_few_peoples_habits(self) -> None:
        trio = _daily("claude-haiku-4-5", AUGUST, team=("ana", "ben", "cy"), fresh=1_000_000)
        assert continuous_usage(trio) is None

    def test_four_people_is_enough_to_call_it_a_team(self) -> None:
        four = _daily(
            "claude-haiku-4-5", AUGUST, team=("ana", "ben", "cy", "dee"), fresh=1_000_000
        )
        assert continuous_usage(four) is not None

    def test_no_identity_at_all_stays_silent(self) -> None:
        # A backend that strips user labels could hold one developer or two hundred.
        anonymous = _daily("claude-haiku-4-5", AUGUST, fresh=1_000_000)
        assert continuous_usage(anonymous) is None

    def test_and_the_report_says_why_rather_than_implying_it_looked(self) -> None:
        # Silence for "we looked and found nothing" and silence for "we could not look" must
        # not read the same.
        notes = " ".join(analyse(_daily("claude-haiku-4-5", AUGUST, fresh=1_000_000)).notes)
        assert "weekend-usage check did not run" in notes
        assert "needs developer identity" in notes

    def test_a_small_team_is_told_the_threshold(self) -> None:
        notes = " ".join(
            analyse(
                _daily("claude-haiku-4-5", AUGUST, team=("ana", "ben"), fresh=1_000_000)
            ).notes
        )
        assert "at least 4 identifiable developers" in notes
        assert "this data has 2" in notes

    def test_a_short_window_is_not_worth_a_note(self) -> None:
        # Under a week there is no weekly shape to talk about, identity or not.
        short = _daily("claude-haiku-4-5", [1, 2, 3], fresh=1_000_000)
        assert not any("weekend" in n for n in analyse(short).notes)


class TestUnpricedModels:
    def test_usage_of_an_unknown_model_is_counted_but_not_costed(self) -> None:
        unknown = _usage([_rec(1, "claude-experimental-9", fresh=9_000_000, output=1_000)])
        a = analyse(unknown)
        assert a.unpriced_models == ("claude-experimental-9",)
        assert any("guessing a rate would put a wrong number" in n for n in a.notes)

    def test_a_dated_model_id_still_resolves_to_its_price(self) -> None:
        dated = _usage(
            [_rec(1, "claude-sonnet-4-6-20260514", fresh=9_000_000, cached=200_000, output=1_000)]
        )
        a = analyse(dated)
        assert a.unpriced_models == ()


class TestTheDriver:
    def test_a_cold_agent_workload_produces_the_caching_finding(self) -> None:
        kinds = {f.kind for f in analyse(COLD).findings}
        assert FindingKind.UNCACHED_INPUT in kinds

    def test_a_healthy_export_produces_no_findings_and_says_nothing(self) -> None:
        # Silence is a valid report. Manufacturing a finding to fill the page is how these
        # products lose the reader. A well-cached workload spread across models, with no
        # developer or repository attribution to concentrate, has genuinely nothing to say.
        a = analyse(WARM)
        assert a.findings == []
        # The only notes allowed are declarations of checks this data could not support -
        # never a finding in disguise.
        assert all("did not run" in n for n in a.notes)

    def test_every_finding_carries_evidence_limits_and_an_action(self) -> None:
        for fixture in (COLD, TestPremiumOverAllocation.SHORT):
            for f in analyse(fixture).findings:
                assert f.evidence, f.kind
                assert f.cannot_tell, f.kind
                assert f.next_action, f.kind
                assert f.source_lines, f.kind

    def test_the_vendor_survives_into_the_analysis(self) -> None:
        assert COLD.vendor is Vendor.ANTHROPIC

    def test_findings_can_co_occur(self) -> None:
        # A cold cache on a job that also runs at weekends is two separate problems, and the
        # report should say both rather than stopping at the first.
        both = _daily(
            "claude-sonnet-4-6",
            AUGUST,
            team=TEAM,
            fresh=9_000_000,
            cached=200_000,
            output=150_000,
        )
        kinds = {f.kind for f in analyse(both).findings}
        assert {FindingKind.UNCACHED_INPUT, FindingKind.CONTINUOUS_USAGE} <= kinds


class TestOneVendorEnum:
    """The parser and the price table each had their own Vendor enum for a while. Members of two
    StrEnums are never `is`-identical, so every cross-layer comparison was silently False. Only
    mypy caught it; no test would have, because nothing compared them yet."""

    def test_the_parser_and_the_price_table_share_one_enum(self) -> None:
        from mycroftcompute.economics import prices
        from mycroftcompute.parsers import model

        assert model.Vendor is prices.Vendor is Vendor

    def test_a_parsed_vendor_compares_identical_to_a_priced_one(self) -> None:
        parsed = COLD
        assert parsed.vendor is Vendor.ANTHROPIC
        assert resolve_model("claude-sonnet-4-6") is not None
        assert resolve_model("claude-sonnet-4-6").vendor is parsed.vendor  # type: ignore[union-attr]
