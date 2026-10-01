"""The cache-economics arithmetic, checked against real published numbers.

The agent-loop shape used throughout comes from TraceLab's measurement of coding-agent workloads:
the median step replays ~141k cached prefix tokens and appends ~992 fresh ones, across roughly
6.7 tool-call steps per response. That shape is where every naive router goes wrong, so it is
where the tests live.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from mycroftcompute.economics.caching import (
    RequestShape,
    SwapVerdict,
    break_even_price,
    can_cache,
    cost_cold,
    cost_warm,
    cost_writing_cache,
    evaluate_swap,
    explicit_cache_storage_cost,
)
from mycroftcompute.economics.prices import (
    MAX_PRICE_AGE_DAYS,
    PRICES_CHECKED_ON,
    TABLE,
    StalePriceTableError,
    models_by_vendor,
    price_of,
    verify_prices,
)
from mycroftcompute.vendors import Vendor


def _cost_at(tokens: int, per_mtok: Decimal) -> Decimal:
    return Decimal(tokens) * per_mtok / Decimal(1_000_000)

SONNET = price_of("claude-sonnet-4-6")
HAIKU = price_of("claude-haiku-4-5")
FLASH_LITE = price_of("gemini-3-1-flash-lite")
OPUS = price_of("claude-opus-5")

# TraceLab median coding-agent step.
AGENT_STEP = RequestShape(cached_prefix_tokens=141_000, fresh_input_tokens=992)

# A short one-off call: below every model's cache minimum, so no cache exists to lose.
ONE_SHOT = RequestShape(cached_prefix_tokens=0, fresh_input_tokens=800)


class TestTheNumbersThemselves:
    def test_a_warm_agent_step_is_billed_almost_entirely_at_the_cached_rate(self) -> None:
        # 141,000 x $0.30/M + 992 x $3.00/M
        assert cost_warm(SONNET, AGENT_STEP) == Decimal("0.045276")

    def test_the_same_step_without_a_cache_costs_nine_times_more(self) -> None:
        # This is what a router that flaps between models pays on every single request.
        cold = cost_cold(SONNET, AGENT_STEP)
        assert cold == Decimal("0.425976")
        assert cold / cost_warm(SONNET, AGENT_STEP) > Decimal("9")

    def test_the_bar_a_target_must_beat_is_the_source_cached_rate(self) -> None:
        # $3.00/M list, 0.1x cached, so a target has to list below $0.30/M to win outright on
        # the first request - more than ten times cheaper than the source.
        assert break_even_price(SONNET) == Decimal("0.300")
        assert HAIKU.input_per_mtok > break_even_price(SONNET)
        assert FLASH_LITE.input_per_mtok < break_even_price(SONNET)


class TestSwappingAnAgentStep:
    """The headline case: the first request after a swap is far more expensive, but a workload
    that stays on the cheaper model rebuilds its own cache and comes out ahead. Committing is
    what pays; flapping is what ruins you."""

    def test_the_first_request_after_a_swap_to_haiku_costs_three_times_more(self) -> None:
        e = evaluate_swap(SONNET, HAIKU, AGENT_STEP, source_cache_warm=True)
        assert e.source_cost == Decimal("0.045276")
        assert e.target_first_request == Decimal("0.177242")
        assert e.switch_cost > 0
        assert e.target_first_request / e.source_cost > Decimal("3")

    def test_but_it_pays_off_within_about_one_agent_response(self) -> None:
        e = evaluate_swap(SONNET, HAIKU, AGENT_STEP, source_cache_warm=True)
        assert e.verdict is SwapVerdict.CHEAPER_AFTER_WARMUP
        # ~6.7 tool-call steps make up one agent response, so this breaks even inside a single
        # response - but only for a router that keeps the whole session on the new model.
        assert e.break_even_requests == 6
        assert e.saves

    def test_a_target_more_than_ten_times_cheaper_wins_from_the_first_request(self) -> None:
        e = evaluate_swap(SONNET, FLASH_LITE, AGENT_STEP, source_cache_warm=True)
        assert e.verdict is SwapVerdict.CHEAPER_IMMEDIATELY
        assert e.break_even_requests == 0
        assert e.switch_cost < 0

    def test_moving_up_in_price_never_saves_however_long_you_wait(self) -> None:
        e = evaluate_swap(HAIKU, OPUS, AGENT_STEP, source_cache_warm=True)
        assert e.verdict is SwapVerdict.LOSES_TO_THE_CACHE
        assert e.break_even_requests is None
        assert not e.saves


class TestTheSubMinimumTrap:
    """Cache minimums differ by model - Sonnet caches from 1,024 tokens, Haiku only from 4,096.
    A prefix between the two is cacheable on the source and permanently uncacheable on the
    target, so the swap is a loss no volume can repair."""

    MID = RequestShape(cached_prefix_tokens=2_000, fresh_input_tokens=500)

    def test_haiku_cannot_cache_a_prefix_sonnet_can(self) -> None:
        assert can_cache(SONNET, self.MID)
        assert not can_cache(HAIKU, self.MID)

    def test_so_the_swap_is_refused_and_says_why(self) -> None:
        e = evaluate_swap(SONNET, HAIKU, self.MID, source_cache_warm=True)
        assert e.verdict is SwapVerdict.TARGET_CANNOT_CACHE
        assert e.break_even_requests is None
        assert "4,096-token cache minimum" in e.explain()


class TestWhenThereIsNoCacheToLose:
    """Roughly 72% of LLM calls show no cached reads at all. For those the cache argument does
    not apply and routing is the naive price comparison everyone already makes."""

    def test_a_short_one_off_call_moves_for_free(self) -> None:
        e = evaluate_swap(SONNET, HAIKU, ONE_SHOT, source_cache_warm=False)
        assert e.verdict is SwapVerdict.FREE_TO_MOVE
        assert e.break_even_requests == 0
        assert e.steady_saving_per_request == Decimal("0.0016")

    def test_a_claimed_warm_cache_below_the_minimum_is_not_believed(self) -> None:
        # Callers pass what the vendor reported. A sub-minimum prefix cannot have been cached,
        # so the arithmetic overrides the claim rather than inventing a discount.
        e = evaluate_swap(SONNET, HAIKU, ONE_SHOT, source_cache_warm=True)
        assert e.source_cache_warm is False
        assert e.source_cost == cost_cold(SONNET, ONE_SHOT)


class TestGuards:
    def test_negative_token_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            RequestShape(cached_prefix_tokens=-1, fresh_input_tokens=0)

    def test_an_unknown_model_names_itself_rather_than_guessing(self) -> None:
        with pytest.raises(KeyError, match="No price on file"):
            price_of("claude-imaginary-9")

    def test_a_stale_price_table_refuses_to_publish(self) -> None:
        verify_prices(PRICES_CHECKED_ON)  # fresh: fine
        with pytest.raises(StalePriceTableError, match="Re-check each vendor"):
            verify_prices(PRICES_CHECKED_ON + timedelta(days=MAX_PRICE_AGE_DAYS + 1))

    def test_expired_promotional_pricing_is_caught_separately(self) -> None:
        # Gemini 3.8 Flash's entire price line doubles on 1 January 2027. The table will not be
        # *stale* that morning - it will be freshly checked and wrong, which staleness cannot
        # catch. Quoting a saving at half the real price is the failure this prevents.
        with pytest.raises(StalePriceTableError) as caught:
            verify_prices(date(2027, 1, 1))
        message = str(caught.value)
        assert "Promotional pricing has ended" in message
        assert "gemini-3-8-flash" in message
        # Both problems are reported, not just whichever trips first: by then the table is also
        # overdue a check, and fixing one would otherwise only reveal the other.
        assert "Re-check each vendor" in message

    def test_every_price_with_an_end_date_is_still_valid_today(self) -> None:
        for model in TABLE.values():
            if model.price_valid_until is not None:
                assert model.price_valid_until >= PRICES_CHECKED_ON, model.model_id

    def test_every_explanation_contains_both_model_names(self) -> None:
        for target in (HAIKU, FLASH_LITE, OPUS):
            for warm in (True, False):
                e = evaluate_swap(SONNET, target, AGENT_STEP, source_cache_warm=warm)
                assert SONNET.model_id in e.explain()
                assert target.model_id in e.explain()


class TestAllThreeVendors:
    """Anthropic, OpenAI and Google hold ~88% of enterprise LLM spend between them and most
    production systems run two or three at once, so the table has to cover all three or it is
    wrong for the majority of who we serve."""

    def test_every_vendor_has_a_priced_ladder(self) -> None:
        for vendor in Vendor:
            ladder = models_by_vendor(vendor)
            assert len(ladder) >= 2, f"{vendor} needs at least a premium and a cheap model"
            assert ladder[0].input_per_mtok < ladder[-1].input_per_mtok

    def test_cached_reads_are_a_tenth_of_list_except_where_the_vendor_says_otherwise(self) -> None:
        # The single number the whole routing argument turns on. If a vendor ever changes it,
        # this test is where we find out - and the exception list is why it is a list rather
        # than one name: a live read turned up Opus 5.5 at 0.05x, which the old assertion
        # would have quietly accepted as 0.1x and double-counted every cached token.
        for model in TABLE.values():
            expected = CACHE_READ_EXCEPTIONS.get(model.model_id, Decimal("0.1"))
            assert model.cache_read_multiplier == expected, model.model_id

    def test_a_cheaper_cache_read_makes_a_model_harder_to_route_away_from(self) -> None:
        # Opus 5.5 lists at $4/M but bills a warm request at $0.20/M. A target has to beat
        # that, not the list price, which is the whole break-even rule.
        opus55 = price_of("claude-opus-5-5")
        assert break_even_price(opus55) == Decimal("0.20")
        assert break_even_price(opus55) < break_even_price(price_of("claude-opus-5"))


#: Models that do not use the standard 0.1x cache-read multiplier, each verified against the
#: vendor's own pricing page. Fable 5.1 reads at 2.5% of list; Opus 5.5 at 5%.
CACHE_READ_EXCEPTIONS = {
    "claude-fable-5-1": Decimal("0.025"),
    "claude-opus-5-5": Decimal("0.05"),
}


class TestFableIsAlmostNeverWorthRoutingAwayFrom:
    """Claude Fable 5.1 charges 2.5% of list for a cache hit rather than 10%, so a warm Fable
    request is already billed at $0.25/M against a $10/M list price - a 40x discount. That makes
    the bar a cheaper model has to clear four times lower than the usual rule implies."""

    FABLE = price_of("claude-fable-5-1")

    def test_a_cache_hit_costs_two_and_a_half_percent_of_list(self) -> None:
        assert self.FABLE.input_per_mtok == Decimal("10.00")
        assert self.FABLE.cache_read_rate(0) == Decimal("0.250")

    def test_so_warm_fable_undercuts_sonnet_5_at_list_price(self) -> None:
        # $0.25/M cached against $2.00/M list: eight times cheaper to stay put.
        assert self.FABLE.cache_read_rate(0) < price_of("claude-sonnet-5").input_per_mtok

    def test_leaving_a_warm_fable_prefix_costs_nearly_eight_times_the_step_it_replaces(self) -> None:
        # Sonnet 5 is five times cheaper at list, so the naive comparison says move. The first
        # request says otherwise: $0.354484 against $0.045170 for staying.
        e = evaluate_swap(self.FABLE, price_of("claude-sonnet-5"), AGENT_STEP, source_cache_warm=True)
        assert e.source_cost == Decimal("0.045170")
        assert e.target_first_request == Decimal("0.354484")
        assert e.target_first_request / e.source_cost > Decimal("7.8")

    def test_and_takes_three_full_agent_responses_to_pay_back_rather_than_one(self) -> None:
        # Sonnet 4.6 -> Haiku 4.5 breaks even after 6 requests, about one agent response. Leaving
        # Fable takes 22, because its warm rate is so low there is far less to save. Same rule,
        # very different patience required - which is why the number is computed, not assumed.
        e = evaluate_swap(self.FABLE, price_of("claude-sonnet-5"), AGENT_STEP, source_cache_warm=True)
        assert e.verdict is SwapVerdict.CHEAPER_AFTER_WARMUP
        assert e.break_even_requests == 22

    def test_exactly_one_model_undercuts_a_warm_fable_prefix_outright(self) -> None:
        bar = break_even_price(self.FABLE)  # $0.25/M
        cheaper = sorted(m.model_id for m in TABLE.values() if m.input_per_mtok < bar)
        assert cheaper == ["gpt-5-6-luna"]

    def test_and_that_one_wins_from_the_first_request(self) -> None:
        e = evaluate_swap(self.FABLE, price_of("gpt-5-6-luna"), AGENT_STEP, source_cache_warm=True)
        assert e.verdict is SwapVerdict.CHEAPER_IMMEDIATELY
        assert e.break_even_requests == 0

    def test_but_a_cold_fable_request_is_the_most_expensive_thing_here(self) -> None:
        # The flip side: with no cache, Fable is $10/M. The finding is "warm your cache", not
        # "switch model" - which is the whole argument for doing caching before routing.
        cold = cost_cold(self.FABLE, AGENT_STEP)
        warm = cost_warm(self.FABLE, AGENT_STEP)
        assert cold / warm > Decimal("28")

    def test_gemini_needs_four_times_sonnets_prefix_before_it_caches_anything(self) -> None:
        assert price_of("gemini-3-1-flash-lite").min_cacheable_tokens == 4096
        assert price_of("claude-sonnet-4-6").min_cacheable_tokens == 1024


class TestGoogleHasNoWarmUpCost:
    """Google's implicit caching is on by default and costs nothing to fill, while Anthropic and
    OpenAI charge 1.25x list on the request that writes the cache. That makes Gemini the cheapest
    kind of model to move *to* - there is no investment to earn back."""

    def test_filling_a_gemini_cache_costs_no_more_than_a_normal_request(self) -> None:
        flash = price_of("gemini-3-8-flash")
        assert cost_writing_cache(flash, AGENT_STEP) == cost_cold(flash, AGENT_STEP)

    def test_whereas_anthropic_and_openai_charge_a_premium_to_fill_one(self) -> None:
        for model_id in ("claude-haiku-4-5", "gpt-5-4-mini"):
            m = price_of(model_id)
            assert cost_writing_cache(m, AGENT_STEP) > cost_cold(m, AGENT_STEP)

    def test_google_still_has_a_warm_up_because_a_cold_cache_pays_list(self) -> None:
        # The write premium is not the only switch cost. Even with no premium, the first request
        # pays list on 141k tokens where the source paid a tenth of that. Missing this is how
        # you would talk yourself into flapping.
        e = evaluate_swap(SONNET, price_of("gemini-3-8-flash"), AGENT_STEP, source_cache_warm=True)
        assert e.verdict is SwapVerdict.CHEAPER_AFTER_WARMUP
        assert e.switch_cost > 0

    def test_but_it_breaks_even_sooner_than_an_identically_priced_rival(self) -> None:
        # Gemini 3.8 Flash and GPT-5.4 Mini both list at $0.75/M. The only difference is that
        # Google does not charge to fill the cache, and that alone halves the payback period.
        gemini = price_of("gemini-3-8-flash")
        openai = price_of("gpt-5-4-mini")
        assert gemini.input_per_mtok == openai.input_per_mtok

        g = evaluate_swap(SONNET, gemini, AGENT_STEP, source_cache_warm=True)
        o = evaluate_swap(SONNET, openai, AGENT_STEP, source_cache_warm=True)
        assert g.switch_cost < o.switch_cost
        assert g.break_even_requests is not None and o.break_even_requests is not None
        assert g.break_even_requests < o.break_even_requests


class TestTheMidSizePrefixIsStranded:
    """A 2,000-token prefix caches on Sonnet (1,024 minimum) and on no cheap model at all: Haiku
    4.5 and every Gemini 3.x need 4,096. Mid-size prefixes are effectively unroutable."""

    MID = RequestShape(cached_prefix_tokens=2_000, fresh_input_tokens=500)

    def test_no_cheap_target_can_cache_what_sonnet_caches(self) -> None:
        assert can_cache(SONNET, self.MID)
        for target_id in ("claude-haiku-4-5", "gemini-3-8-flash", "gemini-3-1-flash-lite"):
            assert not can_cache(price_of(target_id), self.MID), target_id

    def test_a_target_that_cannot_cache_is_refused_when_that_makes_it_lose(self) -> None:
        e = evaluate_swap(SONNET, price_of("claude-haiku-4-5"), self.MID, source_cache_warm=True)
        assert e.verdict is SwapVerdict.TARGET_CANNOT_CACHE
        assert e.break_even_requests is None

    def test_but_a_cheap_enough_target_still_wins_paying_full_price(self) -> None:
        # Gemini 3.8 Flash cannot cache this prefix either, yet at $0.75/M it still beats
        # Sonnet - because on a short request most of the cost is the fresh tokens, which
        # Sonnet bills at $3.00/M. "Cannot cache" is not a synonym for "costs more".
        e = evaluate_swap(SONNET, price_of("gemini-3-8-flash"), self.MID, source_cache_warm=True)
        assert not can_cache(price_of("gemini-3-8-flash"), self.MID)
        assert e.saves


class TestLongContextSurcharge:
    """Gemini 3.1 Pro doubles its input rate above 200k tokens. Agent traffic sits just under
    that line, which makes it exactly the kind of cliff a router must not walk off."""

    PRO = price_of("gemini-3-1-pro")

    def test_under_the_threshold_bills_at_list(self) -> None:
        assert self.PRO.input_rate(199_000) == Decimal("2.00")

    def test_over_it_bills_at_double(self) -> None:
        assert self.PRO.input_rate(201_000) == Decimal("4.00")

    def test_and_the_cached_rate_doubles_with_it(self) -> None:
        assert self.PRO.cache_read_rate(201_000) == Decimal("0.400")

    def test_a_swap_that_crosses_the_cliff_costs_what_the_cliff_costs(self) -> None:
        big = RequestShape(cached_prefix_tokens=210_000, fresh_input_tokens=1_000)
        assert cost_cold(self.PRO, big) == _cost_at(211_000, Decimal("4.00"))


class TestExplicitCacheStorage:
    """Google charges by the hour to hold an *explicit* cache; nobody else does, and Google's
    implicit cache is free to hold. On a low-traffic workload the storage can exceed the saving,
    which is a finding the report should be able to make."""

    def test_only_google_charges_storage(self) -> None:
        assert explicit_cache_storage_cost(price_of("claude-sonnet-4-6"), 100_000, Decimal(1)) == 0
        assert explicit_cache_storage_cost(price_of("gpt-5-6-sol"), 100_000, Decimal(1)) == 0
        assert explicit_cache_storage_cost(
            price_of("gemini-3-8-flash"), 100_000, Decimal(1)
        ) == Decimal("0.05")

    def test_holding_a_big_cache_on_pro_for_a_day_is_not_free(self) -> None:
        # 141k tokens at $4.50/M/hr for 24 hours.
        cost = explicit_cache_storage_cost(price_of("gemini-3-1-pro"), 141_000, Decimal(24))
        assert cost == Decimal("15.228")

    def test_an_explicit_cache_on_flash_pays_from_about_one_request_an_hour(self) -> None:
        flash = price_of("gemini-3-8-flash")
        saved_per_request = cost_cold(flash, AGENT_STEP) - cost_warm(flash, AGENT_STEP)
        stored_per_hour = explicit_cache_storage_cost(flash, 141_000, Decimal(1))
        assert saved_per_request > stored_per_hour  # one request an hour already covers it

    def test_but_on_pro_it_needs_roughly_three_requests_an_hour(self) -> None:
        # Pro holds at $4.50/M/hr against Flash's $0.50, so the same cache needs far more
        # traffic to justify holding. Below that, implicit caching - free to hold - wins.
        pro = price_of("gemini-3-1-pro")
        saved_per_request = cost_cold(pro, AGENT_STEP) - cost_warm(pro, AGENT_STEP)
        stored_per_hour = explicit_cache_storage_cost(pro, 141_000, Decimal(1))
        assert saved_per_request < stored_per_hour
        requests_needed = math.ceil(stored_per_hour / saved_per_request)
        assert requests_needed == 3
