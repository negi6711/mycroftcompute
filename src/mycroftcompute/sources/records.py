"""Turning metric series into usage records, once, for every metrics backend.

Grafana and Datadog differ in their URLs, their auth, their query languages and their timestamp
units. They do not differ in what a series *means*, so everything downstream of "here is a
labelled series of daily totals" lives here rather than being written twice.

The piece that matters most is `vendors_in`. A record's vendor is decided by its model, not by
whatever the telemetry calls the serving platform, and that rule has to be identical in both
connectors or the same company gets two different bills depending on which backend we read.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from mycroftcompute.economics.prices import resolve_model
from mycroftcompute.parsers.model import UsageRecord
from mycroftcompute.sources.metrics_names import vendor_from_system
from mycroftcompute.vendors import Vendor

#: The record fields a metrics backend can fill from token series.
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")

_EPOCH_DAY = date(1970, 1, 1)


@dataclass(frozen=True, slots=True)
class Key:
    """Everything that makes one row distinct. The day arrives last, per sample."""

    model: str
    user: str | None = None
    repo: str | None = None
    query_source: str | None = None
    effort: str | None = None
    day: date = _EPOCH_DAY

    def on(self, day: date) -> Key:
        return Key(self.model, self.user, self.repo, self.query_source, self.effort, day)


@dataclass(slots=True)
class Accumulator:
    """Daily totals, gathered from however many queries a backend needs."""

    tokens: dict[Key, dict[str, int]] = field(default_factory=dict)
    costs: dict[Key, Decimal] = field(default_factory=lambda: defaultdict(Decimal))

    #: Raw `gen_ai.system` values seen, kept only as a fallback for models we cannot price.
    systems: set[str] = field(default_factory=set)

    def _bucket(self, key: Key) -> dict[str, int]:
        return self.tokens.setdefault(key, dict.fromkeys(TOKEN_FIELDS, 0))

    def add_tokens(self, key: Key, field_name: str, count: int) -> None:
        self._bucket(key)[field_name] += count

    def add_cost(self, key: Key, amount: Decimal) -> None:
        # Touch the bucket too, so a backend with cost but no readable tokens still has rows.
        self._bucket(key)
        self.costs[key] += amount

    def records(self, *, knows_cache: bool, knows_tokens: bool) -> list[UsageRecord]:
        """The rows, in the order a reader scans them.

        `knows_cache` and `knows_tokens` are what the *backend* could tell us, not what happened
        to be non-zero. Where a dimension is missing the field stays None: zero is a claim, and
        "no cached tokens" and "this backend does not report cached tokens" are different
        sentences that would otherwise print the same number.

        A key with no tokens and no cost is left out. A Prometheus series still exporting an
        unchanged total yields one on every day its session stays open, and those rows would
        count as days with usage when nothing was used.
        """
        return [
            UsageRecord(
                day=key.day,
                model=key.model,
                input_tokens=counts["input_tokens"] if knows_tokens else None,
                cached_input_tokens=counts["cached_input_tokens"] if knows_cache else None,
                cache_write_tokens=counts["cache_write_tokens"] if knows_cache else None,
                output_tokens=counts["output_tokens"] if knows_tokens else None,
                cost=self.costs.get(key) or None,
                user=key.user,
                repo=key.repo,
                query_source=key.query_source,
                effort=key.effort,
            )
            for key, counts in sorted(
                self.tokens.items(),
                key=lambda kv: (kv[0].day, kv[0].model, kv[0].user or "", kv[0].repo or ""),
            )
            if any(counts.values()) or self.costs.get(key)
        ]


def vendors_in(records: list[UsageRecord], systems: set[str]) -> set[Vendor]:
    """Who actually served this traffic.

    The model decides. A platform like Bedrock or Vertex serves other vendors' models and the
    price table is keyed by model, so `gen_ai.system` is consulted only for models we cannot
    price - and then only to name a vendor, never to price one.
    """
    found: set[Vendor] = set()
    unresolved = False
    for record in records:
        price = resolve_model(record.model)
        if price is None:
            unresolved = True
        else:
            found.add(price.vendor)
    if unresolved:
        found |= {v for s in systems if (v := vendor_from_system(s)) is not None}
    return found or {Vendor.ANTHROPIC}


def dominant(records: list[UsageRecord], vendors: set[Vendor]) -> Vendor:
    """The single `vendor` field, for the code that predates multi-vendor sources.

    Whoever served the most tokens wins, because a report headed with a vendor that supplied 2%
    of the traffic reads as an error. `ParsedUsage.vendors` carries the full set; this is the
    summary.
    """
    if len(vendors) == 1:
        return next(iter(vendors))
    weight: dict[Vendor, int] = defaultdict(int)
    for record in records:
        price = resolve_model(record.model)
        if price is not None:
            weight[price.vendor] += (record.total_input_tokens or 0) + (record.output_tokens or 0)
    return max(weight, key=lambda v: (weight[v], v)) if weight else sorted(vendors)[0]


VENDOR_NAMES = {Vendor.ANTHROPIC: "Anthropic", Vendor.OPENAI: "OpenAI", Vendor.GOOGLE: "Google"}


def multi_vendor_message(vendors: set[Vendor]) -> str:
    return (
        "This backend carries "
        + ", ".join(VENDOR_NAMES[v] for v in sorted(vendors))
        + " usage side by side. Every figure below spans all of them, which is the view none of "
        "those vendors' own consoles can give you."
    )


#: The two sentences every metrics backend needs when a dimension the analysis wants is absent.
#: Kept here so both connectors say the same thing, in the same words.
NO_USER_LABEL = (
    "No developer identity label is present on these metrics, so per-developer figures cannot "
    "be produced. This is usually deliberate: per-user labels multiply cardinality, which is "
    "what metrics backends bill on."
)

NO_CACHE_DIMENSION = (
    "These metrics do not separate cached input tokens, so the cache analysis - usually the "
    "most valuable finding - cannot run. The OpenTelemetry GenAI conventions carry this as "
    "gen_ai.usage.cache_read.input_tokens; instrumentation predating those attributes will not "
    "have it. We will not report a zero hit rate, because a missing dimension and a cold cache "
    "are different things."
)


def no_kind_label_message(metric: str) -> str:
    return (
        f"{metric} carries no label separating input from output tokens, and they are priced "
        f"five to ten times apart. We will not split a single total on a guessed ratio. The "
        f"OpenTelemetry GenAI conventions carry this as gen_ai.token.type; Claude Code's own "
        f"metric calls it type."
    )
