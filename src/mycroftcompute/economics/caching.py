"""Whether moving a request to a cheaper model actually costs less.

The arithmetic every LLM router gets wrong. A cached input token costs 0.1x the list price on all
three major vendors, and the cache key includes the model identifier. So a request sitting on a
warm cache is already billed at a 90% discount, and re-routing it throws that away.

Measured on real coding-agent workloads - the median step replays ~141k cached prefix tokens
against ~1k fresh ones - moving from Sonnet 4.6 to Haiku 4.5:

    stay on Sonnet 4.6, cache warm                       $0.045276
    first request on Haiku 4.5, cold + 1.25x write       $0.177242   3.9x worse
    steady state on Haiku 4.5, its own cache warm        $0.015092   3x better
    flapping between the two, neither ever warm          $0.425976   9.4x worse

So a swap is not a loss - it is an investment that pays back after six requests, about one agent
response at 6.7 tool-call steps. What ruins the economics is *flapping*: deciding per request and
alternating, so neither cache ever warms. The rule that falls out is to route at session
boundaries and then stay put.

Two cases are permanent losses rather than warm-up costs, and both are handled here: a target
that lists above the source's cached rate, and a prefix below the target's cache minimum. The
minimums differ by vendor (512-4,096), so a swap can make a request uncacheable rather than
merely re-cached.

Google is the exception worth knowing: its implicit cache has no write premium, so there is no
warm-up cost to earn back and Gemini is structurally the cheapest kind of model to move to.

Nothing here talks to a network or reads a prompt. It is arithmetic over token counts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .prices import ModelPrice


class SwapVerdict(StrEnum):
    """Why a swap was allowed or refused. Every one of these is a sentence we can show a user."""

    # No cache on the source, so there is nothing to lose. The common case: ~72% of LLM calls
    # show no cached reads at all.
    FREE_TO_MOVE = "free_to_move"

    # The source cache is warm but the target is cheap enough to win anyway, and it wins from
    # the very first request.
    CHEAPER_IMMEDIATELY = "cheaper_immediately"

    # The target is cheaper in steady state, but the first request pays to refill a cache. Worth
    # it only if the workload stays on the target long enough.
    CHEAPER_AFTER_WARMUP = "cheaper_after_warmup"

    # The target's list price is above the source's cached price. No amount of volume fixes this.
    LOSES_TO_THE_CACHE = "loses_to_the_cache"

    # The prefix is too short to cache on the target, so the target can never reach its own
    # discount. A permanent penalty rather than a warm-up cost.
    TARGET_CANNOT_CACHE = "target_cannot_cache"


@dataclass(frozen=True, slots=True)
class RequestShape:
    """The token shape of one request, which is all the economics depends on.

    `cached_prefix_tokens` is the stable prefix - tools, system prompt, prior turns - that a
    cache would serve. `fresh_input_tokens` is what changes each request. On agent traffic the
    first dwarfs the second, which is exactly why the cache matters so much.
    """

    cached_prefix_tokens: int
    fresh_input_tokens: int
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("cached_prefix_tokens", "fresh_input_tokens", "output_tokens"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def total_input_tokens(self) -> int:
        return self.cached_prefix_tokens + self.fresh_input_tokens


_PER_MTOK = Decimal(1_000_000)


def _cost(tokens: int, per_mtok: Decimal) -> Decimal:
    return Decimal(tokens) * per_mtok / _PER_MTOK


def can_cache(model: ModelPrice, shape: RequestShape) -> bool:
    """Whether this model would cache this prefix at all.

    Below the model's minimum, `cache_control` is accepted and silently does nothing - no error,
    just full-price tokens. Since the minimum varies from 512 to 4,096 tokens across models, a
    swap can move a request from cacheable to not.
    """
    return shape.cached_prefix_tokens >= model.min_cacheable_tokens


def cost_warm(model: ModelPrice, shape: RequestShape) -> Decimal:
    """Input cost with the prefix already cached: cache-read on the prefix, list on the rest."""
    if not can_cache(model, shape):
        return cost_cold(model, shape)
    n = shape.total_input_tokens
    return _cost(shape.cached_prefix_tokens, model.cache_read_rate(n)) + _cost(
        shape.fresh_input_tokens, model.input_rate(n)
    )


def cost_cold(model: ModelPrice, shape: RequestShape) -> Decimal:
    """Input cost with no cache: everything at list price."""
    n = shape.total_input_tokens
    return _cost(n, model.input_rate(n))


def cost_writing_cache(model: ModelPrice, shape: RequestShape) -> Decimal:
    """Input cost on the request that fills the cache: the prefix carries the write premium.

    On Google this equals the cold cost, because implicit caching has no write premium - which
    is why Gemini has no warm-up to earn back and is a structurally cheap model to move to.

    If the prefix is too short for this model to cache, there is no write and no premium - just
    a full-price request, forever.
    """
    if not can_cache(model, shape):
        return cost_cold(model, shape)
    n = shape.total_input_tokens
    return _cost(shape.cached_prefix_tokens, model.cache_write_rate(n)) + _cost(
        shape.fresh_input_tokens, model.input_rate(n)
    )


def explicit_cache_storage_cost(model: ModelPrice, tokens: int, hours: Decimal) -> Decimal:
    """What Google charges to *hold* an explicit cache. Zero everywhere else.

    Anthropic and OpenAI price the holding cost into the write instead (Anthropic: 2x list for a
    one-hour TTL against 1.25x for five minutes). Google's implicit cache is free to hold, so
    this only applies when someone has deliberately created a CachedContent object - and it is
    the reason an explicit cache can cost more than it saves on low-traffic workloads.
    """
    if model.cache_storage_per_mtok_hour is None:
        return Decimal(0)
    return Decimal(tokens) * model.cache_storage_per_mtok_hour * hours / _PER_MTOK


def break_even_price(source: ModelPrice, total_input_tokens: int = 0) -> Decimal:
    """The list price a target must beat to be worth moving a cache-warm request to *today*.

    This is just the source's cached rate. Because that rate is 0.1x list, the target has to be
    **more than ten times cheaper** than the source to win on the very first request - which
    rules out most of the obvious down-route targets. Sonnet 4.6 -> Haiku 4.5 is only 3x.

    It is a bar for winning *immediately*, not for winning at all: a workload that commits to the
    cheaper model rebuilds its own cache and comes out ahead. See `evaluate_swap`.
    """
    return source.cache_read_rate(total_input_tokens)


@dataclass(frozen=True, slots=True)
class SwapEconomics:
    """The full arithmetic for one proposed swap, kept so the report can show its working."""

    source: ModelPrice
    target: ModelPrice
    shape: RequestShape
    source_cache_warm: bool

    source_cost: Decimal
    target_first_request: Decimal
    target_steady_state: Decimal

    verdict: SwapVerdict

    #: Requests that must run on the target before the swap has paid for itself. None when the
    #: swap never pays for itself, 0 when it wins immediately.
    break_even_requests: int | None

    @property
    def saves(self) -> bool:
        return self.verdict in (
            SwapVerdict.FREE_TO_MOVE,
            SwapVerdict.CHEAPER_IMMEDIATELY,
            SwapVerdict.CHEAPER_AFTER_WARMUP,
        )

    @property
    def steady_saving_per_request(self) -> Decimal:
        """Positive when the target is cheaper once its own cache is warm."""
        return self.source_cost - self.target_steady_state

    @property
    def switch_cost(self) -> Decimal:
        """What the first request on the target costs over staying put. Negative means it is
        cheaper even before any cache is rebuilt."""
        return self.target_first_request - self.source_cost

    def explain(self, *, aggregate: bool = False) -> str:
        """One sentence, with the numbers in it. The report never asserts without showing this.

        `aggregate` changes the unit, and it is not cosmetic. The arithmetic here is per request,
        but a finding feeds it a whole period's tokens as one shape - and saying "saves $12.00 per
        request" about a period total is a wrong number, not a loose phrase. In aggregate mode the
        break-even count is dropped too: it is derived from a single request's switch cost, so
        against a period total it means nothing.
        """
        src, tgt = self.source.model_id, self.target.model_id
        each = " over this period" if aggregate else " per request"
        this = "this traffic" if aggregate else "this request"

        if self.verdict is SwapVerdict.FREE_TO_MOVE:
            return (
                f"No cache on {src} for {this}, so moving it to {tgt} costs nothing to switch "
                f"and saves ${self.steady_saving_per_request:,.2f}{each}."
                if aggregate
                else f"No cache on {src} for {this}, so moving it to {tgt} costs nothing to "
                f"switch and saves ${self.steady_saving_per_request:.6f}{each}."
            )
        if self.verdict is SwapVerdict.LOSES_TO_THE_CACHE:
            return (
                f"{tgt} lists at ${self.target.input_per_mtok}/M, above the "
                f"${break_even_price(self.source)}/M {this} already pays on {src}'s warm cache. "
                f"Moving it would cost ${-self.steady_saving_per_request:,.2f} more{each}, "
                f"permanently."
                if aggregate
                else f"{tgt} lists at ${self.target.input_per_mtok}/M, above the "
                f"${break_even_price(self.source)}/M {this} already pays on {src}'s warm cache. "
                f"Moving it would cost ${-self.steady_saving_per_request:.6f} more{each}, "
                f"permanently."
            )
        if self.verdict is SwapVerdict.TARGET_CANNOT_CACHE:
            return (
                f"The {self.shape.cached_prefix_tokens:,}-token prefix is below "
                f"{tgt}'s {self.target.min_cacheable_tokens:,}-token cache minimum, so {tgt} "
                f"would pay list price on every request while {src} pays its cached rate."
            )
        if self.verdict is SwapVerdict.CHEAPER_IMMEDIATELY:
            return (
                f"{tgt} is cheaper than {src}'s cached rate from the first request: "
                f"${self.steady_saving_per_request:,.2f} saved{each}, no warm-up."
                if aggregate
                else f"{tgt} is cheaper than {src}'s cached rate from the first request: "
                f"${self.steady_saving_per_request:.6f} saved{each}, no warm-up."
            )
        if aggregate:
            # No refill figure here. A period's cached tokens are the same prefixes read over and
            # over; pricing them all as one cache write charged a whole month of reads as though
            # every one were the first request of a session, which on a live read came out ten
            # times the real saving. The refill is paid once per session, and daily totals do not
            # count sessions, so the honest sentence names the cost without inventing its size.
            return (
                f"Once each session's cache is warm on {tgt}, the same tokens would cost "
                f"${self.steady_saving_per_request:,.2f} less{each}. The first request of every "
                f"session that moves pays to write its cache again, and daily totals do not count "
                f"sessions, so that cost is not subtracted here. Moving mid-session, or back and "
                f"forth, costs more than it saves."
            )
        return (
            f"Moving from {src} to {tgt} costs ${self.switch_cost:.6f} once to refill the "
            f"cache, then saves ${self.steady_saving_per_request:.6f} per request - break-even "
            f"after {self.break_even_requests} requests on {tgt}. Worth it only if the whole "
            f"session stays on {tgt}."
        )


def evaluate_swap(
    source: ModelPrice,
    target: ModelPrice,
    shape: RequestShape,
    *,
    source_cache_warm: bool,
) -> SwapEconomics:
    """Decide whether moving this request from `source` to `target` saves money.

    `source_cache_warm` is a fact about the caller's traffic, not a guess: it comes from the
    `cache_read_input_tokens` the vendor already reports. When it is False there is no cache to
    destroy and the decision is the naive price comparison everyone else makes.
    """
    if source_cache_warm and not can_cache(source, shape):
        # The caller believes the cache is warm but the prefix is under this model's minimum.
        # Trust the arithmetic over the claim; a sub-minimum prefix is never cached.
        source_cache_warm = False

    source_cost = cost_warm(source, shape) if source_cache_warm else cost_cold(source, shape)
    target_first = cost_writing_cache(target, shape)
    target_steady = cost_warm(target, shape)

    steady_saving = source_cost - target_steady
    switch_cost = target_first - source_cost

    verdict: SwapVerdict
    break_even: int | None

    if steady_saving <= 0:
        # The target never gets cheaper, so no volume rescues it. Distinguish the two reasons,
        # because they call for different advice: one says pick a cheaper target, the other says
        # this prefix is too short for the target to ever discount it.
        verdict = (
            SwapVerdict.TARGET_CANNOT_CACHE
            if source_cache_warm and not can_cache(target, shape)
            else SwapVerdict.LOSES_TO_THE_CACHE
        )
        break_even = None
    elif not source_cache_warm:
        verdict = SwapVerdict.FREE_TO_MOVE
        break_even = 0
    elif switch_cost <= 0:
        verdict = SwapVerdict.CHEAPER_IMMEDIATELY
        break_even = 0
    else:
        verdict = SwapVerdict.CHEAPER_AFTER_WARMUP
        # The first request costs `switch_cost` extra; each one after saves `steady_saving`.
        break_even = 1 + math.ceil(switch_cost / steady_saving)

    return SwapEconomics(
        source=source,
        target=target,
        shape=shape,
        source_cache_warm=source_cache_warm,
        source_cost=source_cost,
        target_first_request=target_first,
        target_steady_state=target_steady,
        verdict=verdict,
        break_even_requests=break_even,
    )
