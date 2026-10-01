"""Findings about a team rather than an export.

The original three were written for an AI-product company reading a console export: how much of
the input bill was uncached, whether a flagship was answering short requests, whether usage ran
at weekends. The buyer we settled on asks different questions, because they have fifty engineers
and a bill that grew:

* where is the money going, by person
* where is it going, by codebase
* would a cheaper model actually be cheaper

The first of those names people, and is the most dangerous thing in the product. Spend follows
work: the most expensive developer on a team is very often the most productive one, running
agents against the hardest problems. A report that reads as a league table of waste gets shown to
a team once and never again, and deserves to be. So it states where the money went and declines to
say whether that is good, and the action it recommends is a question rather than a correction.
"""

from __future__ import annotations

from decimal import Decimal

from mycroftcompute.analysis.costing import Costed, cost_of, median, ranked, share
from mycroftcompute.analysis.findings import (
    MIN_DEVELOPERS,
    Finding,
    FindingKind,
    _by_model,
    _pct,
    _sum,
)
from mycroftcompute.economics.caching import RequestShape, SwapEconomics, evaluate_swap
from mycroftcompute.economics.prices import ModelPrice, models_by_vendor, resolve_model
from mycroftcompute.parsers.model import ParsedUsage, UsageRecord

#: A developer spending this many times the median is worth a conversation, not a reprimand.
OUTLIER_MULTIPLE = Decimal("3")

#: Or: the top three together taking this share of everything...
TOP_THREE_SHARE = Decimal("0.60")

#: ...and at least this multiple of what three people would hold if spend were perfectly even.
#: Without it, a team of five splitting spend exactly equally was told "60% of agent spend went
#: to three developers" - true, and meaningless, because three of five people *is* 60%. On the
#: finding that names people, a false positive is the worst kind there is.
OVER_EVEN_SHARE = Decimal("1.5")

#: A single repository taking this share of spend is where the agent budget actually went.
REPO_CONCENTRATION = Decimal("0.40")

#: Only compare models that dominate the bill. Swapping a rounding error is not a finding.
DOMINANT_MODEL_SHARE = Decimal("0.50")

#: A model is a candidate target if it lists at or below this fraction of the current one.
CHEAPER_RATIO = Decimal("0.5")


def _cheaper_sibling(price: ModelPrice) -> ModelPrice | None:
    for candidate in models_by_vendor(price.vendor):
        if candidate.input_per_mtok <= price.input_per_mtok * CHEAPER_RATIO:
            return candidate
    return None


# ----------------------------------------------- where the money went, by person ---


def developer_concentration(parsed: ParsedUsage, costed: Costed) -> Finding | None:
    """Which developers the agent budget went to, without implying any of them are at fault."""
    if not costed.knows_users or len(costed.by_user) < MIN_DEVELOPERS:
        return None

    order = ranked(costed.by_user)
    mid = median([amount for _, amount in order])
    top_name, top_amount = order[0]
    top_three = sum((amount for _, amount in order[:3]), Decimal(0))

    # Relative to team size: three people's even share of a team of n is 3/n. Concentration
    # means holding well above that, not merely being the three largest.
    even_three = Decimal(min(3, len(order))) / Decimal(len(order))
    bar = max(TOP_THREE_SHARE, even_three * OVER_EVEN_SHARE)
    concentrated = share(top_three, costed.total) >= bar
    outlying = mid > 0 and top_amount / mid >= OUTLIER_MULTIPLE
    if not (concentrated or outlying):
        return None

    evidence = [
        f"{len(order)} developers ran agents in this period, for ${costed.total:,.2f} in total.",
        f"The top three account for ${top_three:,.2f}, {_pct(top_three, costed.total)} of "
        f"everything.",
        f"{top_name} spent ${top_amount:,.2f} against a team median of ${mid:,.2f}.",
        *(f"  {name}: ${amount:,.2f}" for name, amount in order[:5]),
    ]

    return Finding(
        kind=FindingKind.DEVELOPER_CONCENTRATION,
        title=f"{_pct(top_three, costed.total)} of agent spend went to three developers",
        subject=top_name,
        evidence=tuple(evidence),
        cannot_tell=(
            "Whether any of this is a problem. Spend follows work, and the most expensive "
            "developer on a team is frequently the most productive one - a large agent bill "
            "usually means somebody is shipping. This finding says where the money went, not "
            "that it went somewhere wrong.",
            "What they were working on. One hard migration can cost more than a quarter of "
            "ordinary feature work, and nothing here separates the two.",
            "Whether the spend produced anything. That needs the pull requests alongside these "
            "numbers, which this data does not carry.",
        ),
        next_action=(
            f"Ask {top_name} what they have been running agents on. The useful outcome is "
            f"usually a technique worth spreading rather than a habit worth stopping: the "
            f"heaviest user is often the one who has worked out how to get the most from it."
        ),
        source_lines=tuple(r.source_line for r in parsed.records),
    )


