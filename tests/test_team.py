"""Findings about a team, and the pricing they rest on.

The developer finding names people. Most of the tests about it are about tone rather than
arithmetic, because getting the arithmetic wrong produces a wrong number and getting the tone
wrong gets the product thrown out of a company.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mycroftcompute.analysis.costing import cost, cost_of, median, ranked
from mycroftcompute.analysis.findings import FindingKind, analyse
from mycroftcompute.analysis.team import (
    developer_concentration,
    model_mix,
    repository_concentration,
    routable_segment,
)
from mycroftcompute.economics.prices import price_of
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord
from mycroftcompute.vendors import Vendor


def _usage(records: list[UsageRecord], *, fields: set[str] | None = None) -> ParsedUsage:
    return ParsedUsage(
        vendor=Vendor.ANTHROPIC,
        records=records,
        fields_present=frozenset(
            fields
            or {"day", "model", "input_tokens", "cached_input_tokens", "output_tokens", "user",
                "repo"}
        ),
        rows_seen=len(records),
        rows_accepted=len(records),
        source_kind="metrics",
    )


def _rec(user: str, repo: str = "api", tokens: int = 1_000_000, model: str = "claude-opus-5") -> UsageRecord:
    return UsageRecord(
        day=date(2026, 9, 7),
        model=model,
        input_tokens=tokens,
        cached_input_tokens=tokens * 4,
        cache_write_tokens=tokens // 10,
        output_tokens=tokens // 10,
        user=user,
        repo=repo,
    )


class TestCosting:
    def test_a_record_is_priced_through_every_cache_mechanic(self) -> None:
        # Opus 5: $5/M input, $0.50/M cached read, $6.25/M 5m write, $25/M output.
        record = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            input_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            cache_write_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # 5.00 + 0.50 + 6.25 + 25.00
        assert cost_of(record, price_of("claude-opus-5")) == Decimal("36.75")

    def test_a_one_hour_cache_write_costs_twice_list_not_1_25(self) -> None:
        # The mechanic real Claude Code traffic uses 92% of the time. Pricing it at the
        # five-minute rate understates the dominant case by 60%.
        short = UsageRecord(day=date(2026, 9, 7), model="claude-opus-5", cache_write_tokens=1_000_000)
        long = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            cache_write_tokens=1_000_000,
            cache_write_1h_tokens=1_000_000,
        )
        assert cost_of(short, price_of("claude-opus-5")) == Decimal("6.25")
        assert cost_of(long, price_of("claude-opus-5")) == Decimal("10.00")

    def test_an_unknown_model_is_counted_but_not_costed(self) -> None:
        costed = cost(_usage([_rec("ana", model="claude-imaginary-9")]))
        assert costed.total == 0
        assert costed.unpriced_models == ("claude-imaginary-9",)

    def test_unknown_identity_is_not_a_bucket(self) -> None:
        # None means "this source cannot say". Grouping unknowns under a label invents an owner.
        anonymous = UsageRecord(day=date(2026, 9, 7), model="claude-opus-5", input_tokens=1_000)
        costed = cost(_usage([anonymous]))
        assert costed.by_user == {}
        assert not costed.knows_users

    def test_median_not_mean_because_the_mean_is_dragged_by_the_outlier(self) -> None:
        assert median([Decimal(1), Decimal(1), Decimal(1), Decimal(100)]) == Decimal(1)

    def test_a_reported_cost_far_from_ours_is_measurable(self) -> None:
        priced = _rec("ana")
        reported = UsageRecord(
            day=date(2026, 9, 7), model="claude-opus-5", input_tokens=1_000_000, cost=Decimal(1)
        )
        costed = cost(_usage([priced, reported]))
        assert costed.reported_total == Decimal(1)
        assert costed.discrepancy is not None and costed.discrepancy > 1


# One developer at six times the others: concentrated enough for the finding to fire.
SKEWED = [_rec("ana", tokens=6_000_000), *(_rec(n) for n in ("ben", "cy", "dee", "eve"))]


class TestDeveloperConcentration:
    """Tone tests. This finding names people, and a report that reads as a league table of waste
    gets shown to a team once and deserves to."""

    def test_it_fires_when_spend_is_concentrated(self) -> None:
        parsed = _usage(SKEWED)
        finding = developer_concentration(parsed, cost(parsed))
        assert finding is not None
        assert finding.kind is FindingKind.DEVELOPER_CONCENTRATION
        assert finding.subject == "ana"

    def test_it_says_plainly_that_high_spend_is_not_a_fault(self) -> None:
        parsed = _usage(SKEWED)
        finding = developer_concentration(parsed, cost(parsed))
        assert finding is not None
        blob = " ".join(finding.cannot_tell)
        assert "Whether any of this is a problem" in blob
        assert "most productive" in blob
        assert "not that it went somewhere wrong" in blob

    def test_the_action_is_a_question_not_a_correction(self) -> None:
        parsed = _usage(SKEWED)
        finding = developer_concentration(parsed, cost(parsed))
        assert finding is not None
        assert finding.next_action.startswith("Ask ")
        assert "worth spreading rather than a habit worth stopping" in finding.next_action

    def test_it_never_uses_blaming_language(self) -> None:
        parsed = _usage(SKEWED)
        finding = developer_concentration(parsed, cost(parsed))
        assert finding is not None
        page = " ".join((finding.title, *finding.evidence, *finding.cannot_tell,
                         finding.next_action)).lower()
        for word in ("waste", "wasteful", "excessive", "abuse", "overuse", "culprit",
                     "offender", "worst", "guilty", "problem user"):
            assert word not in page, word

    def test_it_carries_no_ceiling_because_there_is_nothing_to_claim(self) -> None:
        parsed = _usage(SKEWED)
        finding = developer_concentration(parsed, cost(parsed))
        assert finding is not None
        assert finding.ceiling is None

    def test_it_stays_quiet_on_a_small_team(self) -> None:
        # With three people, "concentration" is arithmetic on a handful and tells nobody anything.
        parsed = _usage([_rec("ana", tokens=9_000_000), _rec("ben"), _rec("cy")])
        assert developer_concentration(parsed, cost(parsed)) is None

    def test_an_evenly_split_team_of_five_is_not_concentrated(self) -> None:
        # Three of five people is 60% of an even split. The old rule flagged exactly that.
        parsed = _usage([_rec(n) for n in ("ana", "ben", "cy", "dee", "eve")])
        assert developer_concentration(parsed, cost(parsed)) is None

    def test_on_a_large_team_the_absolute_bar_still_applies(self) -> None:
        # Twenty people: three would hold 15% evenly, so 1.5x that is 22.5% - the 60% floor is
        # what governs, and three people taking most of the budget still fires.
        team = [_rec("ana", tokens=8_000_000), _rec("ben", tokens=8_000_000),
                _rec("cy", tokens=8_000_000)]
        team += [_rec(f"dev{i}", tokens=300_000) for i in range(17)]
        parsed = _usage(team)
        assert developer_concentration(parsed, cost(parsed)) is not None

    def test_it_stays_quiet_when_spend_is_even(self) -> None:
        parsed = _usage([_rec(n) for n in ("ana", "ben", "cy", "dee", "eve", "fay")])
        assert developer_concentration(parsed, cost(parsed)) is None

    def test_it_stays_quiet_when_the_source_has_no_identity(self) -> None:
        anonymous = [
            UsageRecord(day=date(2026, 9, 7), model="claude-opus-5", input_tokens=1_000_000)
            for _ in range(6)
        ]
        parsed = _usage(anonymous)
        assert developer_concentration(parsed, cost(parsed)) is None


class TestRepositoryConcentration:
    def test_it_fires_when_one_repo_dominates(self) -> None:
        parsed = _usage([_rec("ana", "payments", 8_000_000), _rec("ben", "web")])
        finding = repository_concentration(parsed, cost(parsed))
        assert finding is not None
        assert finding.subject == "payments"

    def test_it_frames_the_question_as_priority_not_behaviour(self) -> None:
        parsed = _usage([_rec("ana", "payments", 8_000_000), _rec("ben", "web")])
        finding = repository_concentration(parsed, cost(parsed))
        assert finding is not None
        assert "where the work should be" in " ".join(finding.cannot_tell)
        assert "nothing to fix" in finding.next_action

    def test_it_stays_quiet_with_one_repository(self) -> None:
        parsed = _usage([_rec("ana"), _rec("ben")])
        assert repository_concentration(parsed, cost(parsed)) is None


class TestModelMix:
    """The honest version of the routing pitch: what the swap costs net of the cache it breaks."""

    def test_the_verdict_depends_on_the_token_shape_not_the_list_price(self) -> None:
        # Four parts cached to one part fresh: the fresh tokens still dominate, so the swap wins
        # immediately. The list-price ratio is identical in the next test and the answer is not,
        # which is the entire argument for computing this rather than comparing price tags.
        parsed = _usage([_rec("ana", tokens=1_000_000)])
        finding = model_mix(parsed, cost(parsed))
        assert finding is not None
        assert finding.kind is FindingKind.MODEL_MIX
        assert "claude-haiku-4-5" in finding.title
        assert "no warm-up" in " ".join(finding.evidence)

    def test_an_agent_shaped_prefix_turns_the_same_swap_into_a_slow_one(self) -> None:
        # Real agent traffic is ~141k cached against ~1k fresh. With the fresh portion that
        # small, the cache discount dominates and the same Opus-to-Haiku move has to be earned
        # back over many requests.
        agentish = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            input_tokens=992,
            cached_input_tokens=141_000,
            output_tokens=250,
            user="ana",
            repo="api",
        )
        parsed = _usage([agentish])
        finding = model_mix(parsed, cost(parsed))
        assert finding is not None
        blob = " ".join(finding.evidence)
        assert "pays to write its cache again" in blob
        assert "daily totals do not count sessions" in blob

    def test_a_period_is_never_charged_a_refill_it_cannot_measure(self) -> None:
        # A refill is paid once per session. Pricing a whole period's cached reads as one refill
        # produced "costs $8,300.54 to refill, then saves $5,884.62" - a switch that could
        # never pay off, under a title saying it would.
        agentish = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            input_tokens=992,
            cached_input_tokens=141_000_000,
            output_tokens=250_000,
            user="ana",
            repo="api",
        )
        parsed = _usage([agentish])
        finding = model_mix(parsed, cost(parsed))
        assert finding is not None
        assert "to refill" not in " ".join(finding.evidence)

    def test_it_never_prices_a_period_as_though_it_were_one_request(self) -> None:
        # The arithmetic is per request; a finding feeds it a whole period's tokens. Saying
        # "$20.80 per request" about a period total is a wrong number, not a loose phrase.
        parsed = _usage([_rec("ana", tokens=1_000_000)])
        finding = model_mix(parsed, cost(parsed))
        assert finding is not None
        blob = " ".join(finding.evidence)
        assert "per request" not in blob
        assert "over this period" in blob

    def test_it_warns_that_a_mid_session_swap_destroys_the_cache(self) -> None:
        parsed = _usage([_rec("ana", tokens=1_000_000)])
        finding = model_mix(parsed, cost(parsed))
        assert finding is not None
        assert "session boundaries" in finding.next_action

    def test_it_refuses_to_claim_the_cheaper_model_would_do_the_work(self) -> None:
        parsed = _usage([_rec("ana", tokens=1_000_000)])
        finding = model_mix(parsed, cost(parsed))
        assert finding is not None
        assert "not a judgement about output quality" in " ".join(finding.cannot_tell)

    def test_it_stays_quiet_when_already_on_the_cheapest_model(self) -> None:
        parsed = _usage([_rec("ana", model="claude-haiku-4-5")])
        assert model_mix(parsed, cost(parsed)) is None

    def test_it_stays_quiet_when_no_model_dominates(self) -> None:
        # Balanced by cost rather than by tokens - Opus is five times Sonnet 5's price, so equal
        # token counts would still leave Opus dominant. Three models, because with two it is
        # arithmetically impossible for neither to hold half the bill.
        parsed = _usage(
            [
                _rec("ana", tokens=1_200_000, model="claude-opus-5"),
                _rec("ben", tokens=2_500_000, model="claude-sonnet-5"),
                _rec("cy", tokens=5_000_000, model="claude-haiku-4-5"),
            ]
        )
        costed = cost(parsed)
        # Opus is still the largest single line, just not a dominant one.
        assert ranked(costed.by_model)[0][0] == "claude-opus-5"
        assert model_mix(parsed, costed) is None


class TestRoutableSegment:
    """The part of the bill nobody is waiting on.

    The strongest routing argument in the product, because it sidesteps both objections the other
    findings carry: nobody reads an auxiliary response, and housekeeping requests carry no warm
    session prefix to break.
    """

    @staticmethod
    def _mix(housekeeping_tokens: int = 3_000_000) -> ParsedUsage:
        # Main-path work on a warm cache, plus auxiliary work that is cache-cold - which is what
        # auxiliary traffic actually looks like, and why it is free to move.
        main = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            input_tokens=200_000,
            cached_input_tokens=20_000_000,
            output_tokens=500_000,
            user="ana",
            query_source="main",
            effort="high",
        )
        auxiliary = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            input_tokens=housekeeping_tokens,
            cached_input_tokens=0,
            output_tokens=housekeeping_tokens // 10,
            user="ana",
            query_source="auxiliary",
            effort="low",
        )
        return _usage(
            [main, auxiliary],
            fields={"day", "model", "input_tokens", "cached_input_tokens", "output_tokens",
                    "user", "query_source", "effort"},
        )

    def test_it_fires_on_housekeeping_spend(self) -> None:
        parsed = self._mix()
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        assert finding.kind is FindingKind.ROUTABLE_SEGMENT
        assert finding.subject == "claude-opus-5"

    def test_the_swap_is_free_because_there_is_no_cache_to_break(self) -> None:
        # The whole point. On main-path traffic the same move has to be earned back over many
        # requests; on housekeeping there is nothing to earn back.
        parsed = self._mix()
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        assert "costs nothing to switch" in " ".join(finding.evidence)

    def test_it_says_the_main_path_should_be_left_alone(self) -> None:
        parsed = self._mix()
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        assert "leave the main path alone" in finding.next_action

    def test_it_admits_the_agent_may_not_expose_the_setting(self) -> None:
        parsed = self._mix()
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        blob = " ".join(finding.cannot_tell)
        assert "lets you choose a model for this traffic separately" in blob
        assert "the agent's own judgement about its own request" in blob

    def test_it_stays_quiet_when_the_source_carries_no_segments(self) -> None:
        # The vendor-neutral GenAI conventions have neither query_source nor effort, so this is
        # the common case and it must not produce a finding built on absent data.
        parsed = _usage([_rec("ana")])
        assert routable_segment(parsed, cost(parsed)) is None

    def test_it_stays_quiet_when_housekeeping_is_a_rounding_error(self) -> None:
        parsed = self._mix(housekeeping_tokens=1_000)
        assert routable_segment(parsed, cost(parsed)) is None

    def test_it_stays_quiet_when_housekeeping_already_runs_cheap(self) -> None:
        # A team that already points auxiliary traffic at the cheapest model should be told
        # nothing. There is no cheaper sibling to move to.
        cheap = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-haiku-4-5",
            input_tokens=5_000_000,
            cached_input_tokens=0,
            output_tokens=500_000,
            user="ana",
            query_source="auxiliary",
        )
        parsed = _usage(
            [cheap],
            fields={"day", "model", "input_tokens", "cached_input_tokens", "output_tokens",
                    "user", "query_source"},
        )
        assert routable_segment(parsed, cost(parsed)) is None

    def test_it_says_where_the_housekeeping_dollars_sit_by_developer(self) -> None:
        parsed = self._mix()
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        blob = " ".join(finding.evidence)
        assert "By developer, that housekeeping spend was:" in blob
        assert "ana:" in blob

    def test_it_refuses_to_let_that_list_read_as_a_ranking_of_people(self) -> None:
        # The housekeeping share is a property of how the agent is configured, not of how
        # anybody works. One settings change moves every line at once.
        parsed = self._mix()
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        blob = " ".join(finding.cannot_tell)
        assert "not of how anybody works" in blob
        assert "roughly the same for everyone on a team" in blob

    def test_the_per_developer_list_is_absent_when_identity_is(self) -> None:
        anonymous = UsageRecord(
            day=date(2026, 9, 7),
            model="claude-opus-5",
            input_tokens=5_000_000,
            cached_input_tokens=0,
            output_tokens=500_000,
            query_source="auxiliary",
        )
        parsed = _usage(
            [anonymous],
            fields={"day", "model", "input_tokens", "cached_input_tokens", "output_tokens",
                    "query_source"},
        )
        finding = routable_segment(parsed, cost(parsed))
        assert finding is not None
        assert "By developer" not in " ".join(finding.evidence)

    def test_spend_is_broken_out_by_request_kind(self) -> None:
        costed = cost(self._mix())
        assert set(costed.by_query_source) == {"main", "auxiliary"}
        assert costed.by_query_source["main"] > costed.by_query_source["auxiliary"]
        assert costed.knows_segments


class TestTheDriverOrdersForTheReader:
    def test_team_findings_come_before_export_findings(self) -> None:
        # "Where did the money go" is the question that brought them here.
        # Cold cache, so the export-shaped finding fires too and the ordering is observable.
        def cold(user: str, repo: str, tokens: int) -> UsageRecord:
            return UsageRecord(
                day=date(2026, 9, 7),
                model="claude-opus-5",
                input_tokens=tokens,
                cached_input_tokens=tokens // 20,
                output_tokens=tokens // 10,
                user=user,
                repo=repo,
            )

        records = [
            cold("ana", "payments", 6_000_000),
            *(cold(n, "web", 1_000_000) for n in ("ben", "cy", "dee", "eve")),
        ]
        kinds = [f.kind for f in analyse(_usage(records)).findings]
        assert kinds[0] is FindingKind.DEVELOPER_CONCENTRATION
        assert FindingKind.UNCACHED_INPUT in kinds
        assert kinds.index(FindingKind.DEVELOPER_CONCENTRATION) < kinds.index(
            FindingKind.UNCACHED_INPUT
        )

    def test_the_costing_is_attached_for_the_report_to_lead_with(self) -> None:
        analysis = analyse(_usage([_rec("ana")]))
        assert analysis.costed is not None
        assert analysis.costed.total > 0

    def test_ranked_puts_the_largest_first(self) -> None:
        assert ranked({"a": Decimal(1), "b": Decimal(9)})[0][0] == "b"
