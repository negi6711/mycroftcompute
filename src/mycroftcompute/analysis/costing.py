"""What the usage cost, from token counts and the verified price table.

Every source gives token counts; only some give money. Pricing here rather than trusting whatever
cost figure a source happens to carry means one arithmetic for all of them, and it means the
cache mechanics are applied correctly - the 0.1x read rate, Fable's 0.025x, and Anthropic's
one-hour cache write at 2x list rather than 1.25x.

When a source *does* report its own cost we compare rather than ignore. A material gap between
what their backend says and what the published prices say is worth surfacing: it usually means a
negotiated rate, a batch tier, or a model we are pricing wrong. All three are things the reader
should know before quoting our number.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from mycroftcompute.economics.prices import ModelPrice, resolve_model
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord

_PER_MTOK = Decimal(1_000_000)

#: A gap this large between reported and computed cost is worth telling the reader about.
COST_DISCREPANCY = Decimal("0.05")


def _at(tokens: int | None, rate: Decimal) -> Decimal:
    return Decimal(tokens or 0) * rate / _PER_MTOK


def cost_of(record: UsageRecord, price: ModelPrice, *, assume_1h_writes: bool = False) -> Decimal:
    """Price one record at list, respecting every cache mechanic the source reported.

    Cache writes split by TTL where the source knows it: Anthropic bills a one-hour write at 2x
    list against 1.25x for five minutes, and on real Claude Code traffic the one-hour TTL is the
    overwhelming majority. Where the split is unknown, everything is charged at the cheaper
    five-minute rate - understating rather than inflating, because a number we quote too high is
    the one that loses trust.
    """
    total_input = record.total_input_tokens or 0

    if assume_1h_writes:
        writes_1h = record.cache_write_tokens or 0
        writes_short = 0
    else:
        writes_1h = record.cache_write_1h_tokens or 0
        writes_short = max((record.cache_write_tokens or 0) - writes_1h, 0)

    return (
        _at(record.input_tokens, price.input_rate(total_input))
        + _at(record.cached_input_tokens, price.cache_read_rate(total_input))
        + _at(writes_1h, price.cache_write_1h_rate(total_input))
        + _at(writes_short, price.cache_write_rate(total_input))
        + _at(record.output_tokens, price.output_per_mtok)
    )


@dataclass(slots=True)
class Costed:
    """The bill, broken down every way the source supports."""

    total: Decimal = Decimal(0)
    by_model: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_user: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_repo: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_day: dict[object, Decimal] = field(default_factory=lambda: defaultdict(Decimal))

    #: Spend by which part of the agent asked - main, subagent, auxiliary - and by the reasoning
    #: effort it asked for. Only an agent's own metrics carry these, and they are the two
    #: dimensions that separate work somebody is waiting on from the agent's own housekeeping.
    by_query_source: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_effort: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))

    #: Models we hold no price for. Their usage is counted but not costed, and the reader is told.
    unpriced_models: tuple[str, ...] = ()

    #: What the source itself said it cost, when it said anything.
    reported_total: Decimal | None = None

    #: The same bill with every cache write charged at the one-hour rate instead of the
    #: five-minute one. A metrics backend carries no TTL dimension, so `total` charges the
    #: cheaper rate - understating rather than inflating. But real Claude Code traffic uses the
    #: one-hour TTL for the large majority of its writes, so when a source reports its own cost
    #: and sits between these two figures, the TTL is the explanation rather than a discount.
    total_if_writes_1h: Decimal = Decimal(0)

    @property
    def knows_users(self) -> bool:
        return bool(self.by_user)

    @property
    def knows_repos(self) -> bool:
        return bool(self.by_repo)

    @property
    def knows_segments(self) -> bool:
        return bool(self.by_query_source or self.by_effort)

    @property
    def discrepancy(self) -> Decimal | None:
        """How far our arithmetic is from what the source reported, as a fraction.

        None when the source reported nothing, or when it reported zero and so there is nothing
        to compare against.
        """
        if self.reported_total is None or self.reported_total == 0:
            return None
        return (self.total - self.reported_total) / self.reported_total


def cost(parsed: ParsedUsage) -> Costed:
    """Price a whole read, grouped by model, developer, repository and day."""
    out = Costed()
    unpriced: set[str] = set()
    reported = Decimal(0)
    saw_reported = False

    for record in parsed.records:
        if record.cost is not None:
            reported += record.cost
            saw_reported = True

        price = resolve_model(record.model)
        if price is None:
            unpriced.add(record.model)
            continue

        amount = cost_of(record, price)
        out.total += amount
        out.total_if_writes_1h += cost_of(record, price, assume_1h_writes=True)
        out.by_model[record.model] += amount
        out.by_day[record.day] += amount
        # None means "this source cannot say", which is not a bucket. Grouping unknowns under a
        # label would invent an owner.
        if record.user is not None:
            out.by_user[record.user] += amount
        if record.repo is not None:
            out.by_repo[record.repo] += amount
        if record.query_source is not None:
            out.by_query_source[record.query_source] += amount
        if record.effort is not None:
            out.by_effort[record.effort] += amount

    out.unpriced_models = tuple(sorted(unpriced))
    out.reported_total = reported if saw_reported else None
    return out


def ranked(bucket: dict[str, Decimal]) -> list[tuple[str, Decimal]]:
    """Largest first. The order every one of these questions is actually asked in."""
    return sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))


def share(part: Decimal, whole: Decimal) -> Decimal:
    return part / whole if whole else Decimal(0)


def median(values: list[Decimal]) -> Decimal:
    """Median, because a mean is dragged around by exactly the outlier we are looking for."""
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