# ------------------------------------------------- where the money went, by repo ---


def repository_concentration(parsed: ParsedUsage, costed: Costed) -> Finding | None:
    """Which codebase absorbed the agent budget.

    Safer ground than naming people and often more useful: spend concentrated in one repository
    is a statement about what the team is working on, which is a priority question rather than a
    behaviour question.
    """
    if not costed.knows_repos or len(costed.by_repo) < 2:
        return None

    order = ranked(costed.by_repo)
    top_name, top_amount = order[0]
    if share(top_amount, costed.total) < REPO_CONCENTRATION:
        return None

    evidence = [
        f"{top_name} took ${top_amount:,.2f} of ${costed.total:,.2f}, "
        f"{_pct(top_amount, costed.total)} of agent spend across {len(order)} repositories.",
        *(f"  {name}: ${amount:,.2f}" for name, amount in order[:5]),
    ]

    return Finding(
        kind=FindingKind.REPOSITORY_CONCENTRATION,
        title=f"{_pct(top_amount, costed.total)} of agent spend went into {top_name}",
        subject=top_name,
        evidence=tuple(evidence),
        cannot_tell=(
            "Whether that matches where the work should be. A repository under active migration "
            "should dominate; one nobody is meant to be touching should not. Only you know "
            "which this is.",
            "Whether the agent is doing the same work repeatedly. A large project and a loop "
            "nobody noticed produce identical figures here.",
        ),
        next_action=(
            f"Check that {top_name} is where this quarter's work is meant to be happening. If it "
            f"is, this is the agent budget doing its job and there is nothing to fix."
        ),
        source_lines=tuple(r.source_line for r in parsed.records),
    )


# ------------------------------- what a cheaper model would actually cost, net of cache ---


def model_mix(parsed: ParsedUsage, costed: Costed) -> Finding | None:
    """What the same work would cost on a cheaper model, net of the cache it would destroy.

    The honest version of the routing pitch, and the arithmetic is why it is honest. A naive
    comparison of list prices says a model three times cheaper saves two thirds. The cache says
    otherwise: a request on a warm prefix is already billed at a tenth of list, so the first
    request after a swap can cost several times *more*, and only a workload that commits to the
    cheaper model earns it back. `evaluate_swap` does that arithmetic; what lands here is its
    verdict rather than a hope.
    """
    if not costed.by_model:
        return None

    top_model, top_spend = ranked(costed.by_model)[0]
    price = resolve_model(top_model)
    if price is None or share(top_spend, costed.total) < DOMINANT_MODEL_SHARE:
        return None

    target = _cheaper_sibling(price)
    if target is None:
        return None

    rows = _by_model(parsed.records).get(top_model, [])
    prefix = _sum([r.cached_input_tokens for r in rows]) + _sum(
        [r.cache_write_tokens for r in rows]
    )
    fresh = _sum([r.input_tokens for r in rows])
    if prefix + fresh == 0:
        return None

    economics = evaluate_swap(
        price,
        target,
        RequestShape(cached_prefix_tokens=prefix, fresh_input_tokens=fresh),
        source_cache_warm=prefix > 0,
    )

    return Finding(
        kind=FindingKind.MODEL_MIX,
        title=(
            f"Moving {top_model} to {target.model_id} would "
            + ("pay off, but not immediately" if economics.saves else "cost more than it saves")
        ),
        subject=top_model,
        evidence=(
            f"{top_model} is {_pct(top_spend, costed.total)} of spend, ${top_spend:,.2f}.",
            f"Its input was {prefix:,} cached or cache-written tokens against {fresh:,} fresh "
            f"ones.",
            economics.explain(aggregate=True),
        ),
        cannot_tell=(
            "Whether the cheaper model would do the work. This compares what the same tokens "
            "would cost; it is not a judgement about output quality, which needs an evaluation "
            "against your own tasks.",
            "How much of this traffic could survive a model change at all. Long agent sessions "
            "build a cache that a mid-session swap throws away, which is why the figure above is "
            "net of that loss rather than a list-price comparison.",
        ),
        next_action=(
            "Try the cheaper model in one repository for a week before changing anything "
            "broadly. Routing at session boundaries preserves the cache; switching mid-session "
            "destroys it and costs more than it saves."
            if economics.saves
            else "Leave this alone. The cheaper model's list price is misleading here - the "
            "cache discount on the current model is worth more than the price difference."
        ),
        ceiling=economics.steady_saving_per_request if economics.saves else None,
        source_lines=tuple(r.source_line for r in rows),
    )


# ------------------------------------- the part of the bill nobody is waiting on ---


#: `query_source` values that are not anybody's work. Claude Code labels its own housekeeping -
#: titles, summaries, the small bookkeeping calls around a session - as `auxiliary`.
HOUSEKEEPING_SOURCES = frozenset({"auxiliary"})

#: `effort` values where the agent has already decided the request did not need deep reasoning.
LOW_EFFORT = frozenset({"low"})

#: Below this share of the bill, moving the segment is not worth anyone's afternoon.
ROUTABLE_SHARE = Decimal("0.08")


def _routable(record: UsageRecord) -> bool:
    return (record.query_source or "") in HOUSEKEEPING_SOURCES or (
        record.effort or ""
    ) in LOW_EFFORT


def _routable_by_user(rows: list[UsageRecord]) -> dict[str, Decimal]:
    """Housekeeping spend per developer. Empty when the source carries no identity."""
    out: dict[str, Decimal] = {}
    for record in rows:
        price = resolve_model(record.model)
        if price is None or record.user is None:
            continue
        out[record.user] = out.get(record.user, Decimal(0)) + cost_of(record, price)
    return out


def routable_segment(parsed: ParsedUsage, costed: Costed) -> Finding | None:
    """The premium spend that is the agent talking to itself.

    This is the strongest routing argument in the product, and it is strong for a reason that has
    nothing to do with being clever: it sidesteps both objections the other findings have to
    carry. Nobody reads an auxiliary response, so "would the cheaper model be good enough" stops
    being a question about quality. And housekeeping requests do not sit on a session's warm
    prefix, so there is usually no cache to break - which is the one thing that makes most
    down-routing lose money.

    It needs `query_source` or `effort`, which only an agent's own metrics carry. The
    vendor-neutral GenAI conventions have neither, so this finding is the concrete reason to keep
    reading the richer metric alongside the standard one.
    """
    if not costed.knows_segments:
        return None

    rows = [r for r in parsed.records if _routable(r)]
    if not rows:
        return None

    # Judge each model separately. A team already running its housekeeping on a cheap model
    # should not be told to move it, and averaging the two together would hide that.
    best: tuple[Decimal, str, ModelPrice, ModelPrice, SwapEconomics] | None = None
    segment_total = Decimal(0)

    for model, model_rows in _by_model(rows).items():
        price = resolve_model(model)
        if price is None:
            continue
        spend = sum((cost_of(r, price) for r in model_rows), Decimal(0))
        segment_total += spend

        target = _cheaper_sibling(price)
        if target is None:
            continue

        prefix = _sum([r.cached_input_tokens for r in model_rows]) + _sum(
            [r.cache_write_tokens for r in model_rows]
        )
        fresh = _sum([r.input_tokens for r in model_rows])
        output = _sum([r.output_tokens for r in model_rows])
        if prefix + fresh == 0:
            continue

        economics = evaluate_swap(
            price,
            target,
            RequestShape(
                cached_prefix_tokens=prefix, fresh_input_tokens=fresh, output_tokens=output
            ),
            source_cache_warm=prefix > 0,
        )
        if not economics.saves:
            continue
        if best is None or spend > best[0]:
            best = (spend, model, price, target, economics)

    if best is None or share(segment_total, costed.total) < ROUTABLE_SHARE:
        return None

    spend, model, _price, target, economics = best
    kinds = ranked(costed.by_query_source) or ranked(costed.by_effort)
    named = ", ".join(f"{name} ${amount:,.2f}" for name, amount in kinds[:4])

    evidence = [
        f"${segment_total:,.2f} of ${costed.total:,.2f} - "
        f"{_pct(segment_total, costed.total)} - went to requests the agent raised for itself or "
        f"marked as needing low effort.",
        f"The largest single line in it is {model}, ${spend:,.2f}.",
        f"Across the whole read, spend by request kind was: {named}.",
        economics.explain(aggregate=True),
    ]

    # Where the dollars sit, by person. Not a ranking of behaviour - see `cannot_tell` - but the
    # absolute figures decide whether a configuration change is worth anyone's afternoon.
    per_developer = _routable_by_user(rows)
    if per_developer:
        order = ranked(per_developer)
        evidence.append("By developer, that housekeeping spend was:")
        evidence.extend(
            f"  {name}: ${amount:,.2f} of their ${costed.by_user.get(name, amount):,.2f} "
            f"({_pct(amount, costed.by_user.get(name, amount))})"
            for name, amount in order[:5]
        )

    return Finding(
        kind=FindingKind.ROUTABLE_SEGMENT,
        title=(
            f"{_pct(segment_total, costed.total)} of spend is the agent's own housekeeping, "
            f"on {model}"
        ),
        subject=model,
        evidence=tuple(evidence),
        cannot_tell=(
            "Whether your agent lets you choose a model for this traffic separately from the "
            "main one. Some do and some do not, and where it cannot be configured this figure "
            "is information rather than a lever.",
            "Anything about the developers named above. The share of spend that is housekeeping "
            "is a property of how the agent is configured, not of how anybody works - it is "
            "roughly the same for everyone on a team, so a larger figure here almost always "
            "just means a larger figure everywhere. This lists where the dollars are, and one "
            "settings change moves all of them at once.",
            "Whether the housekeeping output feeds back into work somebody does read. A summary "
            "the agent writes for itself is one thing; a summary it then acts on is another, and "
            "nothing in these metrics separates them.",
            "Whether the low-effort label is accurate. It is the agent's own judgement about its "
            "own request, made before it saw the answer.",
        ),
        next_action=(
            f"Check what model your agent uses for auxiliary and low-effort requests. If it is "
            f"{model}, point it at {target.model_id} and leave the main path alone - this is the "
            f"one part of the bill where a cheaper model costs you nothing, because nobody is "
            f"reading the output and there is no session cache to break."
        ),
        ceiling=economics.steady_saving_per_request,
        source_lines=tuple(r.source_line for r in rows),
    )


TEAM_FINDINGS = (
    developer_concentration,
    repository_concentration,
    routable_segment,
    model_mix,
)
